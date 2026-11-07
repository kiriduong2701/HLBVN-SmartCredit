# v7: live judge input and credit-officer workbench redesign

Scope: the two requirements in `TASK_PROTOTYPE.md` (navigation + live judge input; officer dashboard). Credit rules, policy file and policy lock are **unchanged** (`2026.11-demo.6`, sha256 `8d220a1f…`).

## 1. Presenter page (`web/navigation.html`, `web/js/navigation.js`, `server/services/live_demo.py`)

**Before:** 18 scenario cards, 7 persona cards and 7 outage toggles on one page.
**After:** one live scenario form, a live result card, and the risk tools folded into two collapsible boxes.

- **One hypothetical customer**: name, segment, monthly income, income variability (%), CIC history, credit score, existing debt payments (% of income), bills paid on time (%), purchase amount and partner merchant. Optional signals (HLB transaction history, rapid drain, weekly pattern, e-wallet top-ups, balance) are folded.
- **At most three starting points**, each copied from a named dataset row: A `HLB_POP_100000070` (gig, no CIC, approved automatically), B `HLB_POP_100000020` (gig, risk flags, credit officer), C `HLB_POP_100000332` (salaried, limit stops at exactly VND 90,100,000, split checkout). Any value can be overwritten live.
- **Validation** runs in the browser and again on the server (422 `INVALID_LIVE_INPUT` with per-field codes). The case documents give bounds for the score (350–850), the ratios (0–1), the segment labels and the product scope. They give no upper bound for inflow, CV or DTI, so wider plausibility bounds are used (inflow 1M–500M, CV 0–100%, DTI 0–100%, purchase 10k–300M). These are for input hygiene only and are not credit thresholds.
- **Live scope hint** on the purchase amount: below 1.6M → Pay Now only; exactly 90.1M → can be fully financed; above 90.1M → at most 90.1M on Pay Later, the rest paid now.
- **No shortcuts**: the page creates a fictional customer and a dynamic-QR merchant order, then opens the customer app on the QR screen. The customer chooses Pay Later and gives the legal data consent, and the normal engine decides. The result card polls `GET /api/presenter/live-demo/{id}` every 3 s and shows the decision band, the plain reason, the limit and what set it, the checkout outcome (full, split or not applicable), and a link straight to the officer case (`/officer.html?app=…`).
- **No HLB transactions**: transaction fields stay empty (policy `transaction_dependent_fields`). The typed income becomes a simulated Open Banking record and the bill ratio a simulated utility record. Both are used only if the engine asks for Layer 2 data and the customer ticks the source.
- **Isolation**: a live customer sees only their own order; other customers never see it. Creation is audited (`LIVE_DEMO_CUSTOMER_CREATED`). A reset removes live customers and keeps the audit log.

**Rule conflict resolved:** the customer app hid Pay Later for amounts above 90.1M when the customer had no line yet. The server and `decision_rules.md` §5 allow inline activation followed by a split (Pay Later ≤ 90.1M, the rest paid now). The client now follows the server rule.

## 2. Officer workbench (`web/js/officer.js`, `web/js/plain.js`, `web/css/app.css`)

See `design/HLB_CONNECT_DESIGN_SYSTEM.md` §4.

**Role separation:**
- The *Review & decide* tab holds what the officer needs to act on, within their authority.
- The *Audit & technical* tab holds what compliance and model-risk monitoring need.

Material reasons, data limitations, consent status and evidence stay on the review tab. The page dropped from about 4,900 px to about 2,850 px for the same case at 1440 px.

All codes (rule ids, states, check ids, reason codes) are still shown, as small tags next to the plain-language text. `plain.js` translates them for people and falls back to the server text for anything it does not know.

## 3. Verification (run on this revision)

| Check | Command | Result |
|---|---|---|
| Unit / service / API | `python3 -m pytest -q` | 210 passed (203 existing + 7 new in `tests/test_live_demo.py`) |
| End-to-end HTTP | `python3 run.py --reset` then `python3 scripts/e2e_scenarios.py` | 17/17 scenarios, 184 calls |
| UI regression (Playwright, Chromium) | `python3 scripts/ui_regression.py` on a fresh server | 51/51 checks (presenter and officer checks rewritten for the new UI) |
| JS syntax | `node --check web/js/*.js` | pass |

The new tests cover the following:
- The three presets route as described.
- Purchases of 1,599,999 (not applicable), 1,600,000 (approved), 90,100,000 (fully approved) and 90,100,001 (split: 90,100,000 on Pay Later and 1 VND paid now).
- The line never exceeds 90.1M.
- The officer can approve exactly 90,100,000, and 90,200,000 is refused.
- Out-of-range and wrong-type inputs are rejected.
- Inclusive bounds are accepted.
- The thin-file Layer 2 path.
- QR isolation, the audit event, and the presenter-only HTTP guard.

The UI checks cover the following:
- No horizontal scroll on the officer page at 1366 px and 390 px.
- The customer app at 393 px.

**Not verified:** Safari and real devices; screen readers; Vietnamese copy reviewed by an HLBVN copywriter.
