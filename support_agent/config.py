"""Settings come from environment variables (or a local .env file). Nothing secret lives in code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FAQ_PATH = ROOT / "data" / "faq.md"
LOG_DIR = ROOT / "logs"
STATE_PATH = ROOT / "state" / "journal.json"
EVAL_DIR = ROOT / "eval" / "results"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible endpoint with a drafter model and a checker model."""
    name: str
    base_url: str
    api_key: str
    model: str
    verifier_model: str
    min_interval: float = 0.0
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    verifier_price_in_per_mtok: float = 0.0
    verifier_price_out_per_mtok: float = 0.0


@dataclass(frozen=True)
class Settings:
    helpdesk_url: str
    helpdesk_key: str
    candidate_email: str
    candidate_name: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    verifier_model: str
    llm_min_interval: float
    price_in_per_mtok: float
    price_out_per_mtok: float
    verifier_price_in_per_mtok: float
    verifier_price_out_per_mtok: float
    price_currency: str
    min_confidence: float
    fallbacks: tuple[Provider, ...] = ()
    llm_name: str = "primary"

    @property
    def providers(self) -> tuple[Provider, ...]:
        """The primary LLM_* provider first, then the fallbacks in LLM_FALLBACKS order."""
        primary = Provider(self.llm_name, self.llm_base_url, self.llm_api_key, self.llm_model, self.verifier_model,
                           self.llm_min_interval, self.price_in_per_mtok, self.price_out_per_mtok,
                           self.verifier_price_in_per_mtok, self.verifier_price_out_per_mtok)
        return (primary, *self.fallbacks)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _num(name: str, default: str = "0") -> float:
    return float(_get(name, default) or default)


def _fallback(name: str) -> tuple[Provider | None, list[str]]:
    """LLM_FALLBACKS=GROQ reads GROQ_BASE_URL, GROQ_API_KEY, GROQ_MODEL, GROQ_VERIFIER_MODEL, GROQ_MIN_INTERVAL,
    GROQ_PRICE_IN_PER_MTOK, ... The key may be empty for a local server (Ollama)."""
    p = name.upper()
    base, model = _get(f"{p}_BASE_URL"), _get(f"{p}_MODEL")
    missing = [n for n, v in [(f"{p}_BASE_URL", base), (f"{p}_MODEL", model)] if not v]
    if missing:
        return None, missing
    price_in, price_out = _num(f"{p}_PRICE_IN_PER_MTOK"), _num(f"{p}_PRICE_OUT_PER_MTOK")
    return Provider(
        name=name.lower(), base_url=base, api_key=_get(f"{p}_API_KEY") or "unused", model=model,
        verifier_model=_get(f"{p}_VERIFIER_MODEL") or model, min_interval=_num(f"{p}_MIN_INTERVAL"),
        price_in_per_mtok=price_in, price_out_per_mtok=price_out,
        verifier_price_in_per_mtok=float(_get(f"{p}_VERIFIER_PRICE_IN_PER_MTOK") or price_in),
        verifier_price_out_per_mtok=float(_get(f"{p}_VERIFIER_PRICE_OUT_PER_MTOK") or price_out),
    ), []


def load_settings(need_helpdesk: bool = True, need_llm: bool = True) -> Settings:
    load_dotenv(ROOT / ".env")
    fallbacks, missing = [], []
    for name in filter(None, (n.strip() for n in _get("LLM_FALLBACKS").split(","))):
        provider, gaps = _fallback(name)
        missing += gaps
        if provider:
            fallbacks.append(provider)
    s = Settings(
        helpdesk_url=_get("HELPDESK_URL").rstrip("/"),
        helpdesk_key=_get("HELPDESK_KEY"),
        candidate_email=_get("CANDIDATE_EMAIL"),
        candidate_name=_get("CANDIDATE_NAME"),
        llm_base_url=_get("LLM_BASE_URL"),
        llm_api_key=_get("LLM_API_KEY"),
        llm_model=_get("LLM_MODEL"),
        verifier_model=_get("VERIFIER_MODEL") or _get("LLM_MODEL"),
        llm_min_interval=float(_get("LLM_MIN_INTERVAL", "0") or 0),
        price_in_per_mtok=float(_get("PRICE_IN_PER_MTOK", "0") or 0),
        price_out_per_mtok=float(_get("PRICE_OUT_PER_MTOK", "0") or 0),
        verifier_price_in_per_mtok=float(_get("VERIFIER_PRICE_IN_PER_MTOK") or _get("PRICE_IN_PER_MTOK", "0") or 0),
        verifier_price_out_per_mtok=float(_get("VERIFIER_PRICE_OUT_PER_MTOK") or _get("PRICE_OUT_PER_MTOK", "0") or 0),
        price_currency=_get("PRICE_CURRENCY", "USD") or "USD",
        min_confidence=float(_get("MIN_CONFIDENCE", "0.75") or 0.75),
        fallbacks=tuple(fallbacks),
        llm_name=(_get("LLM_NAME") or "primary").lower(),
    )
    if not need_llm:
        missing = []
    if need_helpdesk:
        missing += [n for n, v in [("HELPDESK_URL", s.helpdesk_url), ("HELPDESK_KEY", s.helpdesk_key),
                                   ("CANDIDATE_EMAIL", s.candidate_email)] if not v]
    if need_llm:
        missing += [n for n, v in [("LLM_BASE_URL", s.llm_base_url), ("LLM_API_KEY", s.llm_api_key),
                                   ("LLM_MODEL", s.llm_model)] if not v]
    if missing:
        raise ConfigError(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
    return s
