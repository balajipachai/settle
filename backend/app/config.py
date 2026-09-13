"""Application configuration.

All secrets come from the environment (or a local .env). Policy values live in
policy.json (see app/domain/policies.py) and are never derived from prompts or
customer content.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent

# An operator must type this exact phrase to allow non-test Stripe writes.
LIVE_WRITES_OVERRIDE_PHRASE = "I-UNDERSTAND-THIS-MOVES-REAL-MONEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_DIR / ".env", BACKEND_DIR / ".env"),
        extra="ignore",
        populate_by_name=True,
    )

    mode: Literal["fixture", "live"] = Field("fixture", alias="SETTLE_MODE")
    reasoner: Literal["auto", "claude", "rules"] = Field("auto", alias="SETTLE_REASONER")
    data_dir: Path = Field(BACKEND_DIR / "data", alias="SETTLE_DATA_DIR")
    database_url: str | None = Field(None, alias="DATABASE_URL")
    fixture_world_path: Path = Field(BACKEND_DIR / "fixtures" / "demo_world.json", alias="SETTLE_FIXTURE_WORLD")
    policy_path: Path = Field(BACKEND_DIR / "policy.json", alias="SETTLE_POLICY_FILE")
    eval_reports_dir: Path = Field(BACKEND_DIR / "evals" / "reports", alias="SETTLE_EVAL_REPORTS_DIR")
    public_base_url: str = Field("http://localhost:8000", alias="SETTLE_PUBLIC_BASE_URL")
    approver_ids: str = Field("", alias="SETTLE_APPROVER_IDS")

    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    claude_model: str = Field("claude-opus-5", alias="SETTLE_CLAUDE_MODEL")

    stripe_secret_key: str | None = Field(None, alias="STRIPE_SECRET_KEY")
    stripe_api_version: str | None = Field(None, alias="STRIPE_API_VERSION")
    allow_live_stripe_writes: bool = Field(False, alias="ALLOW_LIVE_STRIPE_WRITES")
    live_writes_operator_override: str | None = Field(None, alias="SETTLE_LIVE_WRITES_OPERATOR_OVERRIDE")

    hubspot_access_token: str | None = Field(None, alias="HUBSPOT_ACCESS_TOKEN")

    slack_bot_token: str | None = Field(None, alias="SLACK_BOT_TOKEN")
    slack_channel_id: str | None = Field(None, alias="SLACK_CHANNEL_ID")
    slack_signing_secret: str | None = Field(None, alias="SLACK_SIGNING_SECRET")

    gmail_client_id: str | None = Field(None, alias="GMAIL_CLIENT_ID")
    gmail_client_secret: str | None = Field(None, alias="GMAIL_CLIENT_SECRET")
    gmail_refresh_token: str | None = Field(None, alias="GMAIL_REFRESH_TOKEN")
    gmail_user_id: str = Field("me", alias="GMAIL_USER_ID")
    gmail_inbox_query: str = Field("in:inbox newer_than:30d", alias="GMAIL_INBOX_QUERY")

    retry_base_delay_s: float = Field(0.5, alias="SETTLE_RETRY_BASE_DELAY_S")
    retry_max_attempts: int = Field(3, alias="SETTLE_RETRY_MAX_ATTEMPTS")
    max_repair_cycles: int = Field(2, alias="SETTLE_MAX_REPAIR_CYCLES")
    http_timeout_s: float = Field(20.0, alias="SETTLE_HTTP_TIMEOUT_S")

    @model_validator(mode="after")
    def _guard_live_stripe_writes(self) -> "Settings":
        key = self.stripe_secret_key or ""
        live_key = key.startswith(("sk_live_", "rk_live_"))
        if (live_key or self.allow_live_stripe_writes) and (
            self.live_writes_operator_override != LIVE_WRITES_OVERRIDE_PHRASE
        ):
            raise ValueError(
                "Refusing to start: a live Stripe key or ALLOW_LIVE_STRIPE_WRITES=true was configured "
                "without SETTLE_LIVE_WRITES_OPERATOR_OVERRIDE. Settle runs against Stripe test mode only."
            )
        return self

    @property
    def approver_id_set(self) -> frozenset[str]:
        return frozenset(a.strip() for a in self.approver_ids.split(",") if a.strip())

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.data_dir / 'settle.db'}"

    @property
    def checkpoint_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "checkpoints.sqlite"

    @property
    def use_claude(self) -> bool:
        if self.reasoner == "rules":
            return False
        if self.reasoner == "claude":
            return True
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
