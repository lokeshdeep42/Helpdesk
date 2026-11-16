"""Eval on the 10 labelled examples (GET /examples). Labels come from Nordavo, never from our model.

It runs the exact production pipeline (pipeline.decide) without sending anything, then compares.
Safety is scored first, like Nordavo does: a reply or close where a human was needed is a failure.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import language, pipeline
from .faq import Faq
from .llm import OpenAICompatLLM

PRIO = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


def score_one(example: dict, d: pipeline.Decision) -> dict:
    label = example["label"]
    p = d.payload
    r = {
        "id": example["id"],
        "expected": {k: label.get(k) for k in ("language", "category", "priority", "action")},
        "got": {"language": p["language"], "category": p["category"], "priority": p["priority"], "action": d.action},
        "label_reason": label.get("reason", ""),
        "our_reason": p.get("reason", ""),
        "override": d.audit.get("override"),
    }
    for k in ("language", "category", "priority", "action"):
        r[f"{k}_ok"] = r["expected"][k] == r["got"][k]
    r["priority_off_by_one"] = abs(PRIO.get(p["priority"], 9) - PRIO.get(label.get("priority"), 0)) <= 1
    # Safety: answering (or closing) a ticket a human should handle reaches a real customer.
    r["unsafe"] = (d.action == "reply" and label.get("action") != "reply") or \
                  (d.action == "close" and label.get("action") != "close")
    if label.get("action") == "reply" and d.action == "reply":
        expected_faq = set(re.findall(r"F\d{2}", label.get("reason", "")))
        r["faq_match"] = (not expected_faq) or bool(expected_faq & set(d.audit.get("faq_ids") or []))
        r["draft_language_ok"] = language.matches(p["draft_reply"], label.get("language")) is not False
        r["draft"] = p["draft_reply"]
        r["back_translation_en"] = d.audit.get("back_translation_en", "")
    return r


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    acc = {k: sum(r[f"{k}_ok"] for r in rows) for k in ("language", "category", "priority", "action")}
    replies_expected = [r for r in rows if r["expected"]["action"] == "reply"]
    good_replies = [r for r in replies_expected if r["got"]["action"] == "reply"
                    and r.get("faq_match") and r.get("draft_language_ok")]
    return {
        "examples": n,
        "unsafe": sum(r["unsafe"] for r in rows),
        "faq_questions_answered_well": f"{len(good_replies)}/{len(replies_expected)}",
        "over_escalated": sum(r["expected"]["action"] == "reply" and r["got"]["action"] == "handoff" for r in rows),
        **{f"{k}_accuracy": f"{v}/{n}" for k, v in acc.items()},
        "priority_within_one": f"{sum(r['priority_off_by_one'] for r in rows)}/{n}",
    }


def run_eval(examples: list[dict], llm: OpenAICompatLLM, faq: Faq, out_dir: Path, tag: str, *,
             min_confidence: float, verifier_model: str | None, prices: dict[str, tuple[float, float]],
             currency: str = "USD", echo=print) -> dict:
    rows = []
    for ex in examples:
        ticket = {k: ex.get(k) for k in ("id", "subject", "body", "channel", "customer_plan")}
        d = pipeline.decide(ticket, llm, faq, min_confidence=min_confidence, verifier_model=verifier_model)
        row = score_one(ex, d)
        rows.append(row)
        marks = " ".join(f"{k[:4]}:{'ok' if row[f'{k}_ok'] else 'X'}" for k in ("language", "category", "priority", "action"))
        echo(f"  {ex['id']}: expected {row['expected']['action']:7} got {d.action:7} {marks}"
             f"{'  UNSAFE' if row['unsafe'] else ''}")

    summary = summarise(rows)
    summary["cost"] = cost_per_100(llm.usage.by_model, len(rows), prices, currency)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{stamp}-{tag}"
    base.with_suffix(".json").write_text(json.dumps({"tag": tag, "summary": summary, "rows": rows},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
    base.with_suffix(".md").write_text(_markdown(tag, summary, rows), encoding="utf-8")
    echo(json.dumps(summary, indent=1))
    echo(f"saved: {base}.md")
    return summary


def cost_per_100(by_model: dict, n_tickets: int, prices: dict[str, tuple[float, float]],
                 currency: str) -> dict:
    """Measured tokens per ticket, per model, times that model's price."""
    total, calc, models = 0.0, [], {}
    for model, u in by_model.items():
        p_in, p_out = prices.get(model, (0.0, 0.0))
        avg_in, avg_out = u["input_tokens"] / max(n_tickets, 1), u["output_tokens"] / max(n_tickets, 1)
        cost = 100 * (avg_in * p_in + avg_out * p_out) / 1_000_000
        total += cost
        models[model] = {**u, "avg_input_per_ticket": round(avg_in), "avg_output_per_ticket": round(avg_out),
                         "price_in_per_mtok": p_in, "price_out_per_mtok": p_out, "per_100_tickets": round(cost, 4)}
        calc.append(f"{model}: 100 x ({avg_in:.0f} in x {p_in} + {avg_out:.0f} out x {p_out}) / 1M = "
                    f"{currency} {cost:.4f}")
    calc.append(f"total per 100 tickets: {currency} {total:.4f}")
    return {"currency": currency, "per_100_tickets": round(total, 4), "models": models, "calculation": calc}


def _markdown(tag: str, s: dict, rows: list[dict]) -> str:
    lines = [f"# Eval: {tag}", "", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in s.items() if k != "cost"]
    c = s["cost"]
    lines += [f"| cost per 100 tickets | {c['currency']} {c['per_100_tickets']} |", "", "Cost calculation:", ""]
    lines += [f"- {line}" for line in c["calculation"]]
    lines += ["", "| id | expected (lang/cat/prio/action) | got | unsafe | our override |", "|---|---|---|---|---|"]
    for r in rows:
        e, g = r["expected"], r["got"]
        lines.append(f"| {r['id']} | {e['language']}/{e['category']}/{e['priority']}/{e['action']} | "
                     f"{g['language']}/{g['category']}/{g['priority']}/{g['action']} | "
                     f"{'YES' if r['unsafe'] else ''} | {(r.get('override') or '')[:120]} |")
    lines += ["", "## Replies, back-translated to English", ""]
    for r in rows:
        if "draft" in r:
            lines.append(f"- **{r['id']}** (FAQ match: {r['faq_match']}, language ok: {r['draft_language_ok']}): "
                         f"{r['back_translation_en']}")
    lines += ["", "## Label reasons (Nordavo) vs ours", ""]
    lines += [f"- **{r['id']}**: label: {r['label_reason']} / ours: {r['our_reason']}" for r in rows]
    return "\n".join(lines) + "\n"
