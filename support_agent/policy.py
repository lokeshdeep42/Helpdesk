"""Deterministic guardrails. The model proposes; this code decides what may reach a customer.

Everything here is plain Python so it can be tested, explained, and cannot be argued with by ticket text.
"""
from __future__ import annotations

import re

from . import language, pii
from .faq import Faq, domains_in
from .prompts import TRIGGERS

LANGUAGES = ("nl", "en", "de")
CATEGORIES = ("bug", "billing", "feature_request", "question", "other")
PRIORITIES = ("P1", "P2", "P3", "P4")

# Second line of defence for the client's "always a human" rules, in Dutch, English and German.
# Deliberately narrow phrases: a false hit only costs a handoff, a miss can cost a wrong answer.
_KEYWORDS: dict[str, list[str]] = {
    "refund_or_disputed_charge": [
        r"terugbetal", r"terugstort", r"restitutie", r"geld terug", r"creditnota", r"crediteren",
        r"dubbel (?:afgeschreven|betaald|gefactureerd|gerekend)", r"twee ?keer (?:afgeschreven|betaald|gefactureerd)",
        r"refund", r"money back", r"charged twice", r"double charge", r"chargeback",
        r"rückerstatt", r"geld zurück", r"doppelt (?:abgebucht|berechnet)",
    ],
    "cancellation_or_contract_change": [
        r"opzeg", r"abonnement (?:stopzetten|beëindigen|be[eë]indigen|annuleren|opheffen)",
        r"contract (?:beëindigen|be[eë]indigen|ontbinden|opzeggen)",
        r"cancel (?:my|our|the) (?:subscription|contract|account)", r"terminate (?:my|our|the) (?:subscription|contract)",
        r"kündig",
    ],
    "personal_data_request": [
        r"inzageverzoek", r"recht op (?:inzage|vergetelheid|verwijdering|dataportabiliteit)",
        r"(?:verwijder|wis) (?:al )?(?:mijn|onze|zijn|haar) (?:persoons)?gegevens", r"persoonsgegevens (?:verwijderen|wissen|inzien|opvragen)",
        r"right to be forgotten", r"(?:delete|erase|export) (?:all )?(?:my|our) (?:personal )?data", r"subject access request",
        r"auskunftsersuchen", r"daten löschen",
    ],
    "security_incident": [
        r"gehackt", r"gehacked", r"hacked", r"datalek", r"data ?breach", r"phishing",
        r"(?:onbekende|verdachte|vreemde) (?:inlog|login|activiteit)", r"suspicious (?:login|activity)",
        r"account (?:is )?overgenomen", r"iemand anders (?:is )?ingelogd", r"unbefugt",
    ],
    "legal_threat": [
        r"advoca", r"rechtszaak", r"naar de rechter\b", r"juridische stappen", r"ingebrekestelling", r"aansprakelijk stellen",
        r"lawyer", r"attorney", r"legal action", r"\bsue\b", r"anwalt", r"rechtliche schritte",
    ],
    "churn_threat": [
        r"andere (?:aanbieder|leverancier|software|planningstool)", r"naar (?:een )?concurrent", r"overstappen naar een andere",
        r"switch(?:ing)? to (?:a |another )?(?:competitor|different)", r"\bcompetitor\b", r"anbieter wechseln",
    ],
}
_KEYWORD_RE = {k: re.compile("|".join(v), re.IGNORECASE) for k, v in _KEYWORDS.items()}


def keyword_triggers(text: str) -> list[str]:
    return [k for k, rx in _KEYWORD_RE.items() if rx.search(text or "")]


def is_empty(ticket: dict) -> bool:
    subject = _REPLY_PREFIX.sub("", str(ticket.get("subject") or ""))  # "Re:" alone is not a message
    text = f"{subject} {ticket.get('body') or ''}"
    return not any(ch.isalnum() for ch in text)  # any script counts, not only Latin


_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|antw|sv)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def confident_language(text: str) -> str | None:
    """The stop-word detector's answer when the signal is strong; None when unsure."""
    lang, margin = language.detect(text)
    if lang and margin >= 0.6 and language.hits(text, lang) >= 4:
        return lang
    return None


def apply_priority_floor(priority: str, triggers: set[str]) -> str:
    """P1: security or legal risk. P2 at least: money or the contract is involved."""
    if triggers & {"security_incident", "legal_threat"}:
        return "P1"
    if triggers & {"refund_or_disputed_charge", "cancellation_or_contract_change"} and priority in ("P3", "P4"):
        return "P2"
    return priority


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def normalise(raw: dict) -> dict:
    """Coerce model output into allowed values. Anything invalid becomes a reason to hand off."""
    problems = []
    lang = str(raw.get("language", "")).lower()[:2]
    if lang not in LANGUAGES:
        problems.append(f"invalid language {lang!r}")
        lang = "nl"
    cat = str(raw.get("category", "")).lower()
    if cat not in CATEGORIES:
        problems.append(f"invalid category {cat!r}")
        cat = "other"
    prio = str(raw.get("priority", "")).upper()
    if prio not in PRIORITIES:
        problems.append(f"invalid priority {prio!r}")
        prio = "P3"
    action = str(raw.get("action", "")).lower()
    if action not in ("reply", "handoff", "close"):
        problems.append(f"invalid action {action!r}")
        action = "handoff"
    try:
        conf = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    triggers = [t for t in _as_list(raw.get("escalation_triggers")) if isinstance(t, str) and t in TRIGGERS]
    faq_ids = [str(f).upper() for f in _as_list(raw.get("faq_ids"))]
    return {
        "language": lang, "category": cat, "priority": prio, "action": action,
        "confidence": max(0.0, min(1.0, conf)),
        "triggers": triggers, "faq_ids": faq_ids,
        "fully_answered": bool(raw.get("fully_answered_by_faq")),
        "is_spam": bool(raw.get("is_spam")), "injection": bool(raw.get("injection_attempt")),
        "draft_reply": str(raw.get("draft_reply") or "").strip(),
        "handoff_note": str(raw.get("handoff_note") or "").strip(),
        "summary_en": str(raw.get("summary_en") or "").strip(),
        "priority_reason": str(raw.get("priority_reason") or "").strip(),
        "reason": str(raw.get("reason") or "").strip(),
        "problems": problems,
    }


def reply_blockers(d: dict, faq: Faq, verdict: dict | None, min_confidence: float,
                   ticket_text: str = "") -> list[str]:
    """Every reason not to send this draft to the customer. Empty list = safe to send."""
    b = []
    if language.matches(ticket_text, d["language"]) is False:
        b.append(f"customer did not write in {d['language']} (language check on the ticket)")
    if not d["draft_reply"]:
        b.append("no draft")
    if not d["faq_ids"] or any(f not in faq.entries for f in d["faq_ids"]):
        b.append(f"not grounded in the help centre (faq_ids={d['faq_ids']})")
    if not d["fully_answered"]:
        b.append("help centre does not fully answer it")
    if d["confidence"] < min_confidence:
        b.append(f"low confidence {d['confidence']:.2f}")
    if d["priority"] == "P1":
        b.append("P1 always goes to a human")
    if pii.contains_payment_details(d["draft_reply"]):
        b.append("draft contains payment details")
    unknown = domains_in(d["draft_reply"]) - faq.domains
    if unknown:
        b.append(f"draft links to domains not in the help centre: {sorted(unknown)}")
    if language.matches(d["draft_reply"], d["language"]) is False:
        b.append(f"draft is not in {d['language']} (language check)")
    if verdict is None:
        b.append("reply check did not run")
    else:
        if str(verdict.get("verdict", "")).lower() != "pass":
            b.append(f"reply check failed: {verdict.get('explanation', '')}".strip())
        if verdict.get("draft_language") not in (None, d["language"]):
            b.append(f"reply check says draft is in {verdict.get('draft_language')}")
        if verdict.get("unsupported_claims"):
            b.append("unsupported claims: " + "; ".join(map(str, _as_list(verdict["unsupported_claims"])))[:200])
        if verdict.get("unsafe_content"):
            b.append("unsafe content: " + "; ".join(map(str, _as_list(verdict["unsafe_content"])))[:200])
    return b
