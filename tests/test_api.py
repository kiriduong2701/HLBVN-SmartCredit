"""HTTP integration tests against a real server on a random port: sessions and roles
(no access code anywhere), data minimisation of public endpoints, presenter gating,
error handling, removed document endpoints, and full customer + officer journeys
over the wire (Layer 2 consent, approval authority below VND 90M)."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid

os.environ["SMART_CREDIT_QUIET"] = "1"
from server.main import build_app  # noqa: E402
from server.api.http import make_server  # noqa: E402

CONSENTS = {"SC_TERMS": True, "DATA_PROCESSING": True}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = build_app(os.path.join(cls.tmp, "test.sqlite3"), payment_delay_ms=0)
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.service.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.presenter = self.call("POST", "/api/session/presenter", {})[1]["token"]
        self.call("POST", "/api/presenter/reset", token=self.presenter)

    def call(self, method, path, body=None, token=None, headers=None, raw=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None))
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def customer(self, cid):
        return self.call("POST", "/api/session/customer", {"customer_id": cid})[1]["token"]

    def officer(self):
        st, body = self.call("POST", "/api/session/officer", {})
        self.assertEqual(st, 201, body)
        return body["token"]

    def payload(self, tok, ref):
        return [q for q in self.call("GET", "/api/qr/demo-codes", token=tok)[1] if q["qr_ref"] == ref][0]["payload"]

    def authorise(self, tok, tx, purpose):
        st, ch = self.call("POST", "/api/transactions/%s/auth" % tx, {"purpose": purpose}, tok)
        self.assertEqual(st, 201, ch)
        body = {"face_result": "MATCH"} if ch["method"] == "FACE" else {"code": ch["simulated_authenticator_code"]}
        self.assertEqual(self.call("POST", "/api/auth/%s/verify" % ch["challenge_id"], body, tok)[0], 200)
        return ch["challenge_id"]

    # ---------------------------------------------------------------- security
    def test_identity_comes_from_session_not_headers(self):
        st, body = self.call("GET", "/api/me", headers={"X-Demo-Customer": "CUS-001"})
        self.assertEqual((st, body["error"]["code"]), (401, "SIGN_IN_REQUIRED"))
        st, _ = self.call("GET", "/api/me", token="forged-token")
        self.assertEqual(st, 401)
        tok = self.customer("CUS-002")
        self.assertEqual(self.call("GET", "/api/me", token=tok)[1]["customer_id"], "CUS-002")

    def test_customer_cannot_touch_other_customers_resources(self):
        a, b = self.customer("CUS-001"), self.customer("CUS-002")
        st, v = self.call("POST", "/api/qr/resolve", {"payload": self.payload(a, "QR-261016-2004")}, a)
        tx = v["view"]["transaction"]["transaction_id"]
        st, body = self.call("GET", "/api/transactions/" + tx, token=b)
        self.assertEqual((st, body["error"]["code"]), (403, "FORBIDDEN"))

    def test_roles_enforced(self):
        cust = self.customer("CUS-001")
        for path in ("/api/officer/queue", "/api/officer/policy", "/api/officer/audit"):
            self.assertEqual(self.call("GET", path, token=cust)[0], 403, path)
        self.assertEqual(self.call("POST", "/api/presenter/clock", {"advance_days": 1}, cust)[0], 403)
        self.assertEqual(self.call("POST", "/api/presenter/clock", {"advance_days": 1})[0], 401)
        self.assertEqual(self.call("POST", "/api/presenter/reset")[0], 401)
        self.assertEqual(self.call("GET", "/api/me", token=self.officer())[0], 403)

    def test_officer_and_navigation_sessions_need_no_access_code(self):
        st, body = self.call("POST", "/api/session/officer", {})
        self.assertEqual((st, body["role"], body["officer"]["approval_authority_max_vnd"]), (201, "OFFICER", 90100000))
        st, body = self.call("POST", "/api/session/presenter", {})
        self.assertEqual((st, body["role"]), (201, "PRESENTER"))
        self.assertEqual(self.call("GET", "/api/public/officers")[0], 404, "officer profile list removed")
        # a stray old-style code is simply ignored (no code is checked anywhere)
        self.assertEqual(self.call("POST", "/api/session/officer", {"officer_id": "OFF-01", "access_code": "guess"})[0], 201)
        self.assertEqual(self.call("GET", "/api/presenter/open-checkouts", token=self.presenter)[0], 200)
        self.assertEqual(self.call("GET", "/api/presenter/open-checkouts", token=self.customer("CUS-001"))[0], 403)

    def test_document_endpoints_removed(self):
        tok = self.customer("CUS-005")
        app = self.call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, tok)[1]["application_id"]
        for path in ("/api/applications/%s/documents" % app, "/api/applications/%s/third-party-consent" % app,
                     "/api/officer/documents/DOC-1/verify", "/api/officer/cases/%s/inputs" % app):
            self.assertEqual(self.call("POST", path, {}, tok)[0], 404, path)
        cfg = self.call("GET", "/api/public/config")[1]
        self.assertNotIn("documents", cfg)
        self.assertEqual(cfg["approval_authority"], {"max_vnd": 90100000, "bound": "INCLUSIVE"})
        for path in ("/api/applications/%s/information" % app, "/api/applications/%s/manual-review" % app):
            self.assertEqual(self.call("POST", path, {}, tok)[0], 404, path + " removed in v6 (no customer question, no skip)")

    def test_public_endpoints_minimise_data(self):
        cfg = self.call("GET", "/api/public/config")[1]
        text = json.dumps(cfg)
        for leaked in ("436", "0.663", "0.3695", "max_total_dti", "review_rules", "income_recognition_factor"):
            self.assertNotIn(leaked, text, leaked)
        self.assertEqual(self.call("GET", "/api/config/policy")[0], 404, "old public policy endpoint removed")
        custs = self.call("GET", "/api/public/customers")[1]
        self.assertEqual(set(custs[0]), {"customer_id", "display_name", "initials"})
        self.assertEqual(self.call("GET", "/api/demo/qr-codes")[0], 404)
        self.assertEqual(self.call("GET", "/api/qr/demo-codes")[0], 401)

    def test_errors_are_generic_and_inputs_validated(self):
        st, body = self.call("POST", "/api/session/customer", raw=b"{not json")
        self.assertEqual((st, body["error"]["code"]), (400, "INVALID_JSON"))
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("GET", "/api/session/customer")[0], 405)
        tok = self.customer("CUS-001")
        st, body = self.call("POST", "/api/transactions/X/tenure", {"tenure_months": "6"}, tok)
        self.assertEqual((st, body["error"]["code"]), (422, "INVALID_FIELD"))
        # A forced internal failure must not leak the exception text.
        orig = self.app.service.credit_summary
        self.app.service.credit_summary = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-internal-detail"))
        try:
            st, body = self.call("GET", "/api/credit", token=tok)
        finally:
            self.app.service.credit_summary = orig
        self.assertEqual(st, 500)
        self.assertNotIn("secret-internal-detail", json.dumps(body))
        self.assertIn("error_id", body["error"])

    def test_security_headers(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/../server/policy/demo_policy.json")

    def test_reset_preserves_audit_log(self):
        tok = self.customer("CUS-001")
        self.call("POST", "/api/qr/resolve", {"payload": self.payload(tok, "QR-261016-2004")}, tok)
        off = self.officer()
        before = len(self.call("GET", "/api/officer/audit", token=off)[1])
        self.call("POST", "/api/presenter/reset", token=self.presenter)
        events = self.call("GET", "/api/officer/audit", token=off)[1]
        self.assertGreater(len(events), before)
        self.assertIn("DEMO_RESET", [e["event_type"] for e in events])

    # ---------------------------------------------------------------- journeys over HTTP
    def test_instant_checkout_with_step_up_and_idempotency(self):
        tok = self.customer("CUS-001")
        st, v = self.call("POST", "/api/qr/resolve", {"payload": self.payload(tok, "QR-261016-0842")}, tok)
        self.assertEqual(st, 201)
        tx = v["view"]["transaction"]["transaction_id"]
        v = self.call("POST", "/api/transactions/%s/pay-later" % tx, {}, tok)[1]
        self.assertEqual(v["pay_later_decision"]["band"], "GREEN")
        self.assertEqual(self.call("POST", "/api/transactions/%s/tenure" % tx, {"tenure_months": 9}, tok)[0], 200)
        st, body = self.call("POST", "/api/transactions/%s/pay" % tx, {}, tok, {"Idempotency-Key": "x1"})
        self.assertEqual((st, body["error"]["code"]), (409, "AGREEMENT_NOT_ACCEPTED"))
        self.call("POST", "/api/transactions/%s/agreement" % tx, {"accepted": True}, tok)
        st, body = self.call("POST", "/api/transactions/%s/pay" % tx, {}, tok, {"Idempotency-Key": "x2"})
        self.assertEqual((st, body["error"]["code"]), (401, "STEP_UP_REQUIRED"))
        ch = self.authorise(tok, tx, "PAY_LATER_AGREEMENT")
        key = str(uuid.uuid4())
        st, v = self.call("POST", "/api/transactions/%s/pay" % tx, {"challenge_id": ch}, tok, {"Idempotency-Key": key})
        self.assertEqual((st, v["outcome"]), (200, "PAYMENT_COMPLETED"))
        st, v2 = self.call("POST", "/api/transactions/%s/pay" % tx, {"challenge_id": ch}, tok, {"Idempotency-Key": key})
        self.assertTrue(v2.get("idempotent_replay"))
        loans = self.call("GET", "/api/loans", token=tok)[1]
        self.assertEqual(len(loans["loans"]), 1)
        self.assertEqual(loans["loans"][0]["terms"]["tenure_months"], 9)

    def test_automated_layer2_and_officer_judgement_over_http(self):
        # (1) data conflict -> automated Layer 2 request -> automatic decision; no officer involved
        tok = self.customer("CUS-003")
        st, a = self.call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, tok)
        self.assertEqual((st, a["status"], a["info_request"]["route"]), (201, "ACTION_NEEDED", "ALTERNATIVE_DATA"))
        self.assertNotIn("missing", a["info_request"])
        app = a["application_id"]
        st, body = self.call("POST", "/api/applications/%s/alternative-data" % app, {"consents": {"open_banking": "yes"}}, tok)
        self.assertEqual((st, body["error"]["code"]), (422, "CONSENT_REQUIRED"))
        st, v = self.call("POST", "/api/applications/%s/alternative-data" % app,
                          {"consents": {"open_banking": True, "utility_bills": True}}, tok)
        self.assertEqual((st, v["status"], v["reason_code"]), (200, "APPROVED", "R001-L2"))
        # (2) risk flag -> officer judgement on the case summary
        tok2 = self.customer("CUS-002")
        app2 = self.call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, tok2)[1]["application_id"]
        off = self.officer()
        q = self.call("GET", "/api/officer/queue", token=off)[1]
        self.assertEqual([c["application_id"] for c in q["queues"]["pending_review"]], [app2])
        case = self.call("GET", "/api/officer/cases/" + app2, token=off)[1]
        el = case["eligibility"]
        self.assertTrue(el["case_eligible"])
        self.assertIn("case_summary", case)
        st, body = self.call("POST", "/api/officer/cases/%s/decision" % app2,
                             {"action": "request_data", "note": "x", "expected_version": case["application"]["version"]}, off)
        self.assertEqual((st, body["error"]["code"]), (422, "DATA_ACTIONS_AUTOMATIC"))
        st, case = self.call("POST", "/api/officer/cases/%s/decision" % app2,
                             {"action": "approve", "reason_code": "AP-05", "note": "Flags explained",
                              "expected_version": case["application"]["version"], "approved_limit_vnd": el["max_approvable_vnd"]}, off)
        self.assertEqual((st, case["application"]["credit_state"]), (200, "APPROVED"))
        me = self.call("GET", "/api/me", token=tok2)[1]
        self.assertEqual((me["credit"]["line_status"], me["credit"]["limit_vnd"]), ("ACTIVE", el["max_approvable_vnd"]))
        notes = [n["title_key"] for n in self.call("GET", "/api/notifications", token=tok2)[1]["items"]]
        self.assertIn("activation_approved", notes)

    def test_layer2_api_failure_over_http_retries_automatically(self):
        self.call("POST", "/api/presenter/controls", {"key": "alt_data_api_unavailable", "value": "1"}, self.presenter)
        tok = self.customer("CUS-005")
        a = self.call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, tok)[1]
        self.assertEqual(a["info_request"]["route"], "ALTERNATIVE_DATA")
        st, v = self.call("POST", "/api/applications/%s/alternative-data" % a["application_id"],
                          {"consents": {"open_banking": True, "utility_bills": True}}, tok)
        self.assertEqual((st, v["status"], v["reason_code"], v["checking_reason"]), (200, "CHECKING", "R010-API", "SOURCE_RETRY"))
        self.assertEqual(v["data_acquisition"]["layer2"][0]["results"][0]["outcome"], "UNAVAILABLE")
        self.call("POST", "/api/presenter/controls", {"key": "alt_data_api_unavailable", "value": "0"}, self.presenter)
        v = self.call("GET", "/api/applications/" + a["application_id"], token=tok)[1]
        self.assertEqual(v["status"], "CHECKING", "nothing changes until the next servicing cycle")
        self.call("POST", "/api/presenter/clock", {"advance_days": 1}, self.presenter)
        v = self.call("GET", "/api/applications/" + a["application_id"], token=tok)[1]
        self.assertEqual(v["status"], "APPROVED")

    def test_presenter_controls_clock_and_outage(self):
        st, body = self.call("POST", "/api/presenter/controls", {"key": "core_system_unavailable", "value": "1"}, self.presenter)
        self.assertEqual(st, 200)
        tok = self.customer("CUS-001")
        v = self.call("POST", "/api/qr/resolve", {"payload": self.payload(tok, "QR-261016-0842")}, tok)[1]
        tx = v["view"]["transaction"]["transaction_id"]
        v = self.call("POST", "/api/transactions/%s/pay-later" % tx, {}, tok)[1]
        self.assertEqual(v["pay_later_decision"]["reason_code"], "R010-API")
        st, body = self.call("POST", "/api/presenter/controls", {"key": "evil", "value": "1"}, self.presenter)
        self.assertEqual(st, 422)
        st, body = self.call("POST", "/api/presenter/clock", {"advance_days": 5}, self.presenter)
        self.assertEqual((st, body["business_date"]), (200, "2026-10-21"))


if __name__ == "__main__":
    unittest.main()
