from __future__ import annotations

import base64
import os
import re
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from loguru import logger


class GmailClient:
    """Client simplifié pour Gmail API (lecture + envoi + pièces jointes)."""

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        scopes: List[str],
    ) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.scopes = scopes
        self._service = None

    def ensure_authenticated(self) -> None:
        creds: Optional[Credentials] = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, self.scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Actualisation du token OAuth…")
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_path):
                    raise FileNotFoundError(
                        f"credentials.json introuvable à {self.credentials_path}. Téléchargez-le depuis Google Cloud."
                    )
                logger.info("Déclenchement du flux OAuth (serveur local)…")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, self.scopes)
                creds = flow.run_local_server(port=0)
            with open(self.token_path, "w") as token:
                token.write(creds.to_json())
        self._service = build("gmail", "v1", credentials=creds)
        logger.success("Authentifié à Gmail API")

    @property
    def service(self):
        if self._service is None:
            self.ensure_authenticated()
        return self._service

    def list_messages(self, query: str, max_results: int = 20) -> List[Dict]:
        try:
            response = self.service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            return response.get("messages", [])
        except HttpError as e:
            logger.exception("Erreur lors de list_messages")
            return []

    def get_message(self, msg_id: str) -> Dict:
        return self.service.users().messages().get(userId="me", id=msg_id, format="full").execute()

    def get_message_raw(self, msg_id: str) -> Dict:
        return self.service.users().messages().get(userId="me", id=msg_id, format="raw").execute()

    def _get_header(self, message: Dict, name: str) -> Optional[str]:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value")
        return None

    def get_subject_from_message(self, message: Dict) -> str:
        return self._get_header(message, "Subject") or ""

    def get_from_from_message(self, message: Dict) -> str:
        return self._get_header(message, "From") or ""

    def get_to_from_message(self, message: Dict) -> str:
        return self._get_header(message, "To") or ""

    def get_thread_id(self, message: Dict) -> str:
        return message.get("threadId", "")

    def _walk_parts(self, payload: Dict) -> List[Dict]:
        parts: List[Dict] = []
        stack: List[Dict] = [payload]
        while stack:
            node = stack.pop()
            if not node:
                continue
            mime_type = node.get("mimeType")
            body = node.get("body", {})
            filename = node.get("filename", "")
            parts.append({"mimeType": mime_type, "body": body, "filename": filename})
            for child in node.get("parts", []) or []:
                stack.append(child)
        return parts

    def extract_text_and_attachments(self, message: Dict) -> Tuple[str, List[Tuple[str, bytes, str]]]:
        """Retourne (texte, pièces) où pièces = [(filename, bytes, mimeType)]."""
        payload = message.get("payload", {})
        parts = self._walk_parts(payload)
        all_text: List[str] = []
        attachments: List[Tuple[str, bytes, str]] = []
        for part in parts:
            mime = part.get("mimeType")
            body = part.get("body", {})
            filename = part.get("filename", "")
            if mime == "text/plain" and "data" in body:
                data = body["data"].replace("-", "+").replace("_", "/")
                all_text.append(base64.b64decode(data).decode("utf-8", errors="ignore"))
            elif mime == "text/html" and "data" in body:
                data = body["data"].replace("-", "+").replace("_", "/")
                html = base64.b64decode(data).decode("utf-8", errors="ignore")
                # Fallback simple: retirer balises
                text = re.sub(r"<[^>]+>", " ", html)
                all_text.append(text)
            elif filename and body.get("attachmentId"):
                att_id = body["attachmentId"]
                att = (
                    self.service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message["id"], id=att_id)
                    .execute()
                )
                data = att.get("data", "").replace("-", "+").replace("_", "/")
                content = base64.b64decode(data)
                attachments.append((filename, content, mime or "application/octet-stream"))
        joined = "\n\n".join(all_text).strip()
        return joined, attachments

    def send_message(self, to_addr: str, subject: str, body: str, thread_id: Optional[str] = None) -> Dict:
        msg = EmailMessage()
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: Dict[str, str] = {"raw": encoded}
        if thread_id:
            payload["threadId"] = thread_id
        try:
            sent = self.service.users().messages().send(userId="me", body=payload).execute()
            logger.info(f"Message envoyé: id={sent.get('id')}")
            return sent
        except HttpError:
            logger.exception("Erreur à l'envoi du message")
            raise
