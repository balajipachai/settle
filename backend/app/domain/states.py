"""Dispute status machine (PRD §8.1–8.2).

Every status change goes through `assert_transition`; illegal transitions fail
closed with UNEXPECTED_STATE_TRANSITION.
"""

from __future__ import annotations

from enum import StrEnum

from app.domain.errors import ErrorCode, InvariantViolation


class DisputeStatus(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTING = "EXTRACTING"
    RESOLVING_IDENTITY = "RESOLVING_IDENTITY"
    GATHERING_EVIDENCE = "GATHERING_EVIDENCE"
    RECONCILING = "RECONCILING"
    ADJUDICATING = "ADJUDICATING"
    POLICY_REVIEW = "POLICY_REVIEW"
    AWAITING_HUMAN_INVESTIGATION = "AWAITING_HUMAN_INVESTIGATION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    # Extension: duplicate already credited -> explain, no mutation (PRD §7.2).
    CLOSED_NO_ACTION = "CLOSED_NO_ACTION"


S = DisputeStatus

LEGAL_TRANSITIONS: dict[DisputeStatus, frozenset[DisputeStatus]] = {
    S.RECEIVED: frozenset({S.EXTRACTING, S.FAILED}),
    S.EXTRACTING: frozenset({S.RESOLVING_IDENTITY, S.AWAITING_HUMAN_INVESTIGATION, S.FAILED}),
    S.RESOLVING_IDENTITY: frozenset({S.GATHERING_EVIDENCE, S.AWAITING_HUMAN_INVESTIGATION, S.FAILED}),
    S.GATHERING_EVIDENCE: frozenset({S.RECONCILING, S.AWAITING_HUMAN_INVESTIGATION, S.FAILED}),
    S.RECONCILING: frozenset({S.ADJUDICATING, S.AWAITING_HUMAN_INVESTIGATION, S.FAILED}),
    S.ADJUDICATING: frozenset(
        {S.POLICY_REVIEW, S.AWAITING_HUMAN_INVESTIGATION, S.CLOSED_NO_ACTION, S.FAILED}
    ),
    S.POLICY_REVIEW: frozenset({S.AWAITING_APPROVAL, S.BLOCKED}),
    # POLICY_REVIEW again = reviewer edited a financial field -> revalidate + re-approve.
    S.AWAITING_APPROVAL: frozenset(
        {S.APPROVED, S.REJECTED, S.AWAITING_HUMAN_INVESTIGATION, S.POLICY_REVIEW}
    ),
    S.APPROVED: frozenset({S.EXECUTING}),
    S.EXECUTING: frozenset({S.PARTIALLY_COMPLETED, S.VERIFYING, S.FAILED, S.ESCALATED}),
    S.PARTIALLY_COMPLETED: frozenset({S.REPAIRING}),
    S.VERIFYING: frozenset({S.RESOLVED, S.REPAIRING, S.ESCALATED}),
    S.REPAIRING: frozenset({S.VERIFYING, S.ESCALATED}),
    # Operator-triggered repair retry after escalation.
    S.ESCALATED: frozenset({S.REPAIRING}),
}

TERMINAL_STATUSES = frozenset(
    {S.RESOLVED, S.REJECTED, S.BLOCKED, S.FAILED, S.CLOSED_NO_ACTION, S.AWAITING_HUMAN_INVESTIGATION}
)


def is_legal(frm: DisputeStatus, to: DisputeStatus) -> bool:
    return frm == to or to in LEGAL_TRANSITIONS.get(frm, frozenset())


def assert_transition(frm: DisputeStatus, to: DisputeStatus) -> None:
    if not is_legal(frm, to):
        raise InvariantViolation(
            f"Illegal status transition {frm} -> {to}", code=ErrorCode.UNEXPECTED_STATE_TRANSITION
        )
