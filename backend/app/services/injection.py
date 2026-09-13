"""Deterministic prompt-injection signal scanner for untrusted customer email (PRD §20).

Signals never change policy; they only make the policy engine fail closed
(NO_PROMPT_INJECTION_SIGNALS) and route the case to a human.
"""

from __future__ import annotations

import re

_PATTERNS: dict[str, list[str]] = {
    "instruction_override": [
        r"\bignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?)",
        r"\bdisregard\s+(the\s+|all\s+|your\s+|any\s+)?(previous\s+|prior\s+)?(instructions|rules|polic(y|ies))",
        r"\byou\s+are\s+now\b",
        r"\bnew\s+instructions\s*:",
        r"\bsystem\s*:\s",
    ],
    "claimed_approval": [
        r"\b(already|pre-?)\s*approved\b",
        r"\bapproval\s+(has\s+been|was|is)\s+(granted|given|obtained|done)\b",
        r"\b(finance|cfo|controller|manager|director)\b[^.\n]{0,40}\b(approved|signed\s+off|authori[sz]ed)\b",
    ],
    "approval_bypass": [
        r"\b(skip|bypass|override|ignore|disable)\b[^.\n]{0,30}\b(approval|polic(y|ies)|limits?|verification|review|checks?)\b",
    ],
    "invoice_redirect": [
        r"\buse\s+invoice\s+\S+\s+instead\b",
        r"\binstead\s+of\s+invoice\b",
        r"\b(apply|issue|put)\b[^.\n]{0,40}\b(to|against|on)\s+invoice\s+[A-Z]{2,5}-\d+\s+instead\b",
    ],
    "instruction_disclosure": [
        r"\b(reveal|show|print|disclose|repeat|output|tell\s+me)\b[^.\n]{0,40}\b(system\s+prompt|your\s+instructions|internal\s+(rules|polic(y|ies)|instructions)|prompt)\b",
    ],
}
_COMPILED = {name: [re.compile(p, re.IGNORECASE) for p in pats] for name, pats in _PATTERNS.items()}


def scan_for_injection(text: str) -> list[str]:
    return sorted(name for name, pats in _COMPILED.items() if any(p.search(text or "") for p in pats))
