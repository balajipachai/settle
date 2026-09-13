"""Place the canonical dispute thread into the support Gmail inbox (messages.insert — nothing is sent).

    uv run python scripts/seed_gmail.py
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import UTC, datetime

from _common import demo_customer_email, load_seed, need, save_seed, settings

from app.fixtures.worlds import ACME_DISPUTE_BODY, ACME_PRIOR_BODY
from app.integrations.gmail import LiveGmail

s = settings()
gmail = LiveGmail(need(s.gmail_client_id, "GMAIL_CLIENT_ID"), need(s.gmail_client_secret, "GMAIL_CLIENT_SECRET"),
                  need(s.gmail_refresh_token, "GMAIL_REFRESH_TOKEN"), user_id=s.gmail_user_id)
inbox_address = gmail._req("GET", "/profile")["emailAddress"]
customer_email = demo_customer_email()
ref = (load_seed().get("stripe") or {}).get("invoice_ref", "INV-10428")


def mime(subject: str, body: str, when: datetime, msg_id: str, in_reply_to: str | None = None) -> str:
    m = EmailMessage()
    m["From"] = f"Maya Chen <{customer_email}>"
    m["To"] = inbox_address
    m["Subject"] = subject
    m["Date"] = format_datetime(when)
    m["Message-ID"] = msg_id
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


message_domain = customer_email.rsplit("@", 1)[-1]
prior_id = make_msgid(domain=message_domain)
prior = gmail._req("POST", "/messages", json={
    "raw": mime("August API usage review", ACME_PRIOR_BODY, datetime(2026, 8, 28, 16, 5, tzinfo=UTC), prior_id),
    "labelIds": []})
dispute = gmail._req("POST", "/messages", json={
    "raw": mime(f"Duplicate API overage charge on {ref}", ACME_DISPUTE_BODY.replace("INV-10428", ref),
                datetime.now(UTC), make_msgid(domain=message_domain), in_reply_to=prior_id),
    "labelIds": ["INBOX", "UNREAD"], "threadId": prior["threadId"]})
save_seed({"gmail": {"dispute_message_id": dispute["id"], "thread_id": dispute["threadId"], "inbox": inbox_address}})
print(f"Gmail seeded in {inbox_address}: dispute message {dispute['id']} (thread {dispute['threadId']}).")
print("Start Settle with SETTLE_MODE=live and click 'Resolve with Settle' on the dispute.")
