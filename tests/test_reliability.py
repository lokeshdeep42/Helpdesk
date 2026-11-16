"""503 / 429 / 409 / lost responses / crash and re-run: no customer ever gets two answers."""
from __future__ import annotations

import json

import pytest

from support_agent import runner
from support_agent.faq import load_faq
from support_agent.helpdesk import AlreadyHandled, HelpdeskClient
from support_agent.journal import Journal, content_hash
from tests.fakes import FakeClock, FakeHelpdesk, FakeLLM, reply_decision

BASE = "https://example.test/functions/v1/helpdesk"
QUIET = {"echo": lambda *_: None}


def make(n=25):
    clock = FakeClock()
    tickets = [{"id": f"T{i:03d}", "subject": "Nieuwe medewerker", "body": "Hoe voeg ik een medewerker toe?",
                "channel": "email", "customer_plan": "Basis"} for i in range(1, n + 1)]
    desk = FakeHelpdesk(tickets, clock)
    client = HelpdeskClient(BASE, "k", "me@example.com", transport=desk.transport(),
                            sleep=clock.sleep, clock=clock)
    return desk, client, clock


def test_pagination_collects_all_open_tickets():
    desk, client, _ = make(25)
    assert [t["id"] for t in client.open_tickets()] == [f"T{i:03d}" for i in range(1, 26)]


def test_503_is_retried_and_nothing_is_lost():
    desk, client, _ = make(3)
    desk.fail_503 = [True, True, False]
    assert len(client.open_tickets()) == 3
    assert client.stats["503"] == 2


def test_we_pace_ourselves_under_the_rate_limit():
    desk, client, _ = make(3)
    for _ in range(20):
        client.get_ticket("T001")
    assert client.stats["429"] == 0


def test_429_waits_retry_after_then_succeeds():
    desk, client, clock = make(3)
    client._pacer.interval = 0  # misbehave on purpose: burst faster than the limit
    for _ in range(8):
        client.get_ticket("T001")
    assert client.stats["429"] >= 1  # we hit the limit, waited, and every call still succeeded


def test_409_is_never_retried():
    desk, client, _ = make(1)
    payload = {"language": "nl", "category": "question", "priority": "P4", "reason": "r", "handoff_note": "n"}
    client.submit("T001", "handoff", payload)
    with pytest.raises(AlreadyHandled):
        client.submit("T001", "handoff", payload)
    assert desk.posts == [("T001", "handoff")]


def test_lost_response_after_save_does_not_send_twice():
    desk, client, _ = make(1)
    desk.lose_response_for = {"T001"}
    payload = {"language": "nl", "category": "question", "priority": "P4", "reason": "r", "handoff_note": "n"}
    with pytest.raises(AlreadyHandled):
        client.submit("T001", "handoff", payload)
    assert desk.posts == [("T001", "handoff")]


def _llm():
    return FakeLLM(lambda user: reply_decision())


def test_full_run_answers_each_ticket_exactly_once(tmp_path):
    desk, client, _ = make(12)
    desk.fail_503 = [i % 4 == 0 for i in range(200)]  # plenty of 503s
    summary = runner.run(client, _llm(), load_faq(), Journal(tmp_path / "j.json"), tmp_path,
                         min_confidence=0.75, **QUIET)
    assert summary["sent"] == 12 and summary["failed"] == 0
    assert sorted(t for t, _ in desk.posts) == [f"T{i:03d}" for i in range(1, 13)]


def test_crash_then_rerun_continues_without_duplicates(tmp_path):
    desk, client, _ = make(6)
    journal_path = tmp_path / "j.json"
    llm = _llm()

    class Crash(Exception):
        pass

    real_submit = client.submit
    count = {"n": 0}

    def crashing_submit(tid, action, payload):
        count["n"] += 1
        if count["n"] == 4:
            real_submit(tid, action, payload)  # saved on the server ...
            raise Crash()                      # ... and we die before recording it
        return real_submit(tid, action, payload)

    client.submit = crashing_submit
    with pytest.raises(Crash):
        runner.run(client, llm, load_faq(), Journal(journal_path), tmp_path, min_confidence=0.75, **QUIET)
    client.submit = real_submit
    calls_before = llm.calls
    summary = runner.run(client, llm, load_faq(), Journal(journal_path), tmp_path, min_confidence=0.75, **QUIET)
    assert summary["open"] == 2               # only the untouched tickets were still open
    assert len(desk.posts) == 6 and len({t for t, _ in desk.posts}) == 6
    assert desk.handled.keys() == desk.tickets.keys()
    assert llm.calls - calls_before == 4      # 2 tickets x (decide + verify)


def test_crash_between_decide_and_send_reuses_the_same_decision(tmp_path):
    desk, client, _ = make(1)
    j = Journal(tmp_path / "j.json")
    j.put("T001", status="sending", hash=content_hash(desk.tickets["T001"]), action="handoff",
          payload={"language": "nl", "category": "question", "priority": "P3", "reason": "saved before crash",
                   "handoff_note": "note", "confidence": 0.5}, audit={})
    llm = _llm()
    runner.run(client, llm, load_faq(), Journal(tmp_path / "j.json"), tmp_path, min_confidence=0.75, **QUIET)
    assert llm.calls == 0                                   # no new model call
    assert desk.handled["T001"]["reason"] == "saved before crash"


def test_after_practice_reset_old_journal_entries_are_ignored(tmp_path):
    desk, client, _ = make(2)
    j = Journal(tmp_path / "j.json")
    runner.run(client, _llm(), load_faq(), j, tmp_path, min_confidence=0.75, **QUIET)
    client.reset_practice()
    summary = runner.run(client, _llm(), load_faq(), Journal(tmp_path / "j.json"), tmp_path,
                         min_confidence=0.75, **QUIET)
    assert summary["sent"] == 2 and summary["reused_decision"] == 0


def test_log_has_one_line_per_ticket_and_no_payment_details(tmp_path):
    desk, client, _ = make(1)
    desk.tickets["T001"]["body"] = "Graag terugbetalen op NL91ABNA0417164300, kaart 4111 1111 1111 1111."
    llm = FakeLLM(lambda user: reply_decision(
        action="handoff", handoff_note="Refund to NL91ABNA0417164300 requested.", summary_en="Refund."))
    runner.run(client, llm, load_faq(), Journal(tmp_path / "j.json"), tmp_path, min_confidence=0.75, **QUIET)
    lines = next(tmp_path.glob("run-*.jsonl")).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    everything = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("run-*"))
    assert "NL91" not in everything and "4111" not in everything
    assert "NL91" not in json.dumps(desk.handled)          # nor in what we sent
    assert desk.handled["T001"]["action"] == "handoff"     # refund: always a human
