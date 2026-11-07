"""Service-level tests of both journeys: Limit Activation and Instant Checkout, the
exposure ledger (holds, concurrency), step-up authentication, API data acquisition
(Layer 1 automatic, Layer 2 chosen by the system with optional per-source consent:
granted, declined, outage + automatic retry, invalid data), officer controls (single
role, full authority within VND 1.6M-90.1M, eligibility guard, boundaries, SLA, no data
actions), delinquency freeze/cure, conversion within 7 days, failures/outages and
auditability."""
import copy
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

from server.services.smart_credit import ServiceError, SmartCreditService
from server.store.db import open_database

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = json.load(open(os.path.join(ROOT, "server", "policy", "demo_policy.json")))
FIXTURES = json.load(open(os.path.join(ROOT, "server", "fixtures", "demo_data.json")))
CONSENTS = {c["id"]: True for c in POLICY["consent"]["activation_consents"]}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def make(policy=POLICY):
    clock = Clock()
    return SmartCreditService(open_database(":memory:", FIXTURES), policy, FIXTURES, payment_delay_ms=0, clock=clock), clock


class Base(unittest.TestCase):
    policy = POLICY

    def setUp(self):
        self.svc, self.clock = make(self.policy)
        self.n = 0
        self.addCleanup(lambda: self.svc.db.close())

    def payload(self, ref):
        return self.svc.db.one("SELECT payload FROM qr_codes WHERE qr_ref = ?", (ref,))["payload"]

    def scan(self, cust, ref, amount=None):
        return self.svc.resolve_qr(cust, self.payload(ref), amount)["view"]["transaction"]["transaction_id"]

    def key(self):
        self.n += 1
        return "k-%d" % self.n

    def auth(self, cust, tx, purpose):
        ch = self.svc.create_challenge(tx, cust, purpose)
        if ch["method"] == "FACE":
            self.svc.verify_challenge(ch["challenge_id"], cust, face_result="MATCH")
        else:
            self.svc.verify_challenge(ch["challenge_id"], cust, code=ch["simulated_authenticator_code"])
        return ch

    def pay_later(self, cust, ref, tenure=None, consents=None):
        tx = self.scan(cust, ref)
        v = self.svc.choose_pay_later(tx, cust, consents)
        return tx, v

    def complete_pl(self, cust, tx, tenure=None):
        if tenure:
            self.svc.select_tenure(tx, cust, tenure)
        self.svc.accept_agreement(tx, cust, True)
        ch = self.auth(cust, tx, "PAY_LATER_AGREEMENT")
        return self.svc.pay(tx, cust, self.key(), ch["challenge_id"])

    def pay_now(self, cust, tx):
        self.svc.choose_pay_now(tx, cust)
        ch = self.auth(cust, tx, "ACCOUNT_PAYMENT")
        return self.svc.pay(tx, cust, self.key(), ch["challenge_id"])

    def events(self, **kw):
        return [e["event_type"] for e in self.svc.audit_log(**kw)]

    def officer(self):
        return self.svc.officer_sign_in()["subject_id"]

    def to_review(self, cust):
        """Activate a persona whose Layer 1 data routes straight to Amber officer review."""
        a = self.svc.start_activation(cust, CONSENTS)
        self.assertEqual(a["status"], "IN_REVIEW", a["reason_code"])
        return a["application_id"]

    def thin_file_with_layer2(self, consents=None):
        a = self.svc.start_activation("CUS-005", CONSENTS)
        return self.svc.alternative_data_consent(a["application_id"], "CUS-005",
                                                 consents or {"open_banking": True, "utility_bills": True})


class InstantCheckoutTests(Base):
    def test_green_checkout_books_loan_against_principal(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        d = v["pay_later_decision"]
        self.assertEqual((v["outcome"], d["band"], d["reason_code"]), ("REVIEW_PAY_LATER", "GREEN", "R001"))
        self.assertNotIn(3, d["affordable_tenures"], "3 months would exceed the DTI ceiling")
        self.assertEqual(v["terms"]["monthly_instalment_vnd"], 2120000)
        self.assertEqual(v["credit"]["held_vnd"], 12000000)
        v = self.complete_pl("CUS-001", tx)
        self.assertEqual(v["transaction"]["state"], "PAYMENT_COMPLETED")
        cs = self.svc.credit_summary("CUS-001")
        self.assertEqual((cs["outstanding_principal_vnd"], cs["held_vnd"], cs["available_vnd"]), (12000000, 0, 17500000))
        self.assertEqual(cs["outstanding_total_vnd"], 12720000, "interest is tracked but not counted against the limit")
        self.assertIsNotNone(v["settlement"])
        self.assertIn("EXPOSURE_HOLD_BOOKED", self.events(transaction_id=tx))

    def test_checkout_never_waits_for_review(self):
        tx, v = self.pay_later("CUS-002", "QR-261016-2004", consents=CONSENTS)
        self.assertEqual(v["activation"]["status"], "IN_REVIEW")
        self.assertEqual(v["pay_later_decision"]["reason_code"], "R010-PENDING")
        self.assertEqual(v["transaction"]["state"], "QR_CREATED")
        self.assertIsNotNone(v["seconds_left"], "the 5-minute checkout timer keeps running")
        self.assertEqual(self.pay_now("CUS-002", tx)["transaction"]["state"], "PAYMENT_COMPLETED")

    def test_inline_activation_green_continues_in_same_checkout(self):
        tx, v = self.pay_later("CUS-007", "QR-261016-1505", consents=CONSENTS)
        self.assertEqual((v["activation"]["status"], v["pay_later_decision"]["band"]), ("APPROVED", "GREEN"))
        # v5: 25th-percentile month 12,998,865 x (1 - 0.6745 x 0.4812) = 8,779,839; 0.5 x that - 3,037,835 obligations
        # = 1,352,084 headroom -> 14,486,615 by capacity (v4's CV haircut gave 3.5M). v6: CUS-007 is a gig worker whose
        # inflow is unclassified, so the income-multiple cap uses recognised income 2 x 0.7 x 8,779,839 = 12,291,775 -> 12.0M.
        self.assertEqual(v["activation"]["approved_limit_vnd"], 12000000)
        self.assertEqual(self.complete_pl("CUS-007", tx)["outcome"], "PAYMENT_COMPLETED")

    def test_no_line_without_consents_offers_activation(self):
        tx, v = self.pay_later("CUS-007", "QR-261016-1505")
        self.assertEqual(v["pay_later_decision"]["band"], "NO_LINE")
        with self.assertRaises(ServiceError) as e:
            self.svc.choose_pay_later(tx, "CUS-007", {"SC_TERMS": True})
        self.assertEqual(e.exception.code, "CONSENT_REQUIRED")

    def test_mandatory_transaction_checks(self):
        """v6: product-scope outcomes are N/A (not credit declines); above the product maximum only the in-scope part can be
        split; Pay Now is always a separate option."""
        for ref, code, band in (("QR-261016-1418", "R030-MER", "N/A"), ("QR-261016-1830", "R030-AMT", "N/A"),
                                ("QR-261016-1915", "R010-SPLIT", "AMBER"), ("QR-261016-1610", "R010-SPLIT", "AMBER")):
            tx, v = self.pay_later("CUS-001", ref)
            d = v["pay_later_decision"]
            self.assertEqual((d["reason_code"], d["band"]), (code, band), ref)
            self.assertIn("PAY_NOW", d["alternatives"])
            self.svc.cancel(tx, "CUS-001")
        # 95M purchase on a 29.5M line: Pay Later is capped by the available limit (below the 90.1M product maximum)
        tx, v = self.pay_later("CUS-001", "QR-261016-1915")
        self.assertEqual(v["pay_later_decision"]["split"]["pay_later_vnd"] + v["pay_later_decision"]["split"]["pay_now_vnd"], 95000000)
        self.assertLessEqual(v["pay_later_decision"]["split"]["pay_later_vnd"], 29500000)

    def test_tenure_must_be_affordable_and_reprices_hold(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        with self.assertRaises(ServiceError) as e:
            self.svc.select_tenure(tx, "CUS-001", 3)
        self.assertEqual(e.exception.code, "TENURE_NOT_AFFORDABLE")
        v = self.svc.select_tenure(tx, "CUS-001", 12)
        self.assertEqual(v["terms"]["monthly_instalment_vnd"], v["hold"]["monthly_instalment_vnd"])

    def test_preview_equals_booked_schedule(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        offer = [o for o in v["pay_later_decision"]["offers"] if o["tenure_months"] == 9][0]
        self.complete_pl("CUS-001", tx, 9)
        loan = self.svc.list_loans("CUS-001")["loans"][0]
        self.assertEqual(loan["instalments"][0]["amount_vnd"], offer["monthly_instalment_vnd"])
        self.assertEqual(sum(i["amount_vnd"] for i in loan["instalments"]), offer["total_repayable_vnd"])

    def test_static_qr_needs_amount_and_is_pay_now_only(self):
        with self.assertRaises(ServiceError) as e:
            self.scan("CUS-001", "QR-STATIC-ANC")
        self.assertEqual(e.exception.code, "AMOUNT_REQUIRED")
        tx = self.scan("CUS-001", "QR-STATIC-ANC", 85000)
        v = self.svc.choose_pay_later(tx, "CUS-001")
        self.assertEqual((v["pay_later_decision"]["reason_code"], v["pay_later_decision"]["band"]), ("R030-MER", "N/A"))
        self.assertEqual(self.pay_now("CUS-001", tx)["transaction"]["state"], "PAYMENT_COMPLETED")
        tx2 = self.scan("CUS-001", "QR-STATIC-ANC", 40000)  # static QR can be paid again (no order key)
        self.assertEqual(self.pay_now("CUS-001", tx2)["transaction"]["state"], "PAYMENT_COMPLETED")

    def test_tampered_or_mismatched_qr_rejected(self):
        p = self.payload("QR-261016-0842")
        with self.assertRaises(ServiceError) as e:
            self.svc.resolve_qr("CUS-001", p.replace("540812000000", "540811000000"))
        self.assertEqual(e.exception.code, "QR_CRC_MISMATCH")
        from server.engine import qr as QR
        forged = QR.build("970999", "1000012345", "5732", "ABC ELECTRONICS", "HO CHI MINH", 1000000, "A-0842")
        with self.assertRaises(ServiceError) as e:
            self.svc.resolve_qr("CUS-001", forged)
        self.assertEqual(e.exception.code, "QR_INVALID", "valid CRC but amount differs from the merchant order")

    def test_duplicate_order_and_idempotent_payment(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0915")
        self.svc.accept_agreement(tx, "CUS-001", True)
        ch = self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        self.svc.pay(tx, "CUS-001", "same", ch["challenge_id"])
        self.assertTrue(self.svc.pay(tx, "CUS-001", "same", ch["challenge_id"])["idempotent_replay"])
        with self.assertRaises(ServiceError) as e:
            self.svc.resolve_qr("CUS-001", self.payload("QR-261016-0915"))
        self.assertEqual(e.exception.code, "QR_ALREADY_PAID")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM loans")["n"], 1)

    def test_payment_failure_keeps_hold_and_retry_needs_new_auth(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-1352")  # fixture: first acquirer attempt fails
        v = self.complete_pl("CUS-001", tx)
        self.assertEqual(v["transaction"]["state"], "PAYMENT_FAILED")
        self.assertIsNotNone(v["hold"], "approval/hold kept after a failed payment")
        old = self.svc.db.one("SELECT challenge_id FROM auth_challenges ORDER BY rowid DESC LIMIT 1")["challenge_id"]
        with self.assertRaises(ServiceError) as e:
            self.svc.pay(tx, "CUS-001", self.key(), old)
        self.assertEqual(e.exception.code, "STEP_UP_REQUIRED", "a used challenge cannot authorise a retry")
        ch = self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        self.assertEqual(self.svc.pay(tx, "CUS-001", self.key(), ch["challenge_id"])["transaction"]["state"], "PAYMENT_COMPLETED")

    def test_cancel_expiry_and_switch_release_hold(self):
        for how in ("cancel", "expire", "switch"):
            self.svc.db.close()
            svc, clock = make()
            self.svc, self.clock = svc, clock
            tx, v = self.pay_later("CUS-001", "QR-261016-0915")
            self.assertEqual(svc.credit_summary("CUS-001")["held_vnd"], 6400000)
            if how == "cancel":
                svc.cancel(tx, "CUS-001")
            elif how == "expire":
                clock.now += timedelta(seconds=301)
                svc.sweep_expired()
                self.assertEqual(svc._tx(tx)["state"], "EXPIRED")
            else:
                svc.choose_pay_now(tx, "CUS-001")
            self.assertEqual(svc.credit_summary("CUS-001")["held_vnd"], 0, how)
            self.assertIn("EXPOSURE_HOLD_RELEASED", self.events(transaction_id=tx))

    def test_system_outage_is_fail_safe(self):
        self.svc.set_demo_control("core_system_unavailable", "1", "test")
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.assertEqual((v["pay_later_decision"]["band"], v["pay_later_decision"]["reason_code"]), ("AMBER", "R010-API"))
        self.assertEqual(v["credit"]["held_vnd"], 0)


class ExposureConcurrencyTests(Base):
    """Audit B3: concurrent checkouts can no longer exceed the limit."""
    policy = copy.deepcopy(POLICY)
    policy["affordability"]["max_total_dti"] = 0.95  # isolate the limit control from affordability

    def test_second_checkout_sees_first_hold(self):
        t1, v1 = self.pay_later("CUS-001", "QR-261016-1133")  # 15.0M
        t2, v2 = self.pay_later("CUS-001", "QR-261016-0842")  # 12.0M -> 27.0M <= 27.5M: allowed
        t3, v3 = self.pay_later("CUS-001", "QR-261016-1021")  # 9.9M -> would be 36.9M
        self.assertEqual((v1["pay_later_decision"]["band"], v2["pay_later_decision"]["band"]), ("GREEN", "GREEN"))
        d3 = v3["pay_later_decision"]
        self.assertEqual(d3["reason_code"], "R010-SPLIT", "over-limit: split tender, never an automatic refusal")
        self.assertEqual(d3["split"]["pay_later_vnd"] + d3["split"]["pay_now_vnd"], 9900000)
        self.assertLessEqual(d3["split"]["pay_later_vnd"], d3["available_vnd"])
        self.assertIsNone(v3["hold"], "no exposure is reserved until the customer accepts the split")
        for t in (t1, t2):
            self.complete_pl("CUS-001", t)
        cs = self.svc.credit_summary("CUS-001")
        self.assertLessEqual(cs["outstanding_principal_vnd"], cs["limit_vnd"])

    def test_parallel_threads_cannot_overbook(self):
        txs = [self.scan("CUS-001", r) for r in ("QR-261016-1133", "QR-261016-0842", "QR-261016-1021")]
        results = {}

        def go(t):
            results[t] = self.svc.choose_pay_later(t, "CUS-001")["pay_later_decision"]["band"]
        threads = [threading.Thread(target=go, args=(t,)) for t in txs]
        [th.start() for th in threads]
        [th.join() for th in threads]
        cs = self.svc.credit_summary("CUS-001")
        self.assertLessEqual(cs["held_vnd"], cs["limit_vnd"], "parallel holds can never exceed the limit")
        self.assertGreaterEqual(list(results.values()).count("GREEN"), 1)
        self.assertNotEqual(list(results.values()).count("GREEN"), 3, "36.9M of purchases cannot all be held on the line")

    def test_payment_rechecks_ledger(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.svc.db.run("UPDATE credit_lines SET limit_vnd = 5000000 WHERE customer_id = 'CUS-001'")  # limit cut after the hold
        self.svc.accept_agreement(tx, "CUS-001", True)
        ch = self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        with self.assertRaises(ServiceError) as e:
            self.svc.pay(tx, "CUS-001", self.key(), ch["challenge_id"])
        self.assertEqual(e.exception.code, "LIMIT_EXCEEDED")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM loans")["n"], 0)


class AuthTests(Base):
    def test_pay_requires_verified_challenge_bound_to_tx(self):
        tx = self.scan("CUS-001", "QR-261016-2004")
        self.svc.choose_pay_now(tx, "CUS-001")
        with self.assertRaises(ServiceError) as e:
            self.svc.pay(tx, "CUS-001", self.key(), None)
        self.assertEqual(e.exception.code, "STEP_UP_REQUIRED")
        other = self.scan("CUS-001", "QR-261016-2110")
        self.svc.choose_pay_now(other, "CUS-001")
        ch = self.auth("CUS-001", other, "ACCOUNT_PAYMENT")
        with self.assertRaises(ServiceError):
            self.svc.pay(tx, "CUS-001", self.key(), ch["challenge_id"])

    def test_method_thresholds(self):
        small = self.scan("CUS-001", "QR-261016-2004")  # 4.5M
        self.svc.choose_pay_now(small, "CUS-001")
        self.assertEqual(self.svc.create_challenge(small, "CUS-001", "ACCOUNT_PAYMENT")["method"], "SMART_OTP")
        big = self.scan("CUS-001", "QR-261016-0842")  # 12M > 10M per transaction
        self.svc.choose_pay_now(big, "CUS-001")
        self.assertEqual(self.svc.create_challenge(big, "CUS-001", "ACCOUNT_PAYMENT")["method"], "FACE")
        self.pay_now("CUS-001", big)
        later = self.scan("CUS-001", "QR-261016-0915")  # 6.4M but 12M + 6.4M ... daily 18.4M <= 20M
        self.svc.choose_pay_now(later, "CUS-001")
        self.assertEqual(self.svc.create_challenge(later, "CUS-001", "ACCOUNT_PAYMENT")["method"], "SMART_OTP")
        self.pay_now("CUS-001", later)
        third = self.scan("CUS-001", "QR-261016-2110")  # 3.1M -> 21.5M > 20M daily
        self.svc.choose_pay_now(third, "CUS-001")
        self.assertEqual(self.svc.create_challenge(third, "CUS-001", "ACCOUNT_PAYMENT")["method"], "FACE")

    def test_face_threshold_is_inclusive(self):
        """Official HLBVN rule: face authentication for 10M or more (not only above 10M)."""
        self.svc.db.run("UPDATE qr_codes SET amount_vnd = 10000000 WHERE qr_ref = 'QR-STATIC-ANC'")
        tx = self.scan("CUS-001", "QR-STATIC-ANC", 10000000)
        self.svc.choose_pay_now(tx, "CUS-001")
        self.assertEqual(self.svc.create_challenge(tx, "CUS-001", "ACCOUNT_PAYMENT")["method"], "FACE")
        tx2 = self.scan("CUS-001", "QR-STATIC-ANC", 9999000)
        self.svc.choose_pay_now(tx2, "CUS-001")
        self.assertEqual(self.svc.create_challenge(tx2, "CUS-001", "ACCOUNT_PAYMENT")["method"], "SMART_OTP")

    def test_wrong_code_lockout_and_expiry(self):
        tx = self.scan("CUS-001", "QR-261016-2004")
        self.svc.choose_pay_now(tx, "CUS-001")
        ch = self.svc.create_challenge(tx, "CUS-001", "ACCOUNT_PAYMENT")
        for i in range(3):
            with self.assertRaises(ServiceError) as e:
                self.svc.verify_challenge(ch["challenge_id"], "CUS-001", code="000000" if ch["simulated_authenticator_code"] != "000000" else "111111")
        self.assertEqual(e.exception.code, "AUTH_LOCKED")
        ch2 = self.svc.create_challenge(tx, "CUS-001", "ACCOUNT_PAYMENT")
        self.clock.now += timedelta(seconds=121)
        with self.assertRaises(ServiceError) as e:
            self.svc.verify_challenge(ch2["challenge_id"], "CUS-001", code=ch2["simulated_authenticator_code"])
        self.assertEqual(e.exception.code, "CHALLENGE_EXPIRED")
        self.assertIn("STEP_UP_FAILED", self.events(transaction_id=tx))

    def test_agreement_required_before_pay_later_auth(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0915")
        with self.assertRaises(ServiceError) as e:
            self.svc.create_challenge(tx, "CUS-001", "PAY_LATER_AGREEMENT")
        self.assertEqual(e.exception.code, "NOTHING_TO_AUTHORISE")

    def test_insufficient_balance_blocked_before_auth(self):
        tx = self.scan("CUS-002", "QR-261016-0915")  # 6.4M > 6.2M balance
        self.svc.choose_pay_now(tx, "CUS-002")
        with self.assertRaises(ServiceError) as e:
            self.svc.create_challenge(tx, "CUS-002", "ACCOUNT_PAYMENT")
        self.assertEqual(e.exception.code, "INSUFFICIENT_FUNDS")


class ActivationTests(Base):
    def test_pre_activated_persona_used_real_engine(self):
        app = self.svc.db.one("SELECT * FROM applications WHERE customer_id = 'CUS-001'")
        self.assertEqual((app["origin"], app["band"], app["reason_code"], app["approved_limit_vnd"]), ("SEED", "GREEN", "R001", 29500000))
        kinds = [c["kind"] for c in self.svc.db.all("SELECT kind FROM consents WHERE application_id = ?", (app["application_id"],))]
        self.assertEqual(sorted(kinds), ["DATA_PROCESSING", "SC_TERMS"], "product terms + the legal data-processing step only")

    def test_legal_data_processing_step_required_but_no_per_variable_consent(self):
        with self.assertRaises(ServiceError) as e:
            self.svc.start_activation("CUS-007", {"SC_TERMS": True})
        self.assertEqual(e.exception.details["missing"], ["DATA_PROCESSING"])
        ids = [c["id"] for c in POLICY["consent"]["activation_consents"]]
        self.assertEqual(ids, ["SC_TERMS", "DATA_PROCESSING"], "no per-variable or per-Layer-1-source consent")

    def test_layer1_is_retrieved_automatically_logged_and_not_shown_to_the_customer(self):
        a = self.svc.start_activation("CUS-007", CONSENTS)
        self.assertNotIn("layer1", a["data_acquisition"], "v6: which Layer 1 data was assessed is internal")
        self.assertNotIn("reason_rule_ids", a)
        l1 = json.loads(self.svc._app(a["application_id"])["acquisition_json"])["layer1"]
        self.assertEqual([e["source"] for e in l1], ["hlb_core_profile", "hlb_transactions", "hlb_credit_exposure", "cic"])
        self.assertTrue(all(e["simulated"] for e in l1))
        self.assertEqual({e["source"]: e["status"] for e in l1}["cic"], "NO_RECORD", "no CIC history is a valid answer")
        self.assertEqual(a["status"], "APPROVED")
        self.assertIsNone(a["info_request"])

    def test_layer2_only_when_layer1_insufficient_and_system_chooses_sources(self):
        for cust in ("CUS-002", "CUS-004", "CUS-006", "CUS-007"):
            v = self.svc.start_activation(cust, CONSENTS)
            self.assertIn(v["status"], ("IN_REVIEW", "APPROVED"), cust)
            self.assertIsNone(v["info_request"], cust)
        a = self.svc.start_activation("CUS-005", CONSENTS)
        req = a["info_request"]
        self.assertEqual((a["status"], a["reason_code"], req["route"]), ("ACTION_NEEDED", "R010-EVD", "ALTERNATIVE_DATA"))
        self.assertEqual(req["purposes"], ["VERIFIED_INCOME", "REPAYMENT_BEHAVIOUR"])
        self.assertNotIn("missing", req, "the customer never sees which Layer 1 data was missing")
        self.assertEqual([x["id"] for x in req["sources"]], ["open_banking", "utility_bills"],
                         "income source for the segment + behaviour only because there is no CIC/bill data; never e-wallet")
        self.assertTrue(all(x["purpose"] for x in req["sources"]))
        self.assertEqual(req["options"], ["alternative_data"], "no 'skip to an officer' path")
        # conflicting HLB data (CUS-003): excluded automatically, Layer 2 requested instead of an officer
        a = self.svc.start_activation("CUS-003", CONSENTS)
        self.assertEqual((a["status"], a["reason_code"]), ("ACTION_NEEDED", "R010-EVD"))

    def test_layer2_consent_is_optional_unticked_and_per_source(self):
        a = self.svc.start_activation("CUS-005", CONSENTS)
        app = a["application_id"]
        for bad, code in (({"open_banking": "yes"}, "CONSENT_REQUIRED"),
                          ({"open_banking": True, "e_invoice": True}, "SOURCE_NOT_OFFERED")):
            with self.assertRaises(ServiceError) as e:
                self.svc.alternative_data_consent(app, "CUS-005", bad)
            self.assertEqual(e.exception.code, code)
        with self.assertRaises(ServiceError) as e:
            self.svc.alternative_data_consent(self.svc.start_activation("CUS-007", CONSENTS)["application_id"], "CUS-007",
                                              {"open_banking": True})
        self.assertEqual(e.exception.code, "ALTERNATIVE_DATA_NOT_REQUESTED")
        # an unticked (omitted) source is recorded as not allowed and never called
        r = self.svc.alternative_data_consent(app, "CUS-005", {"open_banking": True})
        cons = {c["kind"]: c["granted"] for c in self.svc.db.all("SELECT kind, granted FROM consents WHERE application_id = ?", (app,))}
        self.assertEqual((cons["ALT_DATA:open_banking"], cons["ALT_DATA:utility_bills"]), (1, 0))
        self.assertEqual([x["source"] for x in r["data_acquisition"]["layer2"][0]["results"]], ["open_banking"])

    def test_layer2_validated_supports_automatic_decision(self):
        """v6: validated, stable Layer 2 income + a behaviour signal -> automatic approval (R001-L2); no officer."""
        a = self.thin_file_with_layer2()
        # Open Banking salary 12,016,666 avg (series min 11.9M -> P25 12.0M); CIC no record -> 0 obligations;
        # 2 x 12.0M = 24.0M binds (capacity 0.5 x 12.0M x 12/1.12 = 64.3M)
        self.assertEqual((a["status"], a["reason_code"], a["band"], a["approved_limit_vnd"]), ("APPROVED", "R001-L2", "GREEN", 24000000))
        self.assertEqual(self.svc.credit_summary("CUS-005")["line_status"], "ACTIVE")
        r = a["data_acquisition"]["layer2"][0]
        self.assertEqual({x["source"]: x["outcome"] for x in r["results"]}, {"open_banking": "ACCEPTED", "utility_bills": "ACCEPTED"})
        app = self.svc._app(a["application_id"])
        acq = json.loads(app["acquisition_json"])
        val = acq["layer2"][0]["results"][0]["validation"]
        self.assertTrue(val["valid"] and val["stp_usable"])
        self.assertEqual({x["id"] for x in val["checks"]}, {"AV-01", "AV-02", "AV-03", "AV-04", "AV-05", "AV-06"})
        self.assertIn("ALTERNATIVE_DATA_RETRIEVED", self.events(application_id=a["application_id"]))
        rt = json.loads(app["routing_json"])
        self.assertTrue(rt["layer2_used"])

    def test_layer2_declined_gives_a_verified_decision_not_a_consent_penalty(self):
        a = self.thin_file_with_layer2({"open_banking": False, "utility_bills": False})
        self.assertEqual((a["status"], a["reason_code"], a["band"]), ("NOT_APPROVED", "R020-EVD", "RED"))
        self.assertTrue(a["appeal_available"])
        rt = json.loads(self.svc._app(a["application_id"])["routing_json"])
        self.assertIn("not because consent was refused", rt["reasons"][-1]["message"])
        self.assertEqual(a["data_acquisition"]["layer2"][0]["outcome"], "DECLINED")
        self.assertNotIn("ALTERNATIVE_DATA_RETRIEVED", self.events(application_id=a["application_id"]), "nothing retrieved without consent")

    def test_no_manual_review_shortcut_but_appeal_reaches_an_officer(self):
        self.assertFalse(hasattr(self.svc, "request_manual_review"))
        a = self.thin_file_with_layer2({"open_banking": False, "utility_bills": False})
        a = self.svc.appeal_decline(a["application_id"], "CUS-005", "I have a salary at another bank")
        self.assertEqual(a["status"], "IN_REVIEW")

    def test_layer2_outage_is_retried_automatically_then_decided(self):
        self.svc.set_demo_control("alt_data_api_unavailable", "1", "t")
        a = self.thin_file_with_layer2()
        self.assertEqual((a["status"], a["credit_state"], a["reason_code"], a["checking_reason"]),
                         ("CHECKING", "ASSESSMENT_PENDING", "R010-API", "SOURCE_RETRY"))
        self.assertEqual({x["outcome"] for x in a["data_acquisition"]["layer2"][0]["results"]}, {"UNAVAILABLE"})
        ev = [e for e in self.svc.audit_log(application_id=a["application_id"]) if e["event_type"] == "ALTERNATIVE_DATA_NOT_USED"]
        self.assertEqual((len(ev), len(ev[0]["details"]["attempts"])), (2, 2), "first call + one retry per source")
        self.assertIn("R-API-01", ev[0]["rule_ids"])
        self.svc.refresh_servicing("CUS-005")  # still down: nothing changes, no officer
        self.assertEqual(self.svc.application_view(a["application_id"])["status"], "CHECKING")
        self.svc.set_demo_control("alt_data_api_unavailable", "0", "t")
        self.svc.refresh_servicing("CUS-005")  # provider back: consented sources re-called, rules re-run automatically
        v = self.svc.application_view(a["application_id"])
        self.assertEqual((v["status"], v["reason_code"]), ("APPROVED", "R001-L2"))
        self.assertIn("LAYER2_RETRIED", self.events(application_id=a["application_id"]))

    def test_layer2_invalid_data_is_rejected_by_validation(self):
        self.svc.set_demo_control("alt_data_incomplete", "1", "t")
        a = self.thin_file_with_layer2({"open_banking": True, "utility_bills": False})
        res = a["data_acquisition"]["layer2"][0]["results"][0]
        self.assertEqual(res["outcome"], "INVALID")
        self.assertEqual((a["status"], a["reason_code"]), ("NOT_APPROVED", "R020-EVD"), "invalid data is never used to size a limit")
        acq = json.loads(self.svc._app(a["application_id"])["acquisition_json"])
        self.assertIn(("AV-02", False), [(x["id"], x["passed"]) for x in acq["layer2"][0]["results"][0]["validation"]["checks"]])

    def test_layer2_provider_has_no_record(self):
        a = self.svc.start_activation("CUS-007", CONSENTS, requested_limit=40000000)
        a = self.svc.decline_offer(a["application_id"], "CUS-007", ask_for_more=True)
        self.assertEqual([x["id"] for x in a["info_request"]["sources"]], ["open_banking", "gig_platform"])
        svc_rec = self.svc.db.run("UPDATE customers SET alternative_data_json = '{}' WHERE customer_id = 'CUS-007'")
        a = self.svc.alternative_data_consent(a["application_id"], "CUS-007", {"open_banking": True, "gig_platform": True})
        self.assertEqual({x["outcome"] for x in a["data_acquisition"]["layer2"][0]["results"]}, {"NOT_FOUND"})
        self.assertEqual((a["status"], a["offer"]["limit_vnd"]), ("OFFER", 12000000), "the original counter-offer stands")

    def test_cic_unavailable_retries_automatically_never_decides_on_missing_data(self):
        self.svc.set_demo_control("cic_unavailable", "1", "t")
        a = self.svc.start_activation("CUS-007", CONSENTS)
        self.assertEqual((a["status"], a["credit_state"], a["reason_code"]), ("CHECKING", "ASSESSMENT_PENDING", "R010-API"))
        rt = json.loads(self.svc._app(a["application_id"])["routing_json"])
        self.assertIn("R-API-01", rt["rule_ids"])
        self.assertEqual(self.svc.officer_queue(self.officer())["queues"]["pending_review"], [], "no officer is involved")
        self.svc.set_demo_control("cic_unavailable", "0", "t")
        self.svc.refresh_servicing("CUS-007")
        self.assertEqual(self.svc.application_view(a["application_id"])["status"], "APPROVED")

    def test_missing_segment_asks_nothing_and_decides_on_verified_data(self):
        a = self.svc.start_activation("CUS-006", CONSENTS)
        self.assertEqual((a["status"], a["reason_code"]), ("APPROVED", "R001"), "income verified from 6 months of HLB transactions")
        self.assertFalse(hasattr(self.svc, "submit_information"), "the employment-type question is removed")

    def test_no_document_upload_capability_remains(self):
        for name in ("upload_application_document", "officer_verify_document", "officer_set_inputs", "third_party_consent"):
            self.assertFalse(hasattr(self.svc, name), name)
        tables = {r["name"] for r in self.svc.db.all("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("documents", tables)
        self.assertNotIn("officers", tables)

    def test_withdraw_consent_stops_and_cancel(self):
        a = self.svc.start_activation("CUS-002", CONSENTS)
        self.assertEqual(self.svc.withdraw_consent(a["application_id"], "CUS-002")["status"], "STOPPED")
        b = self.svc.start_activation("CUS-002", CONSENTS)
        self.assertEqual(self.svc.cancel_application(b["application_id"], "CUS-002")["status"], "CANCELLED")

    def test_capacity_decline_has_single_appeal(self):
        svc = self.svc
        # verified obligations alone above the ceiling even on average income (DTI 0.55 > 0.5)
        svc.db.run("UPDATE customers SET applicant_json = json_set(applicant_json, '$.dti_ratio', 0.55, '$.inflow_cv', 0.05) "
                   "WHERE customer_id = 'CUS-007'")
        a = svc.start_activation("CUS-007", CONSENTS)
        self.assertEqual((a["status"], a["reason_code"]), ("ACTION_NEEDED", "R010-CAPX"), "Layer 2 is offered before any decline")
        offered = a["info_request"]["sources"]
        a = svc.alternative_data_consent(a["application_id"], "CUS-007", {x["id"]: False for x in offered})
        self.assertEqual((a["status"], a["reason_code"], a["appeal_available"]), ("NOT_APPROVED", "R020-CAP", True))
        a = svc.appeal_decline(a["application_id"], "CUS-007", "My salary rose last month")
        self.assertEqual(a["status"], "IN_REVIEW")
        el = svc.officer_case(a["application_id"], self.officer())["eligibility"]
        self.assertFalse(el["case_eligible"], "a RED capacity decline is not approvable just because the amount is small")
        self.assertIn(("EL-04", False), [(c["id"], c["passed"]) for c in el["checks"]])


class OfficerControlTests(Base):
    def case(self, app, officer=None):
        return self.svc.officer_case(app, officer or self.officer())

    def boost_capacity(self, cust, inflow=60000000, svc=None):
        """Simulate a customer whose verified capacity reaches the product maximum (the classified breakdown, if any, is
        dropped so the boosted inflow is used consistently for income and obligations)."""
        (svc or self.svc).db.run("UPDATE customers SET applicant_json = json_set(applicant_json, '$.monthly_inflow_vnd', ?, "
                                 "'$.inflow_cv', 0.0), sources_json = json_remove(sources_json, '$.hlb_transactions.inflow_breakdown') "
                                 "WHERE customer_id = ?", (inflow, cust))

    def test_officer_session_needs_no_code_or_profile(self):
        s1, s2 = self.svc.officer_sign_in(), self.svc.officer_sign_in()
        self.assertEqual((s1["role"], s1["officer"]["approval_authority_max_vnd"], s1["officer"]["approval_authority_bound"]),
                         ("OFFICER", 90100000, "INCLUSIVE"))
        self.assertNotEqual(s1["subject_id"], s2["subject_id"], "each session is distinguishable in the audit trail")
        self.assertEqual(self.svc.resolve_session(s1["token"], ("OFFICER",))["role"], "OFFICER")
        with self.assertRaises(ServiceError) as e:
            self.svc.resolve_session(s1["token"], ("CUSTOMER",))
        self.assertEqual(e.exception.code, "FORBIDDEN_ROLE")
        self.assertFalse(hasattr(self.svc, "public_officers"))

    def test_any_officer_approves_eligible_amber_below_90m_with_reason_and_note(self):
        app = self.to_review("CUS-004")
        o = self.officer()
        c = self.case(app, o)
        el = c["eligibility"]
        self.assertTrue(el["case_eligible"])
        # officer maximum = capacity on AVERAGE verified income (>= the stressed automatic capacity)
        self.assertEqual(el["max_approvable_vnd"], c["capacity"]["officer_max_limit_vnd"])
        self.assertGreaterEqual(el["max_approvable_vnd"], c["capacity"]["limit_vnd"])
        for code, note, err in ((None, "x", "REASON_CODE_REQUIRED"), ("AP-01", "", "NOTE_REQUIRED")):
            with self.assertRaises(ServiceError) as e:
                self.svc.officer_decide(app, o, "approve", code, note, c["application"]["version"], 5000000)
            self.assertEqual(e.exception.code, err)
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(app, o, "approve", "AP-01", "ok", c["application"]["version"], el["max_approvable_vnd"] + 500000)
        self.assertEqual(e.exception.code, "LIMIT_OUTSIDE_CAPACITY")
        if el["max_approvable_vnd"] > el["stp_capacity_vnd"]:
            with self.assertRaises(ServiceError) as e:  # above stressed capacity needs a basis code (AP-01/02/05)
                self.svc.officer_decide(app, o, "approve", "AP-03", "flag explained", c["application"]["version"],
                                        el["max_approvable_vnd"])
            self.assertEqual(e.exception.code, "REASON_CODE_NOT_ALLOWED")
        c = self.svc.officer_decide(app, self.officer(), "approve", "AP-04", "Counter-offer", c["application"]["version"], 15000000)
        self.assertEqual(c["application"]["credit_state"], "APPROVED", "a different officer session can approve: no profiles")
        self.assertEqual(self.svc.credit_summary("CUS-004")["limit_vnd"], 15000000)

    def test_flagged_gig_case_is_approvable_on_the_case_summary(self):
        """Volatile gig income with risk flags: an officer judgement on the consolidated summary; no data collection by the
        officer (data actions are rejected); approvable up to the average-income officer maximum with AP-05."""
        app = self.to_review("CUS-002")
        o = self.officer()
        c = self.case(app, o)
        cap, el, cs = c["capacity"], c["eligibility"], c["case_summary"]
        self.assertEqual(cap["income"]["basis"], "VERIFIED_TRANSACTIONS")
        self.assertTrue(el["case_eligible"])
        self.assertLess(cap["stp"]["limit_vnd"], el["max_approvable_vnd"], "average-income maximum is above the stressed capacity")
        self.assertEqual(cs["recommended_limit_vnd"], el["max_approvable_vnd"])
        self.assertTrue(any("RF-02" in x for x in cs["unresolved_issues"]))
        self.assertEqual(cs["income_streams"][0]["lineage"], "hlb_transactions")
        for action in ("request_data", "refresh_data"):
            with self.assertRaises(ServiceError) as e:
                self.svc.officer_decide(app, o, action, None, "x", c["application"]["version"])
            self.assertEqual(e.exception.code, "DATA_ACTIONS_AUTOMATIC")
        mx = el["max_approvable_vnd"]
        c = self.svc.officer_decide(app, o, "approve", "AP-05", "Stable platform history despite flags", c["application"]["version"], mx)
        self.assertEqual((c["application"]["credit_state"], self.svc.credit_summary("CUS-002")["limit_vnd"]), ("APPROVED", mx))

    def test_officer_authority_covers_the_whole_product_scope(self):
        """v6: full officer authority within VND 1.6M-90.1M inclusive; above 90.1M is out of scope."""
        pol = copy.deepcopy(POLICY)
        pol["aggregate_exposure"]["max_digital_unsecured_outstanding_vnd"] = None  # isolate the authority control
        svc, _ = make(pol)
        self.addCleanup(lambda: svc.db.close())
        self.boost_capacity("CUS-004", svc=svc)  # rapid-drain flag -> officer case; capacity at the product maximum
        a = svc.start_activation("CUS-004", CONSENTS)
        app, o = a["application_id"], svc.officer_sign_in()["subject_id"]
        c = svc.officer_case(app, o)
        self.assertEqual(c["capacity"]["officer_max_limit_vnd"], 90100000)
        el = c["eligibility"]
        self.assertFalse(el["capacity_above_authority"])
        self.assertEqual(el["max_approvable_vnd"], 90100000, "the product maximum itself is approvable")
        v = c["application"]["version"]
        for limit, code in ((90200000, "ABOVE_APPROVAL_AUTHORITY"), (90100001, "ABOVE_APPROVAL_AUTHORITY"),
                            (90050000, "LIMIT_OUTSIDE_CAPACITY")):
            with self.assertRaises(ServiceError) as e:
                svc.officer_decide(app, o, "approve", "AP-03", "boundary", v, limit)
            self.assertEqual(e.exception.code, code, limit)
        svc.officer_decide(app, o, "approve", "AP-03", "at the product maximum", v, 90100000)
        self.assertEqual(svc.credit_summary("CUS-004")["limit_vnd"], 90100000)

    def test_green_can_reach_the_product_maximum(self):
        """v6: one operating model - automatic decisions cover the product scope; capacity, not an amount ceiling, bounds them."""
        self.boost_capacity("CUS-007", inflow=70000000)  # gig: multiple cap 2 x 0.7 x 70M = 98M > the product maximum
        a = self.svc.start_activation("CUS-007", CONSENTS)
        self.assertEqual((a["status"], a["band"], a["approved_limit_vnd"]), ("APPROVED", "GREEN", 90100000))
        self.assertEqual(POLICY["affordability"]["stp_max_limit_vnd"], POLICY["officer_controls"]["approval_authority_max_vnd"])

    def test_authority_is_configurable(self):
        pol = copy.deepcopy(POLICY)
        pol["officer_controls"]["approval_authority_max_vnd"] = 20000000
        pol["officer_controls"]["approval_authority_bound"] = "INCLUSIVE"
        bad = copy.deepcopy(pol)
        with self.assertRaises(ValueError, msg="automatic ceiling above the officer authority is a configuration error"):
            make(bad)
        pol["affordability"]["stp_max_limit_vnd"] = 15000000
        svc, _ = make(pol)
        self.addCleanup(lambda: svc.db.close())
        a = svc.start_activation("CUS-004", CONSENTS)
        o = svc.officer_sign_in()["subject_id"]
        c = svc.officer_case(a["application_id"], o)
        self.assertGreater(c["capacity"]["limit_vnd"], 20000000)
        self.assertEqual(c["eligibility"]["max_approvable_vnd"], 20000000)
        with self.assertRaises(ServiceError) as e:
            svc.officer_decide(a["application_id"], o, "approve", "AP-01", "x", c["application"]["version"], 20000001)
        self.assertEqual(e.exception.code, "ABOVE_APPROVAL_AUTHORITY")
        svc.officer_decide(a["application_id"], o, "approve", "AP-01", "inclusive bound", c["application"]["version"], 20000000)
        self.assertEqual(svc.credit_summary("CUS-004")["limit_vnd"], 20000000)

    def test_restricted_cases_cannot_be_approved(self):
        o = self.officer()
        # (1) capacity not computable: invalid source data in review -> blocked
        self.svc.db.run("UPDATE customers SET applicant_json = json_set(applicant_json, '$.unified_credit_score', 900) "
                        "WHERE customer_id = 'CUS-004'")
        app = self.to_review("CUS-004")
        c = self.case(app, o)
        self.assertEqual(c["application"]["reason_code"], "R010-VER")
        # (2) arrears arise while the case is in review -> hard stop blocks approval
        app2 = self.to_review("CUS-002")
        orig = self.svc._delinquency
        self.svc._delinquency = lambda cid: {"arrears": True, "default": False, "days_overdue": 5} if cid == "CUS-002" else orig(cid)
        try:
            c = self.case(app2, o)
            self.assertIn(("EL-03", False), [(x["id"], x["passed"]) for x in c["eligibility"]["checks"]])
            with self.assertRaises(ServiceError) as e:
                self.svc.officer_decide(app2, o, "approve", "AP-01", "small", c["application"]["version"], 2000000)
            self.assertEqual(e.exception.code, "NOT_ELIGIBLE_FOR_APPROVAL")
        finally:
            self.svc._delinquency = orig
        # (3) consent withdrawn -> POLICY_STOP, nothing to approve
        self.svc.withdraw_consent(app2, "CUS-002")
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(app2, o, "approve", "AP-01", "x", self.case(app2, o)["application"]["version"], 2000000)
        self.assertEqual(e.exception.code, "INVALID_CREDIT_STATE")
        # (4) GREEN cases are automated: nothing for an officer to approve
        g = self.svc.start_activation("CUS-007", CONSENTS)
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(g["application_id"], o, "approve", "AP-01", "x",
                                    self.case(g["application_id"], o)["application"]["version"], 2000000)
        self.assertEqual(e.exception.code, "INVALID_CREDIT_STATE")

    def test_data_conflict_is_resolved_by_automated_layer2_without_an_officer(self):
        """CUS-003 (has_observable_tx = 0 with populated values): v5 needed an officer to request Open Banking; v6 excludes the
        conflicting values, requests Open Banking + utility history itself and decides automatically."""
        a = self.svc.start_activation("CUS-003", CONSENTS)
        self.assertEqual((a["status"], [x["id"] for x in a["info_request"]["sources"]]), ("ACTION_NEEDED", ["open_banking", "utility_bills"]))
        v = self.svc.alternative_data_consent(a["application_id"], "CUS-003", {"open_banking": True, "utility_bills": True})
        # Open Banking salary avg 11,113,333, P25 month 11,062,500; CIC no record & no observable tx -> 0 obligations;
        # 2 x 11,062,500 = 22,125,000 -> 22.0M on the 500k grid
        self.assertEqual((v["status"], v["reason_code"], v["approved_limit_vnd"]), ("APPROVED", "R001-L2", 22000000))
        self.assertEqual(self.svc.officer_queue(self.officer())["queues"]["pending_review"], [])

    def test_officer_never_refreshes_data(self):
        app = self.to_review("CUS-004")
        o = self.officer()
        c = self.case(app, o)
        self.assertNotIn("alternative_sources", c)
        self.assertIn("automated_data_operations", c["case_summary"])
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(app, o, "refresh_data", None, "x", c["application"]["version"])
        self.assertEqual(e.exception.code, "DATA_ACTIONS_AUTOMATIC")

    def test_reject_stale_version_and_no_escalation(self):
        app = self.to_review("CUS-004")
        o = self.officer()
        v = self.case(app, o)["application"]["version"]
        self.svc.db.run("UPDATE applications SET version = version + 1 WHERE application_id = ?", (app,))
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(app, o, "reject", "RJ-03", "x", v)
        self.assertEqual(e.exception.code, "STALE_DECISION")
        c = self.case(app, o)
        for action in ("escalate", "return", "request_info"):
            with self.assertRaises(ServiceError) as e:
                self.svc.officer_decide(app, o, action, None, "x", c["application"]["version"])
            self.assertEqual(e.exception.code, "INVALID_ACTION", "no escalation / maker-checker / document requests")
        c = self.svc.officer_decide(app, o, "reject", "RJ-03", "Unexplained drain", c["application"]["version"])
        self.assertEqual(c["application"]["credit_state"], "REJECTED")
        self.assertEqual(self.svc.application_view(app, "CUS-004")["customer_reason"], "OFFICER_DECLINED")

    def test_green_qa_sample_is_deterministic_and_reviewable(self):
        import hashlib
        pol = copy.deepcopy(POLICY)
        pol["identity_fraud"]["max_applications_30d"] = 1000  # this synthetic loop would (correctly) trip Layer 0 velocity
        self.svc, _ = make(pol)
        picked = None
        for i in range(60):  # create GREEN activations until one falls in the 5% sample
            self.svc.db.run("DELETE FROM credit_lines WHERE customer_id = 'CUS-007'")
            a = self.svc.start_activation("CUS-007", CONSENTS)
            self.assertEqual(a["status"], "APPROVED")
            sel = int(hashlib.sha256(a["application_id"].encode()).hexdigest(), 16) % 100 < 5
            row = self.svc.db.one("SELECT qa_status FROM applications WHERE application_id = ?", (a["application_id"],))
            self.assertEqual(row["qa_status"] == "PENDING", sel)
            if sel:
                picked = a["application_id"]
                break
        self.assertIsNotNone(picked)
        self.assertIn(picked, [c["application_id"] for c in self.svc.officer_queue(self.officer())["queues"]["qa_sample"]])
        with self.assertRaises(ServiceError):
            self.svc.officer_qa_review(picked, self.officer(), "AGREE", "")
        c = self.svc.officer_qa_review(picked, self.officer(), "AGREE", "Inputs and capacity consistent")
        self.assertEqual(c["application"]["qa_status"], "AGREE")
        self.assertEqual(self.svc.officer_queue(self.officer())["queues"]["qa_sample"], [])

    def test_queue_shows_sla_and_capacity(self):
        self.to_review("CUS-002")
        q = self.svc.officer_queue(self.officer())
        item = q["queues"]["pending_review"][0]
        self.assertIsNotNone(item["sla_due_at"])
        self.assertEqual(set(q["queues"]), {"pending_review", "awaiting_customer", "qa_sample", "decided"})
        self.assertEqual((q["stats"]["daily_review_capacity"], q["authority"]["max_vnd"]), (48, 90100000))

    def test_limit_increase_is_automatic_on_refreshed_data(self):
        """v6: increases are decided automatically: approve / counter-offer / automated Layer 2 / RED R020-INC (line unchanged)."""
        current = self.svc.credit_summary("CUS-001")["limit_vnd"]
        a = self.svc.create_limit_increase("CUS-001", 40000000, "same job")
        # capacity = current 29.5M: Layer 1 is short of the request -> automated Open Banking request (salaried segment)
        self.assertEqual((a["status"], a["reason_code"], [x["id"] for x in a["info_request"]["sources"]]),
                         ("ACTION_NEEDED", "R010-CAPX", ["open_banking"]))
        a = self.svc.alternative_data_consent(a["application_id"], "CUS-001", {"open_banking": False})
        self.assertEqual((a["status"], a["reason_code"]), ("NOT_APPROVED", "R020-INC"))
        self.assertEqual(self.svc.credit_summary("CUS-001")["limit_vnd"], current, "a declined increase leaves the line unchanged")
        # salary rises -> refreshed Layer 1 supports more: partial -> counter-offer, accepted by the customer
        self.svc.db.run("UPDATE customers SET applicant_json = json_set(applicant_json, '$.monthly_inflow_vnd', 30000000) "
                        "WHERE customer_id = 'CUS-001'")
        a = self.svc.create_limit_increase("CUS-001", 80000000, "new job")
        self.assertEqual((a["status"], a["reason_code"]), ("OFFER", "R001-CO"))
        offered = a["offer"]["limit_vnd"]
        self.assertTrue(current < offered < 80000000, offered)
        self.assertEqual(self.svc.credit_summary("CUS-001")["limit_vnd"], current, "unchanged until the customer accepts")
        self.svc.accept_offer(a["application_id"], "CUS-001")
        self.assertEqual(self.svc.credit_summary("CUS-001")["limit_vnd"], offered)

class DelinquencyTests(Base):
    def setUp(self):
        super().setUp()
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx, 6)
        self.loan = self.svc.list_loans("CUS-001")["loans"][0]["loan_ref"]

    def test_arrears_freeze_block_and_unfreeze(self):
        self.svc.set_business_date("t", advance_days=40)
        cs = self.svc.credit_summary("CUS-001")
        self.assertEqual((cs["line_status"], cs["internal_delinquency_flag"]), ("FROZEN", 1))
        tx, v = self.pay_later("CUS-001", "QR-261016-2230")
        self.assertEqual(v["pay_later_decision"]["reason_code"], "R020-ARR")
        with self.assertRaises(ServiceError):
            self.svc.create_limit_increase("CUS-001", 30000000, "x")
        self.svc.repay(self.loan, "CUS-001", [1], self.key())
        self.assertEqual(self.svc.credit_summary("CUS-001")["line_status"], "ACTIVE")
        self.assertEqual(self.svc.credit_summary("CUS-001")["internal_delinquency_flag"], 1, "flag stays until cured")
        loan = self.svc.get_loan(self.loan, "CUS-001")
        # No flat late fee (Circular 39 Art. 14). Instalment 1 (due 16 Nov, paid 25 Nov = 9 days late): principal 2,000,000
        # at 150% x 12% and interest 120,000 at 10%/year, simple daily (Art. 13.4 caps).
        od = round(2000000 * 0.12 * 1.5 * 9 / 365 + 120000 * 0.10 * 9 / 365)
        self.assertEqual(loan["repayments"][0]["amount_vnd"], 2120000 + od)

    def test_cure_after_n_on_time_instalments(self):
        """audit F-15 / V-26: prepaying future instalments the same day does not cure; the flag clears only once three
        due dates have passed with the instalment paid on or before each of them."""
        self.svc.set_business_date("t", advance_days=40)
        self.svc.repay(self.loan, "CUS-001", [1], self.key())
        self.svc.repay(self.loan, "CUS-001", [2, 3, 4], self.key())  # prepaid on 25 Nov; due 16 Dec, 16 Jan, 16 Feb
        self.assertEqual(self.svc.credit_summary("CUS-001")["internal_delinquency_flag"], 1, "liquidity on one day is not behaviour")
        self.svc.set_business_date("t", new_date="2027-01-17")  # two of the three due dates passed
        self.assertEqual(self.svc.credit_summary("CUS-001")["internal_delinquency_flag"], 1)
        self.svc.set_business_date("t", new_date="2027-02-17")  # third due date passed, all paid on time
        self.assertEqual(self.svc.credit_summary("CUS-001")["internal_delinquency_flag"], 0)
        self.assertIn("DELINQUENCY_FLAG_CURED", self.events())

    def test_default_suspends_line_and_collections(self):
        self.svc.set_business_date("t", advance_days=125)
        cs = self.svc.credit_summary("CUS-001")
        self.assertEqual(cs["line_status"], "SUSPENDED")
        loan = self.svc.get_loan(self.loan, "CUS-001")
        self.assertEqual(loan["status"], "DEFAULT")
        self.assertIsNotNone(loan["collections_status"])

    def test_early_settlement_quote_is_principal_based(self):
        loan = self.svc.get_loan(self.loan, "CUS-001")
        self.assertEqual(loan["early_settlement_quote_vnd"], 12000000)


class ConversionTests(Base):
    def test_convert_pay_now_purchase_after_approval_within_window(self):
        tx, v = self.pay_later("CUS-004", "QR-261016-2004", consents=CONSENTS)
        self.pay_now("CUS-004", tx)
        bal = self.svc._customer("CUS-004")["balance_vnd"]
        self.assertFalse(self.svc.conversion_offer(tx, "CUS-004")["eligible"])
        app = v["activation"]["application_id"]
        o = self.officer()
        self.svc.officer_decide(app, o, "approve", "AP-01", "ok", self.svc.officer_case(app, o)["application"]["version"], 15000000)
        offer = self.svc.conversion_offer(tx, "CUS-004")
        self.assertTrue(offer["eligible"])
        ch = self.auth("CUS-004", tx, "INSTALMENT_CONVERSION")
        v = self.svc.convert_to_instalments(tx, "CUS-004", offer["affordable_tenures"][0], True, ch["challenge_id"])
        self.assertIsNotNone(v["transaction"]["converted_loan_ref"])
        self.assertEqual(self.svc._customer("CUS-004")["balance_vnd"], bal + 4500000)
        self.assertEqual(self.svc.credit_summary("CUS-004")["outstanding_principal_vnd"], 4500000)

    def test_window_closes(self):
        tx = self.scan("CUS-001", "QR-261016-2004")
        self.pay_now("CUS-001", tx)
        self.assertTrue(self.svc.conversion_offer(tx, "CUS-001")["eligible"])
        self.svc.set_business_date("t", advance_days=8)
        self.assertEqual(self.svc.conversion_offer(tx, "CUS-001")["reason"], "WINDOW_CLOSED")


class AuditTests(Base):
    def test_audit_is_append_only(self):
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            self.svc.db.run("DELETE FROM audit_events")
        with self.assertRaises(sqlite3.DatabaseError):
            self.svc.db.run("UPDATE audit_events SET event_type = 'x'")

    def test_key_events_recorded(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx)
        ev = self.events(transaction_id=tx)
        for e in ("QR_TRANSACTION_CREATED", "CHECKOUT_DECISION", "EXPOSURE_HOLD_PLACED", "INSTALMENT_AGREEMENT_ACCEPTED",
                  "STEP_UP_CHALLENGE_CREATED", "STEP_UP_VERIFIED", "PAYMENT_PROCESSING_STARTED", "SIMULATED_LOAN_BOOKED",
                  "EXPOSURE_HOLD_BOOKED", "MERCHANT_SETTLEMENT_SCHEDULED", "PAYMENT_COMPLETED"):
            self.assertIn(e, ev)


if __name__ == "__main__":
    unittest.main()
