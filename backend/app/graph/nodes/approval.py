"""Safety + HITL: deterministic action, policy gate, action-bound approval, edits."""

from __future__ import annotations

from langgraph.types import interrupt

from app.domain.actions import build_credit_action, recompute_hash
from app.domain.errors import ErrorCode
from app.domain.models import ApprovalCard, ApprovalDecision, EvidenceBundle, ResolutionAction
from app.domain.money import format_money
from app.domain.policies import PolicyContext, evaluate_resolution
from app.domain.states import DisputeStatus as S
from app.graph.common import NodeContext, traced
from app.integrations.base import IntegrationError
from app.services.messages import draft_customer_message, validate_customer_message


@traced("prepare_resolution")
def prepare_resolution(state: dict, ctx: NodeContext) -> dict:
    """Build the exact financial action. The amount comes from reconciliation, never from the LLM."""
    ctx.transition(S.POLICY_REVIEW)
    recon, ident, ev = state["reconciliation"], state["identity"], state["evidence"]
    sel = recon.selected
    line = next(ln for ln in ev.invoice.lines if ln.id == sel.duplicate_line_id)
    reason = f"Duplicate charge: {line.description or 'invoice line'} ({sel.duplicate_line_id}) duplicates {sel.original_line_id}"
    action = build_credit_action(
        invoice_id=ident.invoice_id, invoice_number=ident.invoice_number, customer_id=ident.customer_id,
        amount_minor=recon.credit_amount_minor, currency=ev.invoice.currency, reason=reason,
        stripe_line_item_ids=[sel.duplicate_line_id],
        customer_message_draft=draft_customer_message(
            first_name=ev.contact.first_name if ev.contact else None, invoice_number=ident.invoice_number,
            amount_minor=recon.credit_amount_minor, currency=ev.invoice.currency, line_description=line.description),
    )
    ledger = ctx.repo.ensure_financial_action(action, ctx.run_id)
    ctx.audit("action_proposed", f"CREDIT_INVOICE {format_money(action.amount_minor, action.currency)} on "
              f"{action.invoice_number} line {sel.duplicate_line_id} (hash {action.action_hash[:12]})",
              object_type="financial_action", object_id=action.action_id,
              metadata={"ledger_status": ledger["status"], "idempotency_key": action.idempotency_key})
    return {"proposed_action": action}


@traced("validate_policy")
def validate_policy(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.POLICY_REVIEW)
    action: ResolutionAction = state["proposed_action"]
    ev: EvidenceBundle = state["evidence"]
    ledger = ctx.repo.get_financial_action_by_hash(action.action_hash)
    result = evaluate_resolution(
        action,
        PolicyContext(identity=state["identity"], invoice=ev.invoice, claim=state["claim"],
                      reconciliation=state["reconciliation"], adjudication=state["adjudication"],
                      credit_notes=ev.credit_notes, ledger_status_for_hash=ledger["status"] if ledger else None),
        ctx.deps.policy,
    )
    failed = [c for c in result.checks if not c.passed]
    ctx.audit("policy_evaluated", "PASSED" if result.passed else f"BLOCKED by {', '.join(c.rule for c in failed)}",
              metadata={"policy_version": result.policy_version,
                        "checks": [c.model_dump(mode="json") for c in result.checks]})
    if result.passed:
        return {"policy_result": result, "outcome_reason": None}
    for c in failed:
        ctx.error(c.error_code or ErrorCode.INVARIANT_VIOLATION, f"{c.rule}: {c.detail}")
    return {"policy_result": result, "outcome_reason": "Policy blocked: " + "; ".join(f"{c.rule} ({c.detail})" for c in failed)}


@traced("request_approval")
def request_approval(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.AWAITING_APPROVAL)
    action: ResolutionAction = state["proposed_action"]
    ev: EvidenceBundle = state["evidence"]
    recon, adj, claim = state["reconciliation"], state["adjudication"], state["claim"]
    p = ev.company.properties
    card = ApprovalCard(
        run_id=ctx.run_id, action_id=action.action_id, action_hash=action.action_hash, company_name=ev.company.name,
        invoice_number=action.invoice_number or action.invoice_id, invoice_id=action.invoice_id,
        claim_summary=claim.summary, amount_minor=action.amount_minor, currency=action.currency,
        evidence_summary=[
            f"Gmail: {ev.source_email.from_name or ev.source_email.from_email} disputes a duplicate on {action.invoice_number}",
            f"Stripe: {recon.selected.original_line_id} and {recon.selected.duplicate_line_id} match on "
            f"{', '.join(recon.selected.matched_signals)}",
            f"Stripe: invoice {ev.invoice.status}, remaining {format_money(ev.invoice.amount_remaining_minor, ev.invoice.currency)}",
            f"HubSpot: {ev.company.name} · {p.get('account_tier') or 'n/a'} · owner {p.get('account_owner') or 'n/a'}",
            f"Agent: {adj.validity} {adj.classification} (confidence {adj.model_confidence:.2f}, heuristic)",
        ],
        policy_checks=[f"✓ {c.rule}" for c in state["policy_result"].checks],
        dashboard_url=f"{ctx.settings.public_base_url}/#/runs/{ctx.run_id}",
    )
    ctx.repo.add_approval(run_id=ctx.run_id, action_id=action.action_id, action_hash=action.action_hash,
                          reviewer_id="settle-agent", decision="requested", channel="system")
    try:
        ctx.call("slack", "post_approval_request", lambda: ctx.slack.post_approval_request(card), object_type="approval",
                 object_id=action.action_id)
    except IntegrationError as exc:
        ctx.error(exc)  # dashboard approval still works; never blocks safety
    ctx.audit("approval_requested", f"Approval requested for {format_money(action.amount_minor, action.currency)} "
              f"(hash {action.action_hash[:12]})", object_type="financial_action", object_id=action.action_id)
    return {"approval": None}


@traced("approval_gate")
def approval_gate(state: dict, ctx: NodeContext) -> dict:
    """LangGraph interrupt before any irreversible side effect. Resumes with a reviewer decision."""
    action: ResolutionAction = state["proposed_action"]
    payload = interrupt({
        "kind": "approval", "run_id": ctx.run_id, "action_id": action.action_id, "action_hash": action.action_hash,
        "amount_minor": action.amount_minor, "currency": action.currency, "invoice_id": action.invoice_id,
        "invoice_number": action.invoice_number,
    })
    # ---- everything below runs only after a human responded ----
    payload = payload if isinstance(payload, dict) else {}
    decision = payload.get("decision")
    reviewer = str(payload.get("reviewer_id") or "unknown")
    channel = str(payload.get("channel") or "api")
    submitted = str(payload.get("action_hash") or "")
    current = recompute_hash(action)

    tampered, stale, unauthorized = [], [], []
    if current != action.action_hash:
        tampered.append("proposed action fields no longer match their hash")
    ledger = ctx.repo.get_financial_action_by_hash(current)
    if ledger is None or (ledger["amount_minor"], ledger["currency"], ledger["invoice_id"], ledger["customer_id"]) != (
            action.amount_minor, action.currency, action.invoice_id, action.customer_id):
        tampered.append("ledger record does not match the action")
    request = ctx.repo.latest_approval_request(ctx.run_id)
    if request is None or request["action_hash"] != current:
        tampered.append("no approval request exists for this exact action")
    if decision not in ("approve", "reject", "edit"):
        stale.append(f"unknown decision {decision!r}")
    if submitted != current:
        stale.append("approval is bound to a different action hash")
    approvers = ctx.settings.approver_id_set
    if approvers and reviewer not in approvers and decision in ("approve", "edit"):
        unauthorized.append(f"{reviewer} is not an authorized finance approver")

    problems = tampered + stale + unauthorized
    valid = not problems or (decision == "reject")
    kind = None if valid else ("tampered" if tampered else "unauthorized" if unauthorized else "stale")
    ctx.repo.add_approval(run_id=ctx.run_id, action_id=action.action_id, action_hash=submitted or current,
                          reviewer_id=reviewer, decision=decision if valid else "invalidated", channel=channel,
                          valid=valid, invalidation_reason="; ".join(problems) or None, note=payload.get("note"))
    ad = ApprovalDecision(
        approval_id=f"{ctx.run_id}:{len(ctx.repo.list_approvals(ctx.run_id))}",
        decision=decision if decision in ("approve", "reject", "edit") else "reject",
        reviewer_id=reviewer, channel=channel, action_hash_submitted=submitted, action_hash_current=current,
        valid=valid, invalidation_reason="; ".join(problems) or None, invalidation_kind=kind,
        note=payload.get("note"), edits=payload.get("edits") if decision == "edit" else None,
    )
    if not valid:
        code = ErrorCode.APPROVER_NOT_AUTHORIZED if kind == "unauthorized" else ErrorCode.APPROVAL_INVALIDATED
        ctx.error(code, f"Approval from {reviewer} rejected by the server: {'; '.join(problems)}")
        return {"approval": ad, "outcome_reason": f"Approval invalidated ({kind}): {'; '.join(problems)}"}
    if decision == "approve":
        ctx.transition(S.APPROVED)
        ctx.repo.cas_financial_action(action.action_id, ("PROPOSED",), "APPROVED")
        ctx.audit("approval_recorded", f"Approved by {reviewer} via {channel} for hash {current[:12]}",
                  object_type="financial_action", object_id=action.action_id)
        return {"approval": ad, "outcome_reason": None}
    if decision == "reject":
        ctx.audit("approval_recorded", f"Rejected by {reviewer} via {channel}", object_type="financial_action",
                  object_id=action.action_id)
        return {"approval": ad, "outcome_reason": f"Rejected by {reviewer}" + (f": {ad.note}" if ad.note else "")}
    ctx.audit("approval_recorded", f"Edit requested by {reviewer}: {sorted((ad.edits or {}).keys())}")
    return {"approval": ad, "outcome_reason": None}


_FINANCIAL_FIELDS = ("amount_minor", "currency", "invoice_id", "customer_id", "stripe_line_item_ids", "reason")


@traced("revalidate_action")
def revalidate_action(state: dict, ctx: NodeContext) -> dict:
    """Apply reviewer edits. Any financial change => new hash => policy re-check => new approval."""
    action: ResolutionAction = state["proposed_action"]
    edits = dict(state["approval"].edits or {})
    ignored = sorted(set(edits) - {*_FINANCIAL_FIELDS, "customer_message_draft"})
    fields = {f: getattr(action, f) for f in _FINANCIAL_FIELDS}
    try:
        for f in _FINANCIAL_FIELDS:
            if f in edits:
                v = edits[f]
                fields[f] = int(v) if f == "amount_minor" else [str(x) for x in v] if f == "stripe_line_item_ids" else str(v)
    except (TypeError, ValueError) as exc:
        ctx.error(ErrorCode.INVARIANT_VIOLATION, f"Invalid reviewer edit: {exc}")
        return {"outcome_reason": "Reviewer edit could not be applied; needs human investigation."}
    message = str(edits.get("customer_message_draft") or action.customer_message_draft)
    problems = validate_customer_message(message, amount_minor=fields["amount_minor"], currency=fields["currency"],
                                         invoice_number=action.invoice_number or action.invoice_id)
    if problems and "customer_message_draft" in edits:
        ctx.error(ErrorCode.INVARIANT_VIOLATION, f"Edited customer wording rejected: {'; '.join(problems)}")
        message = action.customer_message_draft
    new = build_credit_action(invoice_number=action.invoice_number, customer_message_draft=message,
                              supersedes_action_hash=action.action_hash, **fields)
    if new.action_hash == action.action_hash:
        new = new.model_copy(update={"supersedes_action_hash": action.supersedes_action_hash})
        ctx.audit("action_wording_edited", "Customer wording edited; financial action unchanged, approval request stays valid"
                  + (f"; ignored fields {ignored}" if ignored else ""))
        return {"proposed_action": new, "outcome_reason": None}
    if problems and "customer_message_draft" not in edits:
        # Amount changed but the auto draft still quotes the old amount: regenerate deterministically.
        ev: EvidenceBundle = state["evidence"]
        line = next((ln for ln in ev.invoice.lines if ln.id in fields["stripe_line_item_ids"]), None)
        new = new.model_copy(update={"customer_message_draft": draft_customer_message(
            first_name=ev.contact.first_name if ev.contact else None, invoice_number=action.invoice_number,
            amount_minor=fields["amount_minor"], currency=fields["currency"],
            line_description=line.description if line else None)})
    ctx.transition(S.POLICY_REVIEW)
    ctx.repo.ensure_financial_action(new, ctx.run_id)
    ctx.repo.cas_financial_action(action.action_id, ("PROPOSED",), "CANCELLED", last_error="superseded by reviewer edit")
    changed = [f for f in _FINANCIAL_FIELDS if fields[f] != getattr(action, f)]
    ctx.audit("approval_invalidated_by_edit", f"Financial fields changed ({', '.join(changed)}): previous approval "
              f"request invalidated; new hash {new.action_hash[:12]} requires policy re-check and re-approval",
              object_type="financial_action", object_id=new.action_id)
    return {"proposed_action": new, "approval": None, "outcome_reason": None}
