"""Gmail REST client using an OAuth refresh token (no Google SDK dependency)."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage
from email.utils import parseaddr, parsedate_to_datetime

import httpx

from app.domain.errors import ErrorCode
from app.domain.models import EmailMessage
from app.integrations.base import IntegrationError, error_from_status, error_from_transport, response_detail

TOKEN_URL = "https://oauth2.googleapis.com/token"


def _b64url_decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _plain_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and (payload.get("body") or {}).get("data"):
        return _b64url_decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


def message_from_api(d: dict) -> EmailMessage:
    payload = d.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    try:
        received = parsedate_to_datetime(headers["date"])
    except (KeyError, TypeError, ValueError):
        received = datetime.fromtimestamp(int(d.get("internalDate", "0")) / 1000, UTC)
    return EmailMessage(
        id=d["id"],
        thread_id=d.get("threadId") or d["id"],
        from_email=addr.lower(),
        from_name=name or None,
        to_email=parseaddr(headers.get("to", ""))[1] or None,
        subject=headers.get("subject", ""),
        body=_plain_text(payload) or d.get("snippet", ""),
        received_at=received,
        message_id_header=headers.get("message-id"),
    )


class LiveGmail:
    system = "gmail"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, user_id: str = "me",
                 inbox_query: str = "in:inbox newer_than:30d", timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._creds = {"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
                       "grant_type": "refresh_token"}
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._base = f"https://gmail.googleapis.com/gmail/v1/users/{user_id}"
        self._inbox_query = inbox_query
        self._token: str | None = None
        self._token_exp = 0.0

    def close(self) -> None:
        self._http.close()

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        try:
            resp = self._http.post(TOKEN_URL, data=self._creds)
        except httpx.TransportError as exc:
            raise error_from_transport("gmail", exc) from exc
        if resp.status_code >= 400:
            raise IntegrationError(ErrorCode.GMAIL_ERROR, "Gmail OAuth refresh failed", system="gmail",
                                   status_code=resp.status_code)
        data = resp.json()
        self._token, self._token_exp = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _req(self, method: str, path: str, *, write: bool = False, **kw) -> dict:
        try:
            resp = self._http.request(method, self._base + path,
                                      headers={"Authorization": f"Bearer {self._access_token()}"}, **kw)
        except httpx.TransportError as exc:
            raise error_from_transport("gmail", exc, write=write) from exc
        if resp.status_code >= 400:
            raise error_from_status("gmail", resp.status_code, response_detail(resp), write=write)
        return resp.json()

    def get_message(self, message_id: str) -> EmailMessage:
        return message_from_api(self._req("GET", f"/messages/{message_id}", params={"format": "full"}))

    def get_thread(self, thread_id: str) -> list[EmailMessage]:
        data = self._req("GET", f"/threads/{thread_id}", params={"format": "full"})
        return sorted((message_from_api(m) for m in data.get("messages", [])), key=lambda m: m.received_at)

    def list_inbox(self, limit: int = 20) -> list[EmailMessage]:
        data = self._req("GET", "/messages", params={"q": self._inbox_query, "maxResults": limit})
        return [self.get_message(m["id"]) for m in data.get("messages", [])]

    def send_reply(self, *, thread_id: str, to_email: str, subject: str, body: str, in_reply_to: str | None) -> str:
        mime = MimeMessage()
        mime["To"] = to_email
        mime["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to
        mime.set_content(body)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        return self._req("POST", "/messages/send", write=True, json={"raw": raw, "threadId": thread_id})["id"]

    def insert_message(self, raw_mime: bytes) -> str:
        """Seed helper: place a message in the mailbox without sending it."""
        raw = base64.urlsafe_b64encode(raw_mime).decode()
        return self._req("POST", "/messages", json={"raw": raw, "labelIds": ["INBOX", "UNREAD"]})["id"]
