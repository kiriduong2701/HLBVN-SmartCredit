# Smart Credit: decision rules (v6, policy `2026.11-demo.6`)

**Status.** This is a prototype rule set on simulated data. Every threshold is a configurable parameter in `server/policy/demo_policy.json`, pinned in `server/policy/policy.lock.json` (sha256 `8d220a1f…`). None of them is an HLBVN-approved policy value.

**Sources.**
- Engine: `server/engine/*.py`.
- Services: `server/services/*.py`.
- Statistics: recomputed from `data/reference/hlbvn_dataset.xlsx` (10,000 rows) by `scripts/segment_replay.py` and `scripts/dataset_audit.py`.
- [`audit_response_v5.md`](audit_response_v5.md) is the v5 record. Where v6 changed a design, this document supersedes it.

**Basis key:**

| Key | Meaning |
|---|---|
| **D** | Documented (case brief or dataset definitions) |
| **L** | Vietnamese legal requirement (source in §13) |
| **P** | Dataset percentile. It describes the data and is not validated against outcomes. |
| **A** | Demo assumption, `PENDING_HLBVN_*` |
| **E** | External benchmark (source in §13), not Vietnamese law |
| **H** | Human decision |

The dataset does not feed live decisions. Outcome columns (approved amount, rate, default, manual/STP results, abandonment) are never inputs.

---

## 0. What v6 changes, and where it departs from the brief

The brief's requirements were treated as objectives. The table below records the decisions that matter most, including the cases where the brief was followed only partly or with a safeguard.

| # | Requirement | v6 decision | Why |
|---|---|---|---|
| 1 | Every dataset variable is Layer 1, with no extra consent | **Adopted for assessment consent.** There is no per-variable or per-source consent for Layer 1, and no question to the customer (the employment-type question is removed). **Kept separately:** one legally required personal-data step (`DATA_PROCESSING`: notice plus consent for the credit-assessment purpose, covering HLB data and the CIC enquiry). | Bank-transaction and credit data are sensitive personal data under Decree 356/2025. The exceptions in Law 91/2025 Art. 19 are not confirmed to cover pre-contract assessment and a CIC enquiry. Having the data does not mean HLBVN may process it. If Legal confirms a non-consent basis, the checkbox becomes an acknowledgement. |
| 2 | The system, not the officer or the customer, finds missing evidence | **Adopted.** The engine detects each gap and either closes it (Layer 2 request, automatic retry) or decides. Conflicting, stale or unverified values are **excluded from use** automatically and are never sent to an officer. The customer app never shows which Layer 1 variables were assessed, missing or excluded. | In v5, 39.4% of replay cases went to an officer at the first pass. 1,943 of them (R010-VER) were contradictory dataset records that an officer cannot fix. |
| 3 | Layer 2 decided automatically, with per-source unticked consent | **Adopted.** The system chooses sources by gap and segment, orders them by reliability, and stops calling once the gap is closed. Validated Layer 1 + Layer 2 income **can** support an automatic approval or increase (R001-L2) when AV-01..AV-07 hold. | v5 never let Layer 2 approve, so every customer who shared data still waited for an officer. |
| 4 | Behaviour signals are not income | **Adopted.** Utility/telecom history is behaviour evidence (AC-07) with factor 0. **E-wallet data is evaluated and not requested**: top-up counts are already a Layer 1 variable, top-ups are funded from bank accounts (double counting), and they are not income. | Data minimisation; avoid double counting. |
| 5 | RED, AMBER and Pay Now kept separate | **Adopted.** RED is a verified decline with an appeal. AMBER is either the system acquiring data or retrying, or an officer judgement. N/A (R030) means the product does not apply. Pay Now is a **payment option** shown next to every checkout outcome, including GREEN. | In v5, Pay Now was presented as if it were the outcome. |
| 6 | Reassess the 50M STP ceiling (consider 90.1M) | **Ceiling removed (automatic scope = 1.6M–90.1M), with a new safeguard:** the income-multiple cap now uses **recognised** income. Unclassified inflow of a merchant counts at 0.3 and of a platform worker at 0.7. | The 50M ceiling bound 918 replay rows without addressing a named risk. Removing it alone let unclassified merchant **turnover** reach 90.1M automatically: 165 MSME approvals above 50M, 11% of approved MSME at 90.1M. The safeguard leaves 48 above 50M and none at 90.1M. |
| 7 | Officer authority over the full scope | **Adopted as a case assumption:** ≤ VND 90,100,000 **inclusive**. v5's "< 90M" left 90.0M–90.1M approvable by nobody. | Production needs HLBVN delegated-authority tiers and four-eyes (§14). |
| 8 | Requests above 90.1M referred out of scope | **Adopted.** The in-scope part is assessed or split. The excess is referred to HLB lending (`OUT_OF_SCOPE_REFERRAL`, R030-OOS) and is never processed as eligible. | |
| 9 | Officers never request data | **Adopted.** `request_data` / `refresh_data` return 422 `DATA_ACTIONS_AUTOMATIC`. Officers decide on a consolidated case summary. | An officer cannot fix an API outage or obtain data the customer has not consented to share. |
| 10 | Capacity audit | Three changes (§4.3): **RF-04 removed** (a second DTI cut-off double-counted DTI; 798 referrals), **Layer 2 streams summed after de-duplication** instead of taking the single highest source, and the **recognised-income multiple** above. | |
| 11 | Immediate RED for verified conditions | **Adopted with limits.** RED only for verified arrears or default, a verified obligations breach (when Layer 2 cannot plausibly close it, or after Layer 2), or capacity that cannot be verified after the customer declined or Layer 2 returned no usable income. **Volatility, a thin file, missing CIC or an outage never produce RED.** | Circular 39 Art. 7 requires demonstrated repayment capacity. Where none can be verified, an officer could not verify it either. |

---

## 1. Operating model and authority

There is **one model**. The engine appraises every case and decides automatically wherever approved rules give a verified answer. It acquires missing evidence itself. A credit officer decides only the cases that need human judgement.

| Actor | Does | Never does |
|---|---|---|
| **Decision engine** | Layer 0 checks, all Layer 1 data, readiness, capacity, routing. GREEN (approve / counter-offer) and RED (verified decline) across the **whole product scope 1.6M–90.1M**. Chooses, calls, validates and retries Layer 2 sources. Re-decides automatically after new data. | Decides on unverified data. Declines for volatility, thin file, missing CIC or outage. Approves above 90.1M. |
| **Customer** | Accepts terms and the legal data step. Ticks the Layer 2 sources they agree to share. Accepts or declines a counter-offer. Appeals a RED once. Pays now at any time. | Answers data questions or uploads documents. Sees which Layer 1 data was used. |
| **Credit officer** | Decides referred cases (approve / reduce / reject / confirm fraud) within **[1.6M, officer maximum] ∩ ≤ 90.1M inclusive**, with a reason code that fits the basis (EL-10). Reviews the QA sample. | Requests, refreshes or edits data. Approves above the officer maximum. |
| **HLB lending (outside Smart Credit)** | Human review of any amount above 90.1M (`OUT_OF_SCOPE_REFERRAL`). | — |

The appraisal is automated, and decisions are either rules approved by HLBVN (automatic) or an officer's (referred). Whether automated decisions satisfy the Circular 39 Art. 17.2 separation of appraisal and decision is **open for HLBVN Legal** (§14).

```
activate ─► L0 identity ─► Layer 1 (all dataset variables; legal data step) ─► readiness (exclude unusable values) ─► capacity ─► routing
                                                                                                                                  │
  GREEN R001 / R001-L2 ── line opens at min(capacity, requested, 90.1M) ◄─────────────────────────────────────────────────────────┤
  GREEN R001-CO ── counter-offer; opens only on accept · "ask for more" → automated Layer 2 ◄──────────────────────────────────────┤
  AMBER R010-EVD/-INCOME/-CAPX/-THIN ── automated Layer 2 request (per-source unticked consent) ── call · validate · stop · re-decide ◄┤
  AMBER R010-API ── SYSTEM_RETRY (state ASSESSMENT_PENDING; retried each servicing cycle; ops alert after 24 h) ◄──────────────────┤
  AMBER R010-FRD/-VER/-OBL/-FLAG/-ALT/-THIN/-CAPX ── officer case summary ── approve / reduce / reject / confirm fraud ◄───────────┤
  RED   R020-DEF/-ARR/-CAP/-EVD/-INC ── verified decline, one appeal ◄─────────────────────────────────────────────────────────────┤
  STOP  HS-01 (no legal data step) · HS-05 (officer-confirmed fraud) ◄──────────────────────────────────────────────────────────────┘
checkout ─► TX-00..TX-08 ─► GREEN (hold) │ AMBER R010-SPLIT (explicit) │ RED │ N/A R030 │ operational AMBER — Pay Now always offered
```

---

## 2. Data acquisition (`services/acquisition.py`, `engine/readiness.py`, `engine/identity.py`)

### 2.1 Layer 0: identity and fraud (A)

The checks below run at activation (L0-04 and L0-06 at activation only; L0-05 at checkout only).

| Check | Rule |
|---|---|
| L0-01 | eKYC completed and CCCD chip matches the bank record |
| L0-02 | National ID not linked to another customer record |
| L0-03 | Session on a bound device |
| L0-04 | No more than 3 applications in 30 days |
| L0-05 | No more than 5 Pay Later purchases per day (checkout) |
| L0-06 | Layer 2 record identity mismatch (AV-01 failed) |

**Effect of a hit:**
- An unreviewed signal routes to AMBER **R010-FRD** (route `FRAUD_REVIEW`). It is never an automatic decline.
- A hit at checkout means Pay Now only.
- STOP **HS-05** applies only after an officer confirms fraud (FR-01).

### 2.2 Layer 1: every dataset variable, no extra consent

Layer 1 is pulled automatically at activation, limit increase, renewal and reinstatement. Each call is logged with status, attempts, latency and request id. The **legal data step** is one checkbox at activation: `DATA_PROCESSING`, kind `LEGAL_PERSONAL_DATA`, categories `hlb_transactions`, `credit_information`, `e_wallet_activity`. It sits next to the product terms (`SC_TERMS`). Without it the outcome is STOP HS-01, because nothing may be read.

| Variable | Source (simulated) | Decision use in v6 |
|---|---|---|
| `customer_profile` | HLB core segment label | Chooses eligible Layer 2 sources and the recognised-income type for unclassified inflow (§4.2). An unknown segment gets Open Banking only and the most conservative factor (0.3). Never asked from the customer. |
| `has_observable_tx` | HLB transactions | Evidence path EV-TX (with ≥ 6 months). |
| `monthly_inflow_vnd`, `inflow_cv` | HLB transactions | Income (average and stressed P25), §4. |
| `on_time_bill_ratio` | HLB transactions | Behaviour signal (AC-07); RF-03 flag below 0.663 (P). |
| `rapid_drain_flag` | HLB transactions | R-REV-01 flag (D). |
| `casa_balance_vnd`, `regular_weekly_pattern` | HLB transactions | Validated and shown to officers. **Not a decision input**: no tested relationship to repayment exists in the data, and a balance is a stock, not income. |
| `has_formal_cic_history` | CIC | Evidence path EV-CIC; behaviour signal; obligation lineage. |
| `unified_credit_score` | CIC | Range check (350–850); RF-02 flag below 436 (P). Never an approval cut-off. |
| `dti_ratio` | CIC / observed | Absolute obligations = ratio × the inflow it was measured on (§4.1). The P90 value (0.3695) is shown to officers only (RF-04 removed). |
| `e_wallet_topups_pm` | Partner data in the dataset | Validated and shown to officers. Not income, not a decision input. |
| `internal_delinquency_flag`, `existing_hlb_products` | HLB loan systems | RF-01 flag; HLB instalments added to obligations unless already in the DTI figure. |

Requested amount and tenure are application fields, not Layer 1. Outcome fields are excluded (`excluded_downstream_fields`).

**Data quality is handled by the system (R-VER-01..04), not routed to an officer:**
1. A value that is conflicting (for example `has_observable_tx = 0` while transaction values are present), unverified, stale (> 90 days) or of unknown provenance is re-pulled once.
2. If it is still unusable, it is **excluded from use**. This is recorded as a `data_quality_event` with resolution `EXCLUDED_FROM_USE`, and the field is treated as absent, so routing can close the gap with Layer 2.
3. Only **INVALID** values (impossible formats or ranges, a data-integrity defect) go to an officer (R010-VER).

**Customer visibility:** the customer application view contains only:
- status;
- the Layer 2 request (generic item text, plus each source's name, purpose and data requested);
- Layer 2 outcomes.

It never includes Layer 1 statuses, variable names, rule ids, excluded fields or evidence gaps. This is tested in `test_customer_view_never_exposes_layer1_detail`.

### 2.3 Layer 2: catalogue (all adapters SIMULATED)

| Source | Tier | Provides | Recognition | Eligible segments | Order | Access and reliability (A, pending Legal) |
|---|---|---|---|---|---|---|
| **Open Banking** (account at another bank) | A | Income by category, history | Same type factors as Layer 1 (salary 1.0, platform 0.7, turnover 0.3, unidentified 0) | All, including unknown | 1 | SBV Circular 64/2024 Open API: consent-based, contract with the data-holding bank (Art. 8), access ≤ 180 days after consent (Art. 11.6), banks must comply by 1 Mar 2027 (Art. 15.2). |
| **Gig platform** payouts | B | Platform earnings | 0.7 | Gig / platform workers | 2 | Partnership and consent needed. First-party payout records; volatile. |
| **Marketplace** seller payouts | B | Turnover | 0.3 | MSME | 3 | No public API; partnership needed. |
| **E-invoice / tax** revenue | B | Turnover | 0.3 | MSME | 4 | Held by the tax authority (Decree 123/2020); taxpayer authorisation needed; conditional. |
| **Utility / telecom** bills | C | Payment behaviour only | 0 (never income) | Anyone with **no** behaviour signal | 5 | Partnerships needed. Predicts delinquency in US studies (PERC). Says nothing about income. |
| E-wallet | C | — | — | **Never requested** | — | Duplicates a Layer 1 variable; funded from bank accounts. |
| Logistics, rental | — | — | — | Excluded | — | Not income; no consent-based access route evidenced. |

**Factor rationale (A, calibration items):**
- **0.7 platform.** Payouts are after commission but before the worker's own vehicle, fuel and maintenance costs. Vietnamese ride-hailing drivers report deductions of about 35–50% of the fare and bear these costs themselves (VietNamNet, Sep 2026). No measured cost share was found, so 0.7 is a policy value.
- **0.3 turnover.** Cost of goods, fees and stock are unknown. The factor caps how much of turnover can count as income until HLB calibrates it on merchant cash flows (inflows minus business outflows).

### 2.4 Layer 2 workflow (rules in `data_acquisition.layer2_plan`)

1. **Detect the gap.** The routing engine names it:

   | Gap | Condition | Rule |
   |---|---|---|
   | `EVIDENCE` | No EV-TX or EV-CIC | R010-EVD |
   | `INCOME` | No verified Layer 1 income | R010-INCOME |
   | `CAPACITY` | Stressed capacity below the minimum | R010-CAPX |
   | `OBLIGATIONS` | Verified obligations breach on average income | R010-CAPX, requested **only if** extra income needed ≤ `max_uplift_ratio` 1.0 × verified income; otherwise RED R020-CAP at once |
   | `BEHAVIOUR` | No CIC history, no bill ratio | R010-THIN |
   | `REQUEST` | Capacity below the requested limit | **Never automatic.** A counter-offer comes first, and Layer 2 runs when the customer asks for more or requests an increase. |

2. **Select sources.**
   - For an income gap: the income sources eligible for the segment, in the order Open Banking → gig → marketplace → e-invoice.
   - For a behaviour gap only: add utility/telecom.
   - A behaviour-only source is never requested to fix an income gap.
3. **Ask once.** One request per application, plus a second only when the customer asks for more after a counter-offer.
   - Each source has its own **optional, unticked** checkbox with its purpose and the data requested. Unticked or missing means not allowed.
   - Each answer is stored as a separate `ALT_DATA:<source>` consent.
   - Declining is **never itself** a decline reason.
4. **Call in priority order** (3 s timeout, one retry).
   - After each validated result, capacity is recomputed. Calling **stops as soon as the gap is closed**; consented but uncalled sources are recorded `NOT_CALLED_GAP_CLOSED` (data minimisation).
   - An outage keeps the case in automatic assessment (`SYSTEM_RETRY`).
5. **Validate** each response:

   | Check | Rule | Effect if failed | Justification |
   |---|---|---|---|
   | AV-01 | Record matches on national ID **and** one more identifier (name or phone) | Data not used. **L0-06 → R010-FRD.** | A mismatch is a fraud signal, not missing data. |
   | AV-02 | ≥ 6 months (utility: 12) | Not usable | Same observation period as Layer 1. |
   | AV-03 | ≤ 45 days old | Not usable | A one-off read near the decision. |
   | AV-04 | Whole, non-negative values; consistent counts | Not usable | Integrity. |
   | AV-05 | Income in ≥ 5 of the last 6 months | Usable by an **officer only** (R010-ALT) | Continuity (cf. Fannie Mae B3-3.1-01: income must be reasonably expected to continue). |
   | AV-06 | Last 3 months ≥ 70% of the previous 3 | Officer only (R010-ALT) | A known reduction means using the lower amount (same source). |
   | AV-07 | De-duplication (below) | Applied when streams are built | No double counting. |

6. **De-duplicate (AV-07):**
   - Platform or marketplace payouts paid into the HLB account are already in Layer 1 and are not added again.
   - Payouts paid into another bank are taken from the platform record and removed from Open Banking credits.
   - Open Banking transfers from the customer's own HLB account are excluded.
   - HLB inflows identified as transfers from the customer's other-bank account are deducted from Layer 1 (`layer1_deduction_vnd`).
7. **Aggregate.** Streams are **summed** after de-duplication.
   - The automatic basis is the sum of each stream's stressed (empirical P25) month, which is never above the P25 of the total.
   - The officer basis is the sum of averages.
   - v5 used only the single highest source, which understated customers with more than one income.
8. **Re-decide automatically.** Possible results:
   - **GREEN R001-L2.** Requires AC-01..AC-08: every Layer 2 income stream used passes AV-01..AV-07, Layer 1 rules still pass, and no flag is raised.
   - **Officer.** R010-ALT (AV-05/06 failed), R010-THIN (still no behaviour signal), R010-FLAG, R010-CAPX (stressed capacity still short).
   - **RED.** R020-EVD (no usable income) or R020-CAP (breach not closed).
   - **Retry.** On outage.
9. **Withdrawal.** The customer can withdraw each source separately. Its data is purged from the application, and a decision already taken is not reversed.

**STP safeguards when Layer 2 was used:**
- Identity match on two identifiers.
- ≥ 6 months and ≤ 45 days old.
- Stability and trend gates.
- Type factors.
- De-duplication.
- No behaviour data used as income.
- The same 50% debt-service and recognised-income-multiple caps.
- QA sample raised from 5% to **10%**.

**Escalation triggers:**
- AV-01 mismatch → fraud review.
- AV-05/06 failure → officer.
- Outage longer than 24 h → customer told and ops alerted (`DATA_SOURCE_OUTAGE_ESCALATED`). No officer is involved.

---

## 3. Limit-activation routing (`engine/routing.decide`)

Precedence is deterministic: the first match wins. Every decision records `secondary_codes` (all other rules that fired), `policy_version`, `policy_hash`, `decided_by` and `route`.

| Order | Condition | Band · code | Route → who acts next | Basis |
|---|---|---|---|---|
| 0a | Legal data step (`DATA_PROCESSING`) missing or withdrawn | STOP · HS-01 | `POLICY_STOP` | L + A |
| 0b | Fraud confirmed by an officer | STOP · HS-05 | `POLICY_STOP`; line suspended | A |
| 0c | Unreviewed Layer 0 hit (L0-01..L0-06) | AMBER · R010-FRD | `FRAUD_REVIEW` → officer (AP-06 / FR-01) | A |
| 1 | Smart Credit default (> 90 days) / any arrears | RED · R020-DEF / R020-ARR | `POLICY_DECLINE`, appeal | A |
| G | Case already with an officer and re-evaluated | AMBER · R010-RECHECK | Stays with the officer; only 0a/0b/1 override | A |
| 2 | Layer 1 (or Layer 2) source unavailable after one retry | AMBER · R010-API | `SYSTEM_RETRY` (state `ASSESSMENT_PENDING`): retried each servicing cycle; ops alert after 24 h; **no officer** | A |
| 3 | INVALID source data (impossible values) | AMBER · R010-VER | Officer (data integrity) | D + A |
| 4 | No evidence path / no verified Layer 1 income | AMBER · R010-EVD / R010-INCOME → **RED · R020-EVD** | Automated Layer 2 (income sources, plus utility if no behaviour signal). After a declined, not-found or invalid result: RED "capacity cannot be verified", with appeal or re-apply. Outage: retry. No eligible source: RED. | L (Circ. 39 Art. 7) + A |
| 5 | Obligations not convertible to VND | AMBER · R010-OBL | Officer | A |
| 6 | **Verified** obligations leave capacity < minimum even on **average** income | AMBER · R010-CAPX → RED · R020-CAP | If extra income needed > 1.0× verified income: **RED at once**. Otherwise Layer 2 first, then RED if it is not closed. | A |
| 7 | Validated Layer 2 income fails AV-05 or AV-06 | AMBER · R010-ALT | Officer, with officer maximum shown | A |
| 8 | No repayment-behaviour signal (no CIC history, no bill ratio, no validated utility history) | AMBER · R010-THIN | Automated utility/telecom request, then officer | A |
| 9 | Profile risk flag: R-REV-01 rapid drain (D), RF-01 internal delinquency, RF-02 score < 436 (P), RF-03 / RF-03L2 on-time bills < 0.663 (P) | AMBER · R010-FLAG | Officer, approvable up to the officer maximum | D / P |
| 10 | **Stressed** capacity < minimum (volatile or partial income) | AMBER · R010-CAPX | Automated Layer 2, then officer. **Never RED.** | A |
| 11 | AC-01..AC-08 pass | GREEN · R001 / R001-L2 | Line opens at min(capacity, product maximum 90.1M) | A |
| 11b | …capacity below the requested limit | GREEN · R001-CO | `OFFER_PENDING`: opens only on accept. "Ask for more" → Layer 2 (`REQUEST` gap). Decline → cancelled. | A |
| 11c | `LIMIT_INCREASE` | GREEN (requested ≤ capacity) / R001-CO (current < capacity < requested) / R010-CAPX Layer 2 / **RED · R020-INC** (line unchanged) | Automatic | A |
| 11d | `LINE_REVIEW` (renewal) | GREEN at ≤ current limit | Never raises a limit automatically | A |

**Outcome taxonomy:**

| Outcome | Meaning | Customer sees | Officer involved? |
|---|---|---|---|
| GREEN | Approved inside the policy rules | Limit (or counter-offer) | No (QA sample 5% / 10%) |
| RED (R020) | Verified decline: arrears, default, verified breach, unverifiable capacity, increase not supported | Reason category, appeal (once) | Only on appeal; approvable only if the reason has cleared (EL-04) |
| AMBER, system acquiring data | Layer 2 request (EVD / INCOME / CAPX / THIN) | Sources with purposes, unticked | No |
| AMBER, system retry | Source outage (R010-API) | "We're still checking" (`checking_reason`) | No |
| AMBER, officer | FRD, VER, OBL, ALT, THIN (after Layer 2), FLAG, CAPX (after Layer 2), RECHECK | "Being reviewed", expected hours | Yes |
| N/A (R030) | Product does not apply: merchant, amount below 1.6M, amount above 90.1M (R030-OOS) | Pay Now; referral above 90.1M | No |
| STOP | HS-01 / HS-05 | Consent or identity message | HS-05 set by an officer |

**Guarantees** (tests in `tests/test_v6_rules.py`, `tests/test_validation_matrix.py`, `tests/test_v5_controls.py`):
- No RED at the first pass in any segment (replay: 0 of 10,000).
- RED after a declined Layer 2 request is only R020-EVD or R020-CAP (V-28).
- RED always carries an R020 code and an appeal (property test, 800 random cases).
- Volatility, partial income, missing CIC and outages never produce RED.
- An outage never routes to an officer.
- Officer referrals carry only judgement codes; Layer 2 is requested only for a data gap and only once.
- GREEN always stays inside [1.6M, 90.1M].

---

## 4. Capacity (`engine/capacity`)

### 4.1 Formula

```
Income, Layer 1    average  = verified 6-month HLB inflow; with a classified breakdown only eligible categories × type
                              factor (salary 1.0, platform 0.7, merchant settlements 0.3); own-account transfers, loan
                              proceeds, refunds/reversals and e-wallet movements excluded; minus identified transfers from the
                              customer's other bank (AV-07)
                   stressed = 25th-percentile month: empirical from a ≥ 6-month series, else mean × (1 − 0.6745 × CV)
                              (CV missing → 0.5)
Income, Layer 2    per stream: validated monthly series × type factor; stressed = empirical P25; summed after AV-07
Recognised income  for the income-multiple cap only: classified income as above; UNCLASSIFIED Layer 1 inflow × the type factor
(v6)               implied by the segment (MSME 0.3, gig 0.7, salaried / first-time 1.0, unknown 0.3) + Layer 2 recognised
Obligations        ABSOLUTE VND/month = dti_ratio × the gross inflow it was measured on; lineage CIC_REPORTED | TX_OBSERVED |
                   CIC_NO_RECORD (= 0); + HLB instalments not already in the DTI figure. Never rescaled to another income.
Sizing(I, R)       headroom = 0.50 × I − obligations
                   raw      = min( headroom × 12 / (1 + 12% × 12/12), 2 × R, 90.1M, aggregate digital-lending headroom )
Automatic limit    Sizing(stressed I, stressed R). Minimum 1.6M tested on RAW capacity; rounded down to 500k, never below the
                   minimum; raw ≥ 90.1M → exactly 90.1M
Officer maximum    Sizing(average I, average R) on a 100k grid
Verified breach    officer maximum < minimum → the only capacity path to RED (§3 rule 6)
Affordability      each tenure tested on its LARGEST instalment: (obligations + Smart Credit instalments + held instalments +
(checkout)         new instalment) / income ≤ 0.50
```

The officer screen shows each income stream with its lineage, type, factor and recognised-income multiplier. It also shows obligation components with lineage, automatic versus officer sizing, and the binding constraint.

### 4.2 The recognised-income multiple (new in v6)

**Problem.** The supplied dataset has no inflow breakdown, so `monthly_inflow_vnd` is used as income with label `UNCLASSIFIED_INFLOW`. For a merchant this is sales turnover; for a platform worker it is pre-cost payouts. With the 50M ceiling removed, the replay showed:

| Replay result | Value |
|---|---|
| Automatic MSME approvals above 50M | 165 |
| Share of approved MSME at 90.1M | 11% |
| MSME median automatic limit | 36.0M (2 × stressed turnover) |

**Options tested** (MSME and gig, replay):

| Option | MSME GREEN | MSME median limit | MSME > 50M | Gig GREEN | Verdict |
|---|---|---|---|---|---|
| No safeguard | 59.4% | 36.0M | 165 | 21.1% | Unjustified approvals |
| 50M ceiling for MSME only | 59.4% | 36.0M | 0 | 21.1% | Amount cap; leaves the turnover overstatement below 50M |
| Type factor on the whole automatic income | 0% (97% to Layer 2) | — | 0 | 6.9% | Excessive conservatism: also cuts the debt-service base, which is inconsistent with the bureau DTI measured on gross inflow |
| **Type factor on the income-multiple cap only (adopted)** | **59.4%** | **11.5M** | **48** | **21.1%** (median 13.0M → 10.5M) | Bounds exposure by recognised income; approval rates unchanged |

**Rule.**
- The 50% debt-service test keeps the gross inflow. This is the base the dataset's DTI ratio is defined on, so income and obligations stay consistent.
- The **exposure** cap (2× income, BNM benchmark) uses recognised income.
- Classified inflow (HLB core breakdown) and Layer 2 streams already carry their factors, so nothing is applied twice.
- Customers whose turnover understates income can raise the limit through Layer 2 (e-invoice, marketplace, Open Banking by category), which runs when they ask for more.

Tests: `RecognisedIncomeMultiple` (3 tests).

### 4.3 Methodology audit

| Element | Finding (evidence) | v6 action |
|---|---|---|
| Income basis | Unclassified turnover and payouts counted at 1.0 (§4.2) | Recognised-income multiple; HLB core classification needed in production |
| Stress | P25 month is a policy choice. Replay sensitivity: P10 → GREEN 54.2% (gig 0.5%, MSME 30.1%); P25 → 63.8%; P50 → 67.0% (gig 30.6%, MSME 66.9%) | Kept P25. **Calibration item** with the most segment-sensitive effect |
| Obligations | Absolute VND with lineage (v5 fix) holds; no rescaling found | Unchanged |
| DTI | Ceiling 0.50 in the formula **plus** a P90 percentile flag RF-04 (0.3695) referred 798 rows for DTI alone: the same risk counted twice | **RF-04 removed from routing** (shown to officers as information) |
| Multiplier | 2× income is the binding constraint for 65% of GREEN rows (4,144 of 6,378). Benchmark: BNM limits credit-card limits to 2× monthly income per issuer for low-income cardholders (E) | Kept at 2×, now on recognised income. **Calibration item** |
| Factors | Platform 0.7 and turnover 0.3 are policy values without measured margins | **Calibration items** |
| Caps | The 50M ceiling bound 918 rows without a stated risk | Removed. Product maximum is an exact cap; the aggregate digital cap of 100M is a placeholder (L principle, A value) |
| Minimum / maximum | Minimum on raw capacity (v5) holds. 500k rounding made 90.1M unreachable | Raw ≥ 90.1M → 90.1M (exact) |
| Rounding | Automatic 500k down; officer 100k grid | Unchanged |
| Layer 2 aggregation | Single highest source understated multi-income customers | Summed after de-duplication |
| Predictive claims | All 436 defaults have approved amount 0: the default label is unusable as a target | **No model, no predictive claim.** Percentile flags route to officers and never decline |

**Segment fairness (replay, §10).**
- No segment receives a first-pass RED.
- Officer referrals fell for every segment.
- Gig remains the most referred segment (42.6%), mostly for R010-FLAG (639 rows: score and bill-payment percentiles), which v6 leaves to judgement.
- First-time borrowers are most dependent on Layer 2: 53.6% receive a request, and 51.4% would be RED if they declined.
- Differences in RED-if-declined reflect evidence coverage, not segment membership. Segment is never an input to approve or decline; it affects only source eligibility and the recognised-income type.

### 4.4 Calibration parameters (all **A**, `PENDING_HLBVN_CREDIT_POLICY`)

| Parameter | Value |
|---|---|
| `max_total_dti` | 0.50 |
| Stress percentile | 25 |
| `cv_when_missing` | 0.5 |
| `income_multiplier_cap` | 2.0 |
| `recognition_by_type` | salary 1.0, platform 0.7, turnover 0.3, unidentified 0 |
| `unclassified_multiplier_type_by_segment` | MSME 0.3, gig 0.7, salaried / first-time 1.0, unknown 0.3 |
| Reference tenure / rate | 12 months / 12% |
| `min_limit_vnd` | 1.6M (D) |
| `product_max_limit_vnd` | 90.1M (D) |
| Rounding | 500k automatic / 100k officer |
| Aggregate digital-lending cap | 100M (placeholder for Art. 22.2(h)) |
| RF-02 / RF-03 | 436 / 0.663 (P10) |
| AV-02 / AV-03 / AV-05 / AV-06 | 6 months / 45 days / 5 of 6 months / 0.7 |
| `max_uplift_ratio` | 1.0 |
| Retry window | 24 h |
| Freshness | 90 days |
| QA sample | 5% / 10% (Layer 2) |
| Renewal | Day 75 |
| Review grace | 30 days |
| Checkout timer / processing timeout | 300 s / 120 s |

None is calibrated on outcomes; §14 lists what is needed.

---

## 5. Product scope, limits, increases and lifecycle decisions

| Case | Rule |
|---|---|
| Below 1.6M | Activation request → 422 `INVALID_LIMIT`. Purchase → N/A **R030-AMT**, Pay Now. Not a credit decision. |
| Within 1.6M–90.1M | Assessed. Automatic GREEN/RED anywhere in scope; officer authority ≤ 90.1M inclusive. |
| Above 90.1M (activation) | Assessed for the in-scope part (requested capped at 90.1M → counter-offer). `out_of_scope {requested_vnd, max_vnd}`, `OUT_OF_SCOPE_REFERRAL` audit event with `excess_vnd`, customer notification. Never processed as eligible above 90.1M. |
| Above 90.1M (checkout) | Split tender with Pay Later ≤ min(available, affordable, 90.1M), cause `PRODUCT_MAX`. If no in-scope part ≥ 1.6M: N/A **R030-AMT** (`AMOUNT_ABOVE_MAX`), Pay Now. |
| Counter-offer | GREEN R001-CO; the line opens only on accept; "ask for more" → Layer 2 (second round). |
| Limit increase | Automatic (§3 rule 11c). The current line stays usable; RED R020-INC leaves it unchanged. |
| Renewal | From day 75 a Layer 1 refresh re-runs the rules. GREEN renews at ≤ current limit; anything else goes to an officer while the line stays usable ≤ 120 days. |
| Reinstatement | After a cured default: officer case R010-REINST (RI-01, ≤ previous limit, refreshed Layer 1). |

---

## 6. Instant checkout (`engine/checkout_rules.decide`), on every purchase

| Check | Fail → band · code | decision_class | Basis |
|---|---|---|---|
| TX-00 Layer 0 (identity, device/session, ≤ 5 Pay Later purchases a day, confirmed fraud) | AMBER R010-FRD (Pay Now; fraud review) / STOP HS-05 | FRAUD_HOLD | A |
| TX-01 Merchant eligible (MCC, partner, dynamic QR verified against the order) | **N/A R030-MER** | PRODUCT_SCOPE | A |
| TX-02 Amount ≥ 1.6M (above 90.1M → only an in-scope part, §5) | **N/A R030-AMT** | PRODUCT_SCOPE | D |
| TX-03 Bank systems available | AMBER R010-API (Pay Now, convert later) | OPERATIONAL | A |
| TX-04 No arrears / default | RED R020-ARR / R020-DEF | CREDIT | A |
| TX-05 Line ACTIVE (pending → R010-PENDING; SUSPENDED → R020-DEF; FROZEN → R020-ARR; none → NO_LINE, offer activation) | AMBER / RED / NO_LINE | OPERATIONAL / CREDIT / NO_LINE | A |
| TX-06 Assessment ≤ 90 days, or renewal in progress within 30-day grace | AMBER R010-STALE | OPERATIONAL | A |
| TX-07 Amount ≤ min(limit − principal − holds, aggregate headroom) | AMBER **R010-SPLIT** if the eligible part ≥ 1.6M; else AMBER R010-LIMREV | CREDIT / OPERATIONAL | A + L |
| TX-08 ≥ 1 tenure affordable on the largest instalment, obligations refreshed | AMBER **R010-SPLIT** if a smaller part ≥ 1.6M is affordable; else **RED R020-AFF** | CREDIT | A |
| All pass | GREEN R001, hold placed | CREDIT | — |

Every result carries `credit_outcome` (`APPROVED` / `PARTIAL` / `DECLINED` / `NOT_ASSESSED`) and `payment_options`, which **always includes `PAY_NOW`**. GREEN gives `["PAY_LATER", "PAY_NOW"]`. Checkout never waits for an officer.

### 6.1 Split-tender lifecycle (`services/checkout.py`, `services/base.py`)

| Step / failure | Behaviour | Test |
|---|---|---|
| Offer | R010-SPLIT returns `{pay_later_vnd, pay_now_vnd, cause}`; **nothing is held** until the customer chooses split | `test_switching_to_pay_now_or_cancelling_releases_the_split_hold` |
| Explicit acceptance `POST /{tx}/split` | Re-decided inside a `BEGIN IMMEDIATE` transaction against the live ledger; the hold covers **only the Pay Later part** | `test_concurrent_split_acceptances_never_exceed_the_limit` |
| Authorisation | One challenge for the full amount; the Pay Now part is checked against the balance (`INSUFFICIENT_FUNDS`) | `test_insufficient_balance_for_the_pay_now_part_fails_cleanly` |
| `pay` (Idempotency-Key) | Re-checks the ledger and aggregate cap; **account debit, loan booking, hold booking and merchant settlement commit in one database transaction** (all or nothing) | `test_partial_failure_posts_nothing_then_retry_completes_once` |
| Replay of the same key | Same result, no second posting; another key on a paid order → `DUPLICATE_PAYMENT` | same test (`DUPLICATE_PAYMENT` asserted) |
| Injected failure | Nothing posted; hold kept; retry with fresh authentication completes once | same |
| Stuck in processing > 120 s | `reconcile_stuck_payments` marks it FAILED (`RECONCILED_TIMEOUT`); nothing to compensate | `test_stuck_processing_is_reconciled_without_posting` |
| Cancel / switch to Pay Now / timer expiry | Hold released (`sweep_expired`) | e2e scenario 14 |
| Concurrency | Holds are placed and rechecked under `BEGIN IMMEDIATE`; of two concurrent acceptances only one that fits succeeds | V-21, split concurrency test |

**Production gap:** with a real core banking system and card/QR switch, the single local transaction becomes a saga (reserve → debit → book → settle, with compensations). The states and idempotency keys here are the contract for that.

---

## 7. Officer controls (`services/officer.py`)

**Case summary (`case_summary`), assembled automatically:**
- Recommended limit, automatic capacity and officer maximum.
- Income streams with lineage, tier, factor and recognised-income multiplier, plus the stressed and average amounts.
- Obligations with lineage, and the binding constraint.
- All Layer 1 evidence, with verified/excluded status and observation date.
- Data-quality events.
- Risk indicators (RF-04 as information only).
- Triggered rules (primary plus secondary).
- Reasons.
- **Unresolved issues** (what the officer must judge).
- Automated data operations (Layer 1 calls, Layer 2 rounds with consents and outcomes).

**Actions:**
- `approve`, `reject` and `confirm_fraud` (FR-01 → HS-05).
- Each needs a note; approve and reject need a reason code.
- Optimistic versioning refuses stale decisions (409).
- `request_data` and `refresh_data` → 422 `DATA_ACTIONS_AUTOMATIC`.

**Eligibility guard** (every check must pass):
- EL-01 in review.
- EL-02 consents.
- EL-03 no arrears or default.
- EL-04 not RED, or on appeal the RED reason has cleared on verified data.
- EL-05 capacity computable.
- EL-06 officer maximum ≥ minimum.
- EL-09 increases and reviews must exceed the current limit.
- **EL-08 authority ≤ 90,100,000 inclusive.**
- EL-07 within [1.6M, officer maximum] on the 100k grid.
- **EL-10 reason code fits the basis:** above the stressed capacity only AP-01 / AP-02 / AP-05; fraud cases only AP-06; reinstatement only RI-01.

**QA:**
- Deterministic sample of automatic GREEN decisions (hash of application id): 5%, or **10% when Layer 2 income was used**.
- A DISAGREE outcome opens a line review (R010-REVIEW).

**Production requirements (not built):**
- Staff SSO identity on every decision.
- HLBVN delegated-authority tiers in place of the single 90.1M case assumption.
- Four-eyes above an HLBVN threshold and for fraud confirmation.
- Credit-committee approval and versioning of the automated rule set.
- Legal confirmation of automated decisions (§14).

---

## 8. Pricing, overdue interest, conversion and servicing (unchanged from v5)

**Pricing.** 12% a year flat on the original principal (A). Equal instalments are rounded down to 1,000 VND, with the remainder on the last. EAR and the reducing-balance equivalent are disclosed.

**Overdue interest** (L, Circular 39 Art. 13.4):
- Overdue principal accrues at 150% of the contract rate.
- Unpaid interest accrues at 10% a year.
- Daily simple interest on a 365-day basis.
- Example: 9 days late on 2,120,000 = **9,173 VND**.
- No late fee.

**Pay Now → instalments within 7 days.** The loan purpose is `PURCHASE_FINANCING`, and the disbursement is recorded as a reversal of the purchase debit (at most once, never more than the purchase). This depends on Circular 39 Art. 8.10 remaining suspended (Circular 10/2023), so it can be switched off.

**Servicing:**

| Event | Rule |
|---|---|
| Arrears | Any overdue instalment freezes new Pay Later (HS-03). |
| Default | More than 90 days overdue → simulated default and suspension (HS-04). |
| Cure | Three on-time instalments, each counted only after its due date. |
| Consent withdrawal | Monitoring stops, the line closes to new drawdowns, and existing loans continue (Decree 356/2025: respond within 2 working days, implement within 15 days). |
| Nightly cycle | Also retries assessments waiting on a source and reconciles stuck payments. |

---

## 9. Explainability and policy integrity

- **Stored with each decision:**
  - band, reason code, rule ids, `secondary_codes` and plain-language messages;
  - the capacity breakdown;
  - the Layer 2 rounds;
  - `policy_version` and `policy_hash`.
- **Customer messages** use reason categories only.
- **Policy lock:** `policy.lock.json` pins each version to its hash, and `scripts/policy_lock.py` refuses to re-pin a changed policy under a released version. Tests fail if the policy and the lock disagree, and the server refuses to start with a policy that fails `policy_integrity.validate()` (legal caps, ceiling ≤ authority, required blocks).

---

## 10. Segment replay (indicative, `scripts/segment_replay.py --compare <v5> --sensitivity`)

**Assumptions:**
- Pre-assessment fields only.
- Every Layer 1 source verified and fresh, with the legal step given.
- **No Layer 2 data is available**, so "Layer 2 request" counts cases that would be asked.
- No other HLB products.

This is engine behaviour on simulated data, **not credit performance**. Output: `docs/test_logs/segment_replay.json`.

| Segment | n | Manual approval (dataset) | GREEN v5 → v6 | Officer referral, 1st pass v5 → v6 | Layer 2 request v5 → v6 | RED v5 → v6 | Purchase financed instantly v5 → v6 (full + split) | RED if Layer 2 declined (v6) |
|---|---|---|---|---|---|---|---|---|
| Salaried, with credit history | 3,550 | 79.5% | 82.1% → 91.4% | 17.7% → 7.9% | 0.2% → 0.6% | 0 → 0 | 82.1% → 91.4% (58.9% + 32.6%) | 0.2% |
| Salaried, no credit history | 2,050 | 40.2% | 71.6% → 77.8% | 28.3% → 3.4% | 0.1% → 18.9% | 0 → 0 | 71.6% → 77.8% (46.2% + 31.5%) | 18.0% |
| First-time borrowers | 1,450 | 9.9% | 24.6% → 24.8% | 74.8% → 21.6% | 0.6% → 53.6% | 0 → 0 | 24.6% → 24.8% (24.6% + 0.3%) | 51.4% |
| Online merchants (MSME) | 1,450 | 28.8% | 58.4% → 59.4% | 38.9% → 3.0% | 2.7% → 37.6% | 0 → 0 | 58.4% → 59.4% (2.1% + 57.3%) | 30.1% |
| Gig / platform workers | 1,500 | 20.0% | 21.1% → 21.1% | 72.4% → 42.6% | 6.5% → 36.3% | 0 → 0 | 21.1% → 21.1% (11.5% + 9.6%) | 26.8% |
| **All** | 10,000 | — | **59.0% → 63.8%** | **39.4% → 13.5%** | 1.5% → 22.8% | 0 → 0 | 59.0% → 63.8% (36.0% + 27.8%) | 19.6% |

**Referrals and RED:**
- First-pass AMBER reasons: R010-EVD 1,943 (Layer 2 request), R010-FLAG 1,347, R010-CAPX 332.
- Officer referral if every Layer 2 request were declined: 16.6%.
- RED if declined: R020-EVD 1,943, R020-CAP 15.

**Limits (GREEN):**

| Segment | Median | P90 | Above 50M | At 90.1M |
|---|---|---|---|---|
| All | 28.5M | 52.5M | 754 | 74 (all salaried with CIC) |
| MSME | 11.5M (v5 36.0M) | 37.5M | 48 | 0 |
| Gig | 10.5M (v5 13.0M) | 17.5M | 0 | 0 |

Binding constraint among GREEN: income multiple 4,144, capacity 2,160, product maximum 74. Officer cases with no approvable limit: 15.

**Interpretation.**
- The 1,943 R010-EVD rows are exactly the dataset's contradictory `has_observable_tx = 0` records in segments without CIC history. v5 sent them to an officer, who could not resolve them either. v6 asks for Layer 2 data.
- Whether Layer 2 then approves is unknown: the replay has no Layer 2 data, and the Layer 2 to STP conversion rate is an **unmeasured** pilot metric.
- Illustrative cost and time figures from the dataset baselines (manual VND 380k / 3.24 days, STP 12k / 2.01 min) are targets, not results.

---

## 11. Implementation cross-check (rule → code → test)

| Rule | Code | Test(s) |
|---|---|---|
| Layer 1 without per-variable consent; legal step HS-01 | `policy.consent.activation_consents`, `activation.start_activation`, `routing` 0a | `test_api`, `test_service` consent tests |
| Unusable values excluded, never routed to an officer | `readiness.evaluate` (`EXCLUDED_FROM_USE`) | `test_engine` readiness tests, e2e 7 |
| No employment question, no manual-review route | `http.py` routes removed; `activation` | `test_engine` (no R010-INC / R010-DECL), `test_api` |
| Layer 2 selection, order, stop rule | `acquisition._alt_plan`, `_run_layer2_round` | `test_calling_stops_once_the_gap_is_closed`, `test_layer2_provider_has_no_record` |
| AV-01..AV-07, L0-06 | `_validate_alt`, `_build_streams`, `identity` | `test_declining_income_goes_to_an_officer_not_automatic`, `test_platform_paid_into_hlb_is_not_added_twice`, `test_layer2_invalid_data_is_rejected_by_validation` |
| Behaviour ≠ income | `recognition_factor`, `stp_income=false` | `test_behaviour_sources_never_count_as_income` |
| Layer 2 → STP (R001-L2) | `routing` rule 11, `capacity.income_basis` | e2e 5, 7 |
| Outage → retry, no officer | `routing._retry`, `servicing.retry_pending_assessments` | `test_cic_unavailable_retries_automatically_never_decides_on_missing_data`, e2e 13, 17 |
| Recognised-income multiple | `capacity.income_basis`, `_sizing` | `RecognisedIncomeMultiple` (3) |
| RED only verified; taxonomy | `routing` | `RoutingProperties`, V-28 |
| Scope 1.6M–90.1M, exact maximum, out-of-scope referral | `capacity.round_limit`, `activation._refer_out_of_scope`, `checkout_rules` | `test_green_can_reach_the_product_maximum`, `test_request_above_product_maximum_is_referred_not_processed`, e2e 16 |
| Officer authority inclusive, EL checks, no data actions | `officer._approval_eligibility`, `officer_decide` | `test_service` boundary test, UI regression |
| Checkout classes, Pay Now always offered | `checkout_rules.out` | `test_green_checkout_still_offers_pay_now`, `test_red_checkout_keeps_pay_now_and_records_the_decline` |
| Split-tender reliability | `checkout.accept_split`, `pay`, `base.reconcile_stuck_payments`, `sweep_expired` | `SplitTender` (5), e2e 2, 14 |

**Mismatches found during this cross-check and fixed in this revision:**
1. `payment_options` was empty for GREEN, which contradicted "Pay Now always offered".
2. Reason code `R010-CUST` ("customer asked for manual review") remained in the policy after the route was removed.
3. The `R010-ALT` label still mentioned "no behaviour signal" (now R010-THIN).
4. The replay script counted v5-only codes (`R010-DECL`, `R010-ALT-NA`) as unapprovable officer cases.
5. The V-28 test asserted equal RED-if-declined rates across segments, a v5 property that is incompatible with verified R020-EVD. It now checks the reason codes instead.
6. Seven tests encoded the pre-safeguard 14M limit for gig fixture CUS-007 (now 12.0M = 2 × 0.7 × 8,779,839).

**Known limitations (not fixed):**
- **Simulated adapters.** All adapters are deterministic simulations; no real CIC, core, Open API, tax, marketplace, platform or utility connection exists.
- **Unclassified Layer 1 inflow.** It still enters the **debt-service** test at 1.0 for merchants and gig workers. Only the exposure cap is corrected; HLB core classification removes this.
- **Segment label.** `customer_profile` is taken as a reliable HLB core label. If it is wrong, the recognised-income factor is wrong. An unknown segment is treated conservatively.
- **RED R020-EVD.** It depends on the customer declining Layer 2 when there is no Layer 1 evidence. The decline reason is unverifiable capacity, but in effect the outcome is conditional on sharing data. Legal and Conduct should confirm this framing (§14).
- **Replay.** Rates are engine behaviour on the dataset, not validated outcomes. No default model exists.
- **Atomicity.** Split-tender atomicity relies on one SQLite transaction (§6.1).

---

## 12. Tests run for this revision (exact)

| Run | Command | Result |
|---|---|---|
| Unit / service / API / matrix | `python3 -m pytest -q` | **203 passed** (final run after all changes) |
| End-to-end HTTP | `python3 run.py --port 8080 --reset` then `python3 scripts/e2e_scenarios.py http://127.0.0.1:8080` | **17/17 scenarios, 184 HTTP calls** (`docs/test_logs/e2e_results.json`) |
| UI regression (Playwright) | `python3 scripts/ui_regression.py http://127.0.0.1:8080` on a fresh `--reset` server | **44/44 checks** |
| Replay | `python3 scripts/segment_replay.py --compare <v5 worktree> --sensitivity` | Tables in §10 (`docs/test_logs/segment_replay.json`) |
| Dataset audit | `python3 scripts/dataset_audit.py` | `data/reference/dataset_audit.json` regenerated; findings unchanged (§10, below) |

Dataset findings:

| Finding | Value |
|---|---|
| `has_observable_tx = 0` with transaction values | 1,943 |
| Unsupported tenures | 342 |
| No CIC history | 6,450 |
| Rapid drain | 266 |
| Defaults with approved amount 0 | All 436 |
| Requested range | 1,600,203 – 90,116,228 |
| Inflow breakdown | None |

---

## 13. References (checked Nov 2026)

| Source | Used for |
|---|---|
| SBV Circular 64/2024/TT-NHNN on Open API in banking: consent definition (Art. 3.6), bank–third-party contract (Art. 8), ≤ 180-day access (Art. 11.6), compliance by 1 Mar 2027 (Art. 15.2). [thuvienphapluat (EN)](https://thuvienphapluat.vn/van-ban/EN/Tien-te-Ngan-hang/Circular-64-2024-TT-NHNN-implementation-of-open-application-programming-interface-in-banking-sector/649174/tieng-anh.aspx) · [LuatVietnam (EN)](https://english.luatvietnam.vn/tai-chinh/circular-64-2024-tt-nhnn-deployment-of-open-application-programming-interfaces-in-the-banking-sector-387124-d1.html) | Open Banking tier A, consent, legal notes |
| Circular 39/2016 consolidated text (86/VBHN-NHNN, 2026): Art. 7 repayment capacity, Art. 13.4 overdue interest, Art. 17.2 separation. [datafiles.chinhphu.vn](https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/7/86-vbhn-nhnn.pdf) · [accgroup.vn summary](https://accgroup.vn/cach-tinh-lai-cham-tra-theo-thong-tu-so-39-2016-tt-nhnn) | R020-EVD basis, overdue interest, separation question |
| Circular 29/2026/TT-NHNN amending Circular 39: per-customer digital-lending limit (Art. 22.2(h)). [thuvienphapluat (EN)](https://thuvienphapluat.vn/van-ban/EN/Tien-te-Ngan-hang/Circular-29-2026-TT-NHNN-amendments-Circular-39-2016-TT-NHNN/718494/tieng-anh.aspx) · [Báo Pháp luật](https://doanhnhan.baophapluat.vn/chinh-thuc-go-tran-vay-online-100-trieu-dong-nang-han-muc-khoan-vay-nho-len-400-trieu.html) · [LuatVietnam (effective-date discrepancy)](https://luatvietnam.vn/legal-updates/small-value-loan-ceiling-raised-to-vnd-400-million-from-august-24-2026-892-110164-article.html) | Aggregate cap (value is a placeholder) |
| Circular 10/2023 suspending Circular 39 Art. 8.10. [LuatVietnam](https://luatvietnam.vn/tin-van-ban-moi/ngung-thi-hanh-3-truong-hop-cam-cho-vay-tu-01-9-2023-186-95309-article.html) | Pay Now → instalment conversion |
| Personal Data Protection Law 91/2025/QH15. [thuvienphapluat (EN)](https://thuvienphapluat.vn/van-ban/Bo-may-hanh-chinh/Law-91-2025-QH15-Personal-Data-Protection-665440.aspx) | Legal data step kept separate from Layer 1 |
| Decree 356/2025/ND-CP: sensitive data, withdrawal deadlines. [EY (Mar 2026)](https://www.ey.com/content/dam/ey-unified-site/ey-com/vi-vn/technical/tax/documents/ey-vietnam-legal-alert-march-2026-decree-no356-2025-nd-cp-providing-detailed-guidance-for-implementation-of-personal-data-protection-law-viet.pdf) · [PwC (Jan 2026)](https://www.pwc.com/vn/vn/publications/2026/20260128-new-rules-personal-data-protection.pdf) · [Mondaq](https://www.mondaq.com/privacy-protection/1731760/key-developments-in-personal-data-protection-under-decree-356) | Consent and withdrawal |
| Bank Negara Malaysia, "New measures on credit cards" (18 Mar 2011): limit ≤ 2× monthly income per issuer for income ≤ RM36,000 a year. [bnm.gov.my](https://www.bnm.gov.my/-/new-measures-on-credit-cards-to-promote-prudent-financial-management-and-responsible-business-practices) | 2× income multiple (E) |
| Fannie Mae Selling Guide B3-3.1-01, General Income Information: income must be reasonably expected to continue; use the lower amount when a reduction is known. [selling-guide.fanniemae.com](https://selling-guide.fanniemae.com/sel/b3-3.1-01/general-income-information) | AV-05 / AV-06 (E) |
| VietNamNet, "Grab drivers in Vietnam call for two-day shutdown over earnings" (12 Sep 2026): deductions about 35–50% of fares; drivers bear vehicle, fuel and maintenance costs. [vietnamnet.vn](https://vietnamnet.vn/en/grab-drivers-in-vietnam-call-for-two-day-shutdown-over-earnings-2554485.html) | Platform factor 0.7 rationale (not a measured margin) |
| PERC, "The Promise of Non-Financial Data" (utility/telecom payment data improves scoring for thin files). [perc.net (PDF)](https://www.perc.net/wp-content/uploads/2013/09/ADI-II-Policy-Brief.pdf) | Utility/telecom as behaviour evidence (US data, not Vietnam) |

These are a prototype team's summaries, not legal advice. HLBVN Legal must confirm each point.

---

## 14. Open HLBVN decisions

1. **Legal basis for Layer 1.** Is consent required for HLB data and the CIC enquiry, or does another basis apply? This decides whether `DATA_PROCESSING` stays a consent or becomes a notice acknowledgement.
2. **Automated decisions versus Circular 39 Art. 17.2 separation,** and whether credit-committee approval of the rule set satisfies it. Also: the named owner of the decision system.
3. **Delegated authority.** Tiers and a four-eyes threshold to replace "every officer ≤ 90.1M inclusive".
4. **Automatic scope.** Confirm automatic decisions up to 90.1M, or set a lower automatic ceiling as a risk-appetite choice (the replay shows 754 GREEN above 50M, 652 of them salaried with CIC).
5. **Calibration** of P25 stress, 2× multiple, factors 0.7 / 0.3, RF-02 / RF-03 cut-offs and AV thresholds on HLB outcome data. A usable default label is needed first.
6. **HLB core inflow classification** (salary, settlements, own transfers), so turnover is never treated as income.
7. **Layer 2 partnerships and contracts:** Open API agreements before the 1 Mar 2027 compliance date, gig and marketplace partners, e-invoice access, utility aggregators. Until these exist, every Layer 2 path is simulated.
8. **R020-EVD after a declined Layer 2 request:** confirm a RED with appeal, or route to an officer instead (conduct review).
9. **Aggregate digital-lending cap value** (Art. 22.2(h)); 100M is a placeholder.
10. **Arrears grace period** before the freeze (currently day 1), the officer SLA (24 h placeholder) and the outage window (24 h).
