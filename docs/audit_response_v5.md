# Response to the Independent Decision Rules Audit (v5, policy `2026.11-demo.5`)

> **Superseded in part by v6 (policy `2026.11-demo.6`).** This file records the v5 response and is kept for traceability. v6 changed these v5 designs; see [`decision_rules.md`](decision_rules.md) §0:
> - The employment-type question and the manual-review route are removed.
> - Data-quality conflicts are excluded automatically instead of being routed to an officer (R010-VER now covers invalid data only).
> - Validated Layer 2 data can support automatic approval (R001-L2), and Layer 2 streams are summed after de-duplication.
> - The 50M STP ceiling is replaced by the product scope plus a recognised-income multiple.
> - Officer authority is now ≤ 90.1M inclusive (it was < 90M).
> - RF-04 no longer routes cases.
> - Officers no longer request or refresh data.
> - Outages are retried automatically.

Baseline audited: commit `188c43f` (policy `2026.11-demo.4`, sha256 `e9d96dde…`). Every finding was first reproduced against that baseline with `probe_before.py`-style engine/service probes, before any change. The backend had not changed since the audit, so each reproduced finding was classified **still requires correction** before work began. The rules themselves are in [`decision_rules.md`](decision_rules.md).

## 1. Finding-by-finding coverage

Status key: **Corrected** = reproduced on the baseline, fixed, covered by tests · **Kept (assumption)** = deliberately unchanged per the case assumption, production requirement documented · **N/A in repo** = concerns the proposal deck, not the code.

| ID | Reproduced on baseline (evidence) | Status | What v5 does | Tests |
|---|---|---|---|---|
| F-01 | CV 0.50 / DTI 0.25 / 14M → RED R020-CAP, limit 0 (CV used as share of income lost) | Corrected | Stressed income = estimated 25th-percentile month; volatility can lower the STP limit or route to R010-CAPX, never RED | V-05, `test_v5_controls` property tests |
| F-02 | HLB 3M of an external salary + validated Open Banking 20M → limit stayed 5.5M on HLB basis | Corrected | Layer 2 triggered on **insufficient** Layer 1 (R010-CAPX, ADDITIONAL_INCOME); validated Layer 2 income sizes the officer maximum (higher of L1/L2, never summed), never STP | V-06, V-18 |
| F-03 | CUS-005 (CIC no record) obligations 3,667,486 from `CREDIT_INFORMATION_DTI`; DTI re-scaled to other incomes | Corrected | Absolute VND obligations with lineage CIC_REPORTED / TX_OBSERVED / CIC_NO_RECORD (= 0); HLB internal dedup via `included_in_dti` | V-13 (engine + service) |
| F-04 | CUS-007 requested 20M, line opened silently at 3.5M | Corrected | GREEN below the request → R001-CO, `OFFER_PENDING`; opens only on acceptance; "ask for more" → Layer 2/officer | V-12, V-12b, e2e 2 |
| F-05 | 60M income → GREEN 90M automatically (above the officer authority) | Corrected | STP ceiling 50M (A); `validate()` refuses ceiling ≥ authority; renewal above ceiling → officer (R010-AUTH) | V-10, `test_service::test_green_is_capped_by_the_automatic_ceiling_below_officer_authority`, `test_v5_controls::test_configuration_inside_legal_caps_and_mandates` |
| F-06 | raw 1,799,999 and 1,607,142 → rounded to 1.5M → below minimum | Corrected | Minimum tested on raw capacity; rounding never goes below the minimum | V-08, V-09 |
| F-07 | Checkout used obligations frozen at activation; new HLB products ignored | Corrected | `refreshed_obligations()` re-reads current HLB instalments (deduplicated) at every checkout | `test_v5_controls::test_new_hlb_loan_after_activation_reduces_checkout_affordability` |
| F-08 | Day 95: R010-STALE and same-limit renewal refused (`INVALID_LIMIT`) | Corrected | Automatic Layer 1 renewal from day 75; GREEN renews ≤ current limit; otherwise officer, line usable ≤ 120 days | V-24, V-24b; demo scenario 15 |
| F-09 | Re-evaluation during review could return RED R020-CAP | Corrected | Review guard R010-RECHECK; only consent, Layer 0 and credit hard stops override | V-19 (engine + service) |
| F-10 | No identity / duplicate / device / velocity rule in the credit flow | Corrected | Layer 0 (L0-01..L0-05) at activation and checkout; unverified signal → R010-FRD review; officer-confirmed fraud → HS-05 | V-20, `test_v5_controls` (device anomaly, velocity); demo scenario 16 |
| F-13 | Single officer role, no profiles, no code, < 90M | **Kept (assumption)** | Unchanged by design (case assumption); production requirements (SSO, tiers, four-eyes, escalation, named owner) recorded in `officer_controls.governance` and shown on the officer screen | V-11, UI regression |
| F-14 | Flat VND 50,000 late fee charged | Corrected | Overdue interest per Circular 39 Art. 13.4 (150% of rate on principal, 10%/yr on unpaid interest), no fee; legal caps validated | `test_v5_controls::test_overdue_interest_never_exceeds_the_legal_caps`, e2e 11 |
| F-15 | Prepaying instalments cured the flag the same day | Corrected | Cure counts only instalments whose due date has passed and were paid on time | V-26, e2e 11 |
| F-16 | Conversion credited cash to the account balance | Corrected | Loan purpose PURCHASE_FINANCING; disbursement = reversal of that purchase's debit (once, ≤ purchase); Art. 8.10 dependency recorded | `test_v5_controls::test_conversion_is_recorded_as_purchase_financing`, `test_service` |
| F-17 | After a default was paid the line stayed SUSPENDED with no path | Corrected | Loan back to ACTIVE servicing; officer reinstatement case (R010-REINST, RI-01, ≤ previous limit); rejection closes | V-27 |
| F-18 | `CANNOT_WITHDRAW` on an active line; Decree 13/2023 cited | Corrected | Line-level and per-source withdrawal; Decree 356/2025 cited; consent never captured under the checkout timer (timer paused) | V-03, V-04, `test_v5_controls::test_withdraw_one_layer2_source_purges_its_data` |
| F-19 | 742 replay flag cases could never be approved (capacity on haircut income) | Corrected | Officer maximum on average verified income; EL-10 reason-code basis above the stressed capacity | V-28b, e2e 8 |
| C-01 | CIC outage routed as R010-VER without R-API-01 | Corrected | Outage → R010-API with R-API-01; officer refresh restores | V-14 |
| C-03 | Over-limit purchase → RED R020-LIM, no partial financing | Corrected | Split tender R010-SPLIT (TX-07 limit / TX-08 affordability); explicit acceptance; hold = Pay Later part | V-22, V-21, e2e 2 |
| C-07 | Inline-activation consent captured under the 5-minute countdown | Corrected | Timer paused while the consent form is open (`/consent-pause`), fresh deadline afterwards | `test_v5_controls::test_inline_activation_consent_is_not_under_the_countdown` |
| C-08 / C-09 | Officer could approve odd amounts (12,345,678); authority boundary 89,999,999 | Corrected | VND 100,000 grid for officer limits; highest approvable 89,900,000; 90,000,000 refused | V-11, e2e 16 |
| C-04, C-10, C-11 | Claims in the proposal deck, no code counterpart | N/A in repo | The deck wording should be aligned with the replay figures in `decision_rules.md` §9 (team action) | – |
| Low: TX-08 | Affordability tested on the first instalment (516k) while the last is 532k | Corrected | Tested on the largest instalment | V-23 |
| Low: SUSPENDED | Suspended line mapped to R020-ARR | Corrected | SUSPENDED → R020-DEF | `test_engine::test_line_status_maps_to_its_own_reason` |

**Note on IDs F-11, F-12, C-02, C-05 and C-06.** The audit file was attached to the conversation but is not stored in the repository, and the exact wording of these five IDs could not be re-read when this record was written. Their topics were addressed as part of the six areas the task listed, and are reproduced above or in `decision_rules.md`. **Please check them against the audit text:**
- income classification (own-account transfers / loan proceeds / refunds excluded; CUS-004 fixture),
- the aggregate digital-lending exposure cap (Circular 39 Art. 22.2(h) as amended by Circular 29/2026; 100M placeholder),
- purchase-level STP metrics (segment replay reports purchase financed instantly, not just activation GREEN),
- correct consent / missing-data / quality routing (R010-INC, R010-VER, R010-EVD, R010-OBL),
- explainability (secondary codes, policy version and hash on every decision and line).

## 2. Acceptance scenarios V-01..V-28

All run in `tests/test_validation_matrix.py` (35 tests: engine-level and service-level variants). Result on this commit: **35 passed**.

| V | Scenario | Expected (v5 target) | Result |
|---|---|---|---|
| V-01 | Salaried, CIC, clean | GREEN R001, ≤ 50M ceiling | PASS |
| V-02 | Required consent missing | HS-01 / `CONSENT_REQUIRED`; nothing read or assessed | PASS |
| V-03 | Consent withdrawn mid-review | STOPPED HS-01, CONSENT_WITHDRAWN audited | PASS |
| V-04 | Consent withdrawn on an active line | line CLOSED to drawdowns, loans continue, no review opened | PASS |
| V-05 | Volatile income (CV 0.50), DTI 0.25 | never RED; GREEN at a smaller limit; R010-CAPX when stressed capacity < minimum | PASS † |
| V-06 | Partial HLB income, salary elsewhere | counter-offer, or R010-CAPX → Layer 2; officer sizes on validated 20M | PASS |
| V-07 | Verified obligations exceed ceiling, Layer 2 exhausted | RED R020-CAP, appeal allowed | PASS |
| V-08 | Raw capacity 1,799,999 | limit 1.6M, meets minimum | PASS |
| V-09 | Raw capacity exactly 1.6M | approved at 1.6M | PASS |
| V-10 | Capacity at product maximum | GREEN capped at 50M; officer maximum 90.0M | PASS |
| V-11 | Officer authority boundary | 89,900,000 approved; 90,000,000 `ABOVE_APPROVAL_AUTHORITY` | PASS † |
| V-12 | Requested above capacity | OFFER R001-CO 14.0M; nothing opens until accepted | PASS |
| V-13 | CIC "no record" | obligations CIC_NO_RECORD = 0; not declined | PASS |
| V-14 | CIC outage then refresh | R010-API + R-API-01; case eligible after refresh | PASS |
| V-15 | Layer 2 API down | R010-ALT-NA officer fallback, never RED | PASS |
| V-16 | Layer 2 identity mismatch | AV-01 fails, INVALID, officer | PASS |
| V-17 | Layer 2 short history | INVALID, officer | PASS |
| V-18 | Two Layer 2 income sources | single highest (12.0M), never summed (23.6M) | PASS |
| V-19 | Re-evaluation during review | R010-RECHECK; only hard stops override | PASS |
| V-20 | Duplicate identity | R010-FRD, no credit decision; confirm fraud → HS-05 | PASS |
| V-21 | Two concurrent purchases | exactly one hold fits | PASS |
| V-22 | Purchase above available | R010-SPLIT; end-to-end ledger (loan = PL part, balance − PN part); < 1.6M → R010-LIMREV | PASS |
| V-23 | Instalment remainder on the last | tested on 532k, not 516k | PASS |
| V-24 | Line older than 90 days | renewed automatically from day 75, never raised | PASS |
| V-25 | Arrears | freeze, unfreeze on payment | PASS |
| V-26 | Cure by prepayment | flag stays until due dates pass | PASS |
| V-27 | Default then arrears paid | loan ACTIVE, line SUSPENDED until officer reinstatement | PASS |
| V-28 | Segment auto-decline | 0 RED on first pass; any later RED has a verified-obligations reason | PASS |

† Reinterpretations (documented in the test docstrings): **V-05** — the audit expected AMBER R010-CAPX; under the P25 rule the stated inputs still leave stressed capacity above the minimum, so the correct v5 outcome is GREEN at a smaller limit (12.0M). R010-CAPX is asserted for the case where stressed capacity falls below the minimum. **V-11** — with the 100k officer grid (C-09) the highest approvable limit is 89,900,000, not 89,999,999.

## 3. Test evidence (this commit)

| Suite | Command | Result |
|---|---|---|
| Unit / service / property / validation matrix | `python3 -m pytest -q` | **182 passed** |
| End-to-end over HTTP (17 scenarios) | `python3 run.py --port 8080 --reset` + `python3 scripts/e2e_scenarios.py` | **17/17 passed** (`docs/test_logs/e2e_results.json`) |
| Browser regression (Playwright, 393 px + officer desktop) | `python3 scripts/ui_regression.py` | **45/45 checks passed** |
| Segment replay v4 → v5 + sensitivity | `python3 scripts/segment_replay.py --compare <v4 worktree> --sensitivity` | see `decision_rules.md` §9; `docs/test_logs/segment_replay.json` |
| Policy integrity | `python3 scripts/policy_lock.py` | version `2026.11-demo.5` pinned to `98ff3c2d…` |

Tests were rewritten to the v5 target, with derivations in comments. Where an old assertion encoded the audited defect (RED over-limit, cure by prepayment, CUS-002 unapprovable, 89,999,999 boundary), it was replaced by the corrected behaviour, not relaxed.

## 4. Remaining demo assumptions, open policy decisions, limitations

**Demo values pending HLBVN approval (all configurable in `demo_policy.json`):**
- STP ceiling 50M; aggregate digital-lending cap 100M (a placeholder: the law requires the bank to set its own limit, it sets no figure).
- DTI ceiling 0.50; 12% flat demo price; 500k STP rounding; 100k officer grid.
- Velocity limits: 3 applications per 30 days, 5 Pay Later purchases per day.
- Renewal from day 75 with a 30-day grace; cure after 3 instalments; default at > 90 days; freeze from day 1.

**Open decisions for HLBVN:**
- The stressed-income percentile (P25) is a material calibration choice: P10 → 50.5% GREEN, P50 → 60.4%.
- Freeze grace period (Circular 31/2024 keeps < 10 days overdue in Group 1).
- Delegated-authority tiers and four-eyes thresholds to replace the < 90M case assumption.
- Whether to keep Pay Now → instalment conversion while Circular 39 Art. 8.10 is only suspended.
- Circular 29/2026 effective date: 15 Aug per the text, 24 Aug in one LuatVietnam headline.
- Pricing (uniform demo rate, no risk-based pricing).

**Limitations of the prototype:**
- Every adapter is simulated.
- Device anomaly and duplicate identity are demo controls, not real eKYC or device intelligence.
- Inflow classification is only demonstrable on fixture CUS-004, because the dataset has no breakdown.
- The replay assumes verified, fresh Layer 1 data and no other HLB products.
- No outcome data, so no threshold is validated against defaults.
- The audit's "decision system must be assigned to named people" claim could not be confirmed from the texts reviewed.
