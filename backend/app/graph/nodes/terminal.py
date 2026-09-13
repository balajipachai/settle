"""Terminal / hand-off nodes. None of these can reach a write tool."""

from __future__ import annotations

from app.domain.states import DisputeStatus as S
from app.graph.common import NodeContext, traced


def _who(state: dict) -> str:
    ident = state.get("identity")
    if ident and ident.company_name:
        return ident.company_name
    email = state.get("source_email")
    return email.from_email if email else state.get("source_email_id", "unknown sender")


def _derive_reason(state: dict) -> str:
    ident = state.get("identity")
    if ident and ident.status != "RESOLVED":
        return ident.reason
    adj = state.get("adjudication")
    if adj:
        return f"{adj.validity}/{adj.resolution}: {adj.rationale}"
    return "Needs human review."


@traced("human_investigation")
def human_investigation(state: dict, ctx: NodeContext) -> dict:
    reason = state.get("outcome_reason") or _derive_reason(state)
    ctx.transition(S.AWAITING_HUMAN_INVESTIGATION, reason=reason)
    ctx.notify(f"🔎 Human investigation needed — {_who(state)}: {reason}")
    return {"outcome_reason": reason}


@traced("block")
def block(state: dict, ctx: NodeContext) -> dict:
    reason = state.get("outcome_reason") or "Policy blocked the proposed action."
    ctx.transition(S.BLOCKED, reason=reason)
    action = state.get("proposed_action")
    if action:
        ctx.repo.cas_financial_action(action.action_id, ("PROPOSED",), "CANCELLED", last_error="policy blocked")
    ctx.notify(f"⛔ {_who(state)}: no financial action. {reason}")
    return {"outcome_reason": reason}


@traced("close_rejected")
def close_rejected(state: dict, ctx: NodeContext) -> dict:
    reason = state.get("outcome_reason") or "Rejected by reviewer."
    ctx.transition(S.REJECTED, reason=reason)
    action = state["proposed_action"]
    ctx.repo.cas_financial_action(action.action_id, ("PROPOSED",), "CANCELLED", last_error="rejected by reviewer")
    ctx.notify(f"🚫 {_who(state)}: credit rejected, no mutation. {reason}")
    return {"outcome_reason": reason}


@traced("close_no_action")
def close_no_action(state: dict, ctx: NodeContext) -> dict:
    recon = state["reconciliation"]
    reason = (f"Duplicate already credited ({', '.join(recon.existing_credit_note_ids)}); no new credit issued."
              if recon.already_credited else state.get("outcome_reason") or "No action required.")
    ctx.transition(S.CLOSED_NO_ACTION, reason=reason)
    ctx.notify(f"ℹ️ {_who(state)}: {reason}")
    return {"outcome_reason": reason}


@traced("escalate")
def escalate(state: dict, ctx: NodeContext) -> dict:
    reason = state.get("outcome_reason") or "Escalated."
    reason = f"Stripe state does not match the approved action — never auto-reversed. {reason}"
    ctx.transition(S.ESCALATED, reason=reason)
    ctx.notify(f"🚨 {_who(state)}: {reason}")
    return {"outcome_reason": reason}
