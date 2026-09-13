"""Deterministic policy engine (PRD §14).

Policy values are loaded from policy.json. Nothing in a prompt or a customer
email can change them.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from app.domain.actions import recompute_hash
from app.domain.errors import ErrorCode
from app.domain.models import (
    Adjudication,
    Claim,
    CreditNoteSnapshot,
    FinancialReconciliation,
    IdentityResolution,
    InvoiceSnapshot,
    PolicyCheck,
    PolicyResult,
    ResolutionAction,
)
from app.domain.money import format_money


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "2026-09-14.1"
    allowed_currencies: tuple[str, ...] = ("usd", "eur", "gbp")
    eligible_invoice_statuses: tuple[str, ...] = ("open",)
    max_credit_amount_minor: int | None = 5_000_000
    min_model_confidence: float = 0.90
    require_human_for_all_financial_actions: bool = True
    block_on_prompt_injection_signals: bool = True

    @field_validator("require_human_for_all_financial_actions")
    @classmethod
    def _human_always(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Settle never executes financial actions without human approval.")
        return v


def load_policy(path: Path | None) -> PolicyConfig:
    if path and path.exists():
        return PolicyConfig(**json.loads(path.read_text()))
    return PolicyConfig()


class PolicyContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    identity: IdentityResolution
    invoice: InvoiceSnapshot
    claim: Claim
    reconciliation: FinancialReconciliation
    adjudication: Adjudication
    credit_notes: list[CreditNoteSnapshot]
    ledger_status_for_hash: str | None = None


def disputed_amount(claim: Claim, recon: FinancialReconciliation) -> int | None:
    """What the customer disputes: the stated amount, else the identified duplicate line."""
    if claim.disputed_amount_minor is not None:
        return claim.disputed_amount_minor
    return recon.selected.amount_minor if recon.selected else None


def evaluate_resolution(action: ResolutionAction, ctx: PolicyContext, config: PolicyConfig) -> PolicyResult:
    checks: list[PolicyCheck] = []

    def check(rule: str, ok: bool, detail: str, code: ErrorCode) -> None:
        checks.append(PolicyCheck(rule=rule, passed=bool(ok), detail=detail, error_code=None if ok else code))

    ident, inv, recon, adj = ctx.identity, ctx.invoice, ctx.reconciliation, ctx.adjudication
    fm = lambda m: format_money(m, action.currency)  # noqa: E731

    check(
        "ACTION_HASH_INTEGRITY",
        recompute_hash(action) == action.action_hash,
        "Action hash matches the canonical action fields.",
        ErrorCode.APPROVAL_INVALIDATED,
    )
    check(
        "IDENTITY_RESOLVED",
        ident.status == "RESOLVED"
        and action.customer_id == ident.customer_id == inv.customer_id
        and action.invoice_id == ident.invoice_id == inv.id,
        f"Identity {ident.status}; action customer {action.customer_id}, invoice customer {inv.customer_id}.",
        ErrorCode.EVIDENCE_CONFLICT if ident.status == "RESOLVED" else ErrorCode.IDENTITY_NOT_FOUND,
    )
    claim_cur = (ctx.claim.currency or "").lower()
    check(
        "CURRENCY_MATCHES",
        action.currency == inv.currency.lower()
        and (not claim_cur or claim_cur == inv.currency.lower())
        and action.currency in config.allowed_currencies,
        f"Action {action.currency.upper()}, invoice {inv.currency.upper()}, claim {claim_cur.upper() or 'n/a'}.",
        ErrorCode.POLICY_CURRENCY_MISMATCH,
    )
    check(
        "INVOICE_OPEN_AND_ELIGIBLE",
        inv.status in config.eligible_invoice_statuses,
        f"Invoice status '{inv.status}'; eligible: {', '.join(config.eligible_invoice_statuses)}.",
        ErrorCode.POLICY_INVOICE_INELIGIBLE,
    )
    check(
        "ADJUDICATION_SUPPORTS_CREDIT",
        adj.validity == "VALID"
        and adj.resolution == "CREDIT"
        and adj.classification == "duplicate_charge"
        and adj.model_confidence >= config.min_model_confidence,
        f"{adj.validity}/{adj.resolution}/{adj.classification}, confidence {adj.model_confidence:.2f} "
        f"(min {config.min_model_confidence:.2f}).",
        ErrorCode.EVIDENCE_INSUFFICIENT,
    )
    check(
        "DUPLICATE_ESTABLISHED",
        recon.outcome == "DUPLICATE_CANDIDATE"
        and recon.selected is not None
        and action.stripe_line_item_ids == [recon.selected.duplicate_line_id],
        f"Reconciliation outcome {recon.outcome}.",
        ErrorCode.EVIDENCE_INSUFFICIENT,
    )
    check("AMOUNT_POSITIVE", action.amount_minor > 0, f"Amount {fm(action.amount_minor)}.", ErrorCode.POLICY_AMOUNT_INVALID)
    reconciled = recon.credit_amount_minor
    check(
        "AMOUNT_NOT_GREATER_THAN_RECONCILED",
        reconciled is not None and action.amount_minor <= reconciled,
        f"Deterministic duplicate amount {fm(reconciled) if reconciled is not None else 'n/a'}.",
        ErrorCode.POLICY_AMOUNT_EXCEEDED,
    )
    disputed = disputed_amount(ctx.claim, recon)
    check(
        "AMOUNT_NOT_GREATER_THAN_DISPUTED",
        disputed is not None and action.amount_minor <= disputed,
        f"Disputed amount {fm(disputed) if disputed is not None else 'n/a'}.",
        ErrorCode.POLICY_AMOUNT_EXCEEDED,
    )
    check(
        "AMOUNT_NOT_GREATER_THAN_INVOICE_REMAINING",
        action.amount_minor <= inv.amount_remaining_minor,
        f"Invoice amount remaining {fm(inv.amount_remaining_minor)}.",
        ErrorCode.POLICY_AMOUNT_EXCEEDED,
    )
    if config.max_credit_amount_minor is not None:
        check(
            "AMOUNT_WITHIN_POLICY_LIMIT",
            action.amount_minor <= config.max_credit_amount_minor,
            f"Policy limit {fm(config.max_credit_amount_minor)}.",
            ErrorCode.POLICY_AMOUNT_EXCEEDED,
        )
    equivalent = [
        cn.id
        for cn in ctx.credit_notes
        if cn.status != "void"
        and (
            cn.metadata.get("settle_action_hash") == action.action_hash
            or set(cn.line_item_ids) & set(action.stripe_line_item_ids)
        )
    ]
    check(
        "NO_EQUIVALENT_CREDIT",
        not recon.already_credited and not equivalent and ctx.ledger_status_for_hash != "SUCCEEDED",
        "No prior credit for this line/action."
        if not (recon.already_credited or equivalent or ctx.ledger_status_for_hash == "SUCCEEDED")
        else f"Existing credit: {', '.join(equivalent or recon.existing_credit_note_ids) or 'ledger SUCCEEDED'}.",
        ErrorCode.POLICY_DUPLICATE_CREDIT,
    )
    invoice_line_ids = {line.id for line in inv.lines}
    check(
        "LINE_ITEM_BELONGS_TO_INVOICE",
        bool(action.stripe_line_item_ids) and set(action.stripe_line_item_ids) <= invoice_line_ids,
        f"Lines {', '.join(action.stripe_line_item_ids)} on invoice {inv.number or inv.id}.",
        ErrorCode.EVIDENCE_CONFLICT,
    )
    if config.block_on_prompt_injection_signals:
        check(
            "NO_PROMPT_INJECTION_SIGNALS",
            not ctx.claim.injection_signals,
            "No instruction-like content in the customer email."
            if not ctx.claim.injection_signals
            else f"Untrusted email contains: {', '.join(ctx.claim.injection_signals)}.",
            ErrorCode.PROMPT_INJECTION_DETECTED,
        )
    check(
        "APPROVAL_REQUIRED",
        config.require_human_for_all_financial_actions,
        "Human approval is required before any financial mutation.",
        ErrorCode.APPROVAL_REQUIRED,
    )

    blocked = [c.error_code for c in checks if not c.passed and c.error_code]
    return PolicyResult(
        passed=all(c.passed for c in checks), checks=checks, blocked_codes=blocked, policy_version=config.version
    )
