"""Deterministic money parsing/formatting in integer minor units.

The LLM returns amounts exactly as written ("$6,000"); this module is the only
place that turns text into numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO_DECIMAL_CURRENCIES = frozenset(
    {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
)
_SYMBOLS = {"$": "usd", "€": "eur", "£": "gbp", "¥": "jpy"}
_DISPLAY_SYMBOLS = {"usd": "$", "eur": "€", "gbp": "£"}
_CODE = r"(?:USD|EUR|GBP|CAD|AUD|INR|JPY|CHF|SGD)"
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MONEY_RE = re.compile(
    rf"(?:(?P<code1>{_CODE})\s?)?(?P<sym>[$€£¥])\s?(?P<num1>{_NUM})(?P<k1>[kK]\b)?"
    rf"|\b(?P<code2>{_CODE})\s?(?P<num2>{_NUM})(?P<k2>[kK]\b)?"
    rf"|\b(?P<num3>{_NUM})(?P<k3>[kK])?\s?(?P<code3>{_CODE})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Money:
    minor: int
    currency: str
    text: str


def minor_exponent(currency: str) -> int:
    return 0 if currency.lower() in ZERO_DECIMAL_CURRENCIES else 2


def to_minor(amount: Decimal, currency: str) -> int:
    scaled = amount * (Decimal(10) ** minor_exponent(currency))
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_major(minor: int, currency: str) -> Decimal:
    exp = minor_exponent(currency)
    return (Decimal(minor) / (Decimal(10) ** exp)).quantize(Decimal(1).scaleb(-exp))


def find_money(text: str) -> list[Money]:
    """All explicit money mentions (requires a currency symbol or ISO code)."""
    found: list[Money] = []
    for m in _MONEY_RE.finditer(text or ""):
        g = m.groupdict()
        num = g["num1"] or g["num2"] or g["num3"]
        code = g["code1"] or g["code2"] or g["code3"]
        currency = (code or _SYMBOLS.get(g["sym"] or "", "")).lower()
        if not num or not currency:
            continue
        try:
            value = Decimal(num.replace(",", ""))
        except InvalidOperation:
            continue
        if g["k1"] or g["k2"] or g["k3"]:
            value *= 1000
        found.append(Money(minor=to_minor(value, currency), currency=currency, text=m.group(0).strip()))
    return found


def parse_money(text: str | None) -> Money | None:
    if not text:
        return None
    found = find_money(text)
    return found[0] if found else None


def format_money(minor: int, currency: str) -> str:
    cur = currency.lower()
    exp = minor_exponent(cur)
    major = to_major(minor, cur)
    body = f"{major:,.{exp}f}"
    sym = _DISPLAY_SYMBOLS.get(cur)
    return f"{sym}{body} {cur.upper()}" if sym else f"{body} {cur.upper()}"
