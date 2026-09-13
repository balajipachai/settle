"""Typed error taxonomy (PRD §21)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.domain.timeutil import utcnow


class ErrorCode(StrEnum):
    IDENTITY_NOT_FOUND = "IDENTITY_NOT_FOUND"
    IDENTITY_AMBIGUOUS = "IDENTITY_AMBIGUOUS"
    INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
    INVOICE_AMBIGUOUS = "INVOICE_AMBIGUOUS"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    POLICY_CURRENCY_MISMATCH = "POLICY_CURRENCY_MISMATCH"
    POLICY_AMOUNT_EXCEEDED = "POLICY_AMOUNT_EXCEEDED"
    POLICY_AMOUNT_INVALID = "POLICY_AMOUNT_INVALID"
    POLICY_DUPLICATE_CREDIT = "POLICY_DUPLICATE_CREDIT"
    POLICY_INVOICE_INELIGIBLE = "POLICY_INVOICE_INELIGIBLE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_INVALIDATED = "APPROVAL_INVALIDATED"
    APPROVER_NOT_AUTHORIZED = "APPROVER_NOT_AUTHORIZED"
    STRIPE_READ_ERROR = "STRIPE_READ_ERROR"
    STRIPE_WRITE_ERROR = "STRIPE_WRITE_ERROR"
    HUBSPOT_ERROR = "HUBSPOT_ERROR"
    GMAIL_ERROR = "GMAIL_ERROR"
    SLACK_ERROR = "SLACK_ERROR"
    LLM_ERROR = "LLM_ERROR"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    PROMPT_INJECTION_DETECTED = "PROMPT_INJECTION_DETECTED"
    UNEXPECTED_STATE_TRANSITION = "UNEXPECTED_STATE_TRANSITION"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    CUSTOMER_EMAIL_BLOCKED = "CUSTOMER_EMAIL_BLOCKED"


class ErrorRecord(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    external_system: str | None = None
    run_id: str | None = None
    node: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)


class SettleError(Exception):
    """Base error. Messages must never contain secrets or tokens."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        external_system: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.external_system = external_system

    def to_record(self, run_id: str | None = None, node: str | None = None) -> ErrorRecord:
        return ErrorRecord(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            external_system=self.external_system,
            run_id=run_id,
            node=node,
        )


class InvariantViolation(SettleError):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.INVARIANT_VIOLATION) -> None:
        super().__init__(code, message)
