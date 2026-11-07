"""Live judge demo (presenter page): one hypothetical customer typed in during the pitch goes through the SAME engine as
every persona. Covers input validation, persona / failure-scenario starting points (scenario_lab), the VND 1.6M / 90.1M product-scope boundaries,
the thin-file (no HLB transactions) path, QR scoping, officer authority at exactly 90,100,000, and the HTTP role guard."""
import copy
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
from server.services.live_demo import FIELDS
from server.services.scenario_lab import FAILURE_SCENARIOS, PERSONAS, persona_inputs, scenario_inputs
from tests.test_service import CONSENTS, Base, ServiceError

MAX = 90_100_000


class LiveDemo(Base):
    def run_checkout(self, inputs):
        r = self.svc.create_live_customer(inputs, "PRESENTER:test")
        cid = r["customer_id"]
        q = self.svc.list_qr_codes(cid)
        self.assertEqual([x["qr_ref"] for x in q], [r["qr_ref"]], "a live customer sees only their own order")
        tx = self.svc.resolve_qr(cid, q[0]["payload"])["view"]["transaction"]["transaction_id"]
        self.svc.choose_pay_later(tx, cid, CONSENTS)
        return cid, self.svc.live_customer_status(cid)

    # Starting points: "A" = persona P2 (GREEN), "B" = persona P5 (officer), "C" = failure scenario F5 (above the cap).
    def preset(self, pid, **over):
        if pid == "C":
            return dict(scenario_inputs(next(x for x in FAILURE_SCENARIOS if x["id"] == "F5")), **over)
        persona = {"A": "P2", "B": "P5"}[pid]
        return dict(persona_inputs(next(p for p in PERSONAS if p["id"] == persona)), **over)

    def test_starting_points_route_as_described_in_the_real_customer_journey(self):
        _, a = self.run_checkout(self.preset("A"))
        self.assertEqual((a["stage"], a["application"]["band"], a["checkout"]["checkout_band"]), ("APPROVED", "GREEN", "GREEN"))
        _, b = self.run_checkout(self.preset("B"))
        self.assertEqual((b["stage"], b["application"]["reason_code"]), ("OFFICER_REVIEW", "R010-FLAG"))
        _, c = self.run_checkout(self.preset("C"))
        self.assertEqual((c["application"]["approved_limit_vnd"], c["application"]["binding_constraint"]), (MAX, "PRODUCT_MAXIMUM"))
        self.assertEqual(c["checkout"]["checkout_reason_code"], "R010-SPLIT")
        self.assertEqual(c["checkout"]["split"]["pay_later_vnd"], MAX)

    def test_product_scope_boundaries_on_the_purchase(self):
        cases = {1_599_999: ("N/A", "R030-AMT"), 1_600_000: ("GREEN", "R001"), MAX: ("GREEN", "R001"), MAX + 1: ("AMBER", "R010-SPLIT")}
        for amount, (band, code) in cases.items():
            _, s = self.run_checkout(self.preset("C", purchase_amount_vnd=amount))
            self.assertEqual((s["checkout"]["checkout_band"], s["checkout"]["checkout_reason_code"]), (band, code), amount)
            self.assertLessEqual(s["line"]["limit_vnd"], MAX)
        _, s = self.run_checkout(self.preset("C", purchase_amount_vnd=MAX + 1))
        self.assertEqual((s["checkout"]["split"]["pay_later_vnd"], s["checkout"]["split"]["pay_now_vnd"]), (MAX, 1),
                         "the 1 VND above the cap is paid now, never financed")

    def test_validation_rejects_out_of_range_and_wrong_types(self):
        bad = {"monthly_inflow_vnd": 999_999, "unified_credit_score": 851, "dti_ratio": 1.01, "on_time_bill_ratio": -0.1,
               "inflow_cv": "x", "customer_profile": "Unknown", "purchase_amount_vnd": 300_000_001, "merchant_id": "M-VTU",
               "has_formal_cic_history": True, "display_name": "x" * 41}
        for k, v in bad.items():
            with self.assertRaises(ServiceError) as e:
                self.svc.create_live_customer(self.preset("A", **{k: v}), "t")
            self.assertEqual(e.exception.code, "INVALID_LIVE_INPUT")
            self.assertIn(k, e.exception.details["fields"])
        inp = self.preset("A"); inp.pop("unified_credit_score")
        with self.assertRaises(ServiceError) as e:
            self.svc.create_live_customer(inp, "t")
        self.assertEqual(e.exception.details["fields"]["unified_credit_score"]["code"], "REQUIRED")
        # Documented bounds are inclusive.
        r = self.svc.create_live_customer(self.preset("A", unified_credit_score=350, dti_ratio=0, on_time_bill_ratio=1), "t")
        self.assertTrue(r["customer_id"].startswith("CUS-J"))
        self.assertEqual(FIELDS["unified_credit_score"]["min"], 350)

    def test_no_hlb_transactions_uses_the_layer2_consent_path(self):
        cid, s = self.run_checkout(self.preset("A", has_observable_tx=0, customer_profile="Salaried, no credit history"))
        self.assertEqual((s["stage"], s["application"]["reason_code"]), ("DATA_REQUESTED", "R010-EVD"))
        app = s["application"]["application_id"]
        v = self.svc.alternative_data_consent(app, cid, {"open_banking": True, "utility_bills": True})
        self.assertIn(v["status"], ("APPROVED", "OFFER_PENDING", "IN_REVIEW"))
        self.assertEqual(v["data_acquisition"]["layer2"][0]["results"][0]["outcome"], "ACCEPTED")

    def test_live_order_is_hidden_from_other_customers_and_audited(self):
        r = self.svc.create_live_customer(self.preset("A"), "PRESENTER:test")
        self.assertNotIn(r["qr_ref"], [x["qr_ref"] for x in self.svc.list_qr_codes("CUS-001")])
        ev = [e for e in self.svc.audit_log() if e["event_type"] == "LIVE_DEMO_CUSTOMER_CREATED"]
        self.assertEqual(ev[-1]["details"]["customer_id"], r["customer_id"])
        with self.assertRaises(ServiceError):
            self.svc.live_customer_status("CUS-001")

    def test_officer_authority_is_inclusive_at_90_1m_for_a_live_case(self):
        """A referred high earner: the officer can approve exactly VND 90,100,000 and nothing above it."""
        cid, s = self.run_checkout(self.preset("C", rapid_drain_flag=1, purchase_amount_vnd=5_000_000))
        self.assertEqual(s["stage"], "OFFICER_REVIEW")
        app = s["application"]["application_id"]
        off = self.svc.officer_sign_in()["subject_id"]
        case = self.svc.officer_case(app, off)
        self.assertEqual(case["eligibility"]["max_approvable_vnd"], MAX)
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(app, off, "approve", "AP-01", "verified", case["application"]["version"], MAX + 100_000)
        self.assertIn(e.exception.code, ("ABOVE_APPROVAL_AUTHORITY", "LIMIT_OUTSIDE_CAPACITY"))
        out = self.svc.officer_decide(app, off, "approve", "AP-01", "verified salary", case["application"]["version"], MAX)
        self.assertEqual((out["application"]["credit_state"], out["application"]["approved_limit_vnd"]), ("APPROVED", MAX))
        self.assertEqual(self.svc.live_customer_status(cid)["stage"], "OFFICER_APPROVED")


class LiveDemoApi(unittest.TestCase):
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

    def test_presenter_only_and_field_errors(self):
        pres = self.call("POST", "/api/session/presenter", {})[1]["token"]
        cust = self.call("POST", "/api/session/customer", {"customer_id": "CUS-001"})[1]["token"]
        self.assertEqual(self.call("GET", "/api/presenter/live-demo", token=cust)[0], 403)
        st, cfg = self.call("GET", "/api/presenter/live-demo", token=pres)
        self.assertEqual(st, 200); self.assertIn("unified_credit_score", cfg["fields"])
        p2 = persona_inputs(PERSONAS[1])
        st, err = self.call("POST", "/api/presenter/live-demo", dict(copy.deepcopy(p2), unified_credit_score=900), pres)
        self.assertEqual((st, err["error"]["code"]), (422, "INVALID_LIVE_INPUT"))
        self.assertEqual(err["error"]["details"]["fields"]["unified_credit_score"], {"code": "OUT_OF_RANGE", "min": 350, "max": 850})
        st, r = self.call("POST", "/api/presenter/live-demo", p2, pres)
        self.assertEqual(st, 201)
        st, s = self.call("GET", "/api/presenter/live-demo/" + r["customer_id"], token=pres)
        self.assertEqual((st, s["stage"]), (200, "WAITING_CUSTOMER"))


if __name__ == "__main__":
    unittest.main()
