"""Loads the help centre (the only knowledge the agent may answer from)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import FAQ_PATH

_HEADING = re.compile(r"^## (F\d{2}) · (.+)$", re.MULTILINE)
_DOMAIN = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:nl|com|eu|de|net|org|io))\b", re.IGNORECASE)


@dataclass(frozen=True)
class Faq:
    text: str                      # the full file, given to the model
    entries: dict[str, str]        # "F02" -> "Adding a new employee\n<body>"
    domains: frozenset[str]        # domains that may appear in a reply


def load_faq(path: Path = FAQ_PATH) -> Faq:
    text = path.read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    matches = list(_HEADING.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else text.find("\n---", m.end())
        entries[m.group(1)] = f"{m.group(2)}\n{text[m.end():end].strip()}"
    domains = frozenset(d.lower() for d in _DOMAIN.findall(text))
    return Faq(text=text, entries=entries, domains=domains)


def domains_in(text: str) -> set[str]:
    return {d.lower() for d in _DOMAIN.findall(text or "")}
