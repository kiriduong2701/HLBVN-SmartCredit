"""Presenter page section 3 - the three risk-round simulations on the actual engine: system overload (bounded pool,
bounded queue, deadline-aware fallback), rule & evidence errors (misconfigured policy copies caught by start-up validation,
the version lock and a shadow replay; unreliable evidence; conflicting rules), and missing / anomalous data. Also checks
that none of it touches the live policy, the policy file or the demo database."""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from server.api.http import make_server
from server.main import build_app
from server.services.risk_lab import DATA_CASES, OVERLOAD_PRESETS, RULE_CASES, simulate_gateway
from tests.test_service import Base, ServiceError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_FILE = os.path.join(ROOT, "server", "policy", "demo_policy.json")
LOCK = json.load(open(os.path.join(ROOT, "server", "policy", "policy.lock.json")))
P = {p["id"]: {k: p[k] for k in ("requests", "window_s", "workers", "queue_max", "budget_ms")} for p in OVERLOAD_PRESETS}


class Overload(Base):
    def test_normal_load_is_fully_decided(self):
        r = self.svc.risk_overload(P["normal"])
        self.assertEqual((r["protected"]["decided"], r["protected"]["fallback"]), (300, 0))
        self.assertLess(r["engine"]["measured_ms_per_decision"], 50, "engine compute is measured, small")
        self.assertEqual(sum(r["decided_outcomes"].values()), 300)

    def test_spike_degrades_safely(self):
        r = self.svc.risk_overload(P["spike"])
        pr, un = r["protected"], r["unprotected"]
        self.assertGreater(pr["fallback"], 0)
        self.assertEqual(pr["decided"] + pr["timed_out"] + pr["shed"], 6000)
        self.assertLessEqual(pr["latency_ms"]["max"], P["spike"]["budget_ms"], "no decided request exceeds the budget")
        self.assertLessEqual(pr["all_latency_p95_ms"], P["spike"]["budget_ms"], "no shopper waits longer than the budget")
        self.assertGreater(un["latency_ms"]["p95"], 10 * P["spike"]["budget_ms"], "without safeguards the queue explodes")
        self.assertGreater(un["waited_over_counter_s"], 0)
        self.assertEqual((r["fallback_state"]["approvals_during_fallback"], r["fallback_state"]["credit_decisions_written"]), (0, 0))
        self.assertIn("PAY_NOW", r["fallback_state"]["payment_options"])

    def test_gateway_model_is_deterministic(self):
        a = simulate_gateway(500, 10, 4, 20, 1.0, 0.001, 7)
        b = simulate_gateway(500, 10, 4, 20, 1.0, 0.001, 7)
        self.assertEqual(a, b)

    def test_validation(self):
        with self.assertRaises(ServiceError) as e:
            self.svc.risk_overload(dict(P["normal"], workers=0))
        self.assertEqual(e.exception.code, "INVALID_OVERLOAD_INPUT")
        self.assertIn("workers", e.exception.details["fields"])


class RulesAndEvidence(Base):
    def test_misconfigured_thresholds_are_caught_and_never_reach_the_live_service(self):
        digest = hashlib.sha256(open(POLICY_FILE, "rb").read()).hexdigest()
        for cid in ("M1", "M2"):
            r = self.svc.risk_rule_case({"case": cid})
            c = r["controls"]
            self.assertEqual(c["startup_validation"]["errors"], [], "well-formed values pass start-up checks...")
            self.assertTrue(c["version_lock"]["release_refused"], "...but the version lock refuses them")
            self.assertTrue(c["live_service"]["still_locked"])
            self.assertGreater(r["shadow_replay"]["decisions_changed"], 0)
            self.assertEqual(r["shadow_replay"]["rows"], 10000)
        m1 = self.svc.risk_rule_case({"case": "M1"})["shadow_replay"]
        self.assertEqual(set(m1["flips"]), {"GREEN→AMBER"})
        m2 = self.svc.risk_rule_case({"case": "M2"})["shadow_replay"]
        self.assertIn("AMBER→GREEN", m2["flips"]); self.assertGreater(m2["higher_limits"], 0)
        self.assertEqual(self.svc.policy_hash, LOCK["sha256"])
        self.assertEqual(hashlib.sha256(open(POLICY_FILE, "rb").read()).hexdigest(), digest, "the policy file is untouched")

    def test_contradictory_configuration_stops_start_up(self):
        r = self.svc.risk_rule_case({"case": "M3"})
        self.assertTrue(r["controls"]["startup_validation"]["service_refuses_to_start"])
        self.assertIn("approval authority", r["controls"]["startup_validation"]["errors"][0])
        self.assertNotIn("shadow_replay", r)

    def test_unreliable_evidence_goes_to_fraud_review_not_approval(self):
        r = self.svc.risk_rule_case({"case": "E1"})
        self.assertEqual(r["result"]["assessment"]["reason_code"], "R010-EVD")
        b = r["branch"]
        self.assertEqual((b["assessment"]["reason_code"], b["next_action"]), ("R010-FRD", "FRAUD_REVIEW"))
        self.assertIn({"source": "open_banking", "outcome": "INVALID"}, b["layer2_results"])
        self.assertIsNone(b["limit"]["approved_limit_vnd"])

    def test_conflicting_rules_resolve_deterministically_with_secondary_codes(self):
        r = self.svc.risk_rule_case({"case": "E2"})
        a = r["result"]["assessment"]
        self.assertEqual((a["band"], a["reason_code"], a["secondary_codes"]), ("RED", "R020-CAP", ["R010-FLAG"]))
        self.assertEqual([c["result"]["assessment"]["reason_code"] for c in r["components"]], ["R010-FLAG", "R020-CAP"])
        self.assertEqual(self.svc.risk_rule_case({"case": "E2"})["result"]["assessment"]["reason_code"], "R020-CAP")


class DataAnomalies(Base):
    def run_case(self, cid):
        return self.svc.risk_data_case({"case": cid})

    def test_each_anomaly_is_handled_by_the_existing_rules(self):
        d1 = self.run_case("D1")["result"]
        self.assertEqual((d1["assessment"]["reason_code"], d1["branches"]["share"]["assessment"]["reason_code"],
                          d1["branches"]["decline"]["assessment"]["reason_code"]), ("R010-EVD", "R001-L2", "R020-EVD"))
        d2 = self.run_case("D2")
        self.assertEqual(d2["baseline"]["band"], "GREEN")
        self.assertEqual(d2["result"]["assessment"]["reason_code"], "R010-INCOME")
        self.assertIn("R-VER-03", [e["rule_id"] for e in d2["result"]["readiness"]["data_quality_events"]])
        d3 = self.run_case("D3")["result"]
        self.assertIn("R-VER-01", [e["rule_id"] for e in d3["readiness"]["data_quality_events"]])
        self.assertIn("monthly_inflow_vnd", d3["assessment"]["fields_not_used"], "conflicting values are excluded, not used")
        d4 = self.run_case("D4")["result"]
        self.assertEqual((d4["assessment"]["reason_code"], d4["next_action"]), ("R010-VER", "OFFICER_REVIEW"))
        self.assertEqual(d4["readiness"]["validation_errors"][0]["rule_id"], "R-INV-01")
        d5 = self.run_case("D5")["result"]
        self.assertEqual(d5["branches"]["share"]["assessment"]["reason_code"], "R020-EVD")
        self.assertTrue(all(x["outcome"] == "INVALID" for x in d5["branches"]["share"]["layer2_results"]))
        d6 = self.run_case("D6")
        self.assertLess(d6["result"]["limit"]["approved_limit_vnd"], d6["baseline"]["approved_limit_vnd"],
                        "unknown segment -> conservative income recognition, no question to the customer")

    def test_cases_do_not_touch_the_demo_database(self):
        tables = ("customers", "applications", "transactions", "audit_events", "demo_controls")
        count = lambda: {t: self.svc.db.one("SELECT COUNT(*) n FROM %s" % t)["n"] for t in tables}  # noqa: E731
        before = count()
        for c in DATA_CASES:
            self.run_case(c["id"])
        for c in RULE_CASES:
            if c["kind"] == "EVIDENCE":
                self.svc.risk_rule_case({"case": c["id"]})
        self.svc.risk_overload(P["normal"])
        self.assertEqual(count(), before)

    def test_unknown_case(self):
        with self.assertRaises(ServiceError):
            self.run_case("D9")


class RiskApi(unittest.TestCase):
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

    def test_presenter_only(self):
        pres = self.call("POST", "/api/session/presenter", {})[1]["token"]
        cust = self.call("POST", "/api/session/customer", {"customer_id": "CUS-001"})[1]["token"]
        for m, path, body in (("GET", "/api/presenter/risk", None), ("POST", "/api/presenter/risk/overload", P["normal"]),
                              ("POST", "/api/presenter/risk/rules", {"case": "E2"}), ("POST", "/api/presenter/risk/data", {"case": "D4"})):
            self.assertEqual(self.call(m, path, body, cust)[0], 403, path)
            st, _ = self.call(m, path, body, pres)
            self.assertEqual(st, 200, path)
        st, cfg = self.call("GET", "/api/presenter/risk", token=pres)
        self.assertTrue(cfg["live_policy"]["matches_lock"])
        self.assertEqual((len(cfg["rule_cases"]), len(cfg["data_cases"]), len(cfg["overload"]["presets"])), (5, 6, 3))


if __name__ == "__main__":
    unittest.main()
