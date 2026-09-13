"""Slack Web API client, approval card, and request-signature verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

from app.domain.errors import ErrorCode
from app.domain.models import ApprovalCard
from app.domain.money import format_money
from app.integrations.base import IntegrationError, TransientError, error_from_status, error_from_transport


def _esc(text: str) -> str:
    """Escape Slack mrkdwn control chars so untrusted text cannot inject links/mentions."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def approval_value(card: ApprovalCard) -> str:
    # Only identifiers travel in the button; the server resolves everything else from stored state.
    return json.dumps({"run_id": card.run_id, "action_id": card.action_id, "action_hash": card.action_hash})


def approval_blocks(card: ApprovalCard) -> list[dict]:
    amount = format_money(card.amount_minor, card.currency)
    value = approval_value(card)
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"Invoice dispute: {card.company_name}"[:150]}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Invoice*\n{_esc(card.invoice_number)}"},
                {"type": "mrkdwn", "text": f"*Credit requested*\n{amount}"},
                {"type": "mrkdwn", "text": f"*Claim*\n{_esc(card.claim_summary)[:500]}"},
                {"type": "mrkdwn", "text": "*Policy*\nPASSED · approval REQUIRED"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Evidence*\n" + "\n".join(f"• {_esc(e)}" for e in card.evidence_summary)[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Action `{card.action_hash[:12]}` · run `{card.run_id}` · <{card.dashboard_url}|Open in Settle>"}]},
        {
            "type": "actions",
            "block_id": "settle_approval",
            "elements": [
                {"type": "button", "style": "primary", "action_id": "settle_approve", "value": value,
                 "text": {"type": "plain_text", "text": f"Approve {amount} credit"},
                 "confirm": {"title": {"type": "plain_text", "text": "Approve credit note?"},
                             "text": {"type": "mrkdwn", "text": f"Creates a {amount} Stripe credit note on {_esc(card.invoice_number)}."},
                             "confirm": {"type": "plain_text", "text": "Approve"}, "deny": {"type": "plain_text", "text": "Cancel"}}},
                {"type": "button", "style": "danger", "action_id": "settle_reject", "value": value,
                 "text": {"type": "plain_text", "text": "Reject"}},
            ],
        },
    ]


def verify_slack_signature(
    signing_secret: str, timestamp: str, body: bytes, signature: str, *, now: float | None = None, tolerance: int = 300
) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


class LiveSlack:
    system = "slack"

    def __init__(self, bot_token: str, channel_id: str, *, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.channel_id = channel_id
        self._http = httpx.Client(base_url="https://slack.com/api", timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {bot_token}"})

    def close(self) -> None:
        self._http.close()

    def _post_message(self, text: str, blocks: list[dict] | None = None) -> str:
        payload = {"channel": self.channel_id, "text": text, "unfurl_links": False}
        if blocks:
            payload["blocks"] = blocks
        try:
            resp = self._http.post("/chat.postMessage", json=payload)
        except httpx.TransportError as exc:
            raise error_from_transport("slack", exc) from exc
        if resp.status_code >= 400:
            raise error_from_status("slack", resp.status_code, resp.text[:200])
        data = resp.json()
        if not data.get("ok"):
            err = data.get("error", "unknown_error")
            if err in ("ratelimited", "internal_error", "service_unavailable"):
                raise TransientError(ErrorCode.SLACK_ERROR, f"slack error: {err}", system="slack")
            raise IntegrationError(ErrorCode.SLACK_ERROR, f"slack error: {err}", system="slack")
        return data["ts"]

    def post_status(self, text: str) -> str:
        return self._post_message(text)

    def post_approval_request(self, card: ApprovalCard) -> str:
        amount = format_money(card.amount_minor, card.currency)
        return self._post_message(f"Approval required: {amount} credit for {card.company_name} ({card.invoice_number})",
                                  approval_blocks(card))
