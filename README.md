# Roosterly support ticket agent

Handles every open helpdesk ticket once: `reply` (customer's language, only from the FAQ), `handoff` (English note) or `close` (spam, empty). Python; any OpenAI-compatible model, with fallbacks. I use Gemini (`gemini-3.5-flash` decides and drafts, `gemini-flash-lite-latest` checks), then Groq (`qwen/qwen3.8-27b` / `openai/gpt-oss-120b`), then a local Ollama model.

## How to run

```bash
git clone https://github.com/lokeshdeep42/Helpdesk && cd Helpdesk
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # fill in HELPDESK_KEY, CANDIDATE_EMAIL, LLM_API_KEY
python -m pytest -q                                   # offline; fake helpdesk throws 503/429/409/lost responses
python -m support_agent eval                          # score on GET /examples; sends nothing
python -m support_agent run                           # THE command: handle all open tickets
```
Also: `run --dry-run`, `status`, `reset --yes`, `costs` (cost per ticket for the newest run). Each run writes `logs/run-*.jsonl` (one line per ticket, with its model calls, tokens and cost) and `logs/run-*.md` (English review).

## Decisions

1. **Work in Dutch directly, check in English.** One call reads the Dutch ticket and returns the decision plus the Dutch draft. I don't translate first, because that would add a call and a second place for meaning to drift. The cost is that I can't read the draft. So every draft is checked without Dutch:
   - A second model back-translates it into English and lists any claim not in the FAQ entries it cites.
   - A stop-word detector checks the language of the ticket and the draft.
   - FAQ ids and links must exist in the FAQ.

   I review the back-translations in `logs/run-*.md`.
2. **The model proposes, code decides** (`policy.py`, tested).
   - The client's "always a human" rules (refund, contract, AVG, security, legal, threat to leave) are flagged by the model *and* by an nl/en/de keyword list. Either one forces a handoff and sets a priority floor (security/legal P1, money/contract at least P2).
   - Ticket text is data inside `<ticket>` tags; an attempt to instruct the agent means a handoff.
   - IBANs and card numbers are redacted before the model sees them.
3. **Unsure means handoff** (left open in the brief, so this is my assumption).
   - Reply only if all of these hold: the FAQ *fully* answers the ticket, a valid FAQ id is cited, confidence ≥ 0.75, the ticket is not P1, and the check passes.
   - Close only empty tickets and spam with confidence ≥ 0.8.
   - Other assumptions: a partly answered ticket is a handoff; "cancel a shift" is a how-to, not a cancellation; an upgrade question is answered from F05.

**AI tools.** I built this with Claude Code (Claude Opus 5.5): it drafted code and tests from my brief, and a separate Claude reviewer agent audited it for double answers and data leaks (its findings are fixed in the history). I chose the design and reviewed every decision. *(Candidate: rewrite this paragraph in your own words so it says exactly what you did.)*

## Reliability

- **503:** up to 8 attempts with exponential backoff and jitter. Safe, because nothing was saved.
- **429:** we pace at 4 calls/s (the limit is 5); on a 429 we wait `Retry-After`.
- **409:** never retried. We only work on open tickets, and if a POST times out we `GET` the ticket before trying again.
- **Crash:** `state/journal.json` (atomic writes) records *decided → sending → done*. A re-run reuses decisions made before the crash and looks up any ticket left in *sending*. The tests crash the agent right after the server saved an answer. One failing ticket doesn't stop the others.
- **Model unavailable** (quota, outage, model missing): the next model in `LLM_FALLBACKS` takes over, and the failed one is skipped for the rest of the run. Drafter and checker fall back separately, and each decision logs which model drafted and checked it. Invalid output is not a reason to switch: that ticket goes to a human. If every model is down, the run stops and leaves the rest open, because a fallback handoff would close them with a useless note.
- **Security:** keys come from the environment (`.env` is gitignored). Payment details are redacted before the model sees them and scrubbed from logs, and a draft containing them is blocked. Model output is validated against the allowed values. Replies may only link to FAQ domains.

## Eval and fix

The eval runs the production pipeline on the 10 labelled examples (Nordavo's labels, never our model's), sends nothing, and scores safety first. Results are in `eval/results/`.

| run | unsafe | FAQ answered well | action | priority | category | language |
|---|---|---|---|---|---|---|
| before | 0 | 4/6 | 8/10 | 8/10 | 10/10 | 10/10 |
| after, run 1 | 0 | 6/6 | 10/10 | 10/10 | 10/10 | 10/10 |
| after, run 2 | 0 | 5/6 | 9/10 | 10/10 | 10/10 | 10/10 |
| Gemini (same prompts) | 0 | 6/6 | 10/10 | 10/10 | 9/10 | 10/10 |

Runs 1–3 are on Groq. The Gemini run used no fallback; its one miss is E09's category, still correctly handed off.

**Problem:** the agent was too strict about what counts as "answered by the FAQ".
- E01 (crash on iOS 15) was handed off as P2, though F12 explains the cause.
- E10 ("maintenance tonight?") flipped between reply and handoff, because F11 only points to the status page.

**Fix (`prompts.py`):** explaining the cause, or pointing to the page the FAQ names, counts as a full answer. One affected user while others can work is P3. E06, a real bug the FAQ doesn't explain, still goes to a human.

The miss in run 2 was a malformed checker output that held a correct reply back (safe). The check is now retried once when its output is malformed. The model varies between runs even at temperature 0, so I report both runs.

**Cost per 100 tickets:** measured in the "after" eval with both calls on qwen, at Groq's list price ($0.80/M in, $4.00/M out). 100 × (3,042 × 0.80 + 353 × 4.00) / 1,000,000 = **$0.38**. The checker now runs on gpt-oss-120b ($0.15/$0.60), which lowers this, and every run prints its own calculation. Everything ran on Groq's free tier; at list prices my estimate for all runs together is about $0.25. The Gemini run used about 2,500 tokens per ticket on the free tier; fill in `PRICE_*` from Google's pricing page to get its paid cost.

## What I would improve

- **Next step:**
  - More labelled tickets, and every eval run 3× (one example is 10 points, and the model varies).
  - Groq's free tier (200k tokens/day per model) ran out during my first 30-ticket dry run; the fallback chain now covers that, but free tiers also get overloaded (Gemini's newest Flash models returned 503 "high demand" on the day I switched). A real 200-tickets-a-week client needs a paid tier, which would cost under $1 a week.
- **A rule I would change:** "contract changes always go to a human" overlaps with F05 ("upgrade any time"). I'd ask the client whether an upgrade counts as a contract change, so that the agent and the team draw the same line.
