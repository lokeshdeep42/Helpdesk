"""A small crash-safe journal (state/journal.json), written atomically after every step.

The helpdesk's list of open tickets is the source of truth for what still needs an answer.
The journal adds two things the helpdesk cannot know:
  * a decision we already paid a model for (so a re-run after a crash sends the same answer), and
  * that we were in the middle of sending (so after a crash we look the ticket up before sending again).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def content_hash(ticket: dict) -> str:
    raw = json.dumps([ticket.get("subject"), ticket.get("body")], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except ValueError:
                # Unreadable journal: start empty. Still safe, because the helpdesk only lists open
                # tickets and answers a second attempt with 409.
                path.replace(path.with_suffix(".corrupt"))
                self._data = {}

    def get(self, ticket_id: str) -> dict | None:
        return self._data.get(ticket_id)

    def drop(self, ticket_id: str) -> None:
        self._data.pop(ticket_id, None)
        self._save()

    def put(self, ticket_id: str, **fields) -> None:
        entry = self._data.setdefault(ticket_id, {})
        entry.update(fields, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._save()

    def clear(self) -> None:
        self._data = {}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash never leaves half a file
