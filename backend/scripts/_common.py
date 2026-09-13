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
