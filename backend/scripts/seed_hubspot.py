"""Seed HubSpot: dispute custom properties, Acme Analytics company, Maya Chen contact, renewal deal, and link the
Stripe customer (both directions). Requires scripts/seed_stripe.py to have run.

    uv run python scripts/seed_hubspot.py
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
from _common import demo_customer_email, load_seed, need, save_seed, settings

from app.integrations.stripe import form_encode

s = settings()
token = need(s.hubspot_access_token, "HUBSPOT_ACCESS_TOKEN")
customer_email = demo_customer_email()
stripe_seed = load_seed().get("stripe") or {}
customer_id = need(stripe_seed.get("customer_id"), "stripe.customer_id in data/live_seed.json (run seed_stripe.py)")
hs = httpx.Client(base_url="https://api.hubapi.com", headers={"Authorization": f"Bearer {token}"}, timeout=30)


def req(method: str, path: str, *, ok: tuple[int, ...] = (), **kw) -> dict:
    r = hs.request(method, path, **kw)
    if r.status_code >= 400 and r.status_code not in ok:
        raise SystemExit(f"{method} {path} failed: {r.status_code} {r.text}")
    return r.json() if r.content else {}


PROPS = ["stripe_customer_id", "account_tier", "account_owner", "renewal_date", "dispute_status", "dispute_amount",
         "dispute_currency", "resolution_type", "stripe_credit_note_id", "settle_run_id", "settle_action_id", "resolved_at"]
for name in PROPS:
    req("POST", "/crm/v3/properties/companies", ok=(409,), json={
        "name": name, "label": name.replace("_", " ").title(), "type": "string", "fieldType": "text",
        "groupName": "companyinformation"})

found = req("POST", "/crm/v3/objects/companies/search", json={
    "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": "acme-analytics.example"}]}]})
company_props = {
    "name": "Acme Analytics", "domain": "acme-analytics.example", "stripe_customer_id": customer_id,
    "account_tier": "enterprise", "account_owner": "Jordan Lee", "renewal_date": "2027-01-31",
    "description": "Enterprise API customer since 2023. Single production API project.",
    "dispute_status": "", "dispute_amount": "", "dispute_currency": "", "resolution_type": "",
    "stripe_credit_note_id": "", "settle_run_id": "", "settle_action_id": "", "resolved_at": "",
}
if found.get("results"):
    company_id = found["results"][0]["id"]
    req("PATCH", f"/crm/v3/objects/companies/{company_id}", json={"properties": company_props})
else:
    company_id = req("POST", "/crm/v3/objects/companies", json={"properties": company_props})["id"]

found = req("POST", "/crm/v3/objects/contacts/search", json={
    "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": customer_email}]}]})
contact_id = found["results"][0]["id"] if found.get("results") else req("POST", "/crm/v3/objects/contacts", json={
    "properties": {"email": customer_email, "firstname": "Maya", "lastname": "Chen"}})["id"]
req("PUT", f"/crm/v4/objects/contact/{contact_id}/associations/default/company/{company_id}")

deal_id = req("POST", "/crm/v3/objects/deals", json={"properties": {
    "dealname": "Acme Analytics - 2026 Enterprise Renewal", "amount": "480000", "pipeline": "default",
    "dealstage": "contractsent"}})["id"]
req("PUT", f"/crm/v4/objects/deal/{deal_id}/associations/default/company/{company_id}")

# Back-reference so identity resolution can cross-check Stripe -> HubSpot.
stripe_key = need(s.stripe_secret_key, "STRIPE_SECRET_KEY")
r = httpx.post(f"https://api.stripe.com/v1/customers/{customer_id}", headers={
    "Authorization": f"Bearer {stripe_key}", "Content-Type": "application/x-www-form-urlencoded"},
    content=urlencode(form_encode({"email": customer_email, "metadata": {"hubspot_company_id": company_id}})).encode(), timeout=30)
if r.status_code >= 400:
    raise SystemExit(f"Stripe customer metadata update failed: {r.status_code} {r.text}")

save_seed({"hubspot": {"company_id": company_id, "contact_id": contact_id, "deal_id": deal_id}})
print(f"HubSpot seeded: company {company_id}, contact {contact_id}, deal {deal_id}; Stripe customer linked.")
print("Next: uv run python scripts/seed_gmail.py")
