"""Stripe REST client (test mode) + payload mappers shared with the fixture adapter."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.domain.models import CreditNoteSnapshot, CustomerSnapshot, InvoiceLine, InvoiceSnapshot
from app.integrations.base import error_from_status, error_from_transport, response_detail


def _meta(d: dict | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (d or {}).items()}


def _id(v: Any) -> Any:
    return v.get("id") if isinstance(v, dict) else v


def line_from_api(d: dict) -> InvoiceLine:
    pricing = (d.get("pricing") or {}).get("price_details") or {}
    legacy = d.get("price") or {}
    price_id = pricing.get("price") or _id(legacy) or None
    product = pricing.get("product") or (legacy.get("product") if isinstance(legacy, dict) else None)
    period = d.get("period") or {}
    return InvoiceLine(
        id=d["id"],
        amount_minor=int(d["amount"]),
        currency=str(d["currency"]).lower(),
        description=d.get("description"),
        price_id=price_id,
        product_id=_id(product),
        period_start=period.get("start"),
        period_end=period.get("end"),
        quantity=d.get("quantity"),
        metadata=_meta(d.get("metadata")),
    )


def invoice_from_api(d: dict, lines: list[InvoiceLine] | None = None) -> InvoiceSnapshot:
    return InvoiceSnapshot(
        id=d["id"],
        number=d.get("number"),
        customer_id=_id(d["customer"]),
        currency=str(d["currency"]).lower(),
        status=d["status"],
        total_minor=int(d.get("total") or 0),
        amount_due_minor=int(d.get("amount_due") or 0),
        amount_paid_minor=int(d.get("amount_paid") or 0),
        amount_remaining_minor=int(d.get("amount_remaining") or 0),
        lines=lines if lines is not None else [line_from_api(x) for x in (d.get("lines") or {}).get("data", [])],
        metadata=_meta(d.get("metadata")),
    )


def credit_note_from_api(d: dict) -> CreditNoteSnapshot:
    lines = (d.get("lines") or {}).get("data", [])
    return CreditNoteSnapshot(
        id=d["id"],
        number=d.get("number"),
        invoice_id=_id(d["invoice"]),
        amount_minor=int(d.get("amount") if d.get("amount") is not None else d.get("total") or 0),
        currency=str(d["currency"]).lower(),
        status=d.get("status") or "issued",
        reason=d.get("reason"),
        line_item_ids=[x["invoice_line_item"] for x in lines if x.get("invoice_line_item")],
        metadata=_meta(d.get("metadata")),
    )


def customer_from_api(d: dict) -> CustomerSnapshot:
    return CustomerSnapshot(id=d["id"], name=d.get("name"), email=d.get("email"), metadata=_meta(d.get("metadata")))


def form_encode(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's bracketed form encoding: lines[0][type]=..., metadata[k]=v."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        full = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            out.extend(form_encode(value, full))
        elif isinstance(value, list | tuple):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(form_encode(item, f"{full}[{i}]"))
                else:
                    out.append((f"{full}[{i}]", str(item)))
        elif isinstance(value, bool):
            out.append((full, "true" if value else "false"))
        else:
            out.append((full, str(value)))
    return out


def credit_note_params(
    *, invoice_id: str, line_item_id: str, amount_minor: int, reason: str, memo: str, metadata: dict[str, str]
) -> dict[str, Any]:
    return {
        "invoice": invoice_id,
        "lines": [{"type": "invoice_line_item", "invoice_line_item": line_item_id, "amount": int(amount_minor)}],
        "reason": reason,
        "email_type": "none",  # Stripe must not email the customer; Settle owns communication after verification.
        "memo": memo,
        "metadata": metadata,
    }


class LiveStripe:
    system = "stripe"

    def __init__(
        self,
        secret_key: str,
        *,
        api_version: str | None = None,
        timeout: float = 20.0,
        allow_live_keys: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not secret_key.startswith(("sk_test_", "rk_test_")) and not allow_live_keys:
            raise ValueError("LiveStripe requires a Stripe *test mode* key (sk_test_/rk_test_).")
        headers = {"Authorization": f"Bearer {secret_key}"}
        if api_version:
            headers["Stripe-Version"] = api_version
        self._http = httpx.Client(base_url="https://api.stripe.com", headers=headers, timeout=timeout,
                                  transport=transport)

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = self._http.get(path, params=params)
        except httpx.TransportError as exc:
            raise error_from_transport("stripe", exc) from exc
        if resp.status_code >= 400:
            raise error_from_status("stripe", resp.status_code, response_detail(resp))
        return resp.json()

    def _list(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        params = {**params, "limit": 100}
        while True:
            page = self._get(path, params)
            items.extend(page.get("data", []))
            if not page.get("has_more") or not items:
                return items
            params["starting_after"] = items[-1]["id"]

    def get_customer(self, customer_id: str) -> CustomerSnapshot:
        return customer_from_api(self._get(f"/v1/customers/{customer_id}"))

    def list_customer_invoices(self, customer_id: str) -> list[InvoiceSnapshot]:
        return [invoice_from_api(d, lines=[]) for d in self._list("/v1/invoices", {"customer": customer_id})]

    def get_invoice(self, invoice_id: str) -> InvoiceSnapshot:
        inv = self._get(f"/v1/invoices/{invoice_id}")
        lines = [line_from_api(x) for x in self._list(f"/v1/invoices/{invoice_id}/lines", {})]
        return invoice_from_api(inv, lines=lines)

    def list_credit_notes(self, invoice_id: str) -> list[CreditNoteSnapshot]:
        notes = []
        for d in self._list("/v1/credit_notes", {"invoice": invoice_id}):
            if (d.get("lines") or {}).get("has_more"):
                d["lines"] = {"data": self._list(f"/v1/credit_notes/{d['id']}/lines", {})}
            notes.append(credit_note_from_api(d))
        return notes

    def get_credit_note(self, credit_note_id: str) -> CreditNoteSnapshot:
        return credit_note_from_api(self._get(f"/v1/credit_notes/{credit_note_id}"))

    def create_credit_note(
        self,
        *,
        invoice_id: str,
        line_item_id: str,
        amount_minor: int,
        reason: str,
        memo: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> CreditNoteSnapshot:
        params = credit_note_params(
            invoice_id=invoice_id, line_item_id=line_item_id, amount_minor=amount_minor, reason=reason,
            memo=memo, metadata=metadata,
        )
        try:
            resp = self._http.post(
                "/v1/credit_notes",
                content=urlencode(form_encode(params)).encode(),
                headers={"Idempotency-Key": idempotency_key, "Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TransportError as exc:
            raise error_from_transport("stripe", exc, write=True) from exc
        if resp.status_code >= 400:
            raise error_from_status("stripe", resp.status_code, response_detail(resp), write=True)
        return credit_note_from_api(resp.json())
