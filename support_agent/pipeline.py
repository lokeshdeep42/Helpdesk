"""Decide one ticket: redact -> model decides (in the customer's language) -> code guardrails ->
for replies, a second model call back-translates and fact-checks the draft -> final decision.

No network calls to the helpdesk happen here, so the eval can run the exact same code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import language, pii, policy
from .faq import Faq
from .llm import JsonLLM, LLMError, LLMUnavailable
from .prompts import decide_messages, verify_messages

_TRIGGER_LABEL = {
    "refund_or_disputed_charge": "refund or disputed charge",
    "cancellation_or_contract_change": "cancellation or contract change",
    "personal_data_request": "personal data (AVG/GDPR) request",
    "security_incident": "possible security incident",
    "legal_threat": "legal threat",
    "churn_threat": "customer threatens to leave",
}


@dataclass
class Decision:
    ticket_id: str
    action: str                       # reply | handoff | close
    payload: dict                     # exactly what we POST
    audit: dict = field(default_factory=dict)  # why; goes to the run report, never to the customer


def _payload(action: str, lang: str, cat: str, prio: str, reason: str, conf: float, text: str = "") -> dict:
    p = {"language": lang, "category": cat, "priority": prio,
         "reason": pii.redact(reason)[0][:400] or "No reason given.", "confidence": round(conf, 2)}
    if action == "reply":
        p["draft_reply"] = text
    elif action == "handoff":
        p["handoff_note"] = pii.redact(text)[0][:1200]
    return p


def decide(ticket: dict, llm: JsonLLM, faq: Faq, *, min_confidence: float = 0.75,
           verifier_model: str | None = None) -> Decision:
    tid = str(ticket.get("id", "?"))

    # 1. Empty tickets are closed without asking a model.
    if policy.is_empty(ticket):
        return Decision(tid, "close",
                        _payload("close", "nl", "other", "P4", "Empty ticket: no subject or text.", 1.0),
                        {"rule": "empty"})

    # 2. Payment details never leave our process: redact before the model sees the text.
    subject, red_s = pii.redact(str(ticket.get("subject") or ""))
    body, red_b = pii.redact(str(ticket.get("body") or ""))
    audit: dict = {"redacted": sorted(set(red_s + red_b))}

    # 3. The model decides, working on the Dutch text directly.
    try:
        raw = llm.complete_json(*decide_messages(faq.text, ticket, subject, body))
        if getattr(llm, "last_used", ""):
            audit["drafted_by"] = llm.last_used  # with fallbacks, which provider made this decision
    except LLMUnavailable:
        raise  # no model right now: leave the ticket open for the next run, do not send a fallback
    except LLMError as exc:  # the model answered with garbage: a human should read this ticket
        lang_guess = language.detect(f"{subject} {body}")[0] or "nl"
        audit["error"] = str(exc)[:200]
        return Decision(tid, "handoff", _payload(
            "handoff", lang_guess, "other", "P3",
            "Automatic triage failed, so a human should read this ticket.", 0.0,
            "Automatic triage failed for this ticket (model error). Please read and handle it manually."), audit)

    d = policy.normalise(raw)
    # Language is checked by code too: a clearly English or German ticket is not labelled Dutch.
    detected = policy.confident_language(f"{subject}\n{body}")
    if detected and detected != d["language"]:
        audit["language_corrected"] = f"{d['language']} -> {detected}"
        d["language"] = detected  # a draft in the old language now fails the draft language check
    kw = policy.keyword_triggers(f"{subject}\n{body}")
    triggers = set(d["triggers"]) | set(kw)
    audit.update({"model_action": d["action"], "model_triggers": d["triggers"], "keyword_triggers": kw,
                  "faq_ids": d["faq_ids"], "summary_en": pii.redact(d["summary_en"])[0],
                  "injection": d["injection"], "problems": d["problems"]})

    prio = policy.apply_priority_floor(d["priority"], triggers)
    if prio != d["priority"]:
        audit["priority_floor"] = f"{d['priority']} -> {prio}"
    d["priority"] = prio
    reason = d["reason"] or d["priority_reason"]
    note = d["handoff_note"]
    if not note or language.matches(note, "en") is False:
        note = d["summary_en"] or "Customer message needs a human; automatic summary unavailable."

    def handoff(why: str) -> Decision:
        audit["override"] = why
        full_note = f"[{why}] {note}" if why else note
        return Decision(tid, "handoff", _payload("handoff", d["language"], d["category"], d["priority"],
                                                 reason, d["confidence"], full_note), audit)

    # 4. Hard rules first: these always go to a human.
    if triggers:
        return handoff("Client rule, always a human: " + ", ".join(_TRIGGER_LABEL[t] for t in sorted(triggers)))
    if d["injection"]:
        return handoff("Ticket tries to instruct the agent; treat with care")
    if d["problems"]:
        return handoff("Model output was invalid: " + "; ".join(d["problems"]))

    # 5. Close only obvious spam with high confidence.
    if d["action"] == "close":
        if d["is_spam"] and d["confidence"] >= 0.8:
            return Decision(tid, "close", _payload("close", d["language"], d["category"], d["priority"],
                                                   reason, d["confidence"]), audit)
        return handoff("Agent suggested closing but is not sure it is spam")

    if d["action"] == "handoff":
        return handoff("")

    # 6. Reply: a second, independent call back-translates and fact-checks the draft.
    verdict = None
    if d["draft_reply"]:
        try:
            # The checker only sees the entries the draft claims to use: anything else is unsupported.
            cited = "\n\n".join(f"## {f} · {faq.entries[f]}" for f in d["faq_ids"] if f in faq.entries)
            msgs = verify_messages(cited or "(no help-centre entry cited)", ticket, subject, body,
                                   d["language"], d["draft_reply"])
            verdict = llm.complete_json(*msgs, model=verifier_model)
            if str(verdict.get("verdict", "")).lower() not in ("pass", "fail"):
                # Malformed check (e.g. a misspelt key): ask once more. A real "fail" is never retried.
                audit["check_retried"] = True
                verdict = llm.complete_json(*msgs, model=verifier_model)
            if getattr(llm, "last_used", ""):
                audit["checked_by"] = llm.last_used
            audit["back_translation_en"] = pii.redact(str(verdict.get("back_translation_en", "")))[0]
            audit["check"] = {k: verdict.get(k) for k in ("verdict", "draft_language", "answers_the_question",
                                                          "unsupported_claims", "unsafe_content", "explanation")}
        except LLMUnavailable:
            raise
        except LLMError as exc:
            audit["check_error"] = str(exc)[:200]
    blockers = policy.reply_blockers(d, faq, verdict, min_confidence, ticket_text=f"{subject}\n{body}")
    if blockers:
        audit["reply_blockers"] = blockers
        return handoff("Agent drafted a reply but held it back: " + "; ".join(blockers)[:300])
    return Decision(tid, "reply", _payload("reply", d["language"], d["category"], d["priority"], reason,
                                           d["confidence"], d["draft_reply"]), audit)
