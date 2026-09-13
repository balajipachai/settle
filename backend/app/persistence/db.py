"""Application tables (PRD §22). Works on SQLite (default) and Postgres."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
)

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("thread_id", String(160), nullable=False, unique=True),
    Column("source_email_id", String(256), nullable=False, index=True),
    Column("replay_of_run_id", String(64)),
    Column("status", String(40), nullable=False),
    Column("customer_name", String(256)),
    Column("invoice_number", String(64)),
    Column("disputed_amount_minor", BigInteger),
    Column("credited_amount_minor", BigInteger),
    Column("currency", String(3)),
    Column("outcome_reason", Text),
    Column("verification_status", String(20)),
    Column("reasoner", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
)

financial_actions = Table(
    "financial_actions",
    metadata,
    Column("action_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("invoice_id", String(128), nullable=False),
    Column("customer_id", String(128), nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("reason", Text, nullable=False),
    Column("action_hash", String(64), nullable=False, unique=True),
    Column("idempotency_key", String(128), nullable=False, unique=True),
    Column("status", String(20), nullable=False),
    Column("stripe_credit_note_id", String(128)),
    Column("attempts", Integer, nullable=False, default=0),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
)

approvals = Table(
    "approvals",
    metadata,
    Column("approval_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("action_id", String(64), nullable=False),
    Column("action_hash", String(64), nullable=False, index=True),
    Column("reviewer_id", String(128), nullable=False),
    Column("decision", String(20), nullable=False),  # requested|approve|reject|edit|invalidated
    Column("channel", String(20), nullable=False),
    Column("valid", Boolean, nullable=False, default=True),
    Column("invalidation_reason", Text),
    Column("reviewer_note", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("node_name", String(64)),
    Column("event_type", String(64), nullable=False),
    Column("external_system", String(20)),
    Column("object_type", String(64)),
    Column("object_id", String(256)),
    Column("summary", Text, nullable=False),
    Column("metadata_json", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

outbound_emails = Table(
    "outbound_emails",
    metadata,
    Column("outbox_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("action_id", String(64), nullable=False, unique=True),
    Column("to_email", String(256), nullable=False),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("status", String(20), nullable=False),  # PENDING|SENT|FAILED
    Column("gmail_message_id", String(256)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sent_at", DateTime(timezone=True)),
)

evaluation_runs = Table(
    "evaluation_runs",
    metadata,
    Column("eval_run_id", String(64), primary_key=True),
    Column("suite_name", String(128), nullable=False),
    Column("model_name", String(128), nullable=False),
    Column("application_version", String(32), nullable=False),
    Column("metrics_json", Text, nullable=False),
    Column("report_path", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)
