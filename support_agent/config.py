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
    min_confidence: float


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_settings(need_helpdesk: bool = True, need_llm: bool = True) -> Settings:
    load_dotenv(ROOT / ".env")
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
        min_confidence=float(_get("MIN_CONFIDENCE", "0.75") or 0.75),
    )
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
