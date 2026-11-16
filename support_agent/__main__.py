"""Command line.

  python -m support_agent run              handle every open ticket (the one command)
  python -m support_agent run --dry-run    decide, but send nothing
  python -m support_agent eval --tag NAME  score the agent on GET /examples
  python -m support_agent status           helpdesk status
  python -m support_agent reset --yes      re-open all practice tickets (and clear the local journal)
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import EVAL_DIR, LOG_DIR, STATE_PATH, ConfigError, load_settings
from .faq import load_faq
from .helpdesk import HelpdeskClient, HelpdeskError
from .journal import Journal
from .llm import LLMUnavailable


def _client(s) -> HelpdeskClient:
    return HelpdeskClient(s.helpdesk_url, s.helpdesk_key, s.candidate_email, s.candidate_name)


def _llm(s):
    from .llm import build_llm
    return build_llm(s.providers)


def _prices(s) -> dict:
    from .llm import prices_for
    return prices_for(s.providers)


def _chain(s) -> str:
    return " -> ".join(f"{p.name} ({p.model} / checker {p.verifier_model})" for p in s.providers)


def _report_fallbacks(llm) -> None:
    for key, why in getattr(llm, "down", {}).items():
        print(f"llm: {key} was unavailable this run: {why}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="support_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="handle all open tickets")
    r.add_argument("--dry-run", action="store_true", help="decide but do not send")
    r.add_argument("--limit", type=int, default=None, help="only the first N open tickets")
    e = sub.add_parser("eval", help="score on the labelled examples")
    e.add_argument("--tag", default="eval")
    sub.add_parser("status")
    rs = sub.add_parser("reset", help="POST /practice/reset")
    rs.add_argument("--yes", action="store_true", help="confirm: this re-opens all 30 practice tickets")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "status":
            s = load_settings(need_llm=False)
            print(json.dumps(_client(s).status(), indent=1))
            return 0
        if args.cmd == "reset":
            if not args.yes:
                print("This re-opens all practice tickets and replaces your last run. Add --yes to confirm.")
                return 2
            s = load_settings(need_llm=False)
            print(json.dumps(_client(s).reset_practice(), indent=1))
            Journal(STATE_PATH).clear()
            return 0

        s = load_settings()
        faq, llm, client = load_faq(), _llm(s), _client(s)
        if args.cmd == "eval":
            from .evaluate import run_eval
            examples = client.examples()
            print(f"eval '{args.tag}' on {len(examples)} examples with {_chain(s)}")
            summary = run_eval(examples, llm, faq, EVAL_DIR, args.tag, min_confidence=s.min_confidence,
                               verifier_model=s.verifier_model, prices=_prices(s),
                               currency=s.price_currency)
            _report_fallbacks(llm)
            return 1 if summary["unsafe"] else 0

        from .runner import run
        print(f"models: {_chain(s)}")
        summary = run(client, llm, faq, Journal(STATE_PATH), LOG_DIR, min_confidence=s.min_confidence,
                      verifier_model=s.verifier_model, dry_run=args.dry_run, limit=args.limit)
        u = llm.usage
        print(f"llm: {u.calls} calls, {u.input_tokens} input + {u.output_tokens} output tokens")
        if summary["open"]:
            from .evaluate import cost_per_100
            c = cost_per_100(u.by_model, summary["open"], _prices(s), s.price_currency)
            print("cost: " + "; ".join(c["calculation"]))
        _report_fallbacks(llm)
        return 1 if summary["failed"] else 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except HelpdeskError as exc:
        print(f"helpdesk error: {exc}", file=sys.stderr)
        return 1
    except LLMUnavailable as exc:
        print(f"model unavailable (quota or outage), nothing was recorded: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
