"""Shared helpers for live seed scripts. Run from backend/: `uv run python scripts/seed_stripe.py`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import Settings  # noqa: E402

SEED_FILE = BACKEND / "data" / "live_seed.json"


def settings() -> Settings:
    return Settings()


def load_seed() -> dict:
    return json.loads(SEED_FILE.read_text()) if SEED_FILE.exists() else {}


def save_seed(update: dict) -> dict:
    SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {**load_seed(), **update}
    SEED_FILE.write_text(json.dumps(data, indent=2))
    return data


def need(value: str | None, name: str) -> str:
    if not value:
        raise SystemExit(f"{name} is not set (see .env.example)")
    return value


def demo_customer_email() -> str:
    """Return an operator-provided recipient, or a controlled alias of the support mailbox.

    Live seed data must not use the reserved ``.example`` fixture address: HubSpot rejects
    it, and a post-verification reply must never target an arbitrary third-party inbox.
    Gmail's plus alias keeps the entire live rehearsal inside the authorized support mailbox.
    """
    s = settings()
    if s.demo_customer_email:
        return s.demo_customer_email

    from app.integrations.gmail import LiveGmail

    gmail = LiveGmail(
        need(s.gmail_client_id, "GMAIL_CLIENT_ID"),
        need(s.gmail_client_secret, "GMAIL_CLIENT_SECRET"),
        need(s.gmail_refresh_token, "GMAIL_REFRESH_TOKEN"),
        user_id=s.gmail_user_id,
    )
    inbox = gmail._req("GET", "/profile")["emailAddress"]
    local, sep, domain = inbox.partition("@")
    if not sep:
        raise SystemExit("Gmail profile returned an invalid email address")
    return f"{local}+settle-demo@{domain}"
