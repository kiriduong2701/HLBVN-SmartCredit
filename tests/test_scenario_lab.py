"""Presenter page sections 1-2: Customer Persona Showcase (five read-only reference profiles) and the Live Scenario
Simulator (sandboxed runs of the actual engine).

Covers: exactly five personas, one per segment; every persona/scenario value equals the supplied dataset (re-read from the
xlsx); clean outcome columns for the selected rows; name provenance; the documented pathway equals the live engine result;
every decision input is editable and genuinely recalculated; reference-only fields do not move the decision; GREEN, AMBER
and RED; failure switches apply to the simulator only; nothing (personas, dataset, demo database) is mutated; validation;
presenter-only HTTP access; the policy is the locked one."""
import copy
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from server.api.http import make_server
from server.main import build_app
from server.services import scenario_lab as SL
from tests.test_service import FIXTURES, Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(ROOT, SL.DATASET_FILE)


def _dataset_rows():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import dataset_audit as D  # stdlib xlsx reader used by the audit scripts
    header, *body = D.read_xlsx(XLSX)["dataset"]
    return {r[0]: dict(zip(header, r)) for r in body}


DATA = _dataset_rows()
NORMAL = lambda v: int(v) if isinstance(v, float) and v.is_integer() else v  # noqa: E731
P = {p["id"]: p for p in SL.PERSONAS}


def inputs(pid, **over):
    return dict(SL.persona_inputs(P[pid]), **over)


class PersonaReference(unittest.TestCase):
    def test_exactly_five_personas_one_per_segment(self):
        self.assertEqual(len(SL.PERSONAS), 5)
        segs = [SL.DATASET_ROWS[p["applicant_id"]]["customer_profile"] for p in SL.PERSONAS]
        self.assertEqual(sorted(segs), sorted(SL.SEGMENTS))
        self.assertEqual(len({p["applicant_id"] for p in SL.PERSONAS}), 5)
        self.assertEqual(len({p["display_name"] for p in SL.PERSONAS}), 5)
        for p in SL.PERSONAS:
            self.assertGreaterEqual(len(p["display_name"].split()), 3, "full display name")

    def test_every_snapshot_value_equals_the_supplied_dataset(self):
        for aid, snap in SL.DATASET_ROWS.items():
            row = DATA[aid]
            self.assertEqual(set(snap) | {"applicant_id"}, set(row), aid)
            for k, v in snap.items():
                if k == "application_date":
                    self.assertEqual(v, (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(row[k]))).isoformat(), aid)
                else:
                    self.assertEqual(v, NORMAL(row[k]), "%s.%s" % (aid, k))

    def test_persona_inputs_are_dataset_facts_plus_stated_simulated_elements(self):
        for p in SL.PERSONAS:
            row = DATA[p["applicant_id"]]
            inp = SL.persona_inputs(p)
            for k in SL.ENGINE_INPUT_FIELDS:
                self.assertEqual(inp[k], NORMAL(row[k]), "%s %s" % (p["id"], k))
            self.assertEqual(inp["purchase_amount_vnd"], NORMAL(row["requested_loan_vnd"]))
            self.assertIn(inp["merchant_id"], SL.LIVE_MERCHANTS)
            extra = set(inp) - set(SL.ENGINE_INPUT_FIELDS)
            self.assertEqual(extra, {"display_name", "purchase_amount_vnd", "merchant_id"}, "nothing else is added")

    def test_selected_rows_have_consistent_outcome_columns(self):
        """Known dataset defects are avoided: abandoned-but-approved, default on a never-approved loan, off-menu tenure."""
        for p in SL.PERSONAS:
            r = SL.DATASET_ROWS[p["applicant_id"]]
            self.assertFalse(r["is_abandoned"] == 1 and r["approved_loan_vnd"] > 0, p["id"])
            self.assertFalse(r["is_default_90d"] == 1 and r["approved_loan_vnd"] == 0, p["id"])
            self.assertIn(r["loan_tenure_months"], (3, 6, 9, 12, 18, 24), p["id"])
        # the two replaced candidates really carry those defects
        self.assertEqual((DATA["HLB_POP_100008470"]["is_default_90d"], DATA["HLB_POP_100008470"]["approved_loan_vnd"]), (1.0, 0.0))
        self.assertEqual(DATA["HLB_POP_100003105"]["is_abandoned"], 1.0)
        self.assertGreater(DATA["HLB_POP_100003105"]["approved_loan_vnd"], 0)

    def test_name_provenance(self):
        fixture = {c["customer_id"]: c for c in FIXTURES["customers"]}
        strip = lambda s: "".join(c for c in __import__("unicodedata").normalize("NFD", s.replace("Đ", "D").replace("đ", "d"))
                                  if __import__("unicodedata").category(c) != "Mn")  # noqa: E731
        names = {strip(c["display_name"]).lower() for c in FIXTURES["customers"]}
        for p in SL.PERSONAS:
            src = p["name_source"]
            if src["kind"] == "RETAINED_FIXTURE_NAME":
                c = fixture[src["customer_id"]]
                self.assertEqual(c["derived_from"], p["applicant_id"])
                self.assertEqual(strip(p["display_name"]), c["display_name"])
            else:
                self.assertEqual(src["kind"], "FICTIONAL_ALIAS")
                self.assertNotIn(strip(p["display_name"]).lower(), names, "an alias never reuses another persona's name")

    def test_segment_shares_match_the_replay_log(self):
        rep = json.load(open(os.path.join(ROOT, "docs", "test_logs", "segment_replay.json")))["after"]["summary"]["by_segment"]
        key = {"P1": lambda b: b["green_pct"], "P2": lambda b: b["green_pct"], "P4": lambda b: b["purchase_split_pct"],
               "P5": lambda b: b["officer_referral_pct"],
               "P3": lambda b: round(100 * b["amber_reasons"]["R010-EVD"] / b["n"], 1)}
        for p in SL.PERSONAS:
            seg = SL.DATASET_ROWS[p["applicant_id"]]["customer_profile"]
            self.assertEqual(p["segment_share"]["pct"], key[p["id"]](rep[seg]), p["id"])


class PersonaEngine(Base):
    def test_documented_pathway_equals_the_live_engine_result(self):
        sc = self.svc.persona_showcase()
        self.assertEqual(len(sc["personas"]), 5)
        for p in sc["personas"]:
            self.assertTrue(p["verification"]["matches"], (p["id"], p["verification"]))
            self.assertTrue(p["engine"]["sandbox"] and not p["engine"]["persisted"])
        got = {p["id"]: p["engine"] for p in sc["personas"]}
        self.assertEqual(got["P1"]["limit"]["approved_limit_vnd"], 29_500_000)
        self.assertEqual(got["P2"]["limit"]["binding_constraint"], "INCOME_MULTIPLIER")
        self.assertEqual(got["P3"]["branches"]["share"]["layer2_results"],
                         [{"source": "open_banking", "outcome": "ACCEPTED"}, {"source": "utility_bills", "outcome": "ACCEPTED"}])
        self.assertEqual(got["P4"]["checkout"]["split"]["pay_later_vnd"], got["P4"]["limit"]["approved_limit_vnd"])
        self.assertEqual(got["P4"]["checkout"]["split"]["pay_now_vnd"], 57_764_188 - got["P4"]["limit"]["approved_limit_vnd"])
        self.assertTrue(got["P5"]["officer_referral"]["referred"])
        self.assertEqual(got["P5"]["assessment"]["rule_ids"], ["RF-02", "RF-03"])

    def test_showcase_ignores_failure_switches_simulator_applies_them(self):
        self.svc.db.set_control("cic_unavailable", "1")
        for p in self.svc.persona_showcase()["personas"]:
            self.assertTrue(p["verification"]["matches"], p["id"])
        r = self.svc.simulate_scenario({"inputs": inputs("P1")})
        self.assertEqual((r["assessment"]["reason_code"], r["next_action"], r["failure_switches_applied"]),
                         ("R010-API", "AUTOMATIC_RETRY", ["cic_unavailable"]))
        r = self.svc.simulate_scenario({"inputs": inputs("P1"), "apply_failure_switches": False})
        self.assertEqual((r["assessment"]["band"], r["failure_switches_applied"]), ("GREEN", []))


class Simulator(Base):
    def sim(self, **kw):
        return self.svc.simulate_scenario({"inputs": kw})

    def test_failure_scenarios_cover_green_amber_red(self):
        bands = set()
        for s in SL.FAILURE_SCENARIOS:
            r = self.svc.simulate_scenario({"inputs": SL.scenario_inputs(s)})
            self.assertTrue(SL.compare_expected(s["expected"], r)["matches"], (s["id"], SL.compare_expected(s["expected"], r)))
            bands.add(r["assessment"]["band"])
        self.assertEqual(bands, {"GREEN", "AMBER", "RED"})
        f5 = self.svc.simulate_scenario({"inputs": SL.scenario_inputs(SL.FAILURE_SCENARIOS[4])})
        self.assertEqual((f5["limit"]["approved_limit_vnd"], f5["limit"]["binding_constraint"]), (90_100_000, "PRODUCT_MAXIMUM"))
        self.assertEqual((f5["checkout"]["split"]["pay_later_vnd"], f5["checkout"]["split"]["pay_now_vnd"]), (90_100_000, 4_900_000))

    def test_every_decision_input_is_recalculated(self):
        base = self.sim(**inputs("P1"))
        self.assertEqual((base["assessment"]["band"], base["limit"]["approved_limit_vnd"]), ("GREEN", 29_500_000))

        def changed(field, value):
            r = self.sim(**inputs("P1", **{field: value}))
            sig = lambda x: (x["assessment"]["band"], x["assessment"]["reason_code"], x["limit"]["approved_limit_vnd"],  # noqa: E731
                             x["checkout"]["reason_code"], x["next_action"])
            self.assertNotEqual(sig(r), sig(base), "%s=%r did not change the engine result" % (field, value))
            return r
        self.assertEqual(changed("unified_credit_score", 435)["assessment"]["reason_code"], "R010-FLAG")
        self.assertEqual(changed("on_time_bill_ratio", 0.66)["assessment"]["reason_code"], "R010-FLAG")
        self.assertEqual(changed("rapid_drain_flag", 1)["assessment"]["reason_code"], "R010-FLAG")
        self.assertEqual(changed("dti_ratio", 1.0)["assessment"]["band"], "RED")
        self.assertLess(changed("monthly_inflow_vnd", 8_000_000)["limit"]["approved_limit_vnd"], 29_500_000)
        self.assertLess(changed("inflow_cv", 0.2)["limit"]["approved_limit_vnd"], 29_500_000, "volatility lowers the stressed income")
        self.assertEqual(changed("inflow_cv", 0.6)["assessment"]["reason_code"], "R010-CAPX", "never RED for volatility")
        self.assertEqual(changed("has_observable_tx", 0)["assessment"]["reason_code"], "R010-INCOME", "CIC alone verifies no income")
        self.assertEqual(changed("purchase_amount_vnd", 1_500_000)["next_action"], "PAY_NOW_ONLY_SCOPE")
        self.assertEqual(changed("purchase_amount_vnd", 40_000_000)["next_action"], "SPLIT_TENDER")
        self.assertNotEqual(changed("customer_profile", "Online merchants (MSME)")["limit"]["approved_limit_vnd"], 29_500_000,
                            "segment sets the income recognition factor")
        thin = self.sim(**inputs("P1", has_formal_cic_history=0, has_observable_tx=0))
        self.assertEqual((thin["assessment"]["reason_code"], thin["next_action"]), ("R010-EVD", "SHARE_DATA"))
        self.assertEqual(thin["derived"]["inflow_role"], "SIMULATED_OPEN_BANKING_RECORD")
        self.assertEqual(set(thin["branches"]), {"share", "decline"})
        self.assertEqual(thin["branches"]["decline"]["assessment"]["band"], "RED")

    def test_reference_only_fields_do_not_move_the_decision(self):
        base = self.sim(**inputs("P1"))
        for field, value in (("e_wallet_topups_pm", 30), ("casa_balance_vnd", 500_000), ("regular_weekly_pattern", 0),
                             ("merchant_id", "M-MKM"), ("display_name", "Khách khác")):
            r = self.sim(**inputs("P1", **{field: value}))
            self.assertEqual((r["assessment"]["band"], r["assessment"]["reason_code"], r["limit"]["approved_limit_vnd"]),
                             (base["assessment"]["band"], base["assessment"]["reason_code"], base["limit"]["approved_limit_vnd"]), field)

    def test_no_mutation_of_personas_dataset_or_demo_database(self):
        snap = copy.deepcopy((SL.PERSONAS, SL.FAILURE_SCENARIOS, SL.DATASET_ROWS))
        tables = ("customers", "applications", "transactions", "qr_codes", "audit_events", "consents", "demo_controls")
        count = lambda: {t: self.svc.db.one("SELECT COUNT(*) n FROM %s" % t)["n"] for t in tables}  # noqa: E731
        before = count()
        digest = hashlib.sha256(open(XLSX, "rb").read()).hexdigest()
        cfg = self.svc.scenario_lab_config()
        edited = cfg["personas"][0]["inputs"]
        edited["unified_credit_score"] = 400  # editing the returned copy...
        self.svc.simulate_scenario({"inputs": edited})
        self.svc.simulate_scenario({"inputs": inputs("P3", dti_ratio=0.9)})
        self.svc.persona_showcase()
        self.assertEqual(SL.persona_inputs(P["P1"])["unified_credit_score"], 681, "...never changes the reference persona")
        self.assertEqual(snap, (SL.PERSONAS, SL.FAILURE_SCENARIOS, SL.DATASET_ROWS))
        self.assertEqual(count(), before, "the sandbox writes nothing to the demo database")
        self.assertEqual(hashlib.sha256(open(XLSX, "rb").read()).hexdigest(), digest)

    def test_validation(self):
        from tests.test_service import ServiceError
        for k, v in (("unified_credit_score", 851), ("dti_ratio", 1.2), ("purchase_amount_vnd", 5_000), ("merchant_id", "M-VTU"),
                     ("customer_profile", "Student"), ("has_formal_cic_history", 2)):
            with self.assertRaises(ServiceError) as e:
                self.sim(**inputs("P2", **{k: v}))
            self.assertEqual(e.exception.code, "INVALID_LIVE_INPUT")
            self.assertIn(k, e.exception.details["fields"])
        with self.assertRaises(ServiceError):
            self.svc.simulate_scenario({"unified_credit_score": 600})

    def test_engine_is_the_locked_policy(self):
        lock = json.load(open(os.path.join(ROOT, "server", "policy", "policy.lock.json")))
        r = self.sim(**inputs("P1"))
        self.assertEqual((r["assessment"]["policy_version"], r["assessment"]["policy_hash"]), (lock["policy_version"], lock["sha256"]))


class ScenarioLabApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = build_app(os.path.join(cls.tmp, "t.sqlite3"), payment_delay_ms=0)
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.app.service.db.close(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, token=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_presenter_only_endpoints(self):
        pres = self.call("POST", "/api/session/presenter", {})[1]["token"]
        cust = self.call("POST", "/api/session/customer", {"customer_id": "CUS-001"})[1]["token"]
        for m, path, body in (("GET", "/api/presenter/scenario-lab", None), ("GET", "/api/presenter/personas", None),
                              ("POST", "/api/presenter/simulate", {"inputs": inputs("P1")})):
            self.assertEqual(self.call(m, path, body, cust)[0], 403, path)
            self.assertEqual(self.call(m, path, body)[0], 401, path)
        st, cfg = self.call("GET", "/api/presenter/scenario-lab", token=pres)
        self.assertEqual((st, len(cfg["personas"]), len(cfg["failure_scenarios"])), (200, 5, 5))
        st, sc = self.call("GET", "/api/presenter/personas", token=pres)
        self.assertEqual(st, 200)
        self.assertTrue(all(p["verification"]["matches"] for p in sc["personas"]))
        st, r = self.call("POST", "/api/presenter/simulate", {"inputs": inputs("P5")}, pres)
        self.assertEqual((st, r["assessment"]["reason_code"], r["next_action"]), (200, "R010-FLAG", "OFFICER_REVIEW"))
        st, e = self.call("POST", "/api/presenter/simulate", {"inputs": inputs("P5", unified_credit_score=900)}, pres)
        self.assertEqual((st, e["error"]["details"]["fields"]["unified_credit_score"]["code"]), (422, "OUT_OF_RANGE"))


if __name__ == "__main__":
    unittest.main()
