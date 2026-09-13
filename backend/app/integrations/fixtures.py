"""Explicit fixture adapters: a stateful, in-memory world that mirrors Stripe/HubSpot/
Gmail/Slack semantics closely enough to exercise the real control plane.

Used for deterministic tests, the eval suite, and the offline demo. Supports
failure injection and records every call (with call-time guard results) so the
evaluators can check ordering invariants from outside the graph.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from app.domain.errors import ErrorCode
from app.domain.models import (
    ApprovalCard,
    CompanySnapshot,
    ContactSnapshot,
    CreditNoteSnapshot,
    CustomerSnapshot,
    DealSnapshot,
    EmailMessage,
    InvoiceSnapshot,
)
from app.domain.money import format_money
from app.integrations.base import IntegrationError, TransientError, code_for, error_from_status
from app.integrations.hubspot import COMPANY_PROPERTIES
from app.integrations.stripe import credit_note_from_api, credit_note_params, customer_from_api, invoice_from_api


class FaultSpec(BaseModel):
    system: Literal["stripe", "hubspot", "gmail", "slack"]
    op: str
    mode: Literal["error", "timeout", "timeout_after_commit", "silent_drop"] = "error"
    status: int = 503
    times: int = 1


class CallRecord(BaseModel):
    seq: int
    system: str
    op: str
    mutation: bool
    args: dict[str, Any]
    outcome: str
    guard: dict[str, Any] | None = None
    at: float


class FixtureWorld:
    def __init__(self, data: dict[str, Any]) -> None:
        data = copy.deepcopy(data)
        self.faults = [FaultSpec(**f) for f in data.pop("faults", [])]
        self.data = data
        g = data.setdefault("gmail", {})
        g.setdefault("messages", {})
        g.setdefault("sent", [])
        h = data.setdefault("hubspot", {})
        for k in ("contacts", "companies", "deals"):
            h.setdefault(k, {})
        s = data.setdefault("stripe", {})
        for k in ("customers", "invoices", "credit_notes", "idempotency"):
            s.setdefault(k, {})
        data.setdefault("slack", {}).setdefault("messages", [])
        self.calls: list[CallRecord] = []
        self.guards: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.lock = threading.RLock()
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)

    @classmethod
    def from_file(cls, path: Path | str) -> "FixtureWorld":
        return cls(json.loads(Path(path).read_text()))

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            out = copy.deepcopy(self.data)
            out["faults"] = [f.model_dump() for f in self.faults]
            return out

    def inject(self, fault: FaultSpec | dict) -> None:
        with self.lock:
            self.faults.append(fault if isinstance(fault, FaultSpec) else FaultSpec(**fault))

    def clear_faults(self) -> None:
        with self.lock:
            self.faults.clear()

    def next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids):04d}"

    def _take_fault(self, system: str, op: str) -> FaultSpec | None:
        for f in self.faults:
            if f.system == system and f.op == op and f.times > 0:
                f.times -= 1
                return f
        return None

    def _record(self, system: str, op: str, mutation: bool, args: dict, outcome: str, guard: dict | None = None) -> None:
        self.calls.append(CallRecord(seq=next(self._seq), system=system, op=op, mutation=mutation, args=args,
                                     outcome=outcome, guard=guard, at=time.time()))

    def invoke(self, system: str, op: str, *, mutation: bool, args: dict, fn: Callable[[], Any],
               silent_drop_result: Any = None) -> Any:
        with self.lock:
            guard = None
            if mutation and (check := self.guards.get(f"{system}.{op}")):
                guard = check(args)  # evaluated for every attempt, including ones that then fail
            fault = self._take_fault(system, op)
            if fault and fault.mode in ("error", "timeout"):
                self._record(system, op, mutation, args,
                             "timeout" if fault.mode == "timeout" else f"error:{fault.status}", guard)
                if fault.mode == "timeout":
                    raise TransientError(code_for(system, write=mutation), f"{system}.{op} timed out (injected)",
                                         system=system, outcome_unknown=mutation)
                raise error_from_status(system, fault.status, f"injected fault on {op}", write=mutation)
            if fault and fault.mode == "silent_drop":
                self._record(system, op, mutation, args, "silent_drop", guard)
                return silent_drop_result
            try:
                result = fn()
            except IntegrationError as exc:
                self._record(system, op, mutation, args, f"rejected:{exc.status_code or exc.code}", guard)
                raise
            if fault and fault.mode == "timeout_after_commit":
                self._record(system, op, mutation, args, "timeout_after_commit", guard)
                raise TransientError(code_for(system, write=mutation), f"{system}.{op} timed out after commit (injected)",
                                     system=system, outcome_unknown=True)
            self._record(system, op, mutation, args, "ok", guard)
            return result

    def mark_last_outcome(self, outcome: str) -> None:
        with self.lock:
            if self.calls:
                self.calls[-1].outcome = outcome


def _not_found(system: str, what: str, write: bool = False) -> IntegrationError:
    return IntegrationError(code_for(system, write=write), f"No such {what}", system=system, status_code=404)


class FixtureStripe:
    system = "stripe"

    def __init__(self, world: FixtureWorld) -> None:
        self.w = world

    @property
    def _s(self) -> dict:
        return self.w.data["stripe"]

    def get_customer(self, customer_id: str) -> CustomerSnapshot:
        def fn():
            c = self._s["customers"].get(customer_id)
            if not c:
                raise _not_found("stripe", f"customer: {customer_id}")
            return customer_from_api(c)
        return self.w.invoke("stripe", "get_customer", mutation=False, args={"customer_id": customer_id}, fn=fn)

    def list_customer_invoices(self, customer_id: str) -> list[InvoiceSnapshot]:
        def fn():
            return [invoice_from_api(i, lines=[]) for i in self._s["invoices"].values() if i["customer"] == customer_id]
        return self.w.invoke("stripe", "list_customer_invoices", mutation=False, args={"customer_id": customer_id}, fn=fn)

    def get_invoice(self, invoice_id: str) -> InvoiceSnapshot:
        def fn():
            inv = self._s["invoices"].get(invoice_id)
            if not inv:
                raise _not_found("stripe", f"invoice: {invoice_id}")
            return invoice_from_api(inv)
        return self.w.invoke("stripe", "get_invoice", mutation=False, args={"invoice_id": invoice_id}, fn=fn)

    def list_credit_notes(self, invoice_id: str) -> list[CreditNoteSnapshot]:
        def fn():
            return [credit_note_from_api(c) for c in self._s["credit_notes"].values() if c["invoice"] == invoice_id]
        return self.w.invoke("stripe", "list_credit_notes", mutation=False, args={"invoice_id": invoice_id}, fn=fn)

    def get_credit_note(self, credit_note_id: str) -> CreditNoteSnapshot:
        def fn():
            cn = self._s["credit_notes"].get(credit_note_id)
            if not cn:
                raise _not_found("stripe", f"credit_note: {credit_note_id}")
            return credit_note_from_api(cn)
        return self.w.invoke("stripe", "get_credit_note", mutation=False, args={"credit_note_id": credit_note_id}, fn=fn)

    def create_credit_note(self, *, invoice_id: str, line_item_id: str, amount_minor: int, reason: str, memo: str,
                           metadata: dict[str, str], idempotency_key: str) -> CreditNoteSnapshot:
        params = credit_note_params(invoice_id=invoice_id, line_item_id=line_item_id, amount_minor=amount_minor,
                                    reason=reason, memo=memo, metadata=metadata)
        replay = {"hit": False}

        def reject(msg: str) -> IntegrationError:
            return IntegrationError(ErrorCode.STRIPE_WRITE_ERROR, msg, system="stripe", status_code=400)

        def fn():
            fingerprint = json.dumps(params, sort_keys=True)
            prior = self._s["idempotency"].get(idempotency_key)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise reject("Keys for idempotent requests can only be used with the same parameters.")
                replay["hit"] = True
                return credit_note_from_api(self._s["credit_notes"][prior["credit_note_id"]])
            inv = self._s["invoices"].get(invoice_id)
            if not inv:
                raise _not_found("stripe", f"invoice: {invoice_id}", write=True)
            if inv["status"] not in ("open", "paid"):
                raise reject(f"Invoice {invoice_id} is {inv['status']}; credit notes require a finalized invoice.")
            line = next((ln for ln in inv["lines"]["data"] if ln["id"] == line_item_id), None)
            if line is None:
                raise reject(f"Line {line_item_id} is not on invoice {invoice_id}.")
            credited = sum(
                cl["amount"] for cn in self._s["credit_notes"].values() if cn["status"] != "void"
                for cl in cn["lines"]["data"] if cl.get("invoice_line_item") == line_item_id
            )
            if amount_minor <= 0 or credited + amount_minor > line["amount"]:
                raise reject("Credit amount exceeds the creditable amount of the line item.")
            if inv["status"] == "open" and amount_minor > inv["amount_remaining"]:
                raise reject("Credit amount exceeds the invoice amount remaining.")
            n = 1 + sum(1 for cn in self._s["credit_notes"].values() if cn["invoice"] == invoice_id)
            cn_id = self.w.next_id("cn_fx")
            cn = {
                "id": cn_id, "object": "credit_note", "number": f"{inv.get('number') or inv['id']}-CN-{n:02d}",
                "invoice": invoice_id, "customer": inv["customer"], "amount": amount_minor, "total": amount_minor,
                "currency": inv["currency"], "status": "issued", "reason": reason, "memo": memo,
                "email_type": params["email_type"], "metadata": dict(metadata), "type": "pre_payment",
                "lines": {"data": [{"id": self.w.next_id("cnli_fx"), "type": "invoice_line_item",
                                    "invoice_line_item": line_item_id, "amount": amount_minor, "quantity": 1}]},
            }
            self._s["credit_notes"][cn_id] = cn
            inv["amount_remaining"] -= amount_minor
            inv["amount_due"] -= amount_minor
            inv["pre_payment_credit_notes_amount"] = inv.get("pre_payment_credit_notes_amount", 0) + amount_minor
            self._s["idempotency"][idempotency_key] = {"fingerprint": fingerprint, "credit_note_id": cn_id}
            return credit_note_from_api(cn)

        args = {"invoice_id": invoice_id, "line_item_id": line_item_id, "amount_minor": amount_minor,
                "reason": reason, "email_type": params["email_type"], "metadata": dict(metadata),
                "idempotency_key": idempotency_key}
        result = self.w.invoke("stripe", "create_credit_note", mutation=True, args=args, fn=fn)
        if replay["hit"]:
            self.w.mark_last_outcome("idempotent_replay")
        return result


class FixtureHubSpot:
    system = "hubspot"

    def __init__(self, world: FixtureWorld) -> None:
        self.w = world

    @property
    def _h(self) -> dict:
        return self.w.data["hubspot"]

    def _company(self, c: dict) -> CompanySnapshot:
        props = c.get("properties", {})
        return CompanySnapshot(id=c["id"], name=props.get("name", ""), domain=props.get("domain"),
                               properties={k: v for k, v in props.items() if k in COMPANY_PROPERTIES})

    def find_contacts_by_email(self, email: str) -> list[ContactSnapshot]:
        def fn():
            return [
                ContactSnapshot(id=c["id"], email=c["email"], first_name=c.get("firstname"), last_name=c.get("lastname"),
                                company_ids=list(c.get("company_ids", [])))
                for c in self._h["contacts"].values() if c["email"].lower() == email.lower()
            ]
        return self.w.invoke("hubspot", "find_contacts_by_email", mutation=False, args={"email": email}, fn=fn)

    def search_companies_by_domain(self, domain: str) -> list[CompanySnapshot]:
        def fn():
            return [self._company(c) for c in self._h["companies"].values()
                    if (c.get("properties", {}).get("domain") or "").lower() == domain.lower()]
        return self.w.invoke("hubspot", "search_companies_by_domain", mutation=False, args={"domain": domain}, fn=fn)

    def get_company(self, company_id: str) -> CompanySnapshot:
        def fn():
            c = self._h["companies"].get(company_id)
            if not c:
                raise _not_found("hubspot", f"company: {company_id}")
            return self._company(c)
        return self.w.invoke("hubspot", "get_company", mutation=False, args={"company_id": company_id}, fn=fn)

    def get_company_deals(self, company_id: str) -> list[DealSnapshot]:
        def fn():
            return [DealSnapshot(id=d["id"], name=d["dealname"], amount=d.get("amount"), stage=d.get("dealstage"))
                    for d in self._h["deals"].values() if d.get("company_id") == company_id]
        return self.w.invoke("hubspot", "get_company_deals", mutation=False, args={"company_id": company_id}, fn=fn)

    def update_dispute(self, company_id: str, properties: dict[str, str]) -> None:
        def fn():
            c = self._h["companies"].get(company_id)
            if not c:
                raise _not_found("hubspot", f"company: {company_id}", write=True)
            c.setdefault("properties", {}).update(properties)
        self.w.invoke("hubspot", "update_dispute", mutation=True,
                      args={"company_id": company_id, "properties": dict(properties)}, fn=fn)


class FixtureSlack:
    system = "slack"

    def __init__(self, world: FixtureWorld) -> None:
        self.w = world

    def _append(self, kind: str, text: str, card: dict | None = None) -> str:
        ts = f"{time.time():.6f}"
        self.w.data["slack"]["messages"].append({"ts": ts, "kind": kind, "text": text, "card": card})
        return ts

    def post_status(self, text: str) -> str:
        return self.w.invoke("slack", "post_status", mutation=False, args={"text": text[:200]},
                             fn=lambda: self._append("status", text))

    def post_approval_request(self, card: ApprovalCard) -> str:
        text = f"Approval required: {format_money(card.amount_minor, card.currency)} credit for {card.company_name}"
        return self.w.invoke("slack", "post_approval_request", mutation=False,
                             args={"run_id": card.run_id, "action_hash": card.action_hash},
                             fn=lambda: self._append("approval_request", text, card.model_dump(mode="json")))


class FixtureGmail:
    system = "gmail"

    def __init__(self, world: FixtureWorld) -> None:
        self.w = world

    @property
    def _g(self) -> dict:
        return self.w.data["gmail"]

    @staticmethod
    def _msg(d: dict) -> EmailMessage:
        fields = {k: v for k, v in d.items() if k != "inbox"}
        return EmailMessage(**{**fields, "received_at": datetime.fromisoformat(d["received_at"])})

    def get_message(self, message_id: str) -> EmailMessage:
        def fn():
            m = self._g["messages"].get(message_id)
            if not m:
                raise _not_found("gmail", f"message: {message_id}")
            return self._msg(m)
        return self.w.invoke("gmail", "get_message", mutation=False, args={"message_id": message_id}, fn=fn)

    def get_thread(self, thread_id: str) -> list[EmailMessage]:
        def fn():
            msgs = [self._msg(m) for m in self._g["messages"].values() if m["thread_id"] == thread_id]
            return sorted(msgs, key=lambda m: m.received_at)
        return self.w.invoke("gmail", "get_thread", mutation=False, args={"thread_id": thread_id}, fn=fn)

    def list_inbox(self, limit: int = 20) -> list[EmailMessage]:
        def fn():
            msgs = [self._msg(m) for m in self._g["messages"].values() if m.get("inbox", True)]
            return sorted(msgs, key=lambda m: m.received_at, reverse=True)[:limit]
        return self.w.invoke("gmail", "list_inbox", mutation=False, args={"limit": limit}, fn=fn)

    def send_reply(self, *, thread_id: str, to_email: str, subject: str, body: str, in_reply_to: str | None) -> str:
        def fn():
            sent_id = self.w.next_id("sent")
            self._g["sent"].append({"id": sent_id, "thread_id": thread_id, "to_email": to_email,
                                    "subject": subject, "body": body, "in_reply_to": in_reply_to})
            return sent_id
        return self.w.invoke("gmail", "send_reply", mutation=True,
                             args={"thread_id": thread_id, "to_email": to_email, "subject": subject}, fn=fn)
