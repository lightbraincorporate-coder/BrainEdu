from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger

from gmail_client import GmailClient
from payment_verifier import PaymentVerifier
from log_config import setup_logging


APP_ROOT = Path(__file__).parent
CONFIG_PATH = APP_ROOT / "config.json"
TEMPLATES_DIR = APP_ROOT / "templates"
STATIC_DIR = APP_ROOT / "static"
LOGS_DIR = APP_ROOT / "logs"

app = FastAPI(title="PaymentVerifierAI")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
scheduler: Optional[BackgroundScheduler] = None


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def build_gmail_client(cfg: Dict[str, Any]) -> GmailClient:
    gmail_cfg = cfg.get("gmail", {})
    return GmailClient(
        credentials_path=gmail_cfg.get("credentials_path", "credentials.json"),
        token_path=gmail_cfg.get("token_path", "token.json"),
        scopes=gmail_cfg.get("scopes", []),
    )


def build_verifier(cfg: Dict[str, Any]) -> PaymentVerifier:
    client = build_gmail_client(cfg)
    return PaymentVerifier(config=cfg, gmail_client=client)


@app.on_event("startup")
def on_startup() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # Configure la journalisation
    try:
        cfg = load_config()
    except Exception:
        # Fallback si config manquante au tout début
        setup_logging(str(LOGS_DIR / "payments.log"), level="INFO")
        raise
    setup_logging(cfg.get("logging", {}).get("file", str(LOGS_DIR / "payments.log")), level=cfg.get("logging", {}).get("level", "INFO"))
    if cfg.get("scheduler", {}).get("enabled", False):
        global scheduler
        scheduler = BackgroundScheduler()
        interval = int(cfg.get("scheduler", {}).get("interval_minutes", 5))
        scheduler.add_job(job_check_inbox, "interval", minutes=interval, id="check_inbox", replace_existing=True)
        scheduler.start()
        logger.info(f"Scheduler démarré: toutes les {interval} minutes")


@app.on_event("shutdown")
def on_shutdown() -> None:
    if scheduler:
        scheduler.shutdown(wait=False)


def job_check_inbox() -> None:
    try:
        cfg = load_config()
        verifier = build_verifier(cfg)
        # Rechercher des messages ciblés demandant vérification
        query = cfg.get("gmail", {}).get("watch_query", "in:inbox newer_than:7d")
        msgs = verifier.gmail.list_messages(query=query, max_results=10)
        for m in msgs:
            msg = verifier.gmail.get_message(m["id"])  # full
            subject = verifier.gmail.get_subject_from_message(msg)
            body_text, attachments = verifier.gmail.extract_text_and_attachments(msg)
            evidence = verifier.extract_evidence_from_email(subject, body_text, attachments)
            # Répond uniquement si texte contient "valider" ou "refuser"
            if evidence.choice:
                verifier.decide_and_reply(msg, evidence)
    except Exception:
        logger.exception("Échec du job périodique")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    cfg = load_config()
    status = "Non connecté"
    try:
        client = build_gmail_client(cfg)
        client.ensure_authenticated()
        status = "Connecté"
    except Exception as e:
        logger.warning(f"Statut Gmail: {e}")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "gmail_status": status,
            "app_name": cfg.get("app_name", "PaymentVerifierAI"),
        },
    )


@app.post("/verify")
async def verify_endpoint(
    request: Request,
    user_hint: Optional[str] = Form(default=None),
    amount: Optional[float] = Form(default=None),
    tx_id: Optional[str] = Form(default=None),
):
    cfg = load_config()
    verifier = build_verifier(cfg)
    # Construire Evidence à partir du formulaire
    from payment_verifier import Evidence

    raw_text = " ".join(filter(None, [str(user_hint or ""), str(amount or ""), str(tx_id or "")]))
    evidence = Evidence(user_hint=user_hint, amount=amount, tx_id=tx_id, choice=None, raw_text=raw_text)
    result = verifier.verify_against_inbox(evidence)
    return JSONResponse(
        {
            "decision": result.decision,
            "reason": result.reason,
            "matched_message_id": result.matched_message_id,
            "matched_snippet": result.matched_snippet,
        }
    )


@app.get("/oauth")
async def oauth_trigger():
    # Déclenche l'auth si pas encore connectée (redirection locale par Google)
    cfg = load_config()
    client = build_gmail_client(cfg)
    client.ensure_authenticated()
    return RedirectResponse("/")


@app.get("/health" )
async def health():
    return {"status": "ok"}
