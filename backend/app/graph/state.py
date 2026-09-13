"""Typed, checkpointable LangGraph state (PRD §8)."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from app.domain.errors import ErrorRecord
from app.domain.models import (
    Adjudication,
    ApprovalDecision,
    Claim,
    CustomerReply,
    EmailMessage,
    EvidenceBundle,
    ExecutionResult,
    FinancialReconciliation,
    IdentityResolution,
    PolicyResult,
    ResolutionAction,
    VerificationResult,
)
from app.domain.states import DisputeStatus


class DisputeState(TypedDict, total=False):
    run_id: str
    thread_id: str
    source_email_id: str
    source_thread_id: str
    replay_of_run_id: str | None

    source_email: EmailMessage
    claim: Claim
    identity: IdentityResolution
    evidence: EvidenceBundle
    reconciliation: FinancialReconciliation
    adjudication: Adjudication
    proposed_action: ResolutionAction
    policy_result: PolicyResult
    approval: ApprovalDecision | None
    execution: ExecutionResult | None
    verification: VerificationResult | None
    customer_reply: CustomerReply | None

    status: DisputeStatus
    outcome_reason: str | None
    repair_cycles: int
    errors: Annotated[list[ErrorRecord], operator.add]
