"""v6 decision-rule tests: automated Layer 2 orchestration (selection, stop rule, stability, de-duplication), product
scope (out-of-scope referral, N/A outcomes), RED vs AMBER vs Pay Now separation, and split-tender reliability (atomicity,
idempotency, partial failure, reconciliation, hold expiry, concurrency)."""
import copy
import json
import random
import threading
import unittest
from datetime import timedelta

from server.engine import routing
from tests.test_engine import POLICY, base_case, cap_for, run
from tests.test_service import CONSENTS, Base, ServiceError, make


class Layer2Orchestration(Base):
    def _ob_salary(self, cust, monthly):
        rec = {"monthly": monthly, "subject_match": True, "match_on": ["national_id", "full_name"],
               "credits_by_category": {"salary": monthly}, "provider": "Other bank via Open API (simulated)"}
        self.svc.db.run("UPDATE customers SET alternative_data_json = json_set(alternative_data_json, '$.open_banking', json(?)) "
                        "WHERE customer_id = ?", (json.dumps(rec), cust))

    def test_calling_stops_once_the_gap_is_closed(self):
        """Data minimisation: Open Banking (priority 1) closes the gap for a 25M request, so the platform is never called."""
        self._ob_salary("CUS-007", [20000000] * 6)
        a = self.svc.start_activation("CUS-007", CONSENTS, requested_limit=25000000)
        a = self.svc.decline_offer(a["application_id"], "CUS-007", ask_for_more=True)
        v = self.svc.alternative_data_consent(a["application_id"], "CUS-007", {"open_banking": True, "gig_platform": True})
        outcomes = {x["source"]: x["outcome"] for x in v["data_acquisition"]["layer2"][0]["results"]}
        self.assertEqual(outcomes, {"open_banking": "ACCEPTED", "gig_platform": "NOT_CALLED_GAP_CLOSED"})
        self.assertEqual((v["status"], v["reason_code"], v["approved_limit_vnd"]), ("APPROVED", "R001-L2", 25000000))
        self.assertNotIn("gig_platform", [e["details"].get("source") for e in self.svc.audit_log(application_id=a["application_id"])
                                          if e["event_type"].startswith("ALTERNATIVE_DATA_")], "never called")

    def test_declining_income_goes_to_an_officer_not_automatic(self):
        """AV-06: last 3 months < 70% of the previous 3 -> valid but not usable automatically -> R010-ALT officer review."""
        self._ob_salary("CUS-005", [12000000, 12000000, 12000000, 7000000, 7000000, 7000000])
        a = self.svc.start_activation("CUS-005", CONSENTS)
        v = self.svc.alternative_data_consent(a["application_id"], "CUS-005", {"open_banking": True, "utility_bills": True})
        self.assertEqual((v["status"], v["reason_code"]), ("IN_REVIEW", "R010-ALT"))
        acq = json.loads(self.svc._app(a["application_id"])["acquisition_json"])
        checks = {c["id"]: c["passed"] for c in acq["layer2"][0]["results"][0]["validation"]["checks"]}
        self.assertEqual((checks["AV-06"], checks["AV-01"]), (False, True))
        cs = self.svc.officer_case(a["application_id"], self.officer())["case_summary"]
        self.assertEqual(cs["income_streams"][0]["stp_usable"], False)
        self.assertTrue(any("stability/trend" in x for x in cs["unresolved_issues"]))

    def test_behaviour_sources_never_count_as_income(self):
        a = self.svc.start_activation("CUS-005", CONSENTS)
        v = self.svc.alternative_data_consent(a["application_id"], "CUS-005", {"open_banking": False, "utility_bills": True})
        sub = json.loads(self.svc._app(a["application_id"])["info_submitted_json"])
        self.assertNotIn("alt_monthly_income_vnd", sub["fields"])
        self.assertEqual(sub["fields"]["alt_on_time_bill_ratio"], 1.0)
        self.assertEqual(v["reason_code"], "R020-EVD", "a bill-payment record proves behaviour, not repayment capacity")

    def test_platform_paid_into_hlb_is_not_added_twice(self):
        """CUS-002's platform pays into the HLB account: its payouts are already the Layer 1 inflow (AV-07)."""
        st = [x for x in self.svc._build_streams(
            [{"source": "gig_platform", "outcome": "ACCEPTED", "validation": {"stp_usable": True, "summary": {
                "series_vnd": [14000000] * 6, "payout_destination": "HLB"}}}], layer1_income=True)[0]]
        self.assertEqual((st[0]["duplicate_of"], st[0]["average_recognised_vnd"]), ("LAYER1", 0))

    def test_customer_view_never_exposes_layer1_detail(self):
        for cust in ("CUS-003", "CUS-005", "CUS-007"):
            v = self.svc.start_activation(cust, CONSENTS)
            blob = json.dumps(v)
            for leak in ("layer1", "monthly_inflow_vnd", "has_observable_tx", "R-VER-01", "reason_rule_ids", "evidence_gaps"):
                self.assertNotIn(leak, blob, (cust, leak))


class ProductScope(Base):
    def test_request_above_product_maximum_is_referred_not_processed(self):
        a = self.svc.start_activation("CUS-007", CONSENTS, requested_limit=150000000)
        self.assertEqual((a["status"], a["offer"]["limit_vnd"]), ("OFFER", 12000000))
        self.assertEqual(a["out_of_scope"], {"requested_vnd": 150000000, "max_vnd": 90100000})
        ev = [e for e in self.svc.audit_log(application_id=a["application_id"]) if e["event_type"] == "OUT_OF_SCOPE_REFERRAL"]
        self.assertEqual((len(ev), ev[0]["details"]["excess_vnd"]), (1, 150000000 - 90100000))
        self.assertIn("out_of_scope_referral", [n["title_key"] for n in self.svc.notifications("CUS-007")["items"]])
        with self.assertRaises(ServiceError):
            self.svc.start_activation("CUS-006", CONSENTS, requested_limit=1500000)  # below the minimum: nothing to assess

    def test_green_checkout_still_offers_pay_now(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        d = v["pay_later_decision"]
        self.assertEqual((d["band"], d["credit_outcome"], d["payment_options"]), ("GREEN", "APPROVED", ["PAY_LATER", "PAY_NOW"]))

    def test_red_checkout_keeps_pay_now_and_records_the_decline(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx, 6)
        self.svc.set_business_date("t", advance_days=40)  # arrears -> line frozen
        tx2, v2 = self.pay_later("CUS-001", "QR-261016-2230")
        d = v2["pay_later_decision"]
        self.assertEqual((d["band"], d["reason_code"], d["credit_outcome"]), ("RED", "R020-ARR", "DECLINED"))
        self.assertIn("PAY_NOW", d["payment_options"])
        self.assertEqual(self.pay_now("CUS-001", tx2)["transaction"]["state"], "PAYMENT_COMPLETED")
        ev = [e for e in self.svc.audit_log(transaction_id=tx2) if e["event_type"] == "CHECKOUT_DECISION"]
        self.assertEqual(ev[0]["details"]["band"], "RED", "the decline is recorded as such, not hidden behind Pay Now")


class SplitTender(Base):
    def _split(self, amount_ref="QR-261016-1610"):
        tx, v = self.pay_later("CUS-001", amount_ref)
        self.assertEqual(v["pay_later_decision"]["reason_code"], "R010-SPLIT")
        v = self.svc.accept_split(tx, "CUS-001")
        self.svc.accept_agreement(tx, "CUS-001", True)
        return tx, v

    def _ledger(self):
        cs = self.svc.credit_summary("CUS-001")
        bal = self.svc.get_customer("CUS-001")["balance_vnd"]
        loans = self.svc.db.one("SELECT COUNT(*) n FROM loans WHERE customer_id = 'CUS-001'")["n"]
        return cs["outstanding_principal_vnd"], cs["held_vnd"], bal, loans

    def test_partial_failure_posts_nothing_then_retry_completes_once(self):
        bal0 = self.svc.get_customer("CUS-001")["balance_vnd"]
        tx, v = self._split()
        pl, pn = v["transaction"]["pay_later_amount_vnd"], v["transaction"]["pay_now_amount_vnd"]
        self.assertEqual(self._ledger(), (0, pl, bal0, 0), "only the Pay Later part is held")
        self.svc.set_demo_control("force_next_payment_failure", "1", "t")
        ch = self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        r = self.svc.pay(tx, "CUS-001", "split-1", ch["challenge_id"])
        self.assertEqual(r["transaction"]["state"], "PAYMENT_FAILED")
        self.assertEqual(self._ledger(), (0, pl, bal0, 0), "no debit, no loan; hold kept for a retry")
        replay = self.svc.pay(tx, "CUS-001", "split-1", ch["challenge_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self._ledger(), (0, pl, bal0, 0), "same key -> same result, nothing re-executed")
        ch2 = self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        r = self.svc.pay(tx, "CUS-001", "split-2", ch2["challenge_id"])
        self.assertEqual(r["transaction"]["state"], "PAYMENT_COMPLETED")
        self.assertEqual(self._ledger(), (pl, 0, bal0 - pn, 1), "loan = Pay Later part, account debited the Pay Now part once")
        with self.assertRaises(ServiceError) as e:
            self.svc.pay(tx, "CUS-001", "split-3", ch2["challenge_id"])
        self.assertEqual(e.exception.code, "DUPLICATE_PAYMENT")
        with self.assertRaises(ServiceError) as e:
            self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        self.assertEqual(e.exception.code, "NOTHING_TO_AUTHORISE", "no second authorisation of a paid purchase")

    def test_insufficient_balance_for_the_pay_now_part_fails_cleanly(self):
        tx, v = self._split()
        self.svc.db.run("UPDATE customers SET balance_vnd = 100 WHERE customer_id = 'CUS-001'")
        with self.assertRaises(ServiceError) as e:  # both portions are validated before authorisation
            self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        self.assertEqual(e.exception.code, "INSUFFICIENT_FUNDS")
        self.svc.db.run("UPDATE customers SET balance_vnd = 50000000 WHERE customer_id = 'CUS-001'")
        ch = self.auth("CUS-001", tx, "PAY_LATER_AGREEMENT")
        self.svc.db.run("UPDATE customers SET balance_vnd = 100 WHERE customer_id = 'CUS-001'")  # drained after authorisation
        r = self.svc.pay(tx, "CUS-001", "nb-1", ch["challenge_id"])
        self.assertEqual((r["transaction"]["state"], r["payments"][-1]["failure_code"]), ("PAYMENT_FAILED", "INSUFFICIENT_FUNDS"))
        self.assertEqual(self._ledger()[3], 0, "no loan without the Pay Now part")

    def test_stuck_processing_is_reconciled_without_posting(self):
        tx, v = self._split()
        pl = v["transaction"]["pay_later_amount_vnd"]
        # simulate a crash between the two payment steps: payment row PROCESSING, transaction DISBURSEMENT_PROCESSING
        with self.svc._unit():
            t = self.svc._tx(tx)
            self.svc.db.run("INSERT INTO payments(payment_id, transaction_id, order_key, idempotency_key, attempt_no, method, "
                            "amount_vnd, status, created_at) VALUES ('PAY-X', ?, ?, 'crash-1', 1, 'SPLIT', ?, 'PROCESSING', ?)",
                            (tx, t["qr_ref"], t["amount_vnd"], (self.clock() - timedelta(seconds=300)).isoformat()))
            self.svc._move_tx(t, "DISBURSEMENT_PROCESSING", "CUSTOMER", "CUS-001")
        self.assertEqual(self.svc.reconcile_stuck_payments(), 1)
        t = self.svc._tx(tx)
        self.assertEqual(t["state"], "PAYMENT_FAILED")
        self.assertEqual(self._ledger()[:2] + (self._ledger()[3],), (0, pl, 0), "nothing posted; hold kept until expiry")
        self.assertIn("PAYMENT_RECONCILED_FAILED", self.events(transaction_id=tx))
        self.clock.now += timedelta(seconds=POLICY["checkout"]["timeout_seconds"] + 5)
        self.svc.sweep_expired()
        self.assertEqual((self.svc._tx(tx)["state"], self._ledger()[1]), ("EXPIRED", 0), "the expiry releases the hold")

    def test_switching_to_pay_now_or_cancelling_releases_the_split_hold(self):
        tx, v = self._split()
        self.svc.choose_pay_now(tx, "CUS-001")
        t = self.svc._tx(tx)
        self.assertEqual((t["method"], t["pay_later_amount_vnd"], self._ledger()[1]), ("ACCOUNT", None, 0))
        self.svc.cancel(tx, "CUS-001")
        tx2, _ = self._split()
        self.assertGreater(self._ledger()[1], 0)
        self.svc.cancel(tx2, "CUS-001")
        self.assertEqual(self._ledger()[1], 0)

    def test_concurrent_split_acceptances_never_exceed_the_limit(self):
        self.svc.db.run("UPDATE customers SET balance_vnd = 200000000 WHERE customer_id = 'CUS-001'")
        txs = [self.scan("CUS-001", ref) for ref in ("QR-261016-1610", "QR-261016-1915")]
        out = []

        def go(t):
            try:
                out.append(self.svc.accept_split(t, "CUS-001")["transaction"]["pay_later_amount_vnd"])
            except ServiceError as e:
                out.append(e.code)
        th = [threading.Thread(target=go, args=(t,)) for t in txs]
        [x.start() for x in th]
        [x.join() for x in th]
        cs = self.svc.credit_summary("CUS-001")
        self.assertLessEqual(cs["held_vnd"] + cs["outstanding_principal_vnd"], cs["limit_vnd"])
        self.assertIn("SPLIT_NOT_AVAILABLE", out, "the second acceptance is re-decided against the first hold")


class RecognisedIncomeMultiple(unittest.TestCase):
    """v6 safeguard: the income-multiple exposure cap uses RECOGNISED income. Unclassified Layer 1 inflow of a merchant is
    turnover (0.3) and of a platform worker pre-cost payouts (0.7); the 50% debt-service test keeps the gross inflow (the
    base the bureau DTI ratio is measured on)."""

    def _cap(self, profile, inflow=60000000, dti=0.05):
        a, p, s, c = base_case()
        p.update(customer_profile=profile, monthly_inflow_vnd=inflow, inflow_cv=0.0, dti_ratio=dti)
        return cap_for(p, s)

    def test_factor_by_segment(self):
        for profile, factor in (("Online merchants (MSME)", 0.3), ("Gig / platform workers", 0.7),
                                ("Salaried, with credit history", 1.0), ("UNKNOWN", 0.3)):
            cap = self._cap(profile)
            self.assertEqual(cap["income"]["layer1"]["multiplier_factor"], factor, profile)
            self.assertEqual(cap["stp"]["limit_by_income_multiplier_vnd"], int(2 * int(60000000 * factor)), profile)

    def test_unclassified_turnover_cannot_reach_the_product_maximum_automatically(self):
        msme, salaried = self._cap("Online merchants (MSME)"), self._cap("Salaried, with credit history")
        self.assertEqual(salaried["stp"]["limit_vnd"], POLICY["affordability"]["product_max_limit_vnd"])
        self.assertEqual((msme["stp"]["binding_constraint"], msme["stp"]["limit_vnd"]), ("INCOME_MULTIPLIER", 36000000))
        self.assertEqual(msme["stp"]["income_vnd"], salaried["stp"]["income_vnd"], "the debt-service test is unchanged")

    def test_classified_inflow_uses_its_own_type_factors(self):
        a, p, s, c = base_case()
        p.update(customer_profile="Online merchants (MSME)", monthly_inflow_vnd=60000000, inflow_cv=0.0, dti_ratio=0.05)
        s["hlb_transactions"]["inflow_breakdown"] = {"salary": 20000000, "merchant_settlements": 40000000}
        cap = cap_for(p, s)
        self.assertEqual(cap["income"]["layer1"]["multiplier_factor"], 1.0, "classification already applied the factors")
        self.assertTrue(cap["income"]["layer1"]["inflow_classified"])


class RoutingProperties(unittest.TestCase):
    def test_outcome_classes_hold_for_random_inputs(self):
        """For random Layer 1 inputs: RED only with R020 codes and an appeal; officer referrals only for judgement codes;
        Layer 2 requests only for a data gap; GREEN never above the product maximum."""
        rnd = random.Random(6)
        judgement = {"R010-FRD", "R010-VER", "R010-OBL", "R010-ALT", "R010-THIN", "R010-FLAG", "R010-CAPX", "R010-RECHECK"}
        for _ in range(400):
            a, p, s, c = base_case()
            p.update(monthly_inflow_vnd=rnd.randint(1, 80) * 1000000, inflow_cv=round(rnd.uniform(0, 0.8), 3),
                     dti_ratio=round(rnd.uniform(0, 0.9), 3), has_formal_cic_history=rnd.choice([0, 1]),
                     has_observable_tx=rnd.choice([0, 1]), unified_credit_score=rnd.randint(380, 800),
                     on_time_bill_ratio=round(rnd.uniform(0.4, 1.0), 3), rapid_drain_flag=rnd.choice([0, 0, 0, 1]))
            p["customer_profile"] = "Salaried, with credit history" if p["has_formal_cic_history"] else "Salaried, no credit history"
            res = run(a, p, s, c)
            vf = [f for f in ("monthly_inflow_vnd", "inflow_cv", "dti_ratio", "unified_credit_score", "on_time_bill_ratio")
                  if f not in res["fields_not_used"]]
            cap = cap_for(p, s, verified=vf)
            for l2 in ({}, {"attempted": True, "outcome": "DECLINED"}):
                d = routing.decide(res, {"capacity": cap, "layer2": l2, "behaviour_signal": True}, POLICY)
                if d["band"] == "RED":
                    self.assertTrue(d["reason_code"].startswith("R020") and d["appeal_allowed"], d["reason_code"])
                if d["route"] == routing.ROUTE_REVIEW:
                    self.assertIn(d["reason_code"], judgement)
                if d["route"] == routing.ROUTE_ALT_DATA:
                    self.assertFalse(l2.get("attempted"), "Layer 2 is requested once")
                    self.assertIn(d["reason_code"], ("R010-EVD", "R010-INCOME", "R010-CAPX", "R010-THIN"))
                if d["band"] == "GREEN":
                    self.assertLessEqual(d["approved_limit_vnd"], POLICY["affordability"]["product_max_limit_vnd"])
                    self.assertGreaterEqual(d["approved_limit_vnd"], POLICY["affordability"]["min_limit_vnd"])


if __name__ == "__main__":
    unittest.main()
