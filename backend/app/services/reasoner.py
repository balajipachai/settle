"""LLM reasoning boundary.

The reasoner answers two questions only: "what does the customer claim?" and
"what happened, given the evidence?". Its outputs are strict Pydantic objects
that deterministic guards re-check before anything downstream uses them.

- ClaudeReasoner: Claude via structured outputs (default when credentials exist).
- RuleBasedReasoner: deterministic offline fallback, also used by the
  reproducible eval suite so metrics measure the control plane, not model variance.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from langsmith import traceable
from pydantic import BaseModel

from app.domain.errors import ErrorCode, SettleError
from app.domain.models import (
    AdjudicationDecision,
    Claim,
    ClaimExtraction,
    EmailMessage,
    EvidenceReference,
    FinancialReconciliation,
)
from app.domain.money import find_money
from app.services.prompts import ADJUDICATION_SYSTEM, EXTRACTION_SYSTEM


class ContextItem(BaseModel):
    ref_id: str
    trusted: bool
    text: str


class AdjudicationPacket(BaseModel):
    claim: Claim
    reconciliation: FinancialReconciliation
    references: list[EvidenceReference]
    source_email: EmailMessage
    source_email_ref: str
    context: list[ContextItem]


class Reasoner(Protocol):
    name: str

    def extract_claim(self, email: EmailMessage) -> ClaimExtraction: ...
    def adjudicate(self, packet: AdjudicationPacket) -> AdjudicationDecision: ...


# --------------------------------------------------------------------------- rule-based

_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("duplicate_charge", [r"charged\s+twice", r"billed\s+twice", r"double[-\s]?(charged|billed|charge)", r"\bduplicate",
                          r"twice\s+for", r"charged\s+two\s+times", r"same\s+charge\s+twice"]),
    ("incorrect_quantity", [r"\bseats?\b", r"\blicen[cs]es?\b", r"\bquantity\b", r"\bnumber\s+of\s+(users|units)\b"]),
    ("incorrect_price", [r"\bprice\b", r"\bpricing\b", r"\brate\b", r"\bdiscount\b", r"\bovercharged\b"]),
    ("service_not_received", [r"never\s+received", r"not\s+delivered", r"didn'?t\s+receive", r"\boutage\b",
                              r"not\s+provided", r"never\s+used"]),
]
CONFLICT_CUES = [
    r"two\s+(separate|different|distinct)\s+(api\s+)?(projects|environments|accounts|keys|workspaces)",
    r"second\s+(api\s+)?(project|environment|workspace)",
    r"both\s+(charges|overages|lines)\s+(are|were)\s+(expected|valid|correct|intentional)",
    r"bill(ed)?\s+separately",
    r"add(ed)?\s+a\s+second\s+(api\s+)?overage",
]
_INVOICE_RE = re.compile(r"\b[A-Z]{2,5}-\d{3,}\b")
_LABELS = {
    "duplicate_charge": "a duplicate charge",
    "incorrect_quantity": "an incorrect quantity",
    "incorrect_price": "an incorrect price",
    "service_not_received": "a service that was not received",
    "unknown": "an unclear billing issue",
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


class RuleBasedReasoner:
    name = "rules-v1"

    def extract_claim(self, email: EmailMessage) -> ClaimExtraction:
        text = f"{email.subject}\n{email.body}"
        dispute_type = "unknown"
        for dtype, pats in _TYPE_PATTERNS:
            if any(re.search(p, text, re.I) for p in pats):
                dispute_type = dtype
                break
        money = find_money(email.body) or find_money(email.subject)
        amount = money[0] if money else None
        invoice = _INVOICE_RE.search(text)
        phrases = [s for s in _sentences(email.body)
                   if any(re.search(p, s, re.I) for _, pats in _TYPE_PATTERNS for p in pats)][:3]
        summary = (f"Customer disputes {_LABELS[dispute_type]}"
                   + (f" of {amount.text}" if amount else "")
                   + (f" on {invoice.group(0)}" if invoice else "") + ".")
        return ClaimExtraction(
            dispute_type=dispute_type, disputed_amount_text=amount.text if amount else None,
            currency_code=amount.currency.upper() if amount else None,
            invoice_hint=invoice.group(0) if invoice else None, summary=summary, evidence_phrases=phrases,
        )

    def adjudicate(self, p: AdjudicationPacket) -> AdjudicationDecision:
        claim, recon = p.claim, p.reconciliation
        known = {r.ref_id for r in p.references}
        invoice_ref = next((r.ref_id for r in p.references if r.object_type == "invoice"), "")
        base = [r for r in (p.source_email_ref, invoice_ref) if r in known]

        def decide(classification, validity, resolution, conf, rationale, refs):
            return AdjudicationDecision(classification=classification, validity=validity, resolution=resolution,
                                        model_confidence=conf, rationale=rationale, evidence_ref_ids=refs)

        if claim.dispute_type != "duplicate_charge":
            return decide(claim.dispute_type, "INCONCLUSIVE", "HUMAN_INVESTIGATION", 0.6,
                          f"The customer disputes {_LABELS[claim.dispute_type]} ({p.source_email_ref}). Settle has no "
                          f"deterministic calculation for this dispute type, so {invoice_ref} needs human review.", base)
        if recon.already_credited:
            cns = [f"stripe:credit_note:{i}" for i in recon.existing_credit_note_ids]
            return decide("duplicate_charge", "VALID", "NO_ACTION", 0.95,
                          f"The duplicated charge was already credited ({', '.join(cns)}); no further credit is due.",
                          base + [c for c in cns if c in known])
        for item in p.context:
            for pat in CONFLICT_CUES:
                if m := re.search(pat, item.text, re.I):
                    return decide("duplicate_charge", "INCONCLUSIVE", "HUMAN_INVESTIGATION", 0.55,
                                  f"Evidence conflicts: {item.ref_id} suggests the matching charges may be intentional "
                                  f"(\"{m.group(0)}\"). A human should confirm before any credit.", base + [item.ref_id])
        if recon.outcome == "DUPLICATE_CANDIDATE" and recon.selected:
            s = recon.selected
            lines = [f"stripe:line:{s.original_line_id}", f"stripe:line:{s.duplicate_line_id}"]
            return decide("duplicate_charge", "VALID", "CREDIT", 0.95,
                          f"Invoice lines {lines[0]} and {lines[1]} match on {', '.join(s.matched_signals)}, and the "
                          f"customer ({p.source_email_ref}) disputes the second charge. The billing data supports a "
                          "duplicate charge.", base + lines)
        if recon.outcome == "INCONCLUSIVE":
            return decide("duplicate_charge", "INVALID", "HUMAN_INVESTIGATION", 0.8,
                          f"No two lines on {invoice_ref} match on amount, product, period and description, so the "
                          "charges appear distinct and billing data does not support the duplicate claim.", base)
        return decide("duplicate_charge", "INCONCLUSIVE", "HUMAN_INVESTIGATION", 0.6,
                      "Several possible duplicate groups exist; a human must identify the disputed charge.", base)


# --------------------------------------------------------------------------- Claude


def _fence(text: str) -> str:
    """Neutralise closing tags so untrusted text cannot escape its container."""
    return (text or "").replace("</", "<\\/")


class ClaudeReasoner:
    def __init__(self, model: str = "claude-opus-5", api_key: str | None = None, client=None) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0)
        self.model = model
        self.name = f"claude:{model}"

    def _parse(self, system: str, user: str, schema: type[BaseModel]):
        a = self._anthropic
        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                # Server-side refusal fallback; the policy engine still gates every action.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (a.RateLimitError, a.InternalServerError, a.APIConnectionError) as exc:
            raise SettleError(ErrorCode.LLM_ERROR, f"Claude unavailable: {type(exc).__name__}", retryable=True) from exc
        except a.APIStatusError as exc:
            raise SettleError(ErrorCode.LLM_ERROR, f"Claude API error {exc.status_code}") from exc
        if resp.stop_reason == "refusal":
            raise SettleError(ErrorCode.LLM_ERROR, "Model declined the request; routing to a human.")
        if resp.parsed_output is None:
            raise SettleError(ErrorCode.LLM_ERROR, f"No structured output (stop_reason={resp.stop_reason}).")
        return resp.parsed_output

    @traceable(run_type="llm", name="extract_claim")
    def extract_claim(self, email: EmailMessage) -> ClaimExtraction:
        user = (f"<untrusted_customer_email>\nFrom: {email.from_name or ''} <{email.from_email}>\n"
                f"Subject: {_fence(email.subject)}\n\n{_fence(email.body)}\n</untrusted_customer_email>")
        return self._parse(EXTRACTION_SYSTEM, user, ClaimExtraction)

    @traceable(run_type="llm", name="adjudicate_dispute")
    def adjudicate(self, p: AdjudicationPacket) -> AdjudicationDecision:
        trusted = "\n".join(f"- {r.ref_id} [{r.source}/{r.object_type}] {r.value_summary}"
                            for r in p.references if r.trusted)
        trusted_ctx = "\n".join(f"- {c.ref_id}: {c.text}" for c in p.context if c.trusted)
        thread = "\n\n".join(f'<message ref_id="{c.ref_id}">\n{_fence(c.text)}\n</message>'
                             for c in p.context if not c.trusted)
        claim = p.claim.model_dump(mode="json", exclude={"raw_text"})
        user = (
            f"<claim>\n{json.dumps(claim, indent=1)}\n</claim>\n"
            f"<deterministic_reconciliation>\n{p.reconciliation.model_dump_json(indent=1)}\n</deterministic_reconciliation>\n"
            f"<trusted_evidence>\n{trusted}\n{trusted_ctx}\n</trusted_evidence>\n"
            f'<untrusted_customer_email ref_id="{p.source_email_ref}">\nSubject: {_fence(p.source_email.subject)}\n\n'
            f"{_fence(p.source_email.body)}\n</untrusted_customer_email>\n"
            f"<untrusted_thread>\n{thread}\n</untrusted_thread>\n\nReturn the structured decision."
        )
        return self._parse(ADJUDICATION_SYSTEM, user, AdjudicationDecision)


def build_reasoner(settings) -> Reasoner:
    if settings.use_claude:
        return ClaudeReasoner(model=settings.claude_model, api_key=settings.anthropic_api_key)
    return RuleBasedReasoner()
