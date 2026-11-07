# v9: Officer Dashboard refinement and the three-risk round

**Scope.** This version implements `TASK_RISK.md`.

**Decision rules are unchanged.** `git diff` against both v8 and the original commit is empty for:
- `docs/decision_rules.md`, `server/engine/*`, `server/policy/*`, `server/domain/*` and the fixtures;
- the decision services (activation, acquisition, checkout, officer, servicing);
- the dataset.

The policy is still `2026.11-demo.6`, sha256 `8d220a1f…`.

## 1. Officer Dashboard (`web/js/officer.js`, officer CSS)

| Problem found | Change |
|---|---|
| Status chips in *Data checks* and *Consent & data sources* sat in heavy dark boxes. Cause: the v8 presenter styles (`.res`, `.callout`, `.kpis`, `.spin`) leaked into other pages. | Every presenter style is now scoped under `body.nav`. A UI check asserts that the chips have no border. |
| Case header did not say at a glance *what the engine decided* or *how far the officer may go* | **Case summary strip** with five cells: engine assessment (band, plain reason, code, secondary codes), most you can approve, automatic limit, requested, your authority. It is colour-coded by reason type. |
| The same three big figures were repeated in "Key figures" | That card is now "Income & repayment capacity" (income, low month, debt payments, DTI, binding constraint, how it is calculated). |
| Triggered risk signals were mixed in with passing ones | Triggered signals are listed first, with an amber row and an accent bar. |
| Consent & data sources took about a third of the page | A one-line summary (consent n/n, Layer 1 sources answered, Layer 2 used or not), with details folded. It opens automatically if something needs attention: a withdrawal, a failed source, or a Layer 2 round. |
| Queue items all looked the same | A left accent shows the reason type (risk judgement, fraud/identity, data integrity). |
| No sense of date | The top bar shows today's date (device clock, refreshed every minute). The queue shows the simulated business date, labelled as simulated. |

Kept as before:
- The decision panel stays sticky on desktop.
- Approve/Reject, eligibility conditions, inline authority checks, reason codes, notes, QA review and the audit tab are unchanged.

## 2. Presenter Page: risk round (`server/services/risk_lab.py`, section 3 of `/navigation.html`)

Three tabs. Each one runs the actual engine in an isolated in-memory sandbox and ends in a defined safe state.

### R1 · System overload
A burst of Pay Later decisions goes through a deterministic gateway model:
- a bounded worker pool;
- a bounded queue (requests beyond it are answered at once);
- deadline-aware dispatch (a request that cannot finish inside the time budget is answered with the fallback instead of starting).

Latency model and settings:
- Downstream latency (core, CIC, eKYC) is **simulated**: median 350 ms, log-normal spread.
- The engine's own compute time is **measured** live (about 0.4 ms per decision), so counts vary by a few between runs.
- Pool, queue and budget are gateway settings. They are not HLBVN figures.

What each request gets:
- **Answered in time:** the real engine result of one of the five personas, in rotation.
- **Not answered in time:** the safe state the rules already define for checkout. No credit decision is written, no hold is placed, and there are 0 approvals. The shopper can Pay Now, retry, or convert within 7 days (the TX-03 / R010-API pattern; checkout never waits).

The same burst is also run **without** safeguards. Flash-sale preset (6,000 requests in 60 s against a capacity of about 43 per second):

| | With safeguards | Without safeguards |
|---|---|---|
| 95th-percentile wait | 3.0 s; nobody waits longer than the budget | 76 s |
| Shoppers waiting more than 30 s at the counter | 0 | about 3,700 |
| Decisions | about 2,200 decided on time; about 3,800 get the fallback within 3 s | — |
| Backlog | — | lasts about 140 s |

Live switches in this tab:
- "Bank systems overloaded / down": the customer screen shows TX-03, Pay Later paused, Pay Now.
- CIC timeout and extra-data API timeout: automatic retry.

### R2 · Rule & evidence errors: how the AI risk was adapted
Smart Credit decides with a deterministic rule set, not an AI credit model, so there is no statistical "uncertainty". The interface says so, and demonstrates the closest real equivalent: a **confidently wrong decision**.

| Case | What is tried | How the existing controls respond |
|---|---|---|
| M1 | Typo in a threshold: credit-score cut-off 436 → 643 (applied to a policy **copy**) | Start-up validation does not catch it (the value is well-formed). The **version lock** refuses it: same version, different content hash. A **shadow replay** on 10,000 rows shows 4,721 GREEN → AMBER, and officer referrals rising from 1,347 to 6,352. The live service still runs the locked hash. |
| M2 | Debt-service ceiling 50% → 85% | Version lock refuses it. The replay shows 328 AMBER → GREEN and 2,122 higher limits: plausible-looking but riskier outputs. |
| M3 | Automatic ceiling 500M above the 90.1M officer authority | Start-up validation (`policy_integrity.validate`) **stops the service from starting**. |
| E1 | Unreliable evidence: the Open Banking record matches on name only | AV-01 rejects the record, so it is never used to size a limit. The case goes to fraud review (R010-FRD) with no approval. |
| E2 | Conflicting rules: two risk flags plus a verified debt breach | Fixed precedence (decision_rules §3): RED R020-CAP, with R010-FLAG recorded as a secondary code. The result is the same on every run. |

Footer safeguards are read from the policy: a 5% QA sample of automatic approvals (10% when Layer 2 data was used), reason codes on officer decisions, one appeal for any RED, and the policy hash stamped on every decision. Nothing on the page can change the live rules.

### R3 · Missing & anomalous data
The sandbox customer record is altered, and the unchanged engine handles it:

| Case | Anomaly | Engine response |
|---|---|---|
| D1 | Missing income evidence (P3: no CIC, no transactions) | R010-EVD → Layer 2 request. Customer shares → GREEN R001-L2; customer declines → RED R020-EVD (appeal) |
| D2 | Stale source: transactions read 200 days ago | R-VER-03 excludes the source → R010-INCOME → Layer 2 |
| D3 | Inconsistent record: `has_observable_tx = 0` but transaction values present (the 1,943-row pattern) | R-VER-01 excludes the values (listed as "not used") → Layer 2 |
| D4 | Impossible value: credit score 920 | R-INV-01 → R010-VER, data-integrity review by an officer |
| D5 | Incomplete Layer 2 data: 3 months instead of 6 | AV-02 marks it INVALID → RED R020-EVD; never approved on partial data |
| D6 | Missing field: customer segment empty | No question to the customer. Unclassified income is recognised conservatively, and the limit drops from 21M to 6M (split tender) |

Each result shows three columns (what changed, what the engine detected, the outcome) next to the clean-record baseline. A live switch (Layer 2 data only 3 months) shows the same case in the customer app.

## 3. Presenter Tools
- **Current date:** the header shows today's date from the device clock, and the tools card shows it with weekday, time and time zone, updated every second.
- **Business date:** the simulated business date stays as seeded (16/10/2026), because the fixtures are dated to it, and it is labelled as simulated. *Set to today* moves it on demand.
- **Switch status:** the section bar shows a live status chip ("All normal" / "n switch(es) on"). A new *Switch status* card lists the active switches and has **Clear all switches**.
- **Other operational tests:** device anomaly, payment failure, authentication failure and the tampered QR are not among the three risks. They moved into a folded card, so the risk round stays focused.
- **Reset:** reloads every section.

## 4. Validation (this revision)

| Check | Result |
|---|---|
| `python3 -m pytest -q` | **237 passed**: 225 existing and 12 new in `tests/test_risk_lab.py`. The new tests cover overload safety and determinism; M1–M3 caught with the live policy and policy file untouched; E1 → fraud review; E2 precedence; D1–D6 handling; no demo-database writes; presenter-only HTTP access. |
| `scripts/e2e_scenarios.py` on a fresh server | **17/17** scenarios |
| `scripts/ui_regression.py` (Playwright) | **63/63**. New checks: R1, R2 (M3 and M1), R3 (D4), the officer summary strip, and no chip boxing. |
| `scripts/policy_lock.py`, `node --check web/js/*.js` | unchanged lock; pass |
| Manual | Desktop 1440 px and phone 390 px for the presenter page and officer case: no horizontal scroll, no JS errors |

## 5. Remaining limitations
- **R1 latency figures are a model, not a load test** of a deployed service. Production sizing needs HLBVN IT and real downstream SLAs.
- **The version lock is enforced at release time**, by `scripts/policy_lock.py` and the test suite, and every decision is stamped with the policy hash. The running server does not re-check the lock file at start-up, and the page says "release refused" rather than claiming a runtime block.
- **The shadow replay uses the assumptions of `scripts/segment_replay.py`** (sources verified, consent given, no Layer 2), so its impact numbers are indicative.
- **No HLBVN screenshots were supplied.** Visual alignment follows the existing prototype and `docs/design/HLB_CONNECT_DESIGN_SYSTEM.md`.
