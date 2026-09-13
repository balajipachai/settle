"""Typed domain objects (PRD §9). All graph state is built from these."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import ErrorCode
from app.domain.timeutil import utcnow

DisputeType = Literal[
    "duplicate_charge", "incorrect_quantity", "incorrect_price", "service_not_received", "unknown"
]
Validity = Literal["VALID", "INVALID", "INCONCLUSIVE"]
ResolutionCategory = Literal["CREDIT", "NO_ACTION", "REQUEST_INFORMATION", "HUMAN_INVESTIGATION"]
IdentityStatus = Literal["RESOLVED", "AMBIGUOUS", "NOT_FOUND", "CONFLICT"]
EvidenceSource = Literal["gmail", "stripe", "hubspot"]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- external snapshots


class EmailMessage(DomainModel):
    id: str
    thread_id: str
    from_email: str
    from_name: str | None = None
    to_email: str | None = None
    subject: str = ""
    body: str = ""
    received_at: datetime
    message_id_header: str | None = None


class CustomerSnapshot(DomainModel):
    id: str
    name: str | None = None
    email: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class InvoiceLine(DomainModel):
    id: str
    amount_minor: int
    currency: str
    description: str | None = None
    price_id: str | None = None
    product_id: str | None = None
    period_start: int | None = None
    period_end: int | None = None
    quantity: int | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class InvoiceSnapshot(DomainModel):
    id: str
    number: str | None = None
    customer_id: str
    currency: str
    status: str
    total_minor: int
    amount_due_minor: int
    amount_paid_minor: int
    amount_remaining_minor: int
    lines: list[InvoiceLine] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class CreditNoteSnapshot(DomainModel):
    id: str
    number: str | None = None
    invoice_id: str
    amount_minor: int
    currency: str
    status: str
    reason: str | None = None
    line_item_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class ContactSnapshot(DomainModel):
    id: str
    email: str
    first_name: str | None = None
    last_name: str | None = None
    company_ids: list[str] = Field(default_factory=list)


class CompanySnapshot(DomainModel):
    id: str
    name: str
    domain: str | None = None
    properties: dict[str, str | None] = Field(default_factory=dict)


class DealSnapshot(DomainModel):
    id: str
    name: str
    amount: str | None = None
    stage: str | None = None


# ---------------------------------------------------------------- investigation


class ClaimExtraction(BaseModel):
    """Structured LLM output. Amounts stay as written text; code parses them."""

    model_config = ConfigDict(extra="forbid")

    dispute_type: DisputeType
    disputed_amount_text: str | None = Field(
        description="The disputed amount exactly as written in the email, e.g. '$6,000'. Null if none stated."
    )
    currency_code: str | None = Field(description="ISO 4217 code if explicitly stated or implied by symbol, else null.")
    invoice_hint: str | None = Field(description="Invoice number exactly as written in the email, else null.")
    summary: str = Field(description="One-sentence neutral summary of what the customer claims.")
    evidence_phrases: list[str] = Field(description="Short verbatim quotes from the email that state the claim.")


class Claim(DomainModel):
    raw_text: str
    dispute_type: DisputeType
    disputed_amount_minor: int | None
    currency: str | None
    invoice_hint: str | None
    invoice_mentions: list[str] = Field(default_factory=list)
    summary: str = ""
    evidence_phrases: list[str] = Field(default_factory=list)
    injection_signals: list[str] = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list)
    extracted_by: str


class IdentityResolution(DomainModel):
    status: IdentityStatus
    customer_id: str | None = None
    contact_id: str | None = None
    hubspot_company_id: str | None = None
    company_name: str | None = None
    invoice_id: str | None = None
    invoice_number: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    reason: str
    error_code: ErrorCode | None = None


class EvidenceReference(DomainModel):
    ref_id: str
    source: EvidenceSource
    object_type: str
    object_id: str
    field: str | None = None
    value_summary: str
    trusted: bool
    retrieved_at: datetime = Field(default_factory=utcnow)


class EvidenceBundle(DomainModel):
    source_email: EmailMessage
    thread_messages: list[EmailMessage] = Field(default_factory=list)
    customer: CustomerSnapshot | None = None
    invoice: InvoiceSnapshot
    credit_notes: list[CreditNoteSnapshot] = Field(default_factory=list)
    contact: ContactSnapshot | None = None
    company: CompanySnapshot
    deals: list[DealSnapshot] = Field(default_factory=list)
    references: list[EvidenceReference] = Field(default_factory=list)

    def ref_ids(self) -> set[str]:
        return {r.ref_id for r in self.references}


class DuplicateCandidate(DomainModel):
    original_line_id: str
    duplicate_line_id: str
    amount_minor: int
    currency: str
    matched_signals: list[str]


class FinancialReconciliation(DomainModel):
    outcome: Literal["DUPLICATE_CANDIDATE", "INCONCLUSIVE", "AMBIGUOUS", "NOT_APPLICABLE"]
    candidates: list[DuplicateCandidate] = Field(default_factory=list)
    selected: DuplicateCandidate | None = None
    credit_amount_minor: int | None = None
    currency: str
    already_credited: bool = False
    existing_credit_note_ids: list[str] = Field(default_factory=list)
    claim_amount_matches: bool | None = None
    invoice_remaining_minor: int
    notes: list[str] = Field(default_factory=list)


class AdjudicationDecision(BaseModel):
    """Structured LLM output for the semantic question only (never 'how much')."""

    model_config = ConfigDict(extra="forbid")

    classification: DisputeType
    validity: Validity
    resolution: ResolutionCategory
    model_confidence: float = Field(description="Heuristic confidence 0.0-1.0; not a calibrated probability.")
    rationale: str = Field(description="2-4 sentences citing evidence by ref_id. No amounts beyond those in evidence.")
    evidence_ref_ids: list[str] = Field(description="ref_ids of the evidence items that support the decision.")


class Adjudication(DomainModel):
    classification: DisputeType
    validity: Validity
    resolution: ResolutionCategory
    model_confidence: float
    rationale: str
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    invalid_ref_ids: list[str] = Field(default_factory=list)
    guard_notes: list[str] = Field(default_factory=list)
    reasoner: str


# ---------------------------------------------------------------- execution control plane


class ResolutionAction(DomainModel):
    action_type: Literal["CREDIT_INVOICE"] = "CREDIT_INVOICE"
    action_id: str
    invoice_id: str
    invoice_number: str | None
    customer_id: str
    amount_minor: int
    currency: str
    reason: str
    reason_code: Literal["duplicate"] = "duplicate"
    stripe_line_item_ids: list[str]
    customer_message_draft: str
    action_hash: str
    idempotency_key: str
    supersedes_action_hash: str | None = None


class PolicyCheck(DomainModel):
    rule: str
    passed: bool
    detail: str
    error_code: ErrorCode | None = None


class PolicyResult(DomainModel):
    passed: bool
    checks: list[PolicyCheck]
    blocked_codes: list[ErrorCode] = Field(default_factory=list)
    policy_version: str
    evaluated_at: datetime = Field(default_factory=utcnow)


class ApprovalDecision(DomainModel):
    approval_id: str
    decision: Literal["approve", "reject", "edit"]
    reviewer_id: str
    channel: str
    action_hash_submitted: str
    action_hash_current: str
    valid: bool
    invalidation_reason: str | None = None
    invalidation_kind: Literal["tampered", "stale", "unauthorized"] | None = None
    note: str | None = None
    edits: dict[str, Any] | None = None
    decided_at: datetime = Field(default_factory=utcnow)


class ExecutionResult(DomainModel):
    financial_action_id: str
    financial_action_status: str
    stripe_credit_note_id: str | None = None
    stripe_credit_note_number: str | None = None
    pre_credit_remaining_minor: int | None = None
    stripe_replayed: bool = False
    stripe_outcome_reconciled: bool = False
    hubspot_updated: bool = False
    hubspot_attempts: int = 0
    hubspot_error: str | None = None
    repair_cycles: int = 0


class VerificationCheck(DomainModel):
    system: Literal["stripe", "hubspot"]
    check: str
    passed: bool
    expected: str
    actual: str


class VerificationResult(DomainModel):
    status: Literal["VERIFIED", "MISMATCH", "ERROR"]
    stripe_ok: bool
    hubspot_ok: bool
    checks: list[VerificationCheck]
    verified_invoice_remaining_minor: int | None = None
    verified_at: datetime = Field(default_factory=utcnow)


class CustomerReply(DomainModel):
    outbox_id: str
    gmail_message_id: str
    to_email: str
    subject: str
    body: str
    sent_at: datetime = Field(default_factory=utcnow)


class ApprovalCard(DomainModel):
    run_id: str
    action_id: str
    action_hash: str
    company_name: str
    invoice_number: str
    invoice_id: str
    claim_summary: str
    amount_minor: int
    currency: str
    evidence_summary: list[str]
    policy_checks: list[str]
    dashboard_url: str
