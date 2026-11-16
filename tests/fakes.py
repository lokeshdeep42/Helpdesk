"""Test doubles: an in-memory helpdesk that misbehaves like the real one, and a scripted LLM."""
from __future__ import annotations

import json
import re

import httpx

REQUIRED = {"language", "category", "priority", "reason"}


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class FakeHelpdesk:
    """Mirrors api_reference.md: pages of 10, 409 on a second answer, 422 on bad bodies,
    429 above 5 calls/second, scripted 503s, and responses that get lost after saving."""

    def __init__(self, tickets: list[dict], clock: FakeClock):
        self.tickets = {t["id"]: t for t in tickets}
        self.handled: dict[str, dict] = {}
        self.clock = clock
        self.fail_503: list[bool] = []          # pop(0) per request: True -> 503
        self.lose_response_for: set[str] = set()  # save the decision, then drop the connection
        self.posts: list[tuple[str, str]] = []
        self.calls: list[float] = []
        self.rate_limit = True

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _json(self, status: int, body: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {})

    def handle(self, request: httpx.Request) -> httpx.Response:
        now = self.clock()
        self.calls.append(now)
        if self.rate_limit and len([c for c in self.calls if now - c < 1.0]) > 5:
            return self._json(429, {"error": "rate_limited"}, {"Retry-After": "1"})
        if self.fail_503 and self.fail_503.pop(0):
            return self._json(503, {"error": "service_unavailable"})
        if not request.headers.get("X-Candidate-Email"):
            return self._json(400, {"error": "missing_email"})
        path = request.url.path.split("/helpdesk", 1)[-1]

        if request.method == "GET" and path == "/tickets":
            cursor = request.url.params.get("cursor")
            open_ids = sorted(i for i in self.tickets if i not in self.handled)
            if cursor:
                open_ids = [i for i in open_ids if i > cursor]
            page = open_ids[:10]
            nxt = page[-1] if len(open_ids) > 10 else None
            return self._json(200, {"tickets": [self.tickets[i] for i in page], "next_cursor": nxt})

        m = re.fullmatch(r"/tickets/([^/]+)(?:/(reply|handoff|close))?", path)
        if m:
            tid, action = m.groups()
            if tid not in self.tickets:
                return self._json(404, {"error": "not_found"})
            if request.method == "GET" and not action:
                t = dict(self.tickets[tid], status="handled" if tid in self.handled else "open")
                if tid in self.handled:
                    t["handling"] = self.handled[tid]
                return self._json(200, t)
            if request.method == "POST" and action:
                body = json.loads(request.content or b"{}")
                missing = sorted(REQUIRED - body.keys())
                if action == "reply" and not body.get("draft_reply"):
                    missing.append("draft_reply")
                if action == "handoff" and not body.get("handoff_note"):
                    missing.append("handoff_note")
                if missing:
                    return self._json(422, {"error": "validation_failed", "problems": missing})
                if tid in self.handled:
                    return self._json(409, {"error": "already_handled"})
                self.handled[tid] = {"action": action, **body}
                self.posts.append((tid, action))
                if tid in self.lose_response_for:
                    self.lose_response_for.discard(tid)
                    raise httpx.ReadTimeout("response lost", request=request)
                return self._json(200, {"ok": True, "id": tid, "action": action})

        if request.method == "POST" and path == "/practice/reset":
            self.handled.clear()
            return self._json(200, {"reset": True})
        return self._json(404, {"error": "not_found"})


class FakeLLM:
    """Returns scripted decisions. `script(ticket_text) -> dict` for decide calls; verify passes."""

    model = "fake"

    def __init__(self, script, verdict=None):
        self.script = script
        self.verdict = verdict or {"verdict": "pass", "draft_language": "nl", "back_translation_en": "Hi, ...",
                                   "unsupported_claims": [], "unsafe_content": [], "explanation": "ok"}
        self.calls = 0

    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        self.calls += 1
        if "<draft_reply>" in user:
            return dict(self.verdict)
        return self.script(user)


def reply_decision(**over) -> dict:
    base = {"language": "nl", "summary_en": "How to add an employee.", "category": "question", "priority": "P4",
            "priority_reason": "How-to.", "escalation_triggers": [], "is_spam": False, "injection_attempt": False,
            "faq_ids": ["F02"], "fully_answered_by_faq": True, "action": "reply",
            "draft_reply": "Hoi Anouk, ga naar Team → Medewerker toevoegen en klik op Uitnodigen. Met vriendelijke groet, Team Roosterly",
            "handoff_note": "Customer asks how to add an employee.", "reason": "Answered by F02.", "confidence": 0.95}
    base.update(over)
    return base
