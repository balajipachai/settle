"""Narrow, typed gateway contracts. Live clients and fixture adapters implement these.

Read methods may be used during investigation. Write methods (create_credit_note,
update_dispute, send_reply) are only called by deterministic execution nodes
after policy + approval gates; they are never exposed to the LLM.
"""

from __future__ import annotations

from typing import Protocol

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


class StripeGateway(Protocol):
    def get_customer(self, customer_id: str) -> CustomerSnapshot: ...
    def list_customer_invoices(self, customer_id: str) -> list[InvoiceSnapshot]: ...
    def get_invoice(self, invoice_id: str) -> InvoiceSnapshot: ...
    def list_credit_notes(self, invoice_id: str) -> list[CreditNoteSnapshot]: ...
    def get_credit_note(self, credit_note_id: str) -> CreditNoteSnapshot: ...

    # WRITE — execution node only
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
    ) -> CreditNoteSnapshot: ...


class HubSpotGateway(Protocol):
    def find_contacts_by_email(self, email: str) -> list[ContactSnapshot]: ...
    def search_companies_by_domain(self, domain: str) -> list[CompanySnapshot]: ...
    def get_company(self, company_id: str) -> CompanySnapshot: ...
    def get_company_deals(self, company_id: str) -> list[DealSnapshot]: ...

    # WRITE — execution node only
    def update_dispute(self, company_id: str, properties: dict[str, str]) -> None: ...


class SlackGateway(Protocol):
    def post_status(self, text: str) -> str: ...
    def post_approval_request(self, card: ApprovalCard) -> str: ...


class GmailGateway(Protocol):
    def get_message(self, message_id: str) -> EmailMessage: ...
    def get_thread(self, thread_id: str) -> list[EmailMessage]: ...
    def list_inbox(self, limit: int = 20) -> list[EmailMessage]: ...

    # WRITE — only after VERIFIED
    def send_reply(
        self, *, thread_id: str, to_email: str, subject: str, body: str, in_reply_to: str | None
    ) -> str: ...
