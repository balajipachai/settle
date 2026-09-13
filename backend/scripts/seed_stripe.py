"""Seed the canonical dispute into Stripe TEST mode: Acme Analytics, $42,000 open invoice INV-10428 with a
duplicated $6,000 API overage line. Writes IDs to data/live_seed.json.

    uv run python scripts/seed_stripe.py
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
from _common import need, save_seed, settings

from app.fixtures.worlds import AUG_2026
from app.integrations.stripe import form_encode

s = settings()
key = need(s.stripe_secret_key, "STRIPE_SECRET_KEY")
if not key.startswith(("sk_test_", "rk_test_")):
    raise SystemExit("Refusing to seed: a Stripe TEST mode key is required.")
headers = {"Authorization": f"Bearer {key}"}
if s.stripe_api_version:
    headers["Stripe-Version"] = s.stripe_api_version
http = httpx.Client(base_url="https://api.stripe.com", headers=headers, timeout=30)


def post(path: str, data: dict, *, ok_errors: tuple[str, ...] = ()) -> dict | None:
    r = http.post(path, content=urlencode(form_encode(data)).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        if any(e in r.text.lower() for e in ok_errors):
            return None
        raise SystemExit(f"POST {path} failed: {r.status_code} {r.text}")
    return r.json()


customer = post("/v1/customers", {"name": "Acme Analytics", "email": "ap@acme-analytics.example",
                                  "metadata": {"settle_seed": "canonical"}})
prices = {}
for key_, name, amount in (("platform", "Enterprise platform subscription", 3_000_000), ("overage", "API overage", 600_000)):
    product = post("/v1/products", {"name": name})
    prices[key_] = post("/v1/prices", {"product": product["id"], "unit_amount": amount, "currency": "usd"})["id"]

invoice = post("/v1/invoices", {"customer": customer["id"], "collection_method": "send_invoice", "days_until_due": 30,
                                "auto_advance": False, "pending_invoice_items_behavior": "exclude", "number": "INV-10428",
                                "metadata": {"settle_invoice_ref": "INV-10428"}}, ok_errors=("number",))
if invoice is None:  # number already used in this account: keep the reference in metadata instead
    invoice = post("/v1/invoices", {"customer": customer["id"], "collection_method": "send_invoice", "days_until_due": 30,
                                    "auto_advance": False, "pending_invoice_items_behavior": "exclude",
                                    "metadata": {"settle_invoice_ref": "INV-10428"}})


def add_item(price: str, description: str) -> None:
    base = {"customer": customer["id"], "invoice": invoice["id"], "description": description,
            "period": {"start": AUG_2026[0], "end": AUG_2026[1]}}
    # API versions >= 2025-03-31 use pricing[price]; older versions use price.
    if post("/v1/invoiceitems", {**base, "pricing": {"price": price}}, ok_errors=("unknown parameter", "received unknown")) is None:
        post("/v1/invoiceitems", {**base, "price": price})


add_item(prices["platform"], "Enterprise platform subscription - Aug 2026")
add_item(prices["overage"], "API overage - Aug 2026")
add_item(prices["overage"], "API overage - Aug 2026")  # the duplicate
final = post(f"/v1/invoices/{invoice['id']}/finalize", {"auto_advance": False})
seed = save_seed({"stripe": {"customer_id": customer["id"], "invoice_id": final["id"], "invoice_number": final["number"],
                             "invoice_ref": "INV-10428", "total": final["total"], "amount_remaining": final["amount_remaining"]}})
print(f"Stripe seeded: customer {customer['id']}, invoice {final['id']} ({final['number']}), status {final['status']}, "
      f"total {final['total']}")
print("Next: uv run python scripts/seed_hubspot.py")
