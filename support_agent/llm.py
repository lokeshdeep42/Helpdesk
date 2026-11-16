"""One thin wrapper over any OpenAI-compatible chat endpoint (Gemini, Groq, OpenRouter, Ollama, OpenAI).

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
    pass


class JsonLLM(Protocol):
    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict: ...


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, inp: int, out: int) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += inp
            self.output_tokens += out


class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, *, min_interval: float = 0.0,
                 temperature: float = 0.0, max_retries: int = 6):
        from openai import OpenAI  # imported here so tests do not need the package configured

        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=max_retries, timeout=90)
        self.model = model
        self.temperature = temperature
        self.min_interval = min_interval
        self._last_call = 0.0
        self.usage = Usage()

    def _create(self, kwargs: dict):
        try:
            return self._client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
        except Exception as exc:  # some providers reject response_format; then try plain text once
            if "response_format" not in str(exc).lower() and "json" not in str(exc).lower():
                raise LLMError(f"LLM call failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMError(f"LLM call failed: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def complete_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_problem = ""
        for _ in range(2):  # one retry if the model returns something that is not JSON
            self._pace()
            resp = self._create(dict(model=model or self.model, messages=messages, temperature=self.temperature))
            if getattr(resp, "usage", None):
                self.usage.add(resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
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


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_object(text: str) -> dict:
    cleaned = _FENCE.sub("", text).strip()
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
