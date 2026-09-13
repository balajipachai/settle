"""Hard financial invariants (PRD §12.3, §19.3). Violations fail closed."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.actions import recompute_hash
from app.domain.errors import ErrorCode, InvariantViolation
from app.domain.models import (
    Claim,
    CreditNoteSnapshot,
    ExecutionResult,
    FinancialReconciliation,
    IdentityResolution,
    InvoiceSnapshot,
    ResolutionAction,
    VerificationResult,
)
from app.domain.policies import PolicyConfig, disputed_amount


@dataclass(frozen=True)
class ApprovalFacts:
    """Server-side approval record (never the Slack display text)."""

    decision: str
    action_hash: str
    valid: bool


def check_pre_execution(
    *,
    action: ResolutionAction,
    identity: IdentityResolution,
    fresh_invoice: InvoiceSnapshot,
    fresh_credit_notes: list[CreditNoteSnapshot],
    claim: Claim,
    reconciliation: FinancialReconciliation,
    approval: ApprovalFacts | None,
    policy: PolicyConfig,
) -> list[tuple[str, bool]]:
    """Evaluate the 11 pre-write invariants against *freshly re-read* Stripe state.

    Invariant 12 (idempotency key has not already succeeded) is enforced by the
    ledger compare-and-swap in the execution service.
    """
    disputed = disputed_amount(claim, reconciliation)
    line_ids = {line.id for line in fresh_invoice.lines}
    prior = [
        cn.id
        for cn in fresh_credit_notes
        if cn.status != "void"
        and (
            cn.metadata.get("settle_action_hash") == action.action_hash
            or set(cn.line_item_ids) & set(action.stripe_line_item_ids)
        )
    ]
    current_hash = recompute_hash(action)
    return [
        ("1 action.customer_id == resolved customer", action.customer_id == identity.customer_id == fresh_invoice.customer_id),
        ("2 action.invoice_id == resolved invoice", action.invoice_id == identity.invoice_id == fresh_invoice.id),
        ("3 currency matches invoice currency", action.currency == fresh_invoice.currency.lower()),
        ("4 amount_minor > 0", action.amount_minor > 0),
        ("5 amount_minor <= disputed amount", disputed is not None and action.amount_minor <= disputed),
        ("6 amount_minor <= invoice amount remaining", action.amount_minor <= fresh_invoice.amount_remaining_minor),
        ("7 no equivalent prior credit exists", not prior),
        ("8 invoice eligible for credit-note mutation", fresh_invoice.status in policy.eligible_invoice_statuses),
        ("9 source line item belongs to the invoice", bool(action.stripe_line_item_ids) and set(action.stripe_line_item_ids) <= line_ids),
        ("10 required human approval exists", approval is not None and approval.valid and approval.decision == "approve"),
        (
            "11 approval action hash matches exact current action",
            approval is not None and approval.action_hash == current_hash == action.action_hash,
        ),
    ]


def assert_pre_execution(**kwargs) -> list[tuple[str, bool]]:
    results = check_pre_execution(**kwargs)
    failed = [name for name, ok in results if not ok]
    if failed:
        code = (
            ErrorCode.APPROVAL_INVALIDATED
            if any(n.startswith(("10", "11")) for n in failed)
            else ErrorCode.INVARIANT_VIOLATION
        )
        raise InvariantViolation("Pre-execution invariants failed: " + "; ".join(failed), code=code)
    return results


def assert_pre_customer_email(
    verification: VerificationResult | None, execution: ExecutionResult | None
) -> None:
    if verification is None or verification.status != "VERIFIED":
        raise InvariantViolation(
            "Customer email blocked: external state is not VERIFIED.", code=ErrorCode.CUSTOMER_EMAIL_BLOCKED
        )
    if not verification.stripe_ok or not verification.hubspot_ok:
        raise InvariantViolation("Customer email blocked: partial verification.", code=ErrorCode.CUSTOMER_EMAIL_BLOCKED)
    if execution is None or not execution.stripe_credit_note_id:
        raise InvariantViolation("Customer email blocked: no Stripe credit note id.", code=ErrorCode.CUSTOMER_EMAIL_BLOCKED)
