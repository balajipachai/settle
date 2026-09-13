"""HubSpot CRM v3/v4 REST client + dispute property contract."""

from __future__ import annotations

import httpx

from app.domain.models import CompanySnapshot, ContactSnapshot, DealSnapshot
from app.domain.money import to_major
from app.integrations.base import error_from_status, error_from_transport, response_detail

COMPANY_PROPERTIES = [
    "name", "domain", "stripe_customer_id", "account_tier", "account_owner", "renewal_date", "description",
    "dispute_status", "dispute_amount", "dispute_currency", "resolution_type", "stripe_credit_note_id",
    "settle_run_id", "settle_action_id", "resolved_at",
]


def dispute_properties(
    *, status: str, amount_minor: int, currency: str, credit_note_id: str, run_id: str, action_id: str,
    resolution_type: str, resolved_at: str,
) -> dict[str, str]:
    return {
        "dispute_status": status,
        "dispute_amount": str(to_major(amount_minor, currency)),
        "dispute_currency": currency.upper(),
        "resolution_type": resolution_type,
        "stripe_credit_note_id": credit_note_id,
        "settle_run_id": run_id,
        "settle_action_id": action_id,
        "resolved_at": resolved_at,
    }


def company_from_api(d: dict) -> CompanySnapshot:
    props = d.get("properties") or {}
    return CompanySnapshot(
        id=str(d["id"]), name=props.get("name") or "", domain=props.get("domain"),
        properties={k: v for k, v in props.items() if k in COMPANY_PROPERTIES},
    )


class LiveHubSpot:
    system = "hubspot"

    def __init__(self, access_token: str, *, timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url="https://api.hubapi.com",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, *, write: bool = False, **kw) -> dict:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise error_from_transport("hubspot", exc, write=write) from exc
        if resp.status_code >= 400:
            raise error_from_status("hubspot", resp.status_code, response_detail(resp), write=write)
        return resp.json() if resp.content else {}

    def _associated_ids(self, from_type: str, from_id: str, to_type: str) -> list[str]:
        data = self._req("GET", f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}")
        return [str(r["toObjectId"]) for r in data.get("results", [])]

    def find_contacts_by_email(self, email: str) -> list[ContactSnapshot]:
        body = {
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email", "firstname", "lastname"],
            "limit": 10,
        }
        results = self._req("POST", "/crm/v3/objects/contacts/search", json=body).get("results", [])
        contacts = []
        for r in results:
            p = r.get("properties") or {}
            contacts.append(ContactSnapshot(
                id=str(r["id"]), email=p.get("email") or email, first_name=p.get("firstname"),
                last_name=p.get("lastname"), company_ids=self._associated_ids("contacts", str(r["id"]), "companies"),
            ))
        return contacts

    def search_companies_by_domain(self, domain: str) -> list[CompanySnapshot]:
        body = {
            "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
            "properties": COMPANY_PROPERTIES,
            "limit": 10,
        }
        return [company_from_api(r) for r in self._req("POST", "/crm/v3/objects/companies/search", json=body).get("results", [])]

    def get_company(self, company_id: str) -> CompanySnapshot:
        return company_from_api(
            self._req("GET", f"/crm/v3/objects/companies/{company_id}", params={"properties": ",".join(COMPANY_PROPERTIES)})
        )

    def get_company_deals(self, company_id: str) -> list[DealSnapshot]:
        ids = self._associated_ids("companies", company_id, "deals")
        if not ids:
            return []
        data = self._req("POST", "/crm/v3/objects/deals/batch/read", json={
            "properties": ["dealname", "amount", "dealstage"], "inputs": [{"id": i} for i in ids],
        })
        return [
            DealSnapshot(id=str(r["id"]), name=(r.get("properties") or {}).get("dealname") or "",
                         amount=(r.get("properties") or {}).get("amount"), stage=(r.get("properties") or {}).get("dealstage"))
            for r in data.get("results", [])
        ]

    def update_dispute(self, company_id: str, properties: dict[str, str]) -> None:
        self._req("PATCH", f"/crm/v3/objects/companies/{company_id}", write=True, json={"properties": properties})
