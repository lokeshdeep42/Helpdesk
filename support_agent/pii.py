"""Payment details (IBANs, card numbers) must never reach a reply or a log.

We redact them from ticket text before it goes to the model, and scan everything we send or log.
Deterministic code, not the model, enforces this rule.
"""
from __future__ import annotations

import re

# Two letters, two check digits, then groups of four letters/digits (spaces optional), and a short tail.
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:[ .-]?[A-Z0-9]{4}){2,7}(?:[ .-]?[A-Z0-9]{1,3})?)\b", re.IGNORECASE)
# 13-19 digits, optionally separated by single spaces, dots or dashes (card numbers).
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ .-]?){12,18}\d(?!\d)")


def _looks_like_iban(raw: str) -> bool:
    # A real IBAN contains many digits; this avoids matching ordinary words. No checksum required:
    # an IBAN with a typo is still payment data.
    return sum(c.isdigit() for c in raw) >= 8


def redact(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, kinds_found)."""
    if not text:
        return text or "", []
    found: list[str] = []

    def iban_sub(m: re.Match) -> str:
        if _looks_like_iban(m.group(1)):
            found.append("iban")
            return "[IBAN REDACTED]"
        return m.group(0)

    def card_sub(m: re.Match) -> str:
        found.append("card")
        return "[CARD NUMBER REDACTED]"

    text = _IBAN_RE.sub(iban_sub, text)
    text = _CARD_RE.sub(card_sub, text)
    return text, sorted(set(found))


def contains_payment_details(text: str) -> bool:
    return bool(redact(text or "")[1])
