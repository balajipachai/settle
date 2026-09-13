"""Action identity: hash, deterministic action_id and idempotency key (PRD §15.2, §17.1).

The hash is SHA-256 over a canonical JSON document instead of raw string
concatenation, so field boundaries are unambiguous ("12"+"3" != "1"+"23").
Customer-facing wording is deliberately excluded: editing it never changes the
financial action, so it never invalidates an approval.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from app.domain.models import ResolutionAction

_ACTION_NAMESPACE = uuid.UUID("7d1f3c2a-5e0b-4c8e-9a61-5e771e000001")


def compute_action_hash(
    *,
    invoice_id: str,
    customer_id: str,
    amount_minor: int,
    currency: str,
    reason: str,
    stripe_line_item_ids: list[str],
) -> str:
    canonical = json.dumps(
        {
            "invoice_id": invoice_id,
            "customer_id": customer_id,
            "amount_minor": int(amount_minor),
            "currency": currency.lower(),
            "reason": reason,
            "stripe_line_item_ids": sorted(stripe_line_item_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def action_id_for(action_hash: str) -> str:
    """Same logical action => same action_id => same idempotency key, across retries and replays."""
    return str(uuid.uuid5(_ACTION_NAMESPACE, action_hash))


def idempotency_key_for(action_id: str) -> str:
    return f"settle:{action_id}:credit"


def recompute_hash(action: ResolutionAction) -> str:
    return compute_action_hash(
        invoice_id=action.invoice_id,
        customer_id=action.customer_id,
        amount_minor=action.amount_minor,
        currency=action.currency,
        reason=action.reason,
        stripe_line_item_ids=action.stripe_line_item_ids,
    )


def build_credit_action(
    *,
    invoice_id: str,
    invoice_number: str | None,
    customer_id: str,
    amount_minor: int,
    currency: str,
    reason: str,
    stripe_line_item_ids: list[str],
    customer_message_draft: str,
    supersedes_action_hash: str | None = None,
) -> ResolutionAction:
    action_hash = compute_action_hash(
        invoice_id=invoice_id,
        customer_id=customer_id,
        amount_minor=amount_minor,
        currency=currency,
        reason=reason,
        stripe_line_item_ids=stripe_line_item_ids,
    )
    action_id = action_id_for(action_hash)
    return ResolutionAction(
        action_id=action_id,
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        customer_id=customer_id,
        amount_minor=int(amount_minor),
        currency=currency.lower(),
        reason=reason,
        stripe_line_item_ids=sorted(stripe_line_item_ids),
        customer_message_draft=customer_message_draft,
        action_hash=action_hash,
        idempotency_key=idempotency_key_for(action_id),
        supersedes_action_hash=supersedes_action_hash,
    )
