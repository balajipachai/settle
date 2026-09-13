"""RunService: start / resume / retry / inspect dispute runs on durable LangGraph threads."""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from pydantic import BaseModel

from app.domain.states import DisputeStatus
from app.graph.builder import build_graph
from app.graph.common import Deps
from app.logging import get_logger, log_event

_log = get_logger("runner")

_RESUME_DECISIONS = {"approval": {"approve", "reject", "edit"}, "repair_required": {"retry_repair", "close"}}


class RunNotWaiting(Exception):
    pass


def jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    return obj


class RunService:
    def __init__(self, deps: Deps, checkpointer: BaseCheckpointSaver, *, raise_errors: bool = False) -> None:
        self.deps = deps
        self.checkpointer = checkpointer
        self.graph = build_graph(deps, checkpointer)
        self.raise_errors = raise_errors
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()

    @property
    def repo(self):
        return self.deps.repo

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks[run_id]

    @staticmethod
    def thread_id_for(source_email_id: str, replay_n: int = 0) -> str:
        """Deterministic LangGraph thread per Gmail message; forced replays get a suffixed thread."""
        h = hashlib.sha256(source_email_id.encode()).hexdigest()[:16]
        return f"settle-{h}" if replay_n == 0 else f"settle-{h}-r{replay_n}"

    def config(self, run_id: str) -> dict:
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return {"configurable": {"thread_id": run["thread_id"]}}

    # ------------------------------------------------------------------ lifecycle
    def start_run(self, source_email_id: str, *, force_new: bool = False) -> tuple[dict, bool]:
        """Idempotent intake: the same Gmail message maps to the same run unless a replay is forced."""
        existing = self.repo.find_original_run_for_source(source_email_id)
        if existing and not force_new:
            self.repo.add_audit(existing["run_id"], event_type="duplicate_event_ignored", node="intake",
                                summary="Same Gmail message received again; returning the existing run (no reprocessing).")
            return existing, False
        n = self.repo.count_runs_for_source(source_email_id)
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.repo.create_run(run_id=run_id, thread_id=self.thread_id_for(source_email_id, n),
                             source_email_id=source_email_id,
                             replay_of_run_id=existing["run_id"] if existing else None)
        self.repo.update_run(run_id, reasoner=self.deps.reasoner.name)
        self.repo.add_audit(run_id, event_type="run_created", node="intake",
                            summary=f"Run created for Gmail message {source_email_id}"
                                    + (f" (forced replay of {existing['run_id']})" if existing else ""),
                            external_system="gmail", object_type="message", object_id=source_email_id)
        return self.repo.get_run(run_id), True

    def execute(self, run_id: str) -> None:
        run = self.repo.get_run(run_id)
        initial = {
            "run_id": run_id, "thread_id": run["thread_id"], "source_email_id": run["source_email_id"],
            "replay_of_run_id": run["replay_of_run_id"], "status": DisputeStatus.RECEIVED, "errors": [],
            "repair_cycles": 0,
        }
        self._invoke(run_id, initial)

    def resume(self, run_id: str, payload: dict) -> None:
        with self._lock(run_id):
            pending = self.pending_interrupt(run_id)
            if not pending:
                raise RunNotWaiting(f"Run {run_id} is not waiting for input.")
            allowed = _RESUME_DECISIONS.get(pending.get("kind"), set())
            if payload.get("decision") not in allowed:
                raise ValueError(f"Decision {payload.get('decision')!r} is not valid for a {pending.get('kind')} pause.")
            self._invoke_locked(run_id, Command(resume=payload))

    def retry(self, run_id: str) -> None:
        """Continue from the last durable checkpoint after a crash / exhausted transient failure."""
        self._invoke(run_id, None)

    def _invoke(self, run_id: str, payload: Any) -> None:
        with self._lock(run_id):
            self._invoke_locked(run_id, payload)

    def _invoke_locked(self, run_id: str, payload: Any) -> None:
        try:
            self.graph.invoke(payload, self.config(run_id))
        except Exception as exc:
            code = getattr(exc, "code", None)
            reason = f"{code or type(exc).__name__}: {exc}"[:500]
            self.repo.update_run(run_id, status="FAILED", outcome_reason=reason)
            self.repo.add_audit(run_id, event_type="run_failed", summary=f"Run halted: {reason}. State is checkpointed; "
                                "POST /api/runs/{id}/retry resumes from the last completed node.",
                                metadata={"retryable": bool(getattr(exc, "retryable", False))})
            log_event(_log, "run_failed", run_id=run_id, error=reason)
            if self.raise_errors:
                raise

    # ------------------------------------------------------------------ inspection
    def snapshot(self, run_id: str):
        return self.graph.get_state(self.config(run_id))

    def pending_interrupt(self, run_id: str) -> dict | None:
        snap = self.snapshot(run_id)
        for task in snap.tasks:
            for intr in task.interrupts:
                return intr.value
        return None

    def state(self, run_id: str) -> dict:
        return dict(self.snapshot(run_id).values)

    def view(self, run_id: str) -> dict | None:
        run = self.repo.get_run(run_id)
        if run is None:
            return None
        snap = self.snapshot(run_id)
        pending = None
        for task in snap.tasks:
            for intr in task.interrupts:
                pending = intr.value
        return {
            "run": jsonable(run),
            "state": jsonable(dict(snap.values)),
            "pending": jsonable(pending),
            "next": list(snap.next),
            "events": jsonable(self.repo.list_audit(run_id)),
            "financial_actions": jsonable(self.repo.list_financial_actions(run_id)),
            "approvals": jsonable(self.repo.list_approvals(run_id)),
            "outbox": jsonable(self.repo.list_outbox(run_id)),
        }
