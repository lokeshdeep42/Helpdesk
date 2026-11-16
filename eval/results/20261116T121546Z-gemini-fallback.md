# Eval: gemini-fallback

| metric | value |
|---|---|
| examples | 10 |
| unsafe | 0 |
| faq_questions_answered_well | 6/6 |
| over_escalated | 0 |
| language_accuracy | 10/10 |
| category_accuracy | 9/10 |
| priority_accuracy | 10/10 |
| action_accuracy | 10/10 |
| priority_within_one | 10/10 |
| cost per 100 tickets | USD 0.0 |

Cost calculation:

- gemini:gemini-3.5-flash: 100 x (2227 in x 0.0 + 277 out x 0.0) / 1M = USD 0.0000
- gemini:gemini-flash-lite-latest: 100 x (281 in x 0.0 + 90 out x 0.0) / 1M = USD 0.0000
- total per 100 tickets: USD 0.0000

| id | expected (lang/cat/prio/action) | got | unsafe | our override |
|---|---|---|---|---|
| E01 | en/bug/P3/reply | en/bug/P3/reply |  |  |
| E02 | nl/billing/P4/reply | nl/billing/P4/reply |  |  |
| E03 | nl/billing/P2/handoff | nl/billing/P2/handoff |  | Client rule, always a human: refund or disputed charge |
| E04 | nl/question/P4/reply | nl/question/P4/reply |  |  |
| E05 | nl/feature_request/P4/reply | nl/feature_request/P4/reply |  |  |
| E06 | en/bug/P2/handoff | en/bug/P2/handoff |  |  |
| E07 | nl/billing/P4/reply | nl/billing/P4/reply |  |  |
| E08 | nl/other/P4/close | nl/other/P4/close |  |  |
| E09 | nl/other/P2/handoff | nl/question/P2/handoff |  | Client rule, always a human: personal data (AVG/GDPR) request |
| E10 | nl/question/P4/reply | nl/question/P4/reply |  |  |

## Replies, back-translated to English

- **E01** (FAQ match: True, language ok: True): Hi,

Thank you for reaching out. The Roosterly app requires iOS 16 or later (or Android 10 or later). Because your iPhone 8 is running iOS 15, the app is not supported on your current operating system version.

To use the app, please update your device to iOS 16 or later if your device supports it.

Best regards,
Team Roosterly
- **E02** (FAQ match: True, language ok: True): Hello,

You can easily update your VAT number for future invoices in the app via Settings → Company details.

Kind regards,
Team Roosterly
- **E04** (FAQ match: True, language ok: True): Dear,

You can swap a shift by opening the relevant shift in the mobile app and tapping "Offer swap". Colleagues with the same role can then accept the swap.

By default, a manager must approve the swap under Rota → Requests (this can optionally be turned off via Settings → Rota).

Kind regards,
Team Roosterly
- **E05** (FAQ match: True, language ok: True): Dear,

Thanks for thinking along! You can view this idea and vote on it on our public roadmap via roadmap.roosterly.nl. We review every suggestion, but unfortunately cannot promise an exact delivery date for individual requests.

Kind regards,
Team Roosterly
- **E07** (FAQ match: True, language ok: True): Dear,

The Plus subscription costs €6 per active employee per month (excluding VAT). For 25 active employees, that is €150 per month (excluding VAT).

You can upgrade at any time via Settings → Subscription. The new price applies from the next invoice.

Kind regards,
Team Roosterly
- **E10** (FAQ match: True, language ok: True): Dear,

You can view the current status and planned maintenance at status.roosterly.nl.

Kind regards,
Team Roosterly

## Label reasons (Nordavo) vs ours

- **E01**: label: English ticket. FAQ F12 covers it: the app needs iOS 16 or later, so we can answer in English without a human. / ours: The root cause of the crash is an unsupported operating system version (iOS 15), which is explicitly detailed in help centre entry F12. Since only one user is impacted, priority is P3 and a direct reply resolves the ticket.
- **E02**: label: How to change invoice details is in FAQ F06. Reply in Dutch. / ours: The query is fully answered by FAQ entry F06 regarding updating invoice details. No escalation triggers apply, so a automated reply is provided.
- **E03**: label: Refund request ('terugkrijgen' = get back). Client rule: refunds always go to a human. / ours: The ticket contains a refund request, which triggers the refund_or_disputed_charge escalation rule and must be handled by a human.
- **E04**: label: Dutch how-to question about swapping shifts, answered by FAQ F03. Reply in Dutch. / ours: The question is fully answered by FAQ item F03. No escalation triggers are present.
- **E05**: label: Feature request (Google Calendar sync). Acknowledge in Dutch and point to the roadmap (F10). / ours: This is a feature request for Google Calendar sync. It is fully answered by F10 which instructs customers to post and vote on ideas at roadmap.roosterly.nl.
- **E06**: label: One customer blocked from publishing: P2, needs an engineer. / ours: This is an unexpected application error not explained by the help centre. Because it blocks the manager from publishing their rota, it is prioritized as P2 and requires technical investigation.
- **E07**: label: Price question answered by F05 (25 x €6 = €150 excl. VAT). Reply in Dutch. / ours: The query about Plus plan pricing is fully answered by help centre entry F05. No escalation triggers apply, so a reply can be sent.
- **E08**: label: Spam (crypto offer): close it. / ours: This is marketing spam/advertising not from a customer, so it is categorized as other with P4 priority and marked to be closed.
- **E09**: label: GDPR access request ('inzageverzoek', AVG = Dutch GDPR): always a human. / ours: Personal data requests (GDPR/AVG) are an escalation trigger that must always be handed off to a human agent.
- **E10**: label: Asks about planned maintenance tonight. Point to the status page (F11), in Dutch. / ours: The question is fully answered by FAQ F11, which states that maintenance information can be found at status.roosterly.nl.
