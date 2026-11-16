# Roosterly support ticket agent

Reads every open ticket from the helpdesk, and posts one decision per ticket: `reply` (in the customer's language, only from the help centre), `handoff` (English note for a human) or `close` (spam, empty). Python, any OpenAI-compatible model.

## How to run

```bash
git clone <this repo> && cd <repo>
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # fill in HELPDESK_KEY, CANDIDATE_EMAIL, LLM_*
python -m pytest -q                                    # offline: fake helpdesk throws 503/429/409/lost responses
python -m support_agent eval --tag before              # score on GET /examples, nothing is sent
python -m support_agent run                            # THE command: handle all open tickets
```
Also: `run --dry-run` (decide, send nothing), `status`, `reset --yes` (re-open practice tickets). Every run writes `logs/run-<time>.jsonl` (one line per ticket) and `logs/run-<time>.md` (English review, with back-translations).

## Decisions

1. **Work in Dutch directly, check in English.** One model call reads the Dutch ticket and returns the decision plus the Dutch draft. I don't translate first, because that would add a call and a second place for meaning to drift, and the reply has to be Dutch anyway. The cost is that I can't read the draft. So every draft is checked in ways that don't need Dutch: a second call back-translates it into English and lists every claim the help centre does not support; a stop-word language detector checks that it is really in `nl`/`en`/`de`; it may only cite FAQ ids that exist and link to domains in the FAQ. I read the back-translations in `logs/run-*.md` and in the eval reports.
2. **The model proposes, code decides.** Everything that protects a customer is plain, tested Python (`policy.py`). The client's "always a human" rules (refund, cancellation/contract, AVG/GDPR, security, legal, threat to leave) are checked twice: the model flags them, and a narrow nl/en/de keyword list backs that up. Either one forces a handoff and sets a priority floor (security/legal → P1, money/contract → at least P2). Ticket text goes in `<ticket>` tags as data. An attempt to instruct the agent means handoff. IBANs and card numbers are redacted before the model ever sees them.
3. **When the agent counts itself unsure** (left open in the brief, so this is my assumption). It replies only if all of these hold: the help centre *fully* answers the ticket, it cites a valid FAQ id, confidence ≥ 0.75, the ticket is not P1, and the reply check passes. It closes only empty tickets (no model call) and spam with confidence ≥ 0.8. Everything else is a handoff, and the note says why the draft was held back. Other assumptions: a partly answered ticket is a handoff; "cancel a shift" is a how-to, not a contract cancellation; a plan *upgrade* question is answered from F05, but a downgrade or cancellation goes to a human.

**AI tools.** I built this with Claude Code (Claude Opus 5.5) as a pair programmer. It drafted the code and the tests from the brief, and a separate Claude reviewer agent audited the code for ways to double-answer or leak data. I chose the design, reviewed the code and wrote these decisions. *(Candidate: edit this paragraph so it describes exactly what you did.)*

## Reliability

- **503:** up to 8 attempts, exponential backoff (0.5 s → 8 s, with jitter). That is safe because nothing was saved.
- **429:** we pace ourselves at 4 calls/s (the limit is 5), and on a 429 we wait `Retry-After`.
- **409:** never retried; logged as `already_handled`. We avoid causing it in the first place. We only work on tickets the helpdesk lists as open. A POST that times out may or may not have been saved, so we `GET` the ticket before trying again.
- **Crash:** `state/journal.json` (written atomically) records *decided → sending → done* for every ticket. On a re-run, the helpdesk's open list says what is left. A decision made before the crash is reused (same answer, no new model cost), and a ticket left in *sending* is looked up first. If one ticket fails, the rest still run; the exit code is 1, so you simply run again. The tests simulate a crash right after the server saved an answer.
- **Security:** keys come from environment variables (`.env` is gitignored; `.env.example` is committed). Payment details are redacted before the model sees them and scrubbed from every log line. A draft that contains them is blocked. Model output is validated against the allowed values. Replies may only link to help-centre domains.

## Eval and fix

_PENDING. Needs a live run, and I don't fill this in by hand._ `eval/results/*-before.md` and `*-after.md` will hold the eval result before and after the fix, along with the fix itself.

**Cost per 100 tickets:** the eval measures it from real token usage: `100 × (avg input tokens × input price + avg output tokens × output price) / 1,000,000`. A reply costs 2 calls; a handoff or close costs 1 (empty tickets cost 0).

## What I would improve

- **Next step:** more labelled tickets. With 10 examples, each one is 10 percentage points, too coarse to tune priority. I would also run the reply check on a *different* model from the drafter, so the same model's blind spots don't approve its own drafts.
- **One rule I would change:** "contract changes always go to a human" overlaps with F05 ("you can upgrade at any time"). I would ask the client to state whether a plan upgrade counts as a contract change, so the agent and the human team apply the same line.
