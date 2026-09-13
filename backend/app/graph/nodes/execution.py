"""Execution phase: Stripe credit (ledger + idempotency) -> HubSpot -> verify -> repair -> customer email."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from langgraph.types import interrupt

from app.domain.errors import ErrorCode, InvariantViolation
from app.domain.invariants import ApprovalFacts, assert_pre_customer_email, assert_pre_execution
from app.domain.models import CustomerReply, ExecutionResult, ResolutionAction, VerificationCheck, VerificationResult
from app.domain.money import format_money, to_major
from app.domain.states import DisputeStatus as S
from app.domain.timeutil import utcnow
from app.graph.common import NodeContext, traced
from app.integrations.base import IntegrationError
from app.integrations.hubspot import dispute_properties
from app.services.messages import draft_customer_message, validate_customer_message, verified_details_block


def _fm(action: ResolutionAction, minor: int | None) -> str:
    return format_money(minor, action.currency) if minor is not None else "n/a"


@traced("execute_financial_action")
def execute_financial_action(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.EXECUTING)
    action: ResolutionAction = state["proposed_action"]
    repo = ctx.repo

    def result(status: str, **kw) -> ExecutionResult:
        return ExecutionResult(financial_action_id=action.action_id, financial_action_status=status, **kw)

    def fail(message: str, ledger_status: str, err) -> dict:
        repo.cas_financial_action(action.action_id, ("PROPOSED", "APPROVED", "EXECUTING", "UNKNOWN"), ledger_status,
                                  last_error=message[:500])
        ctx.error(err) if not isinstance(err, ErrorCode) else ctx.error(err, message)
        ctx.transition(S.FAILED, reason=message)
        ctx.notify(f"❌ {action.invoice_number}: financial action not completed — {message}")
        return {"execution": result(ledger_status), "outcome_reason": message}

    ledger = repo.get_financial_action_by_hash(action.action_hash)
    if ledger is None:
        return fail("No ledger record for the approved action.", "FAILED", ErrorCode.INVARIANT_VIOLATION)

    # 1) Replay: this exact logical action already produced a credit note. Never write again.
    if ledger["status"] == "SUCCEEDED":
        cn = ctx.call("stripe", "get_credit_note", lambda: ctx.stripe.get_credit_note(ledger["stripe_credit_note_id"]),
                      object_type="credit_note", object_id=ledger["stripe_credit_note_id"])
        ctx.audit("financial_action_replayed", f"Ledger already SUCCEEDED for hash {action.action_hash[:12]}; reusing "
                  f"{cn.id}. No Stripe write.", object_type="credit_note", object_id=cn.id)
        return {"execution": result("SUCCEEDED", stripe_credit_note_id=cn.id, stripe_credit_note_number=cn.number,
                                    stripe_replayed=True)}

    # 2) Fresh reads: invariants are checked against current Stripe state, not the investigation snapshot.
    inv = ctx.call("stripe", "get_invoice", lambda: ctx.stripe.get_invoice(action.invoice_id), object_type="invoice",
                   object_id=action.invoice_id)
    cns = ctx.call("stripe", "list_credit_notes", lambda: ctx.stripe.list_credit_notes(action.invoice_id),
                   object_type="invoice", object_id=action.invoice_id)
    landed = [c for c in cns if c.metadata.get("settle_action_id") == action.action_id and c.status != "void"]
    if landed:  # an earlier attempt (e.g. crash after POST) already committed in Stripe
        cn = landed[0]
        repo.cas_financial_action(action.action_id, ("APPROVED", "EXECUTING", "UNKNOWN", "FAILED"), "SUCCEEDED",
                                  stripe_credit_note_id=cn.id, completed_at=utcnow())
        ctx.audit("financial_action_reconciled", f"Found {cn.id} created by an earlier attempt of this action; no new write.",
                  object_type="credit_note", object_id=cn.id)
        return {"execution": result("SUCCEEDED", stripe_credit_note_id=cn.id, stripe_credit_note_number=cn.number,
                                    stripe_outcome_reconciled=True,
                                    pre_credit_remaining_minor=inv.amount_remaining_minor + cn.amount_minor)}

    row = repo.valid_approval_for(ctx.run_id, action.action_hash)
    facts = ApprovalFacts(decision=row["decision"], action_hash=row["action_hash"], valid=bool(row["valid"])) if row else None
    try:
        checks = assert_pre_execution(action=action, identity=state["identity"], fresh_invoice=inv, fresh_credit_notes=cns,
                                      claim=state["claim"], reconciliation=state["reconciliation"], approval=facts,
                                      policy=ctx.deps.policy)
    except InvariantViolation as exc:
        return fail(f"Blocked before Stripe write: {exc.message}", "FAILED", exc)
    ctx.audit("invariants_passed", f"{len(checks)}/{len(checks)} pre-execution invariants passed on fresh Stripe state",
              metadata={"checks": [name for name, _ in checks]})

    # 3) Application idempotency guard: atomically claim the action.
    if not repo.cas_financial_action(action.action_id, ("APPROVED", "FAILED", "UNKNOWN"), "EXECUTING"):
        current = repo.get_financial_action(action.action_id)
        if current and current["status"] == "EXECUTING":
            # A prior attempt died mid-flight and nothing landed in Stripe (checked above): retry with the SAME key.
            repo.cas_financial_action(action.action_id, ("EXECUTING",), "UNKNOWN")
            repo.cas_financial_action(action.action_id, ("UNKNOWN",), "EXECUTING")
        else:
            return fail(f"Ledger status {current and current['status']} does not permit execution.", "FAILED",
                        ErrorCode.INVARIANT_VIOLATION)

    metadata = {"settle_action_id": action.action_id, "settle_action_hash": action.action_hash, "settle_run_id": ctx.run_id}
    memo = f"Settle: {action.reason}"[:500]
    cn, reconciled, last_exc = None, False, None
    attempts = ctx.settings.retry_max_attempts
    for attempt in range(1, attempts + 1):
        try:
            cn = ctx.call("stripe", "create_credit_note", lambda: ctx.stripe.create_credit_note(
                invoice_id=action.invoice_id, line_item_id=action.stripe_line_item_ids[0], amount_minor=action.amount_minor,
                reason=action.reason_code, memo=memo, metadata=metadata, idempotency_key=action.idempotency_key,
            ), object_type="financial_action", object_id=action.action_id, retry=False, write=True)
            break
        except IntegrationError as exc:
            last_exc = exc
            if not exc.retryable:
                break
            if exc.outcome_unknown:
                repo.cas_financial_action(action.action_id, ("EXECUTING",), "UNKNOWN", last_error=exc.message)
                ctx.audit("financial_action_unknown", "Stripe outcome unknown after the request was sent; inspecting Stripe "
                          "by action identity before any retry", object_type="financial_action", object_id=action.action_id)
                found = [c for c in ctx.call("stripe", "list_credit_notes",
                                             lambda: ctx.stripe.list_credit_notes(action.invoice_id))
                         if c.metadata.get("settle_action_id") == action.action_id and c.status != "void"]
                if found:
                    cn, reconciled = found[0], True
                    ctx.audit("financial_action_reconciled", f"Timed-out request had committed: {cn.id}. No retry needed.",
                              object_type="credit_note", object_id=cn.id)
                    break
                repo.cas_financial_action(action.action_id, ("UNKNOWN",), "EXECUTING")
            if attempt < attempts:
                ctx.deps.sleep(ctx.settings.retry_base_delay_s * 2 ** (attempt - 1))
    if cn is None:
        ledger_status = "UNKNOWN" if last_exc is not None and last_exc.retryable else "FAILED"
        return fail(f"Stripe credit note not created ({last_exc.message if last_exc else 'unknown'}); ledger {ledger_status}. "
                    "Any retry reuses the same idempotency key.", ledger_status, last_exc or ErrorCode.STRIPE_WRITE_ERROR)

    repo.cas_financial_action(action.action_id, ("EXECUTING", "UNKNOWN"), "SUCCEEDED", stripe_credit_note_id=cn.id,
                              completed_at=utcnow())
    ctx.audit("credit_note_created", f"Stripe credit note {cn.id} ({cn.number}) for {_fm(action, cn.amount_minor)} on "
              f"{action.invoice_number}; ledger SUCCEEDED", external_system="stripe", object_type="credit_note", object_id=cn.id)
    ctx.transition(S.EXECUTING, credited_amount_minor=cn.amount_minor)
    return {"execution": result("SUCCEEDED", stripe_credit_note_id=cn.id, stripe_credit_note_number=cn.number,
                                pre_credit_remaining_minor=inv.amount_remaining_minor, stripe_outcome_reconciled=reconciled)}


def _dispute_props(state: dict, ctx: NodeContext) -> dict[str, str]:
    action: ResolutionAction = state["proposed_action"]
    return dispute_properties(status="RESOLVED", amount_minor=action.amount_minor, currency=action.currency,
                              credit_note_id=state["execution"].stripe_credit_note_id, run_id=ctx.run_id,
                              action_id=action.action_id, resolution_type="CREDIT_NOTE",
                              resolved_at=utcnow().isoformat(timespec="seconds"))


@traced("update_crm")
def update_crm(state: dict, ctx: NodeContext) -> dict:
    execn: ExecutionResult = state["execution"]
    company_id = state["identity"].hubspot_company_id
    props = _dispute_props(state, ctx)
    try:
        ctx.call("hubspot", "update_dispute", lambda: ctx.hubspot.update_dispute(company_id, props), object_type="company",
                 object_id=company_id, write=True)
    except IntegrationError as exc:
        ctx.error(exc)
        ctx.transition(S.PARTIALLY_COMPLETED)
        ctx.notify(f"⚠️ {state['evidence'].company.name}: Stripe credit {execn.stripe_credit_note_id} ✓, HubSpot update "
                   f"failed ({exc.message}). PARTIALLY_COMPLETED — repairing HubSpot only; no second credit will be issued.")
        return {"execution": execn.model_copy(update={"hubspot_updated": False, "hubspot_attempts": ctx.last_attempts,
                                                      "hubspot_error": exc.message})}
    ctx.audit("crm_updated", f"HubSpot company {company_id}: dispute_status=RESOLVED, "
              f"stripe_credit_note_id={execn.stripe_credit_note_id}", external_system="hubspot", object_type="company",
              object_id=company_id)
    return {"execution": execn.model_copy(update={"hubspot_updated": True, "hubspot_attempts": ctx.last_attempts,
                                                  "hubspot_error": None})}


@traced("repair_or_escalate")
def repair_or_escalate(state: dict, ctx: NodeContext) -> dict:
    """Stripe success is durable truth: repair the remaining systems, never repeat the financial mutation."""
    ctx.transition(S.REPAIRING)
    cycles = int(state.get("repair_cycles") or 0) + 1
    execn: ExecutionResult = state["execution"]
    action: ResolutionAction = state["proposed_action"]
    ledger = ctx.repo.get_financial_action_by_hash(action.action_hash)
    if not ledger or ledger["status"] != "SUCCEEDED" or ledger["stripe_credit_note_id"] != execn.stripe_credit_note_id:
        msg = "Repair requires a SUCCEEDED ledger entry matching the Stripe credit note."
        ctx.error(ErrorCode.INVARIANT_VIOLATION, msg)
        ctx.transition(S.ESCALATED, reason=msg)
        return {"repair_cycles": cycles, "outcome_reason": msg,
                "execution": execn.model_copy(update={"hubspot_updated": False, "repair_cycles": cycles})}
    ctx.audit("repair_started", f"Repair cycle {cycles}: credit {execn.stripe_credit_note_id} is durable (ledger SUCCEEDED); "
              "re-applying HubSpot only — Stripe is not touched")
    company_id = state["identity"].hubspot_company_id
    props = _dispute_props(state, ctx)
    try:
        ctx.call("hubspot", "update_dispute", lambda: ctx.hubspot.update_dispute(company_id, props), object_type="company",
                 object_id=company_id, write=True)
    except IntegrationError as exc:
        ctx.error(exc)
        msg = (f"REPAIR_REQUIRED: HubSpot still unavailable ({exc.message}). Stripe credit {execn.stripe_credit_note_id} "
               "stands; customer email withheld until verification passes.")
        ctx.transition(S.ESCALATED, reason=msg)
        return {"repair_cycles": cycles, "outcome_reason": msg,
                "execution": execn.model_copy(update={"hubspot_updated": False, "hubspot_error": exc.message,
                                                      "repair_cycles": cycles})}
    ctx.audit("repair_applied", f"HubSpot repaired for {execn.stripe_credit_note_id}", external_system="hubspot",
              object_type="company", object_id=company_id)
    return {"repair_cycles": cycles, "outcome_reason": None,
            "execution": execn.model_copy(update={"hubspot_updated": True, "hubspot_error": None, "repair_cycles": cycles})}


@traced("escalate_for_repair")
def escalate_for_repair(state: dict, ctx: NodeContext) -> dict:
    reason = state.get("outcome_reason") or "Verification did not pass after repair; operator action required."
    ctx.transition(S.ESCALATED, reason=reason)
    ctx.notify(f"🚨 REPAIR REQUIRED — {state['evidence'].company.name} {state['proposed_action'].invoice_number}: {reason}")
    return {"outcome_reason": reason}


@traced("await_operator")
def await_operator(state: dict, ctx: NodeContext) -> dict:
    cmd = interrupt({"kind": "repair_required", "run_id": ctx.run_id, "reason": state.get("outcome_reason"),
                     "stripe_credit_note_id": state["execution"].stripe_credit_note_id})
    cmd = cmd if isinstance(cmd, dict) else {}
    operator = str(cmd.get("operator_id") or "unknown")
    if cmd.get("decision") == "retry_repair":
        ctx.audit("operator_retry", f"Operator {operator} requested a repair retry")
        return {"repair_cycles": 0, "outcome_reason": None}
    note = cmd.get("note") or "closed without repair"
    ctx.audit("operator_closed", f"Operator {operator} closed the escalation: {note}")
    return {"outcome_reason": f"Escalation closed by {operator}: {note}"}


@traced("verify_state")
def verify_state(state: dict, ctx: NodeContext) -> dict:
    """Independent re-reads. A 200 from a write is not verification."""
    ctx.transition(S.VERIFYING)
    action: ResolutionAction = state["proposed_action"]
    execn: ExecutionResult = state["execution"]
    company_id = state["identity"].hubspot_company_id
    checks: list[VerificationCheck] = []

    def chk(system, name, passed, expected, actual):
        checks.append(VerificationCheck(system=system, check=name, passed=bool(passed), expected=str(expected),
                                        actual=str(actual)))

    remaining = None
    try:
        cn = ctx.call("stripe", "get_credit_note", lambda: ctx.stripe.get_credit_note(execn.stripe_credit_note_id),
                      object_type="credit_note", object_id=execn.stripe_credit_note_id)
        inv = ctx.call("stripe", "get_invoice", lambda: ctx.stripe.get_invoice(action.invoice_id), object_type="invoice",
                       object_id=action.invoice_id)
        cns = ctx.call("stripe", "list_credit_notes", lambda: ctx.stripe.list_credit_notes(action.invoice_id),
                       object_type="invoice", object_id=action.invoice_id)
        remaining = inv.amount_remaining_minor
        chk("stripe", "credit_note_exists", cn.id == execn.stripe_credit_note_id and cn.status != "void",
            f"{execn.stripe_credit_note_id} issued", f"{cn.id} {cn.status}")
        chk("stripe", "amount_matches", cn.amount_minor == action.amount_minor, _fm(action, action.amount_minor),
            _fm(action, cn.amount_minor))
        chk("stripe", "currency_matches", cn.currency == action.currency, action.currency.upper(), cn.currency.upper())
        chk("stripe", "linked_to_invoice", cn.invoice_id == action.invoice_id, action.invoice_id, cn.invoice_id)
        chk("stripe", "credits_intended_line", set(cn.line_item_ids) == set(action.stripe_line_item_ids),
            ",".join(action.stripe_line_item_ids), ",".join(cn.line_item_ids))
        equivalent = [c for c in cns if c.status != "void" and (
            c.metadata.get("settle_action_hash") == action.action_hash
            or set(c.line_item_ids) & set(action.stripe_line_item_ids))]
        chk("stripe", "no_duplicate_credit", len(equivalent) == 1, "exactly 1", len(equivalent))
        if execn.pre_credit_remaining_minor is not None:
            expected = execn.pre_credit_remaining_minor - action.amount_minor
            chk("stripe", "invoice_remaining_amount", remaining == expected, _fm(action, expected), _fm(action, remaining))
    except IntegrationError as exc:
        ctx.error(exc)
        chk("stripe", "stripe_readable", False, "readable", exc.message)
    try:
        company = ctx.call("hubspot", "get_company", lambda: ctx.hubspot.get_company(company_id), object_type="company",
                           object_id=company_id)
        p = company.properties
        chk("hubspot", "dispute_status_resolved", p.get("dispute_status") == "RESOLVED", "RESOLVED", p.get("dispute_status"))
        expected_major = to_major(action.amount_minor, action.currency)
        try:
            actual_major = Decimal(p.get("dispute_amount") or "NaN")
        except InvalidOperation:
            actual_major = Decimal("NaN")
        chk("hubspot", "credited_amount_matches", actual_major == expected_major, expected_major, p.get("dispute_amount"))
        chk("hubspot", "credit_note_id_stored", p.get("stripe_credit_note_id") == execn.stripe_credit_note_id,
            execn.stripe_credit_note_id, p.get("stripe_credit_note_id"))
    except IntegrationError as exc:
        ctx.error(exc)
        chk("hubspot", "hubspot_readable", False, "readable", exc.message)

    stripe_ok = all(c.passed for c in checks if c.system == "stripe")
    hubspot_ok = all(c.passed for c in checks if c.system == "hubspot")
    status = "VERIFIED" if stripe_ok and hubspot_ok else "MISMATCH"
    ctx.transition(S.VERIFYING, verification_status=status)
    failed = [c for c in checks if not c.passed]
    ctx.audit("verification_completed", f"{status}: {len(checks) - len(failed)}/{len(checks)} independent checks passed",
              metadata={"checks": [c.model_dump() for c in checks]})
    reason = None
    if failed:
        detail = "; ".join(f"{c.system}.{c.check}: expected {c.expected}, got {c.actual}" for c in failed)
        ctx.error(ErrorCode.VERIFICATION_FAILED, detail)
        reason = f"Verification failed — customer email withheld. {detail}"
    return {"verification": VerificationResult(status=status, stripe_ok=stripe_ok, hubspot_ok=hubspot_ok, checks=checks,
                                               verified_invoice_remaining_minor=remaining),
            "outcome_reason": reason}


@traced("send_customer_reply")
def send_customer_reply(state: dict, ctx: NodeContext) -> dict:
    verification, execn = state.get("verification"), state.get("execution")
    action: ResolutionAction = state["proposed_action"]
    ev = state["evidence"]
    try:
        assert_pre_customer_email(verification, execn)
    except InvariantViolation as exc:
        ctx.error(exc)
        ctx.transition(S.ESCALATED, reason=exc.message)
        return {"outcome_reason": exc.message}

    email = ev.source_email
    draft = action.customer_message_draft
    problems = validate_customer_message(draft, amount_minor=action.amount_minor, currency=action.currency,
                                         invoice_number=action.invoice_number or action.invoice_id)
    if problems:
        ctx.error(ErrorCode.CUSTOMER_EMAIL_BLOCKED, f"Draft failed validation ({'; '.join(problems)}); using the template.")
        line = next((ln for ln in ev.invoice.lines if ln.id in action.stripe_line_item_ids), None)
        draft = draft_customer_message(first_name=ev.contact.first_name if ev.contact else None,
                                       invoice_number=action.invoice_number, amount_minor=action.amount_minor,
                                       currency=action.currency, line_description=line.description if line else None)
    body = draft + "\n\n" + verified_details_block(
        invoice_number=action.invoice_number, credit_note_id=execn.stripe_credit_note_id,
        credit_note_number=execn.stripe_credit_note_number, amount_minor=action.amount_minor, currency=action.currency,
        remaining_minor=verification.verified_invoice_remaining_minor)
    subject = email.subject if email.subject.lower().startswith("re:") else f"Re: {email.subject}"
    outbox = ctx.repo.get_or_create_outbox(run_id=ctx.run_id, action_id=action.action_id, to_email=email.from_email,
                                           subject=subject, body=body)
    if outbox["status"] == "SENT":
        gmail_id = outbox["gmail_message_id"]
        ctx.audit("customer_reply_replayed", f"Resolution email for this action was already sent ({gmail_id}); not resending")
    elif outbox["status"] == "SENDING":
        msg = "A previous send attempt has an unknown outcome; not resending blindly."
        ctx.error(ErrorCode.CUSTOMER_EMAIL_BLOCKED, msg)
        ctx.transition(S.ESCALATED, reason=msg)
        return {"outcome_reason": msg}
    else:
        ctx.repo.mark_outbox(outbox["outbox_id"], status="SENDING")
        try:
            gmail_id = ctx.call("gmail", "send_reply", lambda: ctx.gmail.send_reply(
                thread_id=email.thread_id, to_email=email.from_email, subject=subject, body=outbox["body"],
                in_reply_to=email.message_id_header), object_type="thread", object_id=email.thread_id, retry=False,
                write=True)
        except IntegrationError as exc:
            if not exc.outcome_unknown:
                ctx.repo.mark_outbox(outbox["outbox_id"], status="FAILED")
            ctx.error(exc)
            msg = f"State verified, but the customer email failed: {exc.message}"
            ctx.transition(S.ESCALATED, reason=msg)
            ctx.notify(f"⚠️ {ev.company.name}: {msg}")
            return {"outcome_reason": msg}
        ctx.repo.mark_outbox(outbox["outbox_id"], status="SENT", gmail_message_id=gmail_id, sent_at=utcnow())
        ctx.audit("customer_reply_sent", f"Resolution email sent to {email.from_email} after verification",
                  external_system="gmail", object_type="message", object_id=gmail_id)
    reason = (f"Credited {_fm(action, action.amount_minor)} on {action.invoice_number} "
              f"({execn.stripe_credit_note_id}); Stripe + HubSpot verified; customer notified.")
    ctx.transition(S.RESOLVED, reason=reason, resolved_at=utcnow())
    ctx.notify(f"✅ {ev.company.name}: {reason}")
    return {"customer_reply": CustomerReply(outbox_id=outbox["outbox_id"], gmail_message_id=gmail_id,
                                            to_email=email.from_email, subject=subject, body=outbox["body"]),
            "outcome_reason": reason}
