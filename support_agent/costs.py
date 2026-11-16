"""What each ticket cost: model calls, tokens and money, priced per model.

The run loop handles one ticket at a time, so the usage counters before and after a ticket give that
ticket's exact usage. Each line in logs/run-*.jsonl carries it; `python -m support_agent costs` reads it back.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

_FIELDS = ("calls", "input_tokens", "output_tokens")


def snapshot(llm) -> dict:
    """A copy of the per-model usage counters ({} for an LLM that does not count, e.g. a test fake)."""
    return copy.deepcopy(getattr(getattr(llm, "usage", None), "by_model", None) or {})


def spent(before: dict, after: dict, prices: dict[str, tuple[float, float]], currency: str) -> dict:
    """Usage between two snapshots, and its price (per million tokens, per model)."""
    calls = inp = out = 0
    money = 0.0
    models = []
    for model, a in after.items():
        b = before.get(model, {})
        dc, di, do = (a.get(k, 0) - b.get(k, 0) for k in _FIELDS)
        if not (dc or di or do):
            continue
        p_in, p_out = prices.get(model, (0.0, 0.0))
        calls, inp, out = calls + dc, inp + di, out + do
        money += (di * p_in + do * p_out) / 1_000_000
        models.append(model)
    return {"llm_calls": calls, "input_tokens": inp, "output_tokens": out,
            "cost": round(money, 6), "currency": currency, "models": models}


def newest_log(log_dir: Path) -> Path | None:
    logs = sorted(log_dir.glob("run-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def report(jsonl: Path) -> str:
    """Per-ticket cost table for one run log, with totals and the average per ticket and per action."""
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if "cost" in r]
    if not rows:
        return f"{jsonl.name}: no cost data (logs from before the cost tracker have none)"
    cur = rows[0].get("currency", "USD")
    out = [f"{jsonl.name}: {len(rows)} ticket(s)", "",
           f"{'ticket':8} {'action':8} {'calls':>5} {'in':>7} {'out':>6} {'cost ' + cur:>12}  model(s)"]
    for r in rows:
        out.append(f"{r.get('ticket', '?'):8} {str(r.get('action') or '-'):8} {r['llm_calls']:>5} "
                   f"{r['input_tokens']:>7} {r['output_tokens']:>6} {r['cost']:>12.6f}  "
                   f"{', '.join(r.get('models') or [])}")
    n, total = len(rows), sum(r["cost"] for r in rows)
    tin, tout = sum(r["input_tokens"] for r in rows), sum(r["output_tokens"] for r in rows)
    out += ["", f"total:            {sum(r['llm_calls'] for r in rows):>5} {tin:>7} {tout:>6} {total:>12.6f}",
            f"average / ticket: {cur} {total / n:.6f}   ({tin // n} in + {tout // n} out tokens)",
            f"per 100 tickets:  {cur} {100 * total / n:.4f}"]
    by_action: dict[str, list[float]] = {}
    for r in rows:
        by_action.setdefault(str(r.get("action") or "not sent"), []).append(r["cost"])
    out.append("average by action: " + ", ".join(f"{a} {cur} {sum(c) / len(c):.6f} (n={len(c)})"
                                                for a, c in sorted(by_action.items())))
    if total == 0 and tin:
        out.append("note: tokens were used but every price is 0; set the PRICE_* values in .env to see money.")
    return "\n".join(out)
