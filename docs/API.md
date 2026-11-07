# Smart Credit demo API (v6, policy `2026.11-demo.6`)

v6 changes: `/{app}/information` and `/{app}/manual-review` removed; officer `request_data` / `refresh_data` refused (`DATA_ACTIONS_AUTOMATIC`); activation consents are `SC_TERMS` + `DATA_PROCESSING`; Layer 2 consents are unticked by default; checkout decisions carry `decision_class`, `credit_outcome` and `payment_options`; officer authority is ≤ VND 90,100,000 inclusive. Design rationale: [`decision_rules.md`](decision_rules.md).

JSON over HTTP. All money is integer VND. Errors: `{"error": {"code", "message", "details"}}`. Unexpected errors return `500 INTERNAL_ERROR` with an `error_id` only, never exception text.

**Identity:** sign in to get a bearer token, then send `Authorization: Bearer <token>`. Roles are enforced per route: customer, officer, presenter.

- **Customers** pick a fictional persona.
- **Officer and navigation/presenter sessions open without any access code or profile.** By case assumption, users of those pages are already authorised; in production this would be HLBVN staff SSO.
- Each officer session gets its own reference (`OFFICER-xxxxxx`) for the audit trail.
- These are demo sessions, not real authentication.

## Public

| Method | Path | Notes |
|---|---|---|
| GET | `/api/public/config` | Only what the customer UI needs: amount range, tenures, pricing (incl. overdue principal / late-interest rates, `late_fee_vnd` = 0), consent texts, auth thresholds, officer approval authority (max 90,100,000 + bound `INCLUSIVE`), automatic-decision ceiling (= product maximum), minimum limit, `policy_version` + `policy_hash`, hotline. **No credit cut-offs.** |
| GET | `/api/public/customers` | Persona picker (id, name, initials) |
| POST | `/api/session/customer` `{customer_id}` · `/api/session/officer` `{}` · `/api/session/presenter` `{}` · `/api/session/sign-out` | 201 with token |

## Customer

| Method | Path | Notes |
|---|---|---|
| GET | `/api/me` · `/api/credit` | Account, credit line (limit, available = limit − principal − holds), delinquency, single exposure view |
| GET | `/api/qr/demo-codes` | Simulated camera: VietQR/EMVCo payloads |
| POST | `/api/qr/resolve` `{payload, amount_vnd?}` | Parses TLV + CRC, resolves merchant by BIN/account, verifies dynamic QR against the merchant order. Static QR needs `amount_vnd`. 201 new / 200 resumed. Errors: `QR_CRC_MISMATCH`, `QR_INVALID`, `QR_UNSUPPORTED`, `QR_ALREADY_PAID`, `AMOUNT_REQUIRED` |
| GET | `/api/transactions` · `/api/transactions/{tx}` · `/{tx}/timeline` | Customer view: outcome, timer, Pay Later decision, terms, hold, auth method |
| POST | `/{tx}/pay-now` | Select account payment |
| POST | `/{tx}/pay-later` `{activation_consents?}` | Instant checkout decision (TX-00..TX-08). With consents and no line: inline activation first. `pay_later_decision` has `band`, `reason_code`, `decision_class` (`CREDIT` / `PRODUCT_SCOPE` / `OPERATIONAL` / `FRAUD_HOLD` / `NO_LINE`), `credit_outcome` (`APPROVED` / `PARTIAL` / `DECLINED` / `NOT_ASSESSED`) and `payment_options` (always contains `PAY_NOW`). GREEN places an exposure hold; AMBER `R010-SPLIT` returns `split {pay_later_vnd, pay_now_vnd, cause (AVAILABLE_LIMIT / AFFORDABILITY / PRODUCT_MAX), above_product_max}` and places nothing; band `N/A` (`R030-MER`, `R030-AMT`) means Pay Later does not apply - not a credit decision |
| POST | `/{tx}/split` | Customer explicitly accepts split tender: re-decided inside the transaction, hold = Pay Later part only. The Pay Now part is checked against the balance at authorisation and at `pay`. Errors: `SPLIT_NOT_AVAILABLE`, `INSUFFICIENT_FUNDS` |
| POST | `/{tx}/consent-pause` `{resume?}` | Pauses the checkout timer while the inline-activation consent form is open; resume (or activation) starts a fresh deadline |
| POST | `/{tx}/tenure` `{tenure_months}` | Only affordable tenures. Reprices the hold |
| POST | `/{tx}/agreement` `{accepted: true}` | Accept the per-purchase instalment agreement |
| POST | `/{tx}/auth` `{purpose}` | `ACCOUNT_PAYMENT` / `PAY_LATER_AGREEMENT` / `INSTALMENT_CONVERSION` → challenge (`SMART_OTP` with a simulated authenticator code, or `FACE`) |
| POST | `/api/auth/{challenge}/verify` `{code}` or `{face_result: "MATCH"}` | 3 attempts, 120 s TTL |
| POST | `/{tx}/pay` `{challenge_id}` + `Idempotency-Key` header | Needs a verified, unused challenge bound to this transaction and amount. Re-checks the exposure ledger and the aggregate cap. `SPLIT`: books the Pay Later loan and debits the Pay Now part in **one** database transaction (all or nothing). Same `Idempotency-Key` → same result; a different key on a paid order → `DUPLICATE_PAYMENT`. A payment stuck in processing > 120 s is reconciled as failed with nothing posted. One success per merchant order |
| POST | `/{tx}/cancel` | Releases any hold |
| GET / POST | `/{tx}/conversion` · `/{tx}/convert` `{tenure_months, accepted, challenge_id}` | Convert a Pay Now purchase within 7 days; loan purpose `PURCHASE_FINANCING`, disbursement recorded as a reversal of the purchase debit |
| GET | `/api/smart-credit/requirements` | |
| POST | `/api/smart-credit/activate` `{consents: {SC_TERMS, DATA_PROCESSING}, requested_limit_vnd?}` | Starts and assesses Limit Activation (Layer 0 → all Layer 1 data → routing; no employment question). `DATA_PROCESSING` is the legally required personal-data notice/consent, not a per-variable consent. Requested above capacity → status `OFFER` (R001-CO) with `offer {limit_vnd, requested_vnd}`. Requested above 90,100,000 → assessed for the in-scope part and `out_of_scope {requested_vnd, max_vnd}` (referral event `OUT_OF_SCOPE_REFERRAL`). Below 1,600,000 → `INVALID_LIMIT`. Error `LINE_SUSPENDED` |
| POST | `/api/smart-credit/withdraw-consent` | Withdraws assessment/monitoring consent on an active line: line closes to new drawdowns, loans continue (Decree 356/2025) |
| POST | `/api/smart-credit/limit-increase` `{requested_limit_vnd, reason, transaction_id?}` | Decided automatically (approve / counter-offer / Layer 2 request / RED R020-INC); referred only for judgement cases |
| GET | `/api/applications` · `/api/applications/{app}` | Plain customer status (APPROVED / OFFER / ACTION_NEEDED / IN_REVIEW / NOT_APPROVED …), `kind` (LIMIT_ACTIVATION, LIMIT_INCREASE, LINE_REVIEW, LINE_REINSTATEMENT), reason category, `checking_reason` while the system retries a source, `out_of_scope`, `info_request` (Layer 2: generic `missing[{item, why}]` and `sources[{id, label, purpose, data_requested, legal_note}]` - never which Layer 1 variables were short), `data_acquisition` (customer view: Layer 2 rounds and outcomes only, **no Layer 1 detail**), expected decision hours |
| POST | `/{app}/alternative-data` `{consents: {source_id: true/false}}` | Layer 2: one optional checkbox per offered source, **unticked by default; a missing or `false` answer = not allowed** (each stored as its own `ALT_DATA:<source>` consent with its purpose). Consented sources are called in priority order and calling stops once the gap is closed (the rest are recorded `NOT_CALLED_GAP_CLOSED`); each response is validated (AV-01..AV-07) and the case is re-decided automatically. Errors: `CONSENT_REQUIRED` (non-boolean answer), `SOURCE_NOT_OFFERED`, `ALTERNATIVE_DATA_NOT_REQUESTED` |
| POST | `/{app}/appeal` `{reason}` (RED only, once) · `/{app}/cancel` · `/{app}/withdraw-consent` | v6: there is no "skip to an officer" route |
| POST | `/{app}/accept-offer` · `/{app}/decline-offer` `{ask_for_more?}` | Counter-offer: the line opens only on accept; `ask_for_more` → automated Layer 2 request; decline → cancelled |
| POST | `/{app}/alternative-data/{source}/withdraw` | Withdraws one Layer 2 source's consent; its data is purged from the application |
| GET / POST | `/api/loans` · `/api/loans/{loan}` · `/api/loans/{loan}/repay` `{instalments}` + `Idempotency-Key` | Oldest first. Overdue instalments show `overdue_interest_vnd` (Circular 39 Art. 13.4) and `total_due_vnd`. Early-settlement quote |
| GET / POST | `/api/notifications` · `/api/notifications/read` | Keys + params, localised client-side |

## Officer

| Method | Path | Notes |
|---|---|---|
| GET | `/api/officer/queue` | Pending review, awaiting customer, QA sample, decided. Age, SLA, capacity stats, approval authority |
| GET | `/api/officer/cases/{app}` | `case_summary` (recommended limit, automatic capacity, officer maximum, income streams with lineage / factor / recognised-income multiplier, obligations, binding constraint, evidence with exclusions, data-quality events, risk indicators, triggered rules, unresolved issues, automated data operations), profile, single exposure view, Layer 0 result, `data_acquisition` (Layer 1 call log; Layer 2 rounds with consent, provider response, validation AV-01..AV-07), `eligibility` (EL-01..EL-10, `max_approvable_vnd`, `stp_capacity_vnd`, `officer_max_limit_vnd`, authority with 100k step), `reason_codes` (incl. fraud), `governance`, consents, decisions, audit. Queue items carry `approvability` |
| POST | `/api/officer/cases/{app}/decision` `{action, reason_code, note, expected_version, approved_limit_vnd?, requested_sources?}` | `approve` (blocked unless every eligibility check passes: `ABOVE_APPROVAL_AUTHORITY` above VND 90,100,000, `LIMIT_OUTSIDE_CAPACITY` above the officer maximum or off the VND 100,000 grid, `REASON_CODE_NOT_ALLOWED` when the code doesn't fit the basis) / `reject` (rejecting a review or reinstatement closes the line) / `confirm_fraud` (FR-01 → HS-05, line suspended). `request_data` / `refresh_data` → 422 `DATA_ACTIONS_AUTOMATIC` (the system acquires and retries data). No escalate, return or document actions |
| POST | `/api/officer/cases/{app}/qa` `{outcome: AGREE / DISAGREE, note}` | QA sample of automatic decisions (5%; 10% when Layer 2 income was used); DISAGREE opens a line review |
| GET | `/api/officer/policy` · `/api/officer/audit` · `/api/reference/dataset` | |

## Navigation / presenter (`/navigation.html`; presenter or officer session)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/presenter/state` | Controls, business date, personas, scenarios |
| POST | `/api/presenter/controls` `{key, value}` | `force_next_payment_failure`, `force_next_auth_failure`, `core_system_unavailable`, `cic_unavailable` (Layer 1), `alt_data_api_unavailable` (Layer 2 timeout), `alt_data_incomplete` (Layer 2 returns 3 months → fails validation), `device_anomaly` (Layer 0 L0-03) |
| POST | `/api/presenter/clock` `{date}` or `{advance_days}` | Simulated business date; advancing runs servicing (overdue interest, freeze, default, cure), the day-75 line renewals, automatic retries of assessments waiting on a source, and the stuck-payment / hold-expiry sweep |
| GET | `/api/presenter/open-checkouts` | Unfinished checkouts (any persona) for the expire control |
| GET | `/api/presenter/scenario-lab` | Simulator config: field specs and bounds, segments, merchants, product scope, the five personas (inputs as copies), the five failure scenarios (base inputs, modifications, inputs, documented expectation), active assessment switches, business date |
| GET | `/api/presenter/personas` | Persona Showcase: per persona the dataset facts (engine inputs, application fields, outcome columns marked "not used"), scenario elements that are simulated, why it was selected, and a fresh engine run under normal conditions with `verification {matches, differences}` against the documented pathway |
| POST | `/api/presenter/simulate` `{inputs, apply_failure_switches?}` | Live Scenario Simulator. Runs the actual engine journey in a throw-away in-memory sandbox (same policy and fixtures); nothing is written to the demo database. Returns `assessment` (band, reason code, rule ids, reasons, route, state, policy hash), `limit` (approved, automatic capacity, officer maximum, binding constraint, income, obligations), `checkout`, `officer_referral`, `data_request`, `next_action`, `derived` (how inputs were used) and, when Layer 2 is requested, `branches.share` / `branches.decline`. Same validation as `/live-demo` (422 `INVALID_LIVE_INPUT`). Active assessment switches apply unless `apply_failure_switches: false` |
| GET | `/api/presenter/risk` | Risk-round config: overload presets and bounds, rule cases (M1–M3 configuration, E1–E2 evidence), data cases (D1–D6), QA sample rates, live policy hash vs lock |
| POST | `/api/presenter/risk/overload` `{requests, window_s, workers, queue_max, budget_ms}` | R1. Deterministic gateway model (bounded pool, bounded queue, deadline-aware fallback) with a real engine decision per on-time request; returns protected and unprotected runs, timeline, fallback state. 422 `INVALID_OVERLOAD_INPUT` |
| POST | `/api/presenter/risk/rules` `{case}` | R2. M1–M3: change applied to a COPY of the policy → start-up validation, version-lock hash check, shadow replay on 10,000 rows, live-service hash. E1–E2: sandbox runs (identity-mismatched Layer 2 record; conflicting rules) |
| POST | `/api/presenter/risk/data` `{case}` | R3. Alters the sandbox customer record (missing, stale, inconsistent, invalid, incomplete Layer 2, missing field); returns readiness findings, outcome, Layer 2 branches and the clean-record baseline |
| GET/POST | `/api/presenter/live-demo` · `/api/presenter/live-demo/{cid}` | Creates a fictional customer + QR order from the simulator inputs and follows it through the real customer app (end-to-end run) |
| POST | `/api/presenter/transactions/{tx}/expire-now` · `/api/presenter/reset` | Reset keeps the append-only audit log |
