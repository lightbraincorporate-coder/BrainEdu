from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from gmail_client import GmailClient
from ocr_utils import ocr_image_bytes


@dataclass
class Evidence:
    user_hint: Optional[str]
    amount: Optional[float]
    tx_id: Optional[str]
    choice: Optional[str]  # "valider"/"refuser" si présent
    raw_text: str


@dataclass
class VerificationResult:
    decision: str  # "VALIDER" ou "REFUSER"
    reason: str
    matched_message_id: Optional[str]
    matched_snippet: Optional[str]


class PaymentVerifier:
    def __init__(self, config: Dict, gmail_client: GmailClient) -> None:
        self.config = config
        self.gmail = gmail_client
        self.amount_tolerance_pct: float = float(
            config.get("verification", {}).get("amount_tolerance_pct", 1.0)
        )
        self.time_window_hours: int = int(
            config.get("verification", {}).get("time_window_hours", 168)
        )
        self.ocr_lang: str = config.get("ocr", {}).get("tesseract_lang", "eng+fra")

    def _parse_amounts(self, text: str) -> List[float]:
        # Exemples à capturer: "50 FCFA", "50.00", "50,00", "1 000", etc.
        clean = text.replace("\xa0", " ")
        patterns = [
            r"(?<!\d)(\d{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{1,2})?)(?:\s*(?:FCFA|XOF))?",
        ]
        amounts: List[float] = []
        for pat in patterns:
            for m in re.finditer(pat, clean, flags=re.IGNORECASE):
                token = m.group(1)
                token = token.replace(" ", "").replace("\u202f", "").replace(",", ".")
                try:
                    amounts.append(float(token))
                except ValueError:
                    continue
        return amounts

    def _parse_tx_ids(self, text: str) -> List[str]:
        # ID numériques ou alphanumériques: 6-20 chars
        ids = re.findall(r"\b([A-Z0-9]{6,20})\b", text, flags=re.IGNORECASE)
        return list(dict.fromkeys(ids))

    def _parse_choice(self, text: str) -> Optional[str]:
        if re.search(r"\bvalider\b", text, flags=re.IGNORECASE):
            return "valider"
        if re.search(r"\brefuser\b", text, flags=re.IGNORECASE):
            return "refuser"
        return None

    def extract_evidence_from_email(self, subject: str, body_text: str, attachments: List[Tuple[str, bytes, str]]) -> Evidence:
        ocr_texts: List[str] = []
        for filename, content, mime in attachments:
            if mime.startswith("image/") or filename.lower().endswith((".png", ".jpg", ".jpeg")):
                try:
                    text = ocr_image_bytes(content, lang=self.ocr_lang)
                    if text:
                        ocr_texts.append(text)
                except Exception:
                    logger.exception(f"OCR échoué pour {filename}")
        full_text = "\n\n".join([subject or "", body_text or "", *ocr_texts])
        amounts = self._parse_amounts(full_text)
        tx_ids = self._parse_tx_ids(full_text)
        choice = self._parse_choice(full_text)
        user_hint = None
        # Heuristique simple: mot après "user"/"utilisateur"/"id"
        m = re.search(r"(?:user|utilisateur|id)[:\s-]+(\S{2,})", full_text, flags=re.IGNORECASE)
        if m:
            user_hint = m.group(1)
        amount = amounts[0] if amounts else None
        tx_id = tx_ids[0] if tx_ids else None
        return Evidence(user_hint=user_hint, amount=amount, tx_id=tx_id, choice=choice, raw_text=full_text)

    def _within_amount_tolerance(self, a: float, b: float) -> bool:
        if a is None or b is None:
            return False
        tol = self.amount_tolerance_pct / 100.0
        return abs(a - b) <= tol * max(a, b)

    def _within_time_window(self, msg_internal_date_ms: int) -> bool:
        # internalDate est en ms depuis epoch
        dt = datetime.fromtimestamp(msg_internal_date_ms / 1000.0, tz=timezone.utc)
        return dt >= datetime.now(tz=timezone.utc) - timedelta(hours=self.time_window_hours)

    def _message_matches(self, message: Dict, evidence: Evidence) -> Tuple[bool, Optional[str]]:
        snippet = message.get("snippet", "")
        payload_text, _ = self.gmail.extract_text_and_attachments(message)
        content = f"{snippet}\n{payload_text}"

        if evidence.tx_id:
            if re.search(re.escape(evidence.tx_id), content, flags=re.IGNORECASE):
                return True, snippet
        if evidence.amount is not None:
            amounts = self._parse_amounts(content)
            for amt in amounts:
                if self._within_amount_tolerance(amt, evidence.amount):
                    return True, snippet
        if evidence.user_hint:
            if re.search(re.escape(evidence.user_hint), content, flags=re.IGNORECASE):
                return True, snippet
        return False, None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def verify_against_inbox(self, evidence: Evidence) -> VerificationResult:
        query = self.config.get("gmail", {}).get("watch_query", "in:inbox newer_than:7d")
        messages = self.gmail.list_messages(query=query, max_results=50)
        logger.info(f"Messages candidats: {len(messages)}")
        for m in messages:
            msg = self.gmail.get_message(m["id"])  # full
            if not self._within_time_window(int(msg.get("internalDate", 0))):
                continue
            matched, snippet = self._message_matches(msg, evidence)
            if matched:
                return VerificationResult(
                    decision="VALIDER",
                    reason="Correspondance trouvée dans Gmail",
                    matched_message_id=msg.get("id"),
                    matched_snippet=snippet,
                )
        return VerificationResult(
            decision="REFUSER",
            reason="Aucune preuve correspondante dans la fenêtre temporelle",
            matched_message_id=None,
            matched_snippet=None,
        )

    def decide_and_reply(self, source_message: Dict, evidence: Evidence) -> VerificationResult:
        result = self.verify_against_inbox(evidence)
        to_addr = self.gmail.get_from_from_message(source_message)
        thread_id = self.gmail.get_thread_id(source_message)
        subject_prefix = "Paiement validé" if result.decision == "VALIDER" else "Paiement refusé"
        subject = f"{subject_prefix} - PaymentVerifierAI"
        body_lines = [
            f"Décision: {result.decision}",
            f"Raison: {result.reason}",
        ]
        if evidence.amount is not None:
            body_lines.append(f"Montant déclaré: {evidence.amount}")
        if evidence.tx_id:
            body_lines.append(f"ID fourni: {evidence.tx_id}")
        if result.matched_snippet:
            body_lines.append("Extrait correspondant: " + (result.matched_snippet or "").strip())
        body = "\n".join(body_lines)
        try:
            self.gmail.send_message(to_addr=to_addr, subject=subject, body=body, thread_id=thread_id)
        except Exception:
            logger.exception("Échec d'envoi de la réponse")
        return result
