"""Customer-facing wording. Deterministic templates + a validator (PRD §4.2, §19.3)."""

from __future__ import annotations

import re

from app.domain.money import find_money, format_money

_REFUND = re.compile(r"\brefund(s|ed|ing)?\b", re.IGNORECASE)


def draft_customer_message(
    *, first_name: str | None, invoice_number: str, amount_minor: int, currency: str, line_description: str | None
) -> str:
    greeting = f"Hi {first_name}," if first_name else "Hello,"
    item = f'"{line_description}"' if line_description else "the disputed charge"
    return (
        f"{greeting}\n\n"
        f"Thank you for flagging this. We reviewed invoice {invoice_number} and confirmed that {item} was billed twice.\n\n"
        f"We have applied a credit of {format_money(amount_minor, currency)} to invoice {invoice_number}. "
        "The credit reduces the balance due on the invoice; no action is needed on your side.\n\n"
        "Best regards,\nSettle Billing"
    )


def validate_customer_message(body: str, *, amount_minor: int, currency: str, invoice_number: str) -> list[str]:
    problems: list[str] = []
    if _REFUND.search(body):
        problems.append("mentions a refund (Settle applies invoice credits, never cash refunds)")
    if invoice_number not in body:
        problems.append("does not reference the invoice number")
    if "credit" not in body.lower():
        problems.append("does not describe a credit")
    mentions = find_money(body)
    if not any(m.minor == amount_minor and m.currency == currency.lower() for m in mentions):
        problems.append("does not state the credited amount")
    if any(m.minor != amount_minor or m.currency != currency.lower() for m in mentions):
        problems.append("mentions amounts other than the credit being applied")
    return problems


def verified_details_block(
    *, invoice_number: str, credit_note_id: str, credit_note_number: str | None, amount_minor: int, currency: str,
    remaining_minor: int | None,
) -> str:
    lines = [
        "Resolution details (independently verified):",
        f"- Credit note: {credit_note_number or credit_note_id}",
        f"- Credit applied to invoice {invoice_number}: {format_money(amount_minor, currency)}",
    ]
    if remaining_minor is not None:
        lines.append(f"- Invoice {invoice_number} balance remaining: {format_money(remaining_minor, currency)}")
    return "\n".join(lines)
