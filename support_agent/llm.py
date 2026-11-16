"""One thin wrapper over any OpenAI-compatible chat endpoint (Gemini, Groq, OpenRouter, Ollama, OpenAI),
plus a fallback chain over several of them.

It returns parsed JSON and counts tokens so we can report cost. Swapping providers is an .env change.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol


class LLMError(RuntimeError):
    """The model answered, but not usefully (invalid output). The ticket goes to a human."""


class LLMUnavailable(LLMError):
    """The model could not be reached (quota, rate limit, outage). The ticket must stay open:
    a fallback handoff would mark it handled with a useless note."""


_UNAVAILABLE = ("RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
                "ServiceUnavailableError", "AuthenticationError", "PermissionDeniedError",
                "NotFoundError")  # model not pulled / retired: a config problem, never the ticket's fault


def _wrap(exc: Exception) -> LLMError:
    name = type(exc).__name__
    msg = f"LLM call failed: {name}: {str(exc)[:200]}"
    return LLMUnavailable(msg) if name in _UNAVAILABLE else LLMError(msg)


class JsonLLM(Protocol):
    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict: ...


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict = field(default_factory=dict)  # model -> {"calls", "input_tokens", "output_tokens"}
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, inp: int, out: int, model: str = "") -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += inp
            self.output_tokens += out
            m = self.by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            m["calls"] += 1
            m["input_tokens"] += inp
            m["output_tokens"] += out


class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, *, min_interval: float = 0.0,
                 temperature: float = 0.0, max_retries: int = 6, usage: Usage | None = None,
                 label: str = ""):
        from openai import OpenAI  # imported here so tests do not need the package configured

        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=max_retries, timeout=90)
        self.model = model
        self.temperature = temperature
        self.min_interval = min_interval
        self._last_call = 0.0
        self.usage = usage if usage is not None else Usage()
        self.label = label  # usage is counted as "label:model" so two providers never share a price

    def _create(self, kwargs: dict):
        try:
            return self._client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
        except Exception as exc:  # some providers reject response_format; then try plain text once
            if type(exc).__name__ != "BadRequestError" or not any(
                    w in str(exc).lower() for w in ("response_format", "json")):
                raise _wrap(exc) from exc
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise _wrap(exc) from exc

    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_problem = ""
        for _ in range(2):  # one retry if the model returns something that is not JSON
            self._pace()
            resp = self._create(dict(model=model or self.model, messages=messages, temperature=self.temperature))
            if getattr(resp, "usage", None):
                used = model or self.model
                self.usage.add(resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0,
                               f"{self.label}:{used}" if self.label else used)
            if not getattr(resp, "choices", None):
                raise LLMError("LLM returned no choices")
            text = (resp.choices[0].message.content or "").strip()
            try:
                return parse_json_object(text)
            except ValueError as exc:
                last_problem = str(exc)
                messages = messages + [{"role": "assistant", "content": text[:2000]},
                                       {"role": "user", "content": "That was not one valid JSON object. "
                                                                   "Answer again with only the JSON object."}]
        raise LLMError(f"model did not return valid JSON: {last_problem}")

    def _pace(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


@dataclass
class Route:
    """One provider in the fallback chain: its client and the model it uses for each role."""
    name: str
    llm: JsonLLM
    model: str           # drafts the decision
    verifier_model: str  # checks the drafted reply


class FallbackLLM:
    """Try each route in order; a route that is unavailable (quota, outage, missing model) is skipped for
    the rest of the run, so a spent daily quota costs one failed call, not one per ticket.

    The pipeline asks for the drafter with model=None and for the checker with model=<anything>; each
    route maps that role to its own model. If every route is down, LLMUnavailable is raised and the
    ticket stays open, exactly as with a single provider.
    """

    def __init__(self, routes: list[Route], *, usage: Usage | None = None, echo=print):
        if not routes:
            raise ValueError("FallbackLLM needs at least one route")
        self.routes = routes
        self.usage = usage if usage is not None else Usage()
        self.model = f"{routes[0].name}:{routes[0].model}"
        self.down: dict[str, str] = {}  # "route:model" -> why it was dropped
        self._echo = echo
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def last_used(self) -> str:
        """Which "route:model" answered the most recent call on this thread (for the audit trail)."""
        return getattr(self._local, "last_used", "")

    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        checker = model is not None
        for r in self.routes:
            m = r.verifier_model if checker else r.model
            key = f"{r.name}:{m}"
            if key in self.down:
                continue
            try:
                out = r.llm.complete_json(system, user, model=m)
            except LLMUnavailable as exc:
                with self._lock:
                    self.down[key] = str(exc)[:200]
                self._echo(f"llm: {key} unavailable for the rest of this run, falling back ({str(exc)[:120]})")
                continue
            self._local.last_used = key
            return out
        raise LLMUnavailable("every model in the fallback chain is unavailable: "
                             + " | ".join(f"{k}: {v}" for k, v in self.down.items()))


def build_llm(providers, echo=print) -> FallbackLLM:
    """One FallbackLLM over config.Provider entries (primary first). Usage is shared and keyed "name:model"."""
    usage, providers = Usage(), list(providers)
    routes = []
    for i, p in enumerate(providers):
        # A route with a fallback behind it gives up sooner: waiting out a spent daily quota helps nobody.
        retries = 6 if i == len(providers) - 1 else 3
        client = OpenAICompatLLM(p.base_url, p.api_key, p.model, min_interval=p.min_interval,
                                 max_retries=retries, usage=usage, label=p.name)
        routes.append(Route(p.name, client, p.model, p.verifier_model))
    return FallbackLLM(routes, usage=usage, echo=echo)


def prices_for(providers) -> dict[str, tuple[float, float]]:
    """Price table keyed the same way as Usage.by_model ("name:model")."""
    prices: dict[str, tuple[float, float]] = {}
    for p in providers:
        prices[f"{p.name}:{p.verifier_model}"] = (p.verifier_price_in_per_mtok, p.verifier_price_out_per_mtok)
        prices[f"{p.name}:{p.model}"] = (p.price_in_per_mtok, p.price_out_per_mtok)  # drafter wins if same model
    return prices


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str) -> dict:
    cleaned = _FENCE.sub("", _THINK.sub("", text)).strip()  # reasoning models may prepend <think>…</think>
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found")
        try:
            obj = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("JSON is not an object")
    return obj
