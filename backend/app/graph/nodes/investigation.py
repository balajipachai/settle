"""Investigation phase: intake -> extract -> resolve -> gather -> reconcile -> adjudicate.

Read-only. No node in this module can reach a write tool.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.domain.errors import ErrorCode, SettleError
from app.domain.models import (
    Adjudication,
    AdjudicationDecision,
    Claim,
    ClaimExtraction,
    CompanySnapshot,
    ContactSnapshot,
    CreditNoteSnapshot,
    CustomerSnapshot,
    DealSnapshot,
    EmailMessage,
    EvidenceBundle,
    EvidenceReference,
    FinancialReconciliation,
    IdentityResolution,
    InvoiceSnapshot,
)
from app.domain.money import find_money, format_money, parse_money
from app.domain.reconciliation import reconcile
from app.domain.states import DisputeStatus as S
from app.graph.common import NodeContext, traced
from app.integrations.base import IntegrationError
from app.services.injection import scan_for_injection
from app.services.reasoner import AdjudicationPacket, ContextItem


_INVOICE_REF = re.compile(r"\b[A-Z]{2,5}-\d{3,}\b")


def ref_id(source: str, object_type: str, object_id: str) -> str:
    return f"{source}:{object_type}:{object_id}"


# --------------------------------------------------------------------------- intake


@traced("intake_email")
def intake_email(state: dict, ctx: NodeContext) -> dict:
    mid = state["source_email_id"]
    email = ctx.call("gmail", "get_message", lambda: ctx.gmail.get_message(mid), object_type="message", object_id=mid)
    ctx.audit("email_ingested", f"Ingested email from {email.from_email}: {email.subject[:80]}", external_system="gmail",
              object_type="message", object_id=email.id,
              metadata={"thread_id": email.thread_id, "received_at": email.received_at.isoformat()})
    return {"source_email": email, "source_thread_id": email.thread_id}


# --------------------------------------------------------------------------- extraction


def guard_claim(ext: ClaimExtraction, email: EmailMessage, extracted_by: str) -> Claim:
    """Deterministic post-checks on LLM output: nothing that is not verbatim in the email survives."""
    text = f"{email.subject}\n{email.body}"
    notes: list[str] = []
    hint = (ext.invoice_hint or "").strip() or None
    if hint and hint.lower() not in text.lower():
        notes.append(f"Dropped invoice hint '{hint}': not present verbatim in the email.")
        hint = None
    amount = currency = None
    if ext.disputed_amount_text:
        parsed = parse_money(ext.disputed_amount_text)
        mentioned = {(m.minor, m.currency) for m in find_money(text)}
        if parsed and (parsed.minor, parsed.currency) in mentioned:
            amount, currency = parsed.minor, parsed.currency
        else:
            notes.append(f"Dropped amount '{ext.disputed_amount_text}': not an amount stated in the email.")
    if currency is None and ext.currency_code and re.fullmatch(r"[A-Za-z]{3}", ext.currency_code.strip()):
        currency = ext.currency_code.strip().lower()
    phrases = [p for p in ext.evidence_phrases if p and p.strip() and p.strip() in text][:5]
    mentions = sorted({m.group(0).upper() for m in _INVOICE_REF.finditer(text)})
    return Claim(raw_text=email.body, dispute_type=ext.dispute_type, disputed_amount_minor=amount, currency=currency,
                 invoice_hint=hint, invoice_mentions=mentions, summary=ext.summary[:300], evidence_phrases=phrases,
                 extraction_notes=notes, extracted_by=extracted_by)


@traced("extract_claim")
def extract_claim(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.EXTRACTING)
    email: EmailMessage = state["source_email"]
    try:
        extraction = ctx.deps.reasoner.extract_claim(email)
    except SettleError as exc:
        ctx.error(exc)
        return {"outcome_reason": f"Claim extraction failed ({exc.message}); needs human review."}
    claim = guard_claim(extraction, email, ctx.deps.reasoner.name)
    signals = scan_for_injection(f"{email.subject}\n{email.body}")
    if signals:
        claim = claim.model_copy(update={"injection_signals": signals})
        ctx.error(ErrorCode.PROMPT_INJECTION_DETECTED,
                  f"Untrusted email contains instruction-like content ({', '.join(signals)}); treated as data only.",
                  system="gmail")
    amount = format_money(claim.disputed_amount_minor, claim.currency or "usd") if claim.disputed_amount_minor else "n/a"
    ctx.audit("claim_extracted", f"{claim.dispute_type}, amount {amount}, invoice hint {claim.invoice_hint or 'none'}",
              metadata={"extracted_by": claim.extracted_by, "notes": claim.extraction_notes, "injection": signals})
    ctx.transition(S.EXTRACTING, disputed_amount_minor=claim.disputed_amount_minor, currency=claim.currency)
    return {"claim": claim}


# --------------------------------------------------------------------------- identity


def _norm_ref(v: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def resolve_identity(ctx: NodeContext, email: EmailMessage, claim: Claim) -> IdentityResolution:
    """sender email -> HubSpot contact -> company -> Stripe customer (cross-checked) -> invoice. Never guesses."""

    def res(status, reason, code=None, **kw):
        return IdentityResolution(status=status, reason=reason, error_code=code, **kw)

    contacts = ctx.call("hubspot", "find_contacts_by_email", lambda: ctx.hubspot.find_contacts_by_email(email.from_email),
                        object_type="contact", object_id=email.from_email)
    if len(contacts) > 1:
        return res("AMBIGUOUS", f"{len(contacts)} HubSpot contacts share {email.from_email}.", ErrorCode.IDENTITY_AMBIGUOUS,
                   candidate_ids=[c.id for c in contacts])
    if not contacts:
        domain = email.from_email.rsplit("@", 1)[-1]
        companies = ctx.call("hubspot", "search_companies_by_domain",
                             lambda: ctx.hubspot.search_companies_by_domain(domain), object_type="company", object_id=domain)
        if companies:
            return res("AMBIGUOUS", f"No HubSpot contact matches {email.from_email} exactly; {len(companies)} "
                       f"compan{'y' if len(companies) == 1 else 'ies'} share the domain. Not guessing.",
                       ErrorCode.IDENTITY_AMBIGUOUS, candidate_ids=[c.id for c in companies])
        return res("NOT_FOUND", f"No HubSpot contact or company matches {email.from_email}.", ErrorCode.IDENTITY_NOT_FOUND)
    contact = contacts[0]
    if len(contact.company_ids) != 1:
        status = "AMBIGUOUS" if contact.company_ids else "NOT_FOUND"
        return res(status, f"Contact {contact.id} is associated with {len(contact.company_ids)} companies.",
                   ErrorCode.IDENTITY_AMBIGUOUS if contact.company_ids else ErrorCode.IDENTITY_NOT_FOUND,
                   contact_id=contact.id, candidate_ids=contact.company_ids)
    company = ctx.call("hubspot", "get_company", lambda: ctx.hubspot.get_company(contact.company_ids[0]),
                       object_type="company", object_id=contact.company_ids[0])
    base = {"contact_id": contact.id, "hubspot_company_id": company.id, "company_name": company.name}
    cus_id = company.properties.get("stripe_customer_id")
    if not cus_id:
        return res("NOT_FOUND", f"HubSpot company {company.name} has no stripe_customer_id.", ErrorCode.IDENTITY_NOT_FOUND, **base)
    try:
        customer = ctx.call("stripe", "get_customer", lambda: ctx.stripe.get_customer(cus_id), object_type="customer",
                            object_id=cus_id)
    except IntegrationError as exc:
        if exc.status_code == 404:
            return res("NOT_FOUND", f"Stripe customer {cus_id} not found.", ErrorCode.IDENTITY_NOT_FOUND, **base)
        raise
    back_ref = customer.metadata.get("hubspot_company_id")
    if back_ref and back_ref != company.id:
        return res("CONFLICT", f"Stripe customer {cus_id} references HubSpot company {back_ref}, but the sender belongs "
                   f"to company {company.id}. Systems disagree.", ErrorCode.EVIDENCE_CONFLICT,
                   customer_id=cus_id, candidate_ids=[company.id, back_ref], **{k: v for k, v in base.items()})
    invoices = ctx.call("stripe", "list_customer_invoices", lambda: ctx.stripe.list_customer_invoices(cus_id),
                        object_type="customer", object_id=cus_id)
    base["customer_id"] = cus_id
    if len({_norm_ref(m) for m in claim.invoice_mentions}) > 1:
        return res("AMBIGUOUS", f"The email references {len(claim.invoice_mentions)} different invoices "
                   f"({', '.join(claim.invoice_mentions)}); not guessing which one is disputed.",
                   ErrorCode.INVOICE_AMBIGUOUS, candidate_ids=claim.invoice_mentions, **base)
    if claim.invoice_hint:
        hint = _norm_ref(claim.invoice_hint)
        matches = [i for i in invoices if hint in {_norm_ref(i.number), _norm_ref(i.metadata.get("settle_invoice_ref")),
                                                   _norm_ref(i.id)}]
        if not matches:
            return res("NOT_FOUND", f"Invoice {claim.invoice_hint} does not exist for {company.name}.",
                       ErrorCode.INVOICE_NOT_FOUND, candidate_ids=[i.number or i.id for i in invoices], **base)
        if len(matches) > 1:
            return res("AMBIGUOUS", f"{len(matches)} invoices match {claim.invoice_hint}.", ErrorCode.INVOICE_AMBIGUOUS,
                       candidate_ids=[i.id for i in matches], **base)
        inv, how = matches[0], f"invoice hint {claim.invoice_hint}"
    else:
        open_invoices = [i for i in invoices if i.status == "open"]
        if len(open_invoices) != 1:
            status = "AMBIGUOUS" if open_invoices else "NOT_FOUND"
            return res(status, f"No invoice number given and {len(open_invoices)} open invoices exist for {company.name}.",
                       ErrorCode.INVOICE_AMBIGUOUS if open_invoices else ErrorCode.INVOICE_NOT_FOUND,
                       candidate_ids=[i.id for i in open_invoices], **base)
        inv, how = open_invoices[0], "the customer's only open invoice"
    return res("RESOLVED", f"Exact sender match → contact {contact.id} → {company.name} → Stripe {cus_id} "
               f"(back-reference verified) → {inv.number or inv.id} via {how}.",
               invoice_id=inv.id, invoice_number=inv.number or inv.id, **base)


@traced("resolve_entities")
def resolve_entities(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.RESOLVING_IDENTITY)
    ident = resolve_identity(ctx, state["source_email"], state["claim"])
    ctx.audit("identity_resolved", f"{ident.status}: {ident.reason}", metadata=ident.model_dump(mode="json"))
    if ident.status != "RESOLVED":
        ctx.error(ident.error_code or ErrorCode.IDENTITY_NOT_FOUND, ident.reason)
        return {"identity": ident, "outcome_reason": ident.reason}
    ctx.transition(S.RESOLVING_IDENTITY, customer_name=ident.company_name, invoice_number=ident.invoice_number)
    return {"identity": ident}


# --------------------------------------------------------------------------- evidence


def _day(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d") if ts else "?"


def build_references(
    thread: list[EmailMessage], invoice: InvoiceSnapshot, credit_notes: list[CreditNoteSnapshot],
    customer: CustomerSnapshot, contact: ContactSnapshot | None, company: CompanySnapshot, deals: list[DealSnapshot],
) -> list[EvidenceReference]:
    cur = invoice.currency
    refs = [
        EvidenceReference(ref_id=ref_id("gmail", "message", m.id), source="gmail", object_type="message", object_id=m.id,
                          value_summary=f"{m.from_email} on {m.received_at:%Y-%m-%d}: “{m.subject}” — "
                                        f"{' '.join(m.body.split())[:180]}", trusted=False)
        for m in thread
    ]
    refs.append(EvidenceReference(
        ref_id=ref_id("stripe", "invoice", invoice.id), source="stripe", object_type="invoice", object_id=invoice.id,
        field="amount_remaining", trusted=True,
        value_summary=f"{invoice.number} {invoice.status}, total {format_money(invoice.total_minor, cur)}, "
                      f"remaining {format_money(invoice.amount_remaining_minor, cur)}"))
    refs += [
        EvidenceReference(ref_id=ref_id("stripe", "line", ln.id), source="stripe", object_type="invoice_line",
                          object_id=ln.id, field="amount", trusted=True,
                          value_summary=f"{ln.description} · {format_money(ln.amount_minor, ln.currency)} · price "
                                        f"{ln.price_id or 'n/a'} · period {_day(ln.period_start)}→{_day(ln.period_end)}")
        for ln in invoice.lines
    ]
    refs += [
        EvidenceReference(ref_id=ref_id("stripe", "credit_note", cn.id), source="stripe", object_type="credit_note",
                          object_id=cn.id, trusted=True,
                          value_summary=f"{cn.number or cn.id} {cn.status} {format_money(cn.amount_minor, cn.currency)} "
                                        f"reason={cn.reason} lines={','.join(cn.line_item_ids) or 'n/a'}")
        for cn in credit_notes
    ]
    refs.append(EvidenceReference(
        ref_id=ref_id("stripe", "customer", customer.id), source="stripe", object_type="customer", object_id=customer.id,
        trusted=True, value_summary=f"{customer.name} · HubSpot ref {customer.metadata.get('hubspot_company_id', 'n/a')}"))
    if contact:
        refs.append(EvidenceReference(
            ref_id=ref_id("hubspot", "contact", contact.id), source="hubspot", object_type="contact", object_id=contact.id,
            trusted=True, value_summary=f"{contact.first_name or ''} {contact.last_name or ''} <{contact.email}>".strip()))
    p = company.properties
    refs.append(EvidenceReference(
        ref_id=ref_id("hubspot", "company", company.id), source="hubspot", object_type="company", object_id=company.id,
        trusted=True, value_summary=f"{company.name} · tier {p.get('account_tier') or 'n/a'} · owner "
                                    f"{p.get('account_owner') or 'n/a'} · renewal {p.get('renewal_date') or 'n/a'}"))
    refs += [
        EvidenceReference(ref_id=ref_id("hubspot", "deal", d.id), source="hubspot", object_type="deal", object_id=d.id,
                          trusted=True, value_summary=f"{d.name} · {d.stage} · {d.amount}")
        for d in deals
    ]
    return refs


@traced("retrieve_evidence")
def retrieve_evidence(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.GATHERING_EVIDENCE)
    ident: IdentityResolution = state["identity"]
    email: EmailMessage = state["source_email"]
    thread = ctx.call("gmail", "get_thread", lambda: ctx.gmail.get_thread(email.thread_id), object_type="thread",
                      object_id=email.thread_id)
    invoice = ctx.call("stripe", "get_invoice", lambda: ctx.stripe.get_invoice(ident.invoice_id), object_type="invoice",
                       object_id=ident.invoice_id)
    credit_notes = ctx.call("stripe", "list_credit_notes", lambda: ctx.stripe.list_credit_notes(ident.invoice_id),
                            object_type="invoice", object_id=ident.invoice_id)
    customer = ctx.call("stripe", "get_customer", lambda: ctx.stripe.get_customer(ident.customer_id),
                        object_type="customer", object_id=ident.customer_id)
    company = ctx.call("hubspot", "get_company", lambda: ctx.hubspot.get_company(ident.hubspot_company_id),
                       object_type="company", object_id=ident.hubspot_company_id)
    deals = ctx.call("hubspot", "get_company_deals", lambda: ctx.hubspot.get_company_deals(company.id),
                     object_type="company", object_id=company.id)
    contacts = ctx.call("hubspot", "find_contacts_by_email", lambda: ctx.hubspot.find_contacts_by_email(email.from_email),
                        object_type="contact", object_id=email.from_email)
    contact = next((c for c in contacts if c.id == ident.contact_id), None)
    refs = build_references(thread or [email], invoice, credit_notes, customer, contact, company, deals)
    bundle = EvidenceBundle(source_email=email, thread_messages=thread or [email], customer=customer, invoice=invoice,
                            credit_notes=credit_notes, contact=contact, company=company, deals=deals, references=refs)
    by_source = {s: sum(1 for r in refs if r.source == s) for s in ("gmail", "stripe", "hubspot")}
    ctx.audit("evidence_gathered", f"{len(refs)} evidence items (Gmail {by_source['gmail']}, Stripe {by_source['stripe']}, "
              f"HubSpot {by_source['hubspot']})", metadata={"ref_ids": [r.ref_id for r in refs]})
    return {"evidence": bundle}


# --------------------------------------------------------------------------- reconciliation


@traced("reconcile_financials")
def reconcile_financials(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.RECONCILING)
    ev: EvidenceBundle = state["evidence"]
    recon = reconcile(state["claim"], ev.invoice, ev.credit_notes)
    amount = format_money(recon.credit_amount_minor, recon.currency) if recon.credit_amount_minor else "none"
    ctx.audit("financials_reconciled", f"{recon.outcome}; deterministic credit amount {amount}"
              + (f"; {' '.join(recon.notes)}" if recon.notes else ""), metadata=recon.model_dump(mode="json"))
    return {"reconciliation": recon}


# --------------------------------------------------------------------------- adjudication


def context_items(ev: EvidenceBundle) -> list[ContextItem]:
    items = [ContextItem(ref_id=ref_id("gmail", "message", m.id), trusted=False, text=m.body)
             for m in ev.thread_messages if m.id != ev.source_email.id]
    desc = ev.company.properties.get("description")
    if desc:
        items.append(ContextItem(ref_id=ref_id("hubspot", "company", ev.company.id), trusted=True, text=f"CRM notes: {desc}"))
    return items


def guard_adjudication(d: AdjudicationDecision, ev: EvidenceBundle, recon: FinancialReconciliation,
                       reasoner: str) -> Adjudication:
    known = {r.ref_id: r for r in ev.references}
    valid = [known[i] for i in d.evidence_ref_ids if i in known]
    invalid = [i for i in d.evidence_ref_ids if i not in known]
    notes: list[str] = []
    resolution = d.resolution
    if invalid:
        notes.append(f"Ignored evidence ids that do not exist: {', '.join(invalid)}.")
    if resolution == "CREDIT" and not valid:
        resolution = "HUMAN_INVESTIGATION"
        notes.append("CREDIT without any valid evidence reference → human investigation.")
    if resolution == "CREDIT" and recon.outcome != "DUPLICATE_CANDIDATE":
        resolution = "HUMAN_INVESTIGATION"
        notes.append(f"CREDIT requires a deterministic duplicate candidate; reconciliation was {recon.outcome}.")
    if resolution == "CREDIT" and recon.already_credited:
        resolution = "NO_ACTION"
        notes.append("An equivalent credit already exists → NO_ACTION.")
    return Adjudication(classification=d.classification, validity=d.validity, resolution=resolution,
                        model_confidence=min(max(float(d.model_confidence), 0.0), 1.0), rationale=d.rationale[:1500],
                        evidence_refs=valid, invalid_ref_ids=invalid, guard_notes=notes, reasoner=reasoner)


@traced("adjudicate_dispute")
def adjudicate_dispute(state: dict, ctx: NodeContext) -> dict:
    ctx.transition(S.ADJUDICATING)
    ev: EvidenceBundle = state["evidence"]
    claim: Claim = state["claim"]
    recon: FinancialReconciliation = state["reconciliation"]
    packet = AdjudicationPacket(claim=claim, reconciliation=recon, references=ev.references, source_email=ev.source_email,
                                source_email_ref=ref_id("gmail", "message", ev.source_email.id), context=context_items(ev))
    reasoner = ctx.deps.reasoner
    try:
        adj = guard_adjudication(reasoner.adjudicate(packet), ev, recon, reasoner.name)
    except SettleError as exc:
        ctx.error(exc)
        adj = Adjudication(classification=claim.dispute_type, validity="INCONCLUSIVE", resolution="HUMAN_INVESTIGATION",
                           model_confidence=0.0, rationale=f"Adjudication unavailable: {exc.message}", reasoner=reasoner.name)
    ctx.audit("dispute_adjudicated", f"{adj.validity} {adj.classification} → {adj.resolution} "
              f"(confidence {adj.model_confidence:.2f}, heuristic)",
              metadata={"evidence_refs": [r.ref_id for r in adj.evidence_refs], "guard_notes": adj.guard_notes,
                        "reasoner": adj.reasoner})
    reason = None
    if adj.resolution != "CREDIT":
        reason = f"{adj.validity}/{adj.resolution}: {adj.rationale}"
    return {"adjudication": adj, "outcome_reason": reason}
