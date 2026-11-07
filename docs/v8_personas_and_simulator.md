# v8: Customer Persona Showcase and Live Scenario Simulator

**Scope.** This version implements `TASK_PERSONA.md`, which covers the final Presenter Page and the five customer personas.

**Decision rules are unchanged.** The following are byte-for-byte identical to v7:
- `server/engine/*`, `server/policy/*` and `server/domain/*`;
- the fixtures;
- the decision services (activation, acquisition, checkout, officer, servicing);
- the dataset.

The policy is still `2026.11-demo.6`, sha256 `8d220a1f…`. `scripts/policy_lock.py` re-confirms the lock, and `git diff` on those paths is empty.

## 1. Page structure (`/navigation.html`)

A sticky section bar shows the live current date. The page has four sections:

| # | Section | What it is | Data source |
|---|---|---|---|
| 1 | **Customer personas** | Exactly five fixed, read-only profile cards. **Details** opens a full-width panel with three columns: dataset facts, why the profile was selected, and engine output. | `GET /api/presenter/personas`. Dataset snapshot plus a fresh sandboxed engine run under normal operating conditions. |
| 2 | **Live scenario simulator** | A fully editable form. Shortcuts start from Blank, a copy of P1–P5, or failure scenarios F1–F5. The panel shows the baseline and every modified input. The read-only engine output recalculates as you type, with a "versus baseline" diff. A separate, dashed **Process explanation** box gives the static description. **C · Run the full journey** sends the same inputs through the real customer app (the v7 path is kept). | `POST /api/presenter/simulate`, which runs the actual engine in an in-memory sandbox. |
| 3 | **Risk failure simulation** | The seven existing switches and the tampered-QR test, grouped into three risks. Each risk names its safe state. Assessment switches also apply to the simulator, and a banner says so. | `POST /api/presenter/controls` |
| 4 | **Presenter tools** | Live current date and time (device clock, updated every second, with time zone). Simulated business date: +1/+30/+40/+95 days, plus *Set to today*. Open checkouts with *Expire now*, refresh, reset, and links to the customer app and the officer workbench. | existing endpoints |

**Engine output vs explanation.** The two are kept visibly apart:
- Engine output always sits in a solid navy-bordered box tagged **ENGINE OUTPUT · READ-ONLY**.
- Static explanations sit in a dashed box tagged **PROCESS EXPLANATION**.
- Dataset facts are tagged **DATASET FACT**, and scenario-specific elements are tagged **SIMULATED**.

**Removed as obsolete:**
- the three v7 presets (A/B/C), which are superseded by personas and failure scenarios;
- the old one-column form/result layout and its CSS (`.presets`, `.preset`, `.story`, `.nav-grid`, `.nav-side`).

The live-demo endpoint and its tests are kept.

## 2. The five personas (each run through the actual engine; business date 16/10/2026)

| ID | Display name | Applicant ID | Segment | Engine result | Pathway shown | Share of segment on this pathway* |
|---|---|---|---|---|---|---|
| P1 | Tạ Mỹ Kim Tiên (fictional name already used for demo customer CUS-001) | HLB_POP_100000005 | Salaried, with credit history | **GREEN R001**, limit VND 29,500,000 (repayment capacity), checkout R001 | Automatic approval; the full VND 20,969,073 purchase goes on Pay Later | 91.4% GREEN |
| P2 | Nguyễn Hoài An (**fictional demo alias**) | HLB_POP_100000160 | Salaried, no credit history | **GREEN R001**, limit VND 21,000,000 (income multiple), checkout R001 | No-CIC inclusion: approved on verified salary transactions | 77.8% GREEN |
| P3 | Phan Khánh Linh (**fictional demo alias**) | HLB_POP_100001384 | First-time borrowers, digitally active | **AMBER R010-EVD**, Layer 2 requested. Shares → **GREEN R001-L2**, VND 16,000,000. Declines → **RED R020-EVD** (appeal) | Evidence acquisition: the system asks for data, never declines blind | 51.2% R010-EVD |
| P4 | Lâm Quốc Bình (**fictional demo alias**) | HLB_POP_100004375 | Online merchants (MSME) | **GREEN R001**, limit VND 11,500,000 (income multiple, unclassified turnover at 0.3). Checkout **R010-SPLIT**: 11,500,000 Pay Later + 46,264,188 Pay Now | Split tender | 57.3% split purchases |
| P5 | Huỳnh Gia Bảo (fictional name already used for demo customer CUS-002) | HLB_POP_100000020 | Gig / platform workers | **AMBER R010-FLAG** (RF-02 score 423 < 436; RF-03 on-time bills 0.587 < 0.663), automatic capacity VND 6,500,000, officer maximum VND 20,000,000 | Officer review, never auto-declined. Pay now, convert within 7 days | 42.6% officer referral |

\* From `docs/test_logs/segment_replay.json`, a replay of all 10,000 rows. This describes engine behaviour on simulated data, not credit performance.

**Shortlist verification.** All five shortlisted candidates exist, and all five produce their intended pathway on the current engine. Two were replaced because their *outcome columns* are internally contradictory, which would undermine a "verified" reference profile:

- **HLB_POP_100008470 (first-time borrower):** `is_default_90d = 1` on a loan that was never approved (`approved_loan_vnd = 0`). It is one of 436 such rows. It was replaced by **HLB_POP_100001384**, which has the same pathway, clean columns, and is the closest to the segment median among clean rows on this pathway.
- **HLB_POP_100003105 (MSME):** abandoned (`is_abandoned = 1`) yet it records an approval of VND 54,689,276. It is one of 1,161 such rows. It was replaced by **HLB_POP_100004375**, on the same basis.

The other three candidates were kept, with their existing fictional names where the demo fixtures already had one. For HLB_POP_100000020, the v7 preset had used a second name, *Trần Quốc Huy*. The fixture name *Huỳnh Gia Bảo* is now used everywhere.

**Facts vs simulated elements.**
- Every engine input on a card is copied verbatim from the dataset. `tests/test_scenario_lab.py` re-reads the xlsx and checks every value of every row used.
- No age, gender, occupation, income or credit attribute has been added.
- The simulated elements are stated on each card:
  - the merchant (the dataset has no merchant field);
  - the purchase (equal to `requested_loan_vnd`);
  - the business date;
  - eKYC, QR and all data connections.
- For P3, the dataset records an inflow although no transactions are observable (one of 1,943 such rows). The card says that this is not verified HLB income. In the simulation, it becomes a simulated Open Banking record only if the customer agrees to share.

## 3. Failure scenarios (simulator shortcuts)

Each scenario is a persona (or a dataset row) plus the modification stated on screen. The engine then decides as usual.

| ID | Base + modification | Engine result |
|---|---|---|
| F1 | P1, debt payments 31.5% → 100% | **RED R020-CAP**, immediate verified decline, appeal |
| F2 | P1, debt payments → 75% | **AMBER R010-CAPX** → Layer 2. Declines (or nothing found) → **RED R020-CAP** |
| F3 | P2 + rapid drain after top-up | **AMBER R010-FLAG**, officer review |
| F4 | P1, purchase → VND 1,500,000 | Checkout **R030-AMT** (N/A): Pay Now only, not a credit decision |
| F5 | Row HLB_POP_100001351 (clean high earner), purchase → VND 95,000,000 | **GREEN**, limit exactly VND 90,100,000 (product maximum). **R010-SPLIT**: 90,100,000 + 4,900,000 |

## 4. How the simulator stays honest

- **The real engine decides.** `ScenarioLabMixin._sandbox_run` builds a fresh `SmartCreditService` on `:memory:`, from deep copies of the same policy and fixtures. It sets the current business date and the active assessment switches. It then runs the normal journey: `create_live_customer` → `resolve_qr` → `choose_pay_later` (legal data step) → optionally `alternative_data_consent` (share / decline). The result is read back from the sandbox's own records.
- **Expected pathways are only compared.** The documented `expected` pathway is checked against the live result (`verification`). It never feeds the result.
- **Nothing is mutated.** Personas reach the browser as copies, and edits stay in the page. Tests confirm that the `PERSONAS`, `FAILURE_SCENARIOS` and `DATASET_ROWS` constants, the row counts of seven demo-database tables, and the xlsx hash are unchanged after simulations.
- **Validation and dependency checks.**
  - The same field specs are enforced in the browser and on the server (422 with per-field codes).
  - Without HLB transactions, the inflow becomes a simulated Open Banking record and the bill ratio a simulated utility record; balance and weekly pattern are marked "not used".
  - A thin-file hint appears when there is neither CIC nor transactions.
  - Product-scope hints are shown for the purchase amount.
  - An empty balance defaults to 40% of inflow, and this is labelled as a simulated default.
  - Reference-only fields are folded under "not decision inputs", and a test proves they do not move the decision.
- **Next action is presentation only.** `next_action` is a plain label read off the engine's own outputs (route, credit state, checkout code). It decides nothing.

## 5. Verification (run on this revision)

| Check | Command | Result |
|---|---|---|
| Unit / service / API | `python3 -m pytest -q` | **225 passed**: 210 existing (7 in `test_live_demo.py` updated for the removed presets) and 15 new in `tests/test_scenario_lab.py` |
| End-to-end HTTP (customer, officer, presenter journeys) | `python3 run.py --reset` then `python3 scripts/e2e_scenarios.py` | **17/17** scenarios, 184 calls |
| UI regression (Playwright, Chromium) | `python3 scripts/ui_regression.py` on a fresh server | **57/57** checks; the presenter checks were rewritten for the four sections |
| Policy lock | `python3 scripts/policy_lock.py` | `2026.11-demo.6` / `8d220a1f…`, unchanged |
| JS syntax | `node --check web/js/*.js` | pass |

The new tests in `tests/test_scenario_lab.py` cover the following:
- Five personas, one per segment, with full names.
- Every snapshot value equals the xlsx.
- Persona inputs are dataset facts plus only the three stated simulated elements.
- The selected rows have no contradictory outcome columns, and the two replaced rows really do.
- Name provenance: retained names match the fixture row; aliases are new.
- Segment shares match the replay log.
- The documented pathway equals the live engine result for all five.
- The showcase ignores failure switches, and the simulator applies them (or not, on request).
- F1–F5 cover GREEN, AMBER and RED.
- Every decision input recalculates: score, bills, rapid drain, DTI, inflow, CV, transactions, CIC, purchase and segment.
- Reference-only fields do not move the decision.
- No mutation of personas, dataset or demo database.
- Validation.
- The result carries the locked policy hash.
- HTTP: presenter-only (401/403), and all three endpoints work.

**Manual browser checks.** I checked desktop at 1440 px and phone at 390 px in Vietnamese and English:
- no horizontal scroll at either width;
- persona details panel;
- edit → recalculation → diff;
- invalid input pauses the result;
- a risk switch shows its banner and its effect in the simulator;
- business date +1 day;
- no JS errors.

## 6. Limitations and open points

- **Three risks in section 3.** The attached Gala Round case and the repository do not list "the three risks of the original brief" by name. The existing switches are grouped by the safe state the rubric asks for: (R1) data source unavailable or incomplete → retry / fallback; (R2) identity fraud or tampered request → human review; (R3) payment or authentication failure → rollback. **Please confirm or rename them to match the original brief.**
- **Engine reason text stays in English** in Vietnamese mode, because it is server text. Short titles are translated by `plain.js`, as on the officer page.
- **The "customer shares" branch** only finds data when the profile has no HLB transactions. Only then does the simulator build a simulated other-bank record from the typed inflow. The result panel says so.
- **Persona checks run at the current simulated business date.** The showcase re-verifies on every load and after every clock change, and shows ✓ or ⚠ per card.
- **Not verified:** Safari and real devices, screen readers, and Vietnamese copy reviewed by an HLBVN copywriter.
