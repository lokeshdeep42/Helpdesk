"""A tiny stop-word language check for nl / en / de.

The model decides the ticket language. This check is an independent second opinion that a draft reply
really is in that language. It needs no model and no Dutch knowledge, so ticket text cannot talk it
into a wrong answer.
"""
from __future__ import annotations

import re

# Only words that are common in one language and rare in the other two.
_STOPWORDS = {
    "nl": {"het", "een", "ik", "je", "jij", "niet", "dat", "voor", "met", "zijn", "maar", "ook", "kan", "hoe",
           "wat", "bij", "naar", "nog", "wij", "ons", "onze", "jullie", "graag", "bedankt", "hoi", "uw", "heb",
           "hebben", "wordt", "geen", "deze", "dit", "u", "kunt", "via", "onder", "klik", "op", "wilt", "groet",
           "groeten", "vriendelijke", "dank", "om", "zo", "mijn", "worden", "gaat", "waar", "goedemorgen"},
    "en": {"the", "and", "to", "of", "you", "it", "not", "for", "with", "our", "can", "how", "what", "please",
           "thanks", "your", "have", "this", "that", "are", "do", "my", "will", "be", "under", "click", "regards",
           "kind", "would", "if", "there", "they", "from", "on", "at", "go"},
    "de": {"der", "das", "und", "ist", "ich", "nicht", "wir", "sie", "ein", "eine", "mit", "für", "auf", "zu",
           "den", "dem", "es", "wie", "kann", "bitte", "danke", "unser", "unsere", "haben", "wird", "kein", "auch",
           "ihr", "ihre", "können", "unter", "klicken", "grüße", "freundlichen", "sich", "nach", "oder", "gibt"},
}
_WORD = re.compile(r"[a-zA-Zäöüßéèëïó]+")


def detect(text: str) -> tuple[str | None, float]:
    """Return (language, margin). language is None when the text gives too little signal."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if not words:
        return None, 0.0
    scores = {lang: sum(w in sw for w in words) for lang, sw in _STOPWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < 2:
        return None, 0.0
    return best, (top - second) / max(top, 1)


def matches(text: str, expected: str) -> bool | None:
    """True/False when the check is confident, None when the text is too short to tell."""
    lang, margin = detect(text)
    if lang is None or margin < 0.2:
        return None
    return lang == expected
