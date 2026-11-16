"""Client for the Roosterly helpdesk API.

Error policy (see README, Reliability):
  503  temporary, nothing was saved      -> retry with exponential backoff + jitter
  429  more than 5 calls a second        -> wait Retry-After, retry; we also pace ourselves below the limit
  409  ticket already handled            -> never retry; raise AlreadyHandled so the caller records it
  timeout / connection error on a POST   -> we do not know if it was saved, so we look the ticket up first
  other 4xx                              -> our bug; raise, do not retry
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

import httpx

ACTIONS = ("reply", "handoff", "close")


class HelpdeskError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class AlreadyHandled(HelpdeskError):
    """409: the ticket was already handled. A customer must never get two answers."""


class AmbiguousWrite(HelpdeskError):
    """A POST failed in transit: it may or may not have been saved."""


class _Pacer:
    """Keeps at least `interval` seconds between requests (one process, any thread)."""

    def __init__(self, max_per_second: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.interval = 1.0 / max_per_second
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class HelpdeskClient:
    def __init__(
        self,
        base_url: str,
        key: str,
        email: str,
        name: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        max_per_second: float = 4.0,      # the limit is 5/s; stay safely under it
        max_attempts: int = 8,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        headers = {"Authorization": f"Bearer {key}", "X-Candidate-Email": email}
        if name:
            headers["X-Candidate-Name"] = name
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport)
        self._pacer = _Pacer(max_per_second, clock, sleep)
        self._sleep = sleep
        self.max_attempts = max_attempts
        self.stats = {"calls": 0, "503": 0, "429": 0, "409": 0, "network_errors": 0}

    # ---- low level -------------------------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        return min(8.0, 0.5 * 2 ** attempt) * (0.5 + random.random() / 2)

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        last_error = "no attempt made"
        for attempt in range(self.max_attempts):
            self._pacer.wait()
            self.stats["calls"] += 1
            try:
                resp = self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:  # timeouts, connection resets
                self.stats["network_errors"] += 1
                if method != "GET":
                    raise AmbiguousWrite(f"{method} {path}: {type(exc).__name__}") from exc
                last_error = type(exc).__name__
                self._sleep(self._backoff(attempt))
                continue

            if resp.status_code < 300:
                return resp.json() if resp.content else {}
            body = _safe_json(resp)
            code = body.get("error") if isinstance(body, dict) else None
            if resp.status_code == 503:
                self.stats["503"] += 1
                last_error = "503 service_unavailable"
                self._sleep(self._backoff(attempt))
                continue
            if resp.status_code == 429:
                self.stats["429"] += 1
                last_error = "429 rate_limited"
                self._sleep(_retry_after(resp, default=self._backoff(attempt)))
                continue
            if resp.status_code == 409:
                self.stats["409"] += 1
                raise AlreadyHandled(f"{method} {path}: already handled", 409, body)
            raise HelpdeskError(f"{method} {path}: HTTP {resp.status_code} {code or ''}".strip(),
                                resp.status_code, body)
        raise HelpdeskError(f"{method} {path}: gave up after {self.max_attempts} attempts ({last_error})")

    # ---- endpoints -------------------------------------------------------------------------
    def status(self) -> dict:
        return self._request("GET", "/status")

    def examples(self) -> list[dict]:
        data = self._request("GET", "/examples")
        return data.get("examples", data) if isinstance(data, dict) else data

    def get_ticket(self, ticket_id: str) -> dict:
        return self._request("GET", f"/tickets/{ticket_id}")

    def open_tickets(self) -> list[dict]:
        """All open tickets. We collect every page first, then work, so handling tickets cannot
        shift the pages under us."""
        tickets, cursor = [], None
        while True:
            params = {"limit": 10, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            page = self._request("GET", "/tickets", params=params)
            tickets.extend(page.get("tickets", []))
            cursor = page.get("next_cursor")
            if not cursor:
                return tickets

    def reset_practice(self) -> dict:
        return self._request("POST", "/practice/reset")

    def submit(self, ticket_id: str, action: str, payload: dict) -> dict:
        """Send one decision. Safe against lost responses: if a POST times out we check the ticket
        before trying again, so a retry can never produce a second answer."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        path = f"/tickets/{ticket_id}/{action}"
        for _ in range(3):
            try:
                return self._request("POST", path, json=payload)
            except AmbiguousWrite:
                if is_handled(self.get_ticket(ticket_id)):
                    raise AlreadyHandled(f"{path}: saved before the connection dropped", 409)
        raise HelpdeskError(f"{path}: connection kept failing")

    def close(self) -> None:
        self._http.close()


def is_handled(ticket: dict) -> bool:
    """GET /tickets/{id} returns the ticket 'plus how it was handled (if it was)'."""
    if not isinstance(ticket, dict):
        return False
    inner = ticket.get("ticket") if isinstance(ticket.get("ticket"), dict) else ticket
    status = str(inner.get("status") or ticket.get("status") or "").lower()
    if status and status != "open":
        return True
    return bool(ticket.get("handled") or ticket.get("handling") or ticket.get("decision")
                or inner.get("handled") or inner.get("decision"))


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "")))
    except ValueError:
        return default
