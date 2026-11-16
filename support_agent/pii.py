"""Payment details (IBANs, card numbers) must never reach a reply or a log.

We redact them from ticket text before it goes to the model, and scan everything we send or log.
Deterministic code, not the model, enforces this rule.
"""
from __future__ import annotations

import re

# Two letters, two check digits, then groups of four letters/digits (spaces optional), and a short tail.
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)\b", re.IGNORECASE)
# 13-19 digits, optionally separated by single spaces or dashes (card numbers).
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IBAN_COUNTRIES = {"NL", "BE", "DE", "FR", "GB", "LU", "ES", "IT", "AT", "IE", "PT", "PL", "DK", "SE", "FI", "CH"}


def _iban_valid(raw: str) -> bool:
    s = re.sub(r"\s", "", raw).upper()
    if not (15 <= len(s) <= 34) or not s.isalnum():
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def _looks_like_iban(raw: str) -> bool:
    s = re.sub(r"\s", "", raw).upper()
    # A real IBAN contains many digits; this avoids matching ordinary words.
    if sum(c.isdigit() for c in s) < 8:
        return False
    return _iban_valid(s) or s[:2] in _IBAN_COUNTRIES


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
