"""Deterministic financial reconciliation (PRD §12). The authority for money.

The LLM never manufactures candidate lines or amounts; it only sees the output
of this module.
"""

from __future__ import annotations

import re

from app.domain.models import (
    Claim,
    CreditNoteSnapshot,
    DuplicateCandidate,
    FinancialReconciliation,
    InvoiceLine,
    InvoiceSnapshot,
)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _signature(line: InvoiceLine) -> tuple | None:
    product_key = line.price_id or line.product_id or line.metadata.get("sku")
    desc = _norm(line.description)
    if not product_key and not desc:
        return None  # amount+currency alone is too weak a signal
    return (line.currency.lower(), line.amount_minor, product_key, line.period_start, line.period_end, desc)


def _signals(line: InvoiceLine) -> list[str]:
    signals = ["same customer (same invoice)", "same currency", "same amount"]
    if line.price_id or line.product_id or line.metadata.get("sku"):
        signals.append("same product/price")
    if line.period_start is not None:
        signals.append("same usage period")
    if _norm(line.description):
        signals.append("same normalized description")
    return signals


def reconcile(
    claim: Claim, invoice: InvoiceSnapshot, credit_notes: list[CreditNoteSnapshot]
) -> FinancialReconciliation:
    base = {"currency": invoice.currency.lower(), "invoice_remaining_minor": invoice.amount_remaining_minor}
    notes: list[str] = []
    if claim.currency and claim.currency.lower() != invoice.currency.lower():
        notes.append(f"Claim currency {claim.currency.upper()} differs from invoice currency {invoice.currency.upper()}.")

    if claim.dispute_type != "duplicate_charge":
        notes.append(f"No deterministic calculation is implemented for '{claim.dispute_type}'; human review required.")
        return FinancialReconciliation(outcome="NOT_APPLICABLE", notes=notes, **base)

    groups: dict[tuple, list[InvoiceLine]] = {}
    for line in invoice.lines:  # invoice order is preserved: first occurrence = original
        if line.amount_minor <= 0:
            continue
        sig = _signature(line)
        if sig is not None:
            groups.setdefault(sig, []).append(line)
    dup_groups = [g for g in groups.values() if len(g) >= 2]
    candidates = [
        DuplicateCandidate(
            original_line_id=g[0].id,
            duplicate_line_id=g[i].id,
            amount_minor=g[i].amount_minor,
            currency=g[i].currency.lower(),
            matched_signals=_signals(g[i]),
        )
        for g in dup_groups
        for i in range(1, len(g))
    ]

    selected: DuplicateCandidate | None = None
    if not dup_groups:
        outcome = "INCONCLUSIVE"
        notes.append("No pair of invoice lines matches on amount, currency, product/price, period and description.")
    elif len(candidates) == 1:
        outcome, selected = "DUPLICATE_CANDIDATE", candidates[0]
    else:
        outcome = "AMBIGUOUS"
        if claim.disputed_amount_minor is not None:
            matching = [c for c in candidates if c.amount_minor == claim.disputed_amount_minor]
            if len(matching) == 1:
                outcome, selected = "DUPLICATE_CANDIDATE", matching[0]
                notes.append("Multiple duplicate groups; disambiguated deterministically by the claimed amount.")
        if selected is None:
            notes.append(f"{len(candidates)} duplicate candidates found; cannot select one without a human.")

    existing: list[str] = []
    if selected is not None:
        pair = {selected.original_line_id, selected.duplicate_line_id}
        for cn in credit_notes:
            if cn.status == "void":
                continue
            if pair & set(cn.line_item_ids) or (
                cn.reason == "duplicate" and cn.amount_minor == selected.amount_minor
            ):
                existing.append(cn.id)
        if existing:
            notes.append(f"Equivalent credit already exists: {', '.join(existing)}.")

    claim_matches = (
        None
        if claim.disputed_amount_minor is None or selected is None
        else claim.disputed_amount_minor == selected.amount_minor
    )
    if claim_matches is False:
        notes.append("Claimed amount differs from the duplicated line amount.")

    return FinancialReconciliation(
        outcome=outcome,
        candidates=candidates,
        selected=selected,
        credit_amount_minor=selected.amount_minor if selected and not existing else None,
        already_credited=bool(existing),
        existing_credit_note_ids=existing,
        claim_amount_matches=claim_matches,
        notes=notes,
        **base,
    )
