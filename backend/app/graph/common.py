"""Node plumbing: dependency container, status transitions, audited tool calls, tracing."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from app.config import Settings
from app.domain.errors import ErrorCode, ErrorRecord, SettleError
from app.domain.policies import PolicyConfig
from app.domain.states import TERMINAL_STATUSES, DisputeStatus, assert_transition
from app.integrations.base import IntegrationError, with_retries
from app.integrations.factory import Gateways
from app.logging import get_logger, log_event
from app.persistence.repository import Repository
from app.services.reasoner import Reasoner

T = TypeVar("T")
_log = get_logger("graph")


@dataclass
class Deps:
    settings: Settings
    repo: Repository
    gateways: Gateways
    reasoner: Reasoner
    policy: PolicyConfig
    sleep: Callable[[float], None] = field(default=time.sleep)


class NodeContext:
    def __init__(self, deps: Deps, state: dict, node: str) -> None:
        self.deps = deps
        self.state = state
        self.node = node
        self.run_id: str = state["run_id"]
        self.status = DisputeStatus(state.get("status", DisputeStatus.RECEIVED))
        self.status_changed = False
        self.errors: list[ErrorRecord] = []
        self.last_attempts = 0

    # shortcuts
    @property
    def repo(self) -> Repository:
        return self.deps.repo

    @property
    def settings(self) -> Settings:
        return self.deps.settings

    @property
    def stripe(self):
        return self.deps.gateways.stripe

    @property
    def hubspot(self):
        return self.deps.gateways.hubspot

    @property
    def slack(self):
        return self.deps.gateways.slack

    @property
    def gmail(self):
        return self.deps.gateways.gmail

    def transition(self, to: DisputeStatus, *, reason: str | None = None, **run_fields: Any) -> None:
        """Validated status change, mirrored to the runs table and the audit trail."""
        assert_transition(self.status, to)
        if self.status != to:
            prev, self.status, self.status_changed = self.status, to, True
            fields: dict[str, Any] = {"status": to.value, **run_fields}
            if to in TERMINAL_STATUSES and reason:
                fields["outcome_reason"] = reason
            self.repo.update_run(self.run_id, **fields)
            self.audit("status_changed", f"{prev.value} → {to.value}" + (f": {reason}" if reason else ""),
                       metadata={"from": prev.value, "to": to.value})
        elif run_fields:
            self.repo.update_run(self.run_id, **run_fields)

    def audit(self, event_type: str, summary: str, **kw: Any) -> str:
        return self.repo.add_audit(self.run_id, node=self.node, event_type=event_type, summary=summary, **kw)

    def error(self, code_or_exc: ErrorCode | SettleError, message: str | None = None, *,
              system: str | None = None) -> ErrorRecord:
        if isinstance(code_or_exc, SettleError):
            rec = code_or_exc.to_record(self.run_id, self.node)
        else:
            rec = ErrorRecord(code=code_or_exc, message=message or "", external_system=system, run_id=self.run_id,
                              node=self.node)
        self.errors.append(rec)
        self.audit("error", f"{rec.code.value}: {rec.message}", external_system=rec.external_system,
                   metadata={"code": rec.code.value, "retryable": rec.retryable})
        return rec

    def call(self, system: str, op: str, fn: Callable[[], T], *, object_type: str | None = None,
             object_id: str | None = None, retry: bool = True, write: bool = False) -> T:
        """External tool call with bounded retries; every attempt outcome lands in the audit trail."""
        attempts = {"n": 0}

        def attempt() -> T:
            attempts["n"] += 1
            return fn()

        def on_retry(n: int, exc: IntegrationError, delay: float) -> None:
            self.audit("tool_call_retry", f"{system}.{op} attempt {n} failed ({exc.code.value}"
                       f"{', HTTP ' + str(exc.status_code) if exc.status_code else ''}); retrying in {delay:.2f}s",
                       external_system=system, object_type=object_type, object_id=object_id,
                       metadata={"status_code": exc.status_code, "attempt": n})

        started = time.perf_counter()
        try:
            result = with_retries(
                attempt,
                attempts=self.settings.retry_max_attempts if retry else 1,
                base_delay=self.settings.retry_base_delay_s,
                sleep=self.deps.sleep,
                on_retry=on_retry,
            )
        except IntegrationError as exc:
            self.last_attempts = attempts["n"]
            self.audit("tool_call_failed", f"{system}.{op} failed after {attempts['n']} attempt(s): {exc.message}",
                       external_system=system, object_type=object_type, object_id=object_id,
                       metadata={"code": exc.code.value, "retryable": exc.retryable,
                                 "outcome_unknown": exc.outcome_unknown, "write": write})
            raise
        self.last_attempts = attempts["n"]
        self.audit("tool_call_completed", f"{system}.{op}" + (f" ({attempts['n']} attempts)" if attempts["n"] > 1 else ""),
                   external_system=system, object_type=object_type, object_id=object_id,
                   metadata={"latency_ms": round((time.perf_counter() - started) * 1000, 1), "write": write,
                             "attempts": attempts["n"]})
        return result

    def notify(self, text: str) -> None:
        """Slack status update. A Slack outage never fails a dispute run."""
        try:
            self.call("slack", "post_status", lambda: self.slack.post_status(text))
        except IntegrationError as exc:
            self.error(exc)


def _short(value: Any) -> str:
    if isinstance(value, BaseModel):
        for attr in ("status", "outcome", "resolution", "dispute_type", "decision"):
            if hasattr(value, attr):
                return f"{type(value).__name__}({getattr(value, attr)})"
        return type(value).__name__
    if isinstance(value, list):
        return f"[{len(value)}]"
    return str(value)[:60]


def traced(name: str):
    """Wrap a node: structured logs, latency, audit event, status + error propagation into state."""

    def deco(fn: Callable[[dict, NodeContext], dict]):
        @functools.wraps(fn)
        def node(state: dict, deps: Deps) -> dict:
            ctx = NodeContext(deps, state, name)
            started = time.perf_counter()
            log_event(_log, "node_started", run_id=ctx.run_id, thread_id=state.get("thread_id"), node=name,
                      status=ctx.status.value)
            try:
                update = fn(state, ctx) or {}
            except GraphBubbleUp:  # interrupt(): the graph is pausing for a human, not failing
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                latency = round((time.perf_counter() - started) * 1000, 1)
                log_event(_log, "node_failed", logging.ERROR, run_id=ctx.run_id, node=name, latency_ms=latency,
                          error_code=str(code or type(exc).__name__))
                ctx.audit("node_failed", f"{name} raised {type(exc).__name__}: {str(exc)[:300]}",
                          metadata={"error_code": str(code) if code else None, "latency_ms": latency})
                raise
            if ctx.status_changed:
                update.setdefault("status", ctx.status)
            if ctx.errors:
                update["errors"] = [*update.get("errors", []), *ctx.errors]
            latency = round((time.perf_counter() - started) * 1000, 1)
            summary = ", ".join(f"{k}={_short(v)}" for k, v in update.items() if k != "errors") or "no changes"
            ctx.audit("node_completed", f"{name}: {summary}",
                      metadata={"latency_ms": latency, "status": ctx.status.value, "output_keys": sorted(update)})
            log_event(_log, "node_completed", run_id=ctx.run_id, thread_id=state.get("thread_id"), node=name,
                      status=ctx.status.value, latency_ms=latency, output_summary=summary,
                      error_code=ctx.errors[-1].code.value if ctx.errors else None)
            return update

        node.node_name = name
        return node

    return deco
