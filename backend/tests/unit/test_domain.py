import pytest

from app.domain.actions import build_credit_action, compute_action_hash, recompute_hash
from app.domain.errors import ErrorCode, InvariantViolation
from app.domain.models import Claim, CreditNoteSnapshot, InvoiceLine, InvoiceSnapshot
from app.domain.money import find_money, format_money, parse_money
from app.domain.reconciliation import reconcile
from app.domain.states import DisputeStatus as S
from app.domain.states import assert_transition, is_legal


# ---------------------------------------------------------------- money
@pytest.mark.parametrize(
    "text,minor,cur",
    [
        ("$6,000", 600_000, "usd"),
        ("the $6,000 API overage", 600_000, "usd"),
        ("€6,000.50", 600_050, "eur"),
        ("USD 6000", 600_000, "usd"),
        ("6,000 USD", 600_000, "usd"),
        ("$6k", 600_000, "usd"),
        ("¥5000", 5000, "jpy"),
    ],
)
def test_parse_money(text, minor, cur):
    m = parse_money(text)
    assert m is not None and (m.minor, m.currency) == (minor, cur)


def test_parse_money_ignores_bare_numbers_and_invoice_ids():
    assert find_money("invoice INV-10428 from 2026") == []
    assert parse_money(None) is None


def test_format_money():
    assert format_money(600_000, "usd") == "$6,000.00 USD"
    assert format_money(3_600_000, "eur") == "€36,000.00 EUR"


# ---------------------------------------------------------------- action hash
def test_action_hash_is_order_insensitive_for_lines_and_sensitive_to_amount():
    base = dict(invoice_id="in_1", customer_id="cus_1", amount_minor=600_000, currency="usd", reason="duplicate")
    h1 = compute_action_hash(**base, stripe_line_item_ids=["b", "a"])
    h2 = compute_action_hash(**base, stripe_line_item_ids=["a", "b"])
    h3 = compute_action_hash(**{**base, "amount_minor": 600_001}, stripe_line_item_ids=["a", "b"])
    assert h1 == h2 and h1 != h3


def test_action_identity_is_deterministic_and_wording_excluded():
    kw = dict(invoice_id="in_1", invoice_number="INV-1", customer_id="cus_1", amount_minor=600_000, currency="USD",
              reason="duplicate", stripe_line_item_ids=["il_2"])
    a1 = build_credit_action(**kw, customer_message_draft="hello")
    a2 = build_credit_action(**kw, customer_message_draft="different wording")
    assert a1.action_hash == a2.action_hash and a1.action_id == a2.action_id
    assert a1.idempotency_key == f"settle:{a1.action_id}:credit"
    assert recompute_hash(a1) == a1.action_hash
    tampered = a1.model_copy(update={"amount_minor": 4_200_000})
    assert recompute_hash(tampered) != tampered.action_hash


# ---------------------------------------------------------------- state machine
def test_legal_and_illegal_transitions():
    assert is_legal(S.AWAITING_APPROVAL, S.APPROVED)
    assert_transition(S.POLICY_REVIEW, S.AWAITING_APPROVAL)
    for frm, to in [(S.POLICY_REVIEW, S.EXECUTING), (S.AWAITING_APPROVAL, S.EXECUTING), (S.EXECUTING, S.RESOLVED),
                    (S.ADJUDICATING, S.APPROVED), (S.RECEIVED, S.RESOLVED)]:
        with pytest.raises(InvariantViolation) as exc:
            assert_transition(frm, to)
        assert exc.value.code == ErrorCode.UNEXPECTED_STATE_TRANSITION


# ---------------------------------------------------------------- reconciliation
def _line(lid, amount, price="price_api", period=(1, 2), desc="API overage - Aug 2026"):
    return InvoiceLine(id=lid, amount_minor=amount, currency="usd", description=desc, price_id=price,
                       period_start=period[0], period_end=period[1])


def _invoice(lines, remaining=None):
    total = sum(ln.amount_minor for ln in lines)
    return InvoiceSnapshot(id="in_1", number="INV-1", customer_id="cus_1", currency="usd", status="open",
                           total_minor=total, amount_due_minor=total, amount_paid_minor=0,
                           amount_remaining_minor=total if remaining is None else remaining, lines=lines)


def _claim(amount=600_000, dtype="duplicate_charge"):
    return Claim(raw_text="x", dispute_type=dtype, disputed_amount_minor=amount, currency="usd", invoice_hint="INV-1",
                 extracted_by="test")


def test_reconcile_single_duplicate_uses_duplicated_line_amount():
    inv = _invoice([_line("il_base", 3_000_000, price="price_base", desc="Platform"), _line("il_a", 600_000), _line("il_b", 600_000)])
    r = reconcile(_claim(), inv, [])
    assert r.outcome == "DUPLICATE_CANDIDATE"
    assert r.selected.original_line_id == "il_a" and r.selected.duplicate_line_id == "il_b"
    assert r.credit_amount_minor == 600_000 and r.claim_amount_matches is True


def test_reconcile_distinct_periods_is_inconclusive():
    inv = _invoice([_line("il_a", 600_000, period=(1, 2)), _line("il_b", 600_000, period=(2, 3))])
    r = reconcile(_claim(), inv, [])
    assert r.outcome == "INCONCLUSIVE" and r.credit_amount_minor is None


def test_reconcile_detects_existing_credit():
    inv = _invoice([_line("il_a", 600_000), _line("il_b", 600_000)])
    cn = CreditNoteSnapshot(id="cn_1", invoice_id="in_1", amount_minor=600_000, currency="usd", status="issued",
                            reason="duplicate", line_item_ids=["il_b"])
    r = reconcile(_claim(), inv, [cn])
    assert r.already_credited and r.credit_amount_minor is None and r.existing_credit_note_ids == ["cn_1"]


def test_reconcile_ambiguous_groups_disambiguated_by_claim_amount():
    inv = _invoice([_line("il_a", 600_000), _line("il_b", 600_000),
                    _line("il_c", 90_000, price="price_sms", desc="SMS"), _line("il_d", 90_000, price="price_sms", desc="SMS")])
    assert reconcile(_claim(amount=None), inv, []).outcome == "AMBIGUOUS"
    r = reconcile(_claim(amount=90_000), inv, [])
    assert r.outcome == "DUPLICATE_CANDIDATE" and r.selected.duplicate_line_id == "il_d"


def test_reconcile_non_duplicate_types_have_no_deterministic_amount():
    inv = _invoice([_line("il_a", 600_000)])
    r = reconcile(_claim(dtype="incorrect_quantity"), inv, [])
    assert r.outcome == "NOT_APPLICABLE" and r.credit_amount_minor is None
