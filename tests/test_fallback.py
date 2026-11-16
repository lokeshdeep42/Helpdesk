"""Fallback chain: Gemini -> Groq -> local. A spent quota on one provider must not stop the run,
and when every provider is down the ticket must stay open (LLMUnavailable), never get a fallback reply."""
from __future__ import annotations

import os

import pytest

from support_agent import config, pipeline
from support_agent.faq import load_faq
from support_agent.llm import FallbackLLM, LLMError, LLMUnavailable, Route, prices_for
from tests.fakes import reply_decision


class Scripted:
    """A provider that records the model it was asked for and fails while `down` is set."""

    def __init__(self, name, down=None, result=None):
        self.name, self.down, self.result, self.asked = name, down, result, []

    def complete_json(self, system, user, *, model=None):
        self.asked.append(model)
        if self.down:
            raise self.down
        return dict(self.result or {"from": self.name})


def chain(*providers):
    return FallbackLLM([Route(p.name, p, f"{p.name}-draft", f"{p.name}-check") for p in providers],
                       echo=lambda *_: None)


def test_primary_answers_when_available():
    a, b = Scripted("a"), Scripted("b")
    llm = chain(a, b)
    assert llm.complete_json("s", "u") == {"from": "a"}
    assert b.asked == [] and llm.last_used == "a:a-draft"


def test_quota_error_falls_back_and_primary_is_not_retried_for_later_tickets():
    a, b = Scripted("a", down=LLMUnavailable("RateLimitError: per day")), Scripted("b")
    llm = chain(a, b)
    for _ in range(5):
        assert llm.complete_json("s", "u") == {"from": "b"}
    assert a.asked == ["a-draft"]  # one failed call, then skipped
    assert "a:a-draft" in llm.down


def test_each_role_uses_its_own_model_on_each_provider():
    a, b = Scripted("a"), Scripted("b")
    llm = chain(a, b)
    llm.complete_json("s", "u")                       # drafter
    llm.complete_json("s", "u", model="anything")     # checker
    assert a.asked == ["a-draft", "a-check"]


def test_checker_quota_does_not_drop_the_drafter():
    class CheckerSpent(Scripted):
        def complete_json(self, system, user, *, model=None):
            self.asked.append(model)
            if model == "a-check":
                raise LLMUnavailable("RateLimitError")
            return {"from": self.name}

    a, b = CheckerSpent("a"), Scripted("b")
    llm = chain(a, b)
    assert llm.complete_json("s", "u", model="x") == {"from": "b"}
    assert llm.complete_json("s", "u") == {"from": "a"}  # drafter on a still works
    assert llm.last_used == "a:a-draft"


def test_all_down_raises_unavailable_so_the_ticket_stays_open():
    a = Scripted("a", down=LLMUnavailable("RateLimitError"))
    b = Scripted("b", down=LLMUnavailable("APIConnectionError: ollama not running"))
    with pytest.raises(LLMUnavailable, match="ollama not running"):
        chain(a, b).complete_json("s", "u")


def test_bad_output_is_not_a_reason_to_switch_provider():
    """Invalid JSON is the model's answer for this ticket: it goes to a human, it does not burn the chain."""
    a, b = Scripted("a", down=LLMError("model did not return valid JSON")), Scripted("b")
    with pytest.raises(LLMError):
        chain(a, b).complete_json("s", "u")
    assert b.asked == []


def test_audit_records_which_provider_drafted_and_checked():
    verdict = {"verdict": "pass", "draft_language": "nl", "back_translation_en": "Hi",
               "unsupported_claims": [], "unsafe_content": [], "explanation": "ok"}

    class Primary(Scripted):
        def complete_json(self, system, user, *, model=None):
            self.asked.append(model)
            if model == "a-draft":
                raise LLMUnavailable("RateLimitError")
            return dict(verdict)

    a, b = Primary("a"), Scripted("b", result=reply_decision())
    llm = chain(a, b)
    ticket = {"id": "T1", "subject": "Nieuwe medewerker", "body": "Hoe voeg ik een medewerker toe?",
              "channel": "email", "customer_plan": "Basis"}
    d = pipeline.decide(ticket, llm, load_faq(), verifier_model="check")
    assert d.audit["drafted_by"] == "b:b-draft"
    assert d.audit["checked_by"] == "a:a-check"


def _clean_env(monkeypatch, env):
    monkeypatch.setattr(config, "load_dotenv", lambda *_a, **_k: None)
    for k in list(os.environ):
        if k.startswith(("LLM_", "GROQ_", "OLLAMA_", "VERIFIER_", "PRICE_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_fallbacks_are_read_from_env(monkeypatch):
    _clean_env(monkeypatch, {
        "LLM_NAME": "Gemini", "LLM_BASE_URL": "https://g", "LLM_API_KEY": "k", "LLM_MODEL": "flash",
        "LLM_FALLBACKS": "groq, ollama",
        "GROQ_BASE_URL": "https://q", "GROQ_API_KEY": "gk", "GROQ_MODEL": "qwen",
        "GROQ_VERIFIER_MODEL": "oss", "GROQ_PRICE_IN_PER_MTOK": "0.8", "GROQ_PRICE_OUT_PER_MTOK": "4",
        "OLLAMA_BASE_URL": "http://localhost:11434/v1", "OLLAMA_MODEL": "qwen3:8b"})
    s = config.load_settings(need_helpdesk=False)
    names = [(p.name, p.model, p.verifier_model) for p in s.providers]
    assert names == [("gemini", "flash", "flash"), ("groq", "qwen", "oss"), ("ollama", "qwen3:8b", "qwen3:8b")]
    assert s.providers[2].api_key == "unused"  # a local server needs no key
    prices = prices_for(s.providers)
    assert prices["groq:qwen"] == (0.8, 4.0) and prices["gemini:flash"] == (0.0, 0.0)


def test_half_configured_fallback_is_a_config_error(monkeypatch):
    _clean_env(monkeypatch, {"LLM_BASE_URL": "https://g", "LLM_API_KEY": "k", "LLM_MODEL": "flash",
                             "LLM_FALLBACKS": "groq", "GROQ_BASE_URL": "https://q"})
    with pytest.raises(config.ConfigError, match="GROQ_MODEL"):
        config.load_settings(need_helpdesk=False)
