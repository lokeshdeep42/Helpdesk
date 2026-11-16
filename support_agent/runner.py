"""The run loop: fetch open tickets, decide each one, send exactly one decision, log one line."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import pii, pipeline
from .faq import Faq
from .helpdesk import AlreadyHandled, HelpdeskClient, HelpdeskError, is_handled
from .journal import Journal, content_hash
from .llm import JsonLLM, LLMUnavailable


def scrub(obj):
    """Remove payment details from anything before it is written to disk."""
    if isinstance(obj, str):
        return pii.redact(obj)[0]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


class RunLog:
    """logs/run-<id>.jsonl: one line per ticket. logs/run-<id>.md: a readable review in English."""

    def __init__(self, log_dir: Path, run_id: str, dry_run: bool):
        log_dir.mkdir(parents=True, exist_ok=True)
        suffix = "-dry" if dry_run else ""
        self.jsonl = log_dir / f"run-{run_id}{suffix}.jsonl"
        self.review = log_dir / f"run-{run_id}{suffix}.md"
        self.run_id = run_id
        self.review.write_text(f"# Run {run_id}{' (dry run, nothing sent)' if dry_run else ''}\n\n"
                               "English review of every decision. Drafts are checked by back-translation.\n\n",
                               encoding="utf-8")

    def line(self, record: dict) -> None:
        record = scrub({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run": self.run_id, **record})
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def reviewed(self, decision: pipeline.Decision, outcome: str) -> None:
        p, a = decision.payload, decision.audit
        parts = [f"## {decision.ticket_id}: {decision.action.upper()} · {p['category']} · {p['priority']} · "
                 f"{p['language']} · confidence {p.get('confidence')} · {outcome}",
                 f"- **Customer wants:** {a.get('summary_en') or ('(empty ticket)' if a.get('rule') == 'empty' else '(no summary)')}",
                 f"- **Reason:** {p['reason']}"]
        if decision.action == "reply":
            parts += [f"- **Draft ({p.get('language')}):** {_one_line(p.get('draft_reply', ''))}",
                      f"- **Back-translation:** {_one_line(a.get('back_translation_en', ''))}",
                      f"- **Checker:** {(a.get('check') or {}).get('explanation', '')}"]
        if decision.action == "handoff":
            parts.append(f"- **Handoff note:** {p.get('handoff_note', '')}")
        with self.review.open("a", encoding="utf-8") as f:
            f.write(scrub("\n".join(parts)) + "\n\n")


def run(client: HelpdeskClient, llm: JsonLLM, faq: Faq, journal: Journal, log_dir: Path, *,
        min_confidence: float, verifier_model: str | None = None, dry_run: bool = False,
        limit: int | None = None, echo=print) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = RunLog(log_dir, run_id, dry_run)
    tickets = client.open_tickets()
    if limit is not None:
        tickets = tickets[:limit]
    echo(f"run {run_id}: {len(tickets)} open ticket(s){' [dry run]' if dry_run else ''}")
    summary = {"run": run_id, "open": len(tickets), "reply": 0, "handoff": 0, "close": 0,
               "sent": 0, "already_handled": 0, "failed": 0, "reused_decision": 0}

    def handle(ticket: dict, started: float) -> None:
        """One ticket, start to finish: decide (or reuse a decision), send once, log once."""
        tid = str(ticket["id"])
        h = content_hash(ticket)
        entry = journal.get(tid)
        if entry and (entry.get("status") == "done" or entry.get("hash") != h):
            entry = None  # the helpdesk re-opened it (practice reset) or it changed: decide again

        if entry and not dry_run:
            # A decision we made before a crash: reuse it, so the customer gets that exact answer.
            decision = pipeline.Decision(tid, entry["action"], entry["payload"], entry.get("audit", {}))
            summary["reused_decision"] += 1
            if entry.get("status") == "sending" and is_handled(client.get_ticket(tid)):
                journal.put(tid, status="done")
                return _finish(log, decision, "already_handled_before_crash", started, summary, echo)
        else:
            decision = pipeline.decide(ticket, llm, faq, min_confidence=min_confidence,
                                       verifier_model=verifier_model)
            if dry_run:
                return _finish(log, decision, "dry_run", started, summary, echo)
            journal.put(tid, status="decided", hash=h, action=decision.action,
                        payload=decision.payload, audit=decision.audit)

        journal.put(tid, status="sending")
        try:
            client.submit(tid, decision.action, decision.payload)
            outcome = "sent"
            journal.put(tid, status="done")
        except AlreadyHandled:
            outcome = "already_handled"  # an earlier run answered it: never send twice
            journal.put(tid, status="done")
        except HelpdeskError as exc:
            outcome = f"failed: {exc}"[:300]
            if exc.status and 400 <= exc.status < 500:
                journal.drop(tid)  # the helpdesk rejected our decision: decide again next run
            elif exc.status == 503 or "gave up" in str(exc):
                journal.put(tid, status="decided")  # 503 means nothing was saved: resend the same
            # otherwise (connection kept failing) it stays 'sending': the next run looks it up first
        _finish(log, decision, outcome, started, summary, echo)

    for n, ticket in enumerate(tickets):
        started = time.monotonic()
        try:
            handle(ticket, started)
        except LLMUnavailable as exc:
            # No model (quota, outage): stop instead of sending useless fallbacks. Nothing was sent for
            # this ticket or the ones after it, so they stay open; run again later to continue.
            summary["failed"] += 1
            summary["stopped_early"] = f"model unavailable, {len(tickets) - n} ticket(s) left open"
            log.line({"ticket": str(ticket.get("id")), "outcome": f"not sent: {exc}"[:300]})
            echo(f"  STOPPED: {exc} -- {len(tickets) - n} ticket(s) left open; re-run later to continue")
            break
        except Exception as exc:  # one bad ticket must never stop the others
            summary["failed"] += 1
            tid = str(ticket.get("id"))
            log.line({"ticket": tid, "outcome": f"failed: {type(exc).__name__}: {exc}"[:300],
                      "seconds": round(time.monotonic() - started, 2)})
            echo(f"  {tid}: FAILED {type(exc).__name__}: {str(exc)[:120]} (left open; re-run to retry)")

    summary["helpdesk"] = dict(client.stats)
    echo(f"done: {json.dumps(summary)}")
    echo(f"log: {log.jsonl}\nreview: {log.review}")
    return summary


def _finish(log: RunLog, d: pipeline.Decision, outcome: str, started: float, summary: dict, echo) -> None:
    summary[d.action] += 1
    if outcome == "sent":
        summary["sent"] += 1
    elif outcome.startswith("already"):
        summary["already_handled"] += 1
    elif outcome.startswith("failed"):
        summary["failed"] += 1
    a = d.audit
    log.line({
        "ticket": d.ticket_id, "action": d.action, "language": d.payload["language"],
        "category": d.payload["category"], "priority": d.payload["priority"],
        "confidence": d.payload.get("confidence"), "outcome": outcome,
        "faq_ids": a.get("faq_ids"),
        "triggers": sorted(set(a.get("model_triggers", []) + a.get("keyword_triggers", []))),
        "override": (a.get("override") or "")[:200] or None, "redacted": a.get("redacted"),
        "check": (a.get("check") or {}).get("verdict"), "seconds": round(time.monotonic() - started, 2),
    })
    log.reviewed(d, outcome)
    echo(f"  {d.ticket_id}: {d.action:7} {d.payload['category']:15} {d.payload['priority']} "
         f"{d.payload['language']}  -> {outcome}")
