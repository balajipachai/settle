"""Repository: runs, financial-action ledger, approvals, audit trail, outbox, evals."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.models import ResolutionAction
from app.domain.timeutil import utcnow
from app.logging import redact
from app.persistence.db import (
    approvals,
    audit_events,
    evaluation_runs,
    financial_actions,
    make_engine,
    metadata,
    outbound_emails,
    runs,
)

LEDGER_STATUSES = ("PROPOSED", "APPROVED", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN", "CANCELLED")


def _row(r) -> dict[str, Any] | None:
    return dict(r._mapping) if r is not None else None


class Repository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> "Repository":
        engine = make_engine(url)
        metadata.create_all(engine)
        return cls(engine)

    def reset(self) -> None:
        metadata.drop_all(self.engine)
        metadata.create_all(self.engine)

    # ------------------------------------------------------------------ runs
    def create_run(
        self, *, run_id: str, thread_id: str, source_email_id: str, replay_of_run_id: str | None = None
    ) -> dict[str, Any]:
        now = utcnow()
        values = dict(
            run_id=run_id,
            thread_id=thread_id,
            source_email_id=source_email_id,
            replay_of_run_id=replay_of_run_id,
            status="RECEIVED",
            created_at=now,
            updated_at=now,
        )
        with self.engine.begin() as c:
            c.execute(insert(runs).values(**values))
        return values

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(runs).where(runs.c.run_id == run_id)).first())

    def find_original_run_for_source(self, source_email_id: str) -> dict[str, Any] | None:
        q = (
            select(runs)
            .where(runs.c.source_email_id == source_email_id, runs.c.replay_of_run_id.is_(None))
            .order_by(runs.c.created_at)
        )
        with self.engine.connect() as c:
            return _row(c.execute(q).first())

    def count_runs_for_source(self, source_email_id: str) -> int:
        with self.engine.connect() as c:
            return c.execute(
                select(func.count()).select_from(runs).where(runs.c.source_email_id == source_email_id)
            ).scalar_one()

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(select(runs).order_by(runs.c.created_at.desc()).limit(limit))]

    def update_run(self, run_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self.engine.begin() as c:
            c.execute(update(runs).where(runs.c.run_id == run_id).values(**fields))

    # ------------------------------------------------------------------ audit
    def add_audit(
        self,
        run_id: str,
        *,
        event_type: str,
        summary: str,
        node: str | None = None,
        external_system: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        with self.engine.begin() as c:
            c.execute(
                insert(audit_events).values(
                    event_id=event_id,
                    run_id=run_id,
                    node_name=node,
                    event_type=event_type,
                    external_system=external_system,
                    object_type=object_type,
                    object_id=object_id,
                    summary=summary,
                    metadata_json=json.dumps(redact(metadata or {}), default=str),
                    created_at=utcnow(),
                )
            )
        return event_id

    def list_audit(self, run_id: str) -> list[dict[str, Any]]:
        q = select(audit_events).where(audit_events.c.run_id == run_id).order_by(audit_events.c.seq)
        with self.engine.connect() as c:
            out = []
            for r in c.execute(q):
                d = _row(r)
                d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
                out.append(d)
            return out

    # ------------------------------------------------------------------ financial action ledger
    def ensure_financial_action(self, action: ResolutionAction, run_id: str) -> dict[str, Any]:
        """Insert the PROPOSED ledger row for this action hash, or return the existing one."""
        now = utcnow()
        try:
            with self.engine.begin() as c:
                c.execute(
                    insert(financial_actions).values(
                        action_id=action.action_id,
                        run_id=run_id,
                        invoice_id=action.invoice_id,
                        customer_id=action.customer_id,
                        amount_minor=action.amount_minor,
                        currency=action.currency,
                        reason=action.reason,
                        action_hash=action.action_hash,
                        idempotency_key=action.idempotency_key,
                        status="PROPOSED",
                        attempts=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError:
            pass
        row = self.get_financial_action_by_hash(action.action_hash)
        assert row is not None
        return row

    def get_financial_action(self, action_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(c.execute(select(financial_actions).where(financial_actions.c.action_id == action_id)).first())

    def get_financial_action_by_hash(self, action_hash: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            return _row(
                c.execute(select(financial_actions).where(financial_actions.c.action_hash == action_hash)).first()
            )

    def list_financial_actions(self, run_id: str | None = None) -> list[dict[str, Any]]:
        q = select(financial_actions).order_by(financial_actions.c.created_at)
        if run_id:
            q = q.where(financial_actions.c.run_id == run_id)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]

    def cas_financial_action(
        self, action_id: str, from_statuses: tuple[str, ...], to_status: str, **fields: Any
    ) -> bool:
        """Atomic compare-and-swap on ledger status: the application idempotency guard."""
        assert to_status in LEDGER_STATUSES
        values = {"status": to_status, "updated_at": utcnow(), **fields}
        if to_status == "EXECUTING":
            values["attempts"] = financial_actions.c.attempts + 1
        with self.engine.begin() as c:
            res = c.execute(
                update(financial_actions)
                .where(financial_actions.c.action_id == action_id, financial_actions.c.status.in_(from_statuses))
                .values(**values)
            )
            return res.rowcount == 1

    # ------------------------------------------------------------------ approvals
    def add_approval(
        self,
        *,
        run_id: str,
        action_id: str,
        action_hash: str,
        reviewer_id: str,
        decision: str,
        channel: str,
        valid: bool = True,
        invalidation_reason: str | None = None,
        note: str | None = None,
    ) -> str:
        approval_id = str(uuid.uuid4())
        with self.engine.begin() as c:
            c.execute(
                insert(approvals).values(
                    approval_id=approval_id,
                    run_id=run_id,
                    action_id=action_id,
                    action_hash=action_hash,
                    reviewer_id=reviewer_id,
                    decision=decision,
                    channel=channel,
                    valid=valid,
                    invalidation_reason=invalidation_reason,
                    reviewer_note=note,
                    created_at=utcnow(),
                )
            )
        return approval_id

    def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [
                _row(r)
                for r in c.execute(select(approvals).where(approvals.c.run_id == run_id).order_by(approvals.c.created_at))
            ]

    def valid_approval_for(self, run_id: str, action_hash: str) -> dict[str, Any] | None:
        q = (
            select(approvals)
            .where(
                approvals.c.run_id == run_id,
                approvals.c.action_hash == action_hash,
                approvals.c.decision == "approve",
                approvals.c.valid.is_(True),
            )
            .order_by(approvals.c.created_at.desc())
        )
        with self.engine.connect() as c:
            return _row(c.execute(q).first())

    def any_valid_approval_for_hash(self, action_hash: str) -> bool:
        q = select(func.count()).select_from(approvals).where(
            approvals.c.action_hash == action_hash, approvals.c.decision == "approve", approvals.c.valid.is_(True)
        )
        with self.engine.connect() as c:
            return c.execute(q).scalar_one() > 0

    def latest_approval_request(self, run_id: str) -> dict[str, Any] | None:
        q = (
            select(approvals)
            .where(approvals.c.run_id == run_id, approvals.c.decision == "requested")
            .order_by(approvals.c.created_at.desc())
        )
        with self.engine.connect() as c:
            return _row(c.execute(q).first())

    # ------------------------------------------------------------------ outbox
    def get_or_create_outbox(
        self, *, run_id: str, action_id: str, to_email: str, subject: str, body: str
    ) -> dict[str, Any]:
        try:
            with self.engine.begin() as c:
                c.execute(
                    insert(outbound_emails).values(
                        outbox_id=str(uuid.uuid4()),
                        run_id=run_id,
                        action_id=action_id,
                        to_email=to_email,
                        subject=subject,
                        body=body,
                        status="PENDING",
                        created_at=utcnow(),
                    )
                )
        except IntegrityError:
            pass
        with self.engine.connect() as c:
            return _row(c.execute(select(outbound_emails).where(outbound_emails.c.action_id == action_id)).first())

    def mark_outbox(self, outbox_id: str, **fields: Any) -> None:
        with self.engine.begin() as c:
            c.execute(update(outbound_emails).where(outbound_emails.c.outbox_id == outbox_id).values(**fields))

    def list_outbox(self, run_id: str | None = None) -> list[dict[str, Any]]:
        q = select(outbound_emails).order_by(outbound_emails.c.created_at)
        if run_id:
            q = q.where(outbound_emails.c.run_id == run_id)
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(q)]

    # ------------------------------------------------------------------ evals
    def save_eval_run(
        self, *, suite_name: str, model_name: str, application_version: str, metrics: dict, report_path: str | None
    ) -> str:
        eval_run_id = str(uuid.uuid4())
        with self.engine.begin() as c:
            c.execute(
                insert(evaluation_runs).values(
                    eval_run_id=eval_run_id,
                    suite_name=suite_name,
                    model_name=model_name,
                    application_version=application_version,
                    metrics_json=json.dumps(metrics),
                    report_path=report_path,
                    created_at=utcnow(),
                )
            )
        return eval_run_id

    def latest_eval_run(self) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = _row(c.execute(select(evaluation_runs).order_by(evaluation_runs.c.created_at.desc())).first())
        if row:
            row["metrics"] = json.loads(row.pop("metrics_json"))
        return row
