"""Audit validation matrix V-01..V-28 (HLB Smart Credit - Independent Decision Rules Audit, section 6), as acceptance
tests of the current (v6) framework. Each test name carries its V-number. Where v6 deliberately changed the expected
behaviour (V-06/V-13 automatic decision on validated Layer 2, V-10/V-11 one operating model up to the product maximum,
V-14/V-15 automatic retry instead of officer refresh, V-18 de-duplicated SUM instead of single highest source), the
docstring says why; the audit's acceptance criterion (no silent cap, no RED on missing data, no double counting) holds."""
import copy
import json
import os
import threading
import unittest
from datetime import date

from server.engine import capacity as CAP
from server.engine import checkout_rules as CR
from server.engine import readiness as R
from server.engine import routing
from server.engine import terms as T
from tests.test_engine import POLICY, base_case, cap_for, run, stream, thin_file, with_alt_income
from tests.test_service import CONSENTS, FIXTURES, Base, ServiceError, make

AS_OF = date(2026, 10, 16)
MIN = POLICY["affordability"]["min_limit_vnd"]
CEILING = POLICY["affordability"]["stp_max_limit_vnd"]


def engine_case(**fields):
    a, p, s, c = base_case()
    p.update(fields)
    res = run(a, p, s, c)
    return res, cap_for(p, s, verified=["monthly_inflow_vnd", "inflow_cv", "dti_ratio", "unified_credit_score", "on_time_bill_ratio"])


class EngineMatrix(unittest.TestCase):
    def test_V01_salaried_with_cic_clean_is_green_within_ceiling(self):
        res, cap = engine_case(monthly_inflow_vnd=17000000, inflow_cv=0.07, dti_ratio=0.31)
        d = routing.decide(res, {"capacity": cap}, POLICY)
        self.assertEqual((d["band"], d["reason_code"]), ("GREEN", "R001"))
        self.assertLessEqual(d["approved_limit_vnd"], CEILING)

    def test_V05_volatile_income_moderate_dti_is_never_red(self):
        """Audit expected AMBER R010-CAPX. Under v5 the stressed (25th-percentile) income of 14M at CV 0.50 still leaves
        headroom, so the customer is GREEN at a smaller limit; R010-CAPX (Layer 2 offer) applies when the stressed capacity
        is below the minimum. Both meet the audit's acceptance criterion: no automatic decline on volatile income."""
        res, cap = engine_case(customer_profile="Gig / platform workers", has_formal_cic_history=0,
                               monthly_inflow_vnd=14000000, inflow_cv=0.50, dti_ratio=0.25, unified_credit_score=600)
        d = routing.decide(res, {"capacity": cap, "layer2_income_source_available": True}, POLICY)
        self.assertEqual(d["band"], "GREEN")
        self.assertGreaterEqual(d["approved_limit_vnd"], MIN)
        res, cap = engine_case(customer_profile="Gig / platform workers", has_formal_cic_history=0,
                               monthly_inflow_vnd=14000000, inflow_cv=0.50, dti_ratio=0.34, unified_credit_score=600)
        d = routing.decide(res, {"capacity": cap, "layer2_income_source_available": True}, POLICY)
        self.assertEqual((d["band"], d["reason_code"], d["route"]), ("AMBER", "R010-CAPX", routing.ROUTE_ALT_DATA))

    def test_V06_partial_hlb_income_salary_elsewhere(self):
        """HLB sees 3M of a salary paid elsewhere. Asking for 15M: never silently capped - a counter-offer whose 'ask for
        more' path is the automated Layer 2 request; with less HLB income the Layer 2 request comes first; validated Open
        Banking salary is ADDED to the Layer 1 stream and can support an automatic decision (v6)."""
        res, cap = engine_case(monthly_inflow_vnd=3000000, inflow_cv=0.40, dti_ratio=0.30)
        d = routing.decide(res, {"capacity": cap, "requested_limit_vnd": 15000000}, POLICY)
        self.assertEqual((d["band"], d["reason_code"], d["final_credit_state"]), ("GREEN", "R001-CO", "OFFER_PENDING"))
        res, cap = engine_case(monthly_inflow_vnd=3000000, inflow_cv=0.40, dti_ratio=0.36)
        d = routing.decide(res, {"capacity": cap}, POLICY)
        self.assertEqual((d["band"], d["reason_code"], d["route"]), ("AMBER", "R010-CAPX", routing.ROUTE_ALT_DATA))
        a, p, s, c = base_case()
        p.update(monthly_inflow_vnd=3000000, inflow_cv=0.40, dti_ratio=0.30)
        with_alt_income(p, s, c, income=20000000)
        cap = cap_for(p, s, verified=["monthly_inflow_vnd", "inflow_cv", "dti_ratio", "alt_monthly_income_vnd", "alt_income_cv"])
        self.assertEqual(cap["officer"]["income_vnd"], 23000000, "3M HLB + 20M validated Open Banking, counted once each")
        d = routing.decide(run(a, p, s, c), {"capacity": cap, "layer2": {"attempted": True, "outcome": "DATA_ADDED"}}, POLICY)
        self.assertEqual((d["band"], d["reason_code"]), ("GREEN", "R001-L2"))

    def test_V07_verified_obligations_exceed_ceiling_layer2_exhausted(self):
        res, cap = engine_case(dti_ratio=0.55, inflow_cv=0.05)
        d = routing.decide(res, {"capacity": cap, "layer2": {"attempted": True, "outcome": "DECLINED"}}, POLICY)
        self.assertEqual((d["band"], d["reason_code"], d["appeal_allowed"]), ("RED", "R020-CAP", True))

    def _cap(self, recognised, obligations):
        inc = {"verified": True, "basis": "VERIFIED_TRANSACTIONS", "stp_monthly_vnd": recognised, "officer_monthly_vnd": recognised,
               "layer1": {"average_monthly_vnd": recognised, "conservative_monthly_vnd": recognised}}
        return CAP.assign_limit(inc, {"known": True, "monthly_vnd": obligations}, POLICY)

    def test_V08_raw_capacity_just_above_minimum_is_not_rounded_into_a_decline(self):
        cap = self._cap(1000000, 332000)  # headroom 168,000 -> raw 1,799,999
        self.assertEqual(cap["stp"]["raw_limit_vnd"], 1799999)
        self.assertEqual((cap["limit_vnd"], cap["meets_minimum"]), (MIN, True))

    def test_V09_raw_capacity_exactly_at_minimum(self):
        cap = self._cap(800000, 0)  # income multiplier 2 x 800,000 = 1,600,000 binds
        self.assertEqual((cap["stp"]["raw_limit_vnd"], cap["binding_constraint"]), (1600000, "INCOME_MULTIPLIER"))
        self.assertEqual((cap["limit_vnd"], cap["meets_minimum"]), (1600000, True))
        self.assertFalse(self._cap(799000, 0)["meets_minimum"])

    def test_V10_capacity_at_product_maximum_is_capped_by_the_product_maximum(self):
        """v6: one operating model - the automatic decision covers the product scope, so the cap is the product maximum
        (90.1M, exact) rather than a lower 50M ceiling (audit F-05's concern - automation wider than the human mandate - is
        met because the officer authority is the same product scope)."""
        res, cap = engine_case(monthly_inflow_vnd=60000000, inflow_cv=0.0, dti_ratio=0.0)
        d = routing.decide(res, {"capacity": cap}, POLICY)
        self.assertEqual((d["band"], d["approved_limit_vnd"]), ("GREEN", 90100000))
        self.assertEqual(CEILING, POLICY["officer_controls"]["approval_authority_max_vnd"])

    def test_V15_alternative_data_api_down_is_never_red(self):
        res, cap = engine_case(dti_ratio=0.55, inflow_cv=0.05)  # even the structural-breach path
        d = routing.decide(res, {"capacity": cap, "layer2": {"attempted": True, "outcome": "UNAVAILABLE"}}, POLICY)
        self.assertEqual((d["band"], d["route"], d["reason_code"]), ("AMBER", routing.ROUTE_RETRY, "R010-API"))

    def test_V19_reevaluation_during_review_stays_with_the_officer(self):
        res, cap = engine_case(dti_ratio=0.55, inflow_cv=0.05)
        for outcome in ("DECLINED", None):
            d = routing.decide(res, {"capacity": cap, "in_human_review": True, "layer2_attempted": True,
                                     "layer2_outcome": outcome}, POLICY)
            self.assertEqual((d["band"], d["reason_code"]), ("AMBER", "R010-RECHECK"))
        d = routing.decide(res, {"capacity": cap, "in_human_review": True, "delinquency": {"arrears": True}}, POLICY)
        self.assertEqual(d["reason_code"], "R020-ARR", "only consent, Layer 0 and credit hard stops override a review")

    def _ctx(self, **kw):
        a, p, s, c = base_case()
        inc = CAP.income_basis(p, s, ["monthly_inflow_vnd", "inflow_cv", "dti_ratio"], POLICY)
        ob = CAP.obligations_basis(p, ["monthly_inflow_vnd", "inflow_cv", "dti_ratio"], inc)
        base = {"amount_vnd": 12500000, "merchant": {"eligible": True}, "exposure": {"outstanding_principal_vnd": 0, "held_vnd": 0},
                "line": {"status": "ACTIVE", "limit_vnd": 10000000, "assessed_on_age_days": 1}, "delinquency": {},
                "affordability": CAP.affordability(12500000, AS_OF, inc, ob, 0, POLICY), "system_available": True}
        base.update(kw)
        return CR.decide(base, POLICY)

    def test_V22_purchase_above_available_offers_split_tender(self):
        d = self._ctx(split_affordable_vnd=10000000)
        self.assertEqual((d["band"], d["reason_code"], d["split"]["pay_later_vnd"], d["split"]["pay_now_vnd"]),
                         ("AMBER", "R010-SPLIT", 10000000, 2500000))
        self.assertIn("PAY_NOW", d["alternatives"])
        d = self._ctx(line={"status": "ACTIVE", "limit_vnd": 1000000, "assessed_on_age_days": 1})
        self.assertEqual((d["band"], d["reason_code"]), ("AMBER", "R010-LIMREV"), "below the minimum: limit review, not RED")

    def test_V23_affordability_tested_on_largest_instalment(self):
        e = T.instalment_extremes(10000000, 24, POLICY)
        self.assertEqual((e["first_vnd"], e["last_vnd"]), (516000, 532000))
        inc = {"verified": True, "recognised_monthly_vnd": 2000000}
        ob = {"known": True, "monthly_vnd": 1000000 - 520000}  # headroom 520,000: first instalment fits, last does not
        aff = CAP.affordability(10000000, AS_OF, inc, ob, 0, POLICY)
        o24 = [o for o in aff["offers"] if o["tenure_months"] == 24][0]
        self.assertEqual((o24["tested_instalment_vnd"], o24["affordable"]), (532000, False))

    def test_V13_cic_no_record_means_verified_zero_external_obligations(self):
        a, p, s, c = thin_file()
        inc = CAP.income_basis(p, s, ["dti_ratio"], POLICY)
        ob = CAP.obligations_basis(p, ["dti_ratio"], inc)
        self.assertEqual((ob["basis"], ob["monthly_vnd"], ob["known"]), ("CIC_NO_RECORD", 0, True))
        self.assertTrue(any("not used" in n for n in ob["notes"]), "the supplied ratio cannot be sourced")
        self.assertNotEqual(routing.decide(run(a, p, s, c), {"capacity": CAP.assign_limit(inc, ob, POLICY)}, POLICY)["band"], "RED")


class ServiceMatrix(Base):
    def test_V02_required_consent_missing_reads_no_data(self):
        before = self.svc.db.one("SELECT COUNT(*) n FROM applications")["n"]
        with self.assertRaises(ServiceError) as e:
            self.svc.start_activation("CUS-007", dict(CONSENTS, DATA_PROCESSING=False))
        self.assertEqual(e.exception.code, "CONSENT_REQUIRED")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM applications")["n"], before, "nothing assessed, nothing read")
        a, p, s, c = base_case()
        c["categories"] = ["hlb_transactions"]
        self.assertEqual(routing.decide(run(a, p, s, c), {}, POLICY)["reason_code"], "HS-01")

    def test_V03_consent_withdrawn_mid_review(self):
        a = self.to_review("CUS-004")
        v = self.svc.withdraw_consent(a, "CUS-004")
        self.assertEqual((v["status"], v["reason_code"]), ("STOPPED", "HS-01"))
        self.assertIn("CONSENT_WITHDRAWN", self.events(application_id=a))

    def test_V04_consent_withdrawal_on_an_active_line(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx, 6)
        loan = self.svc.list_loans("CUS-001")["loans"][0]["loan_ref"]
        cs = self.svc.withdraw_line_consent("CUS-001")
        self.assertEqual(cs["line_status"], "CLOSED", "no new drawdowns")
        tx2, v2 = self.pay_later("CUS-001", "QR-261016-2004")
        self.assertNotEqual(v2["pay_later_decision"]["band"], "GREEN")
        self.assertEqual(self.svc.get_loan(loan, "CUS-001")["status"], "ACTIVE", "existing loans continue")
        self.svc.repay(loan, "CUS-001", [1], self.key())
        self.svc.set_business_date("t", advance_days=80)
        self.assertFalse(self.svc.db.one("SELECT 1 FROM applications WHERE customer_id = 'CUS-001' AND kind = 'LINE_REVIEW'"),
                         "monitoring stopped: no renewal assessment runs")
        self.assertIn("LINE_CONSENT_WITHDRAWN", self.events())

    def test_V11_officer_authority_boundary(self):
        """v6: officer authority = product scope, inclusive; 90.1M approvable, anything above refused."""
        pol = copy.deepcopy(POLICY)
        pol["aggregate_exposure"]["max_digital_unsecured_outstanding_vnd"] = None
        svc, _ = make(pol)
        self.addCleanup(lambda: svc.db.close())
        svc.db.run("UPDATE customers SET applicant_json = json_set(applicant_json, '$.monthly_inflow_vnd', 60000000, '$.inflow_cv', 0.0), "
                   "sources_json = json_remove(sources_json, '$.hlb_transactions.inflow_breakdown') WHERE customer_id = 'CUS-004'")
        app = svc.start_activation("CUS-004", CONSENTS)["application_id"]
        o = svc.officer_sign_in()["subject_id"]
        v = svc.officer_case(app, o)["application"]["version"]
        with self.assertRaises(ServiceError) as e:
            svc.officer_decide(app, o, "approve", "AP-03", "x", v, 90200000)
        self.assertEqual(e.exception.code, "ABOVE_APPROVAL_AUTHORITY")
        svc.officer_decide(app, o, "approve", "AP-03", "product maximum", v, 90100000)
        self.assertEqual(svc.credit_summary("CUS-004")["limit_vnd"], 90100000)

    def test_V12_requested_above_capacity_is_a_counter_offer_opened_only_on_acceptance(self):
        a = self.svc.start_activation("CUS-007", CONSENTS, requested_limit=20000000)
        self.assertEqual((a["status"], a["reason_code"]), ("OFFER", "R001-CO"))
        self.assertEqual(a["offer"]["limit_vnd"], 12000000, "v6: gig multiple cap on recognised income (0.7)")
        self.assertEqual(self.svc.credit_summary("CUS-007")["line_status"], "NONE", "nothing opens silently")
        self.assertIn("accept_offer", a["options"])
        a = self.svc.accept_offer(a["application_id"], "CUS-007")
        self.assertEqual((a["status"], self.svc.credit_summary("CUS-007")["limit_vnd"]), ("APPROVED", 12000000))
        self.assertIn("COUNTER_OFFER_ACCEPTED", self.events(application_id=a["application_id"]))

    def test_V12b_declining_a_counter_offer_or_asking_for_more(self):
        a = self.svc.start_activation("CUS-007", CONSENTS, requested_limit=20000000)
        a = self.svc.decline_offer(a["application_id"], "CUS-007", ask_for_more=True)
        self.assertEqual((a["status"], a["info_request"]["route"]), ("ACTION_NEEDED", "ALTERNATIVE_DATA"))
        self.assertEqual(self.svc.cancel_application(a["application_id"], "CUS-007")["status"], "CANCELLED")
        b = self.svc.start_activation("CUS-007", CONSENTS, requested_limit=20000000)
        self.assertEqual(self.svc.decline_offer(b["application_id"], "CUS-007")["status"], "CANCELLED")
        self.assertEqual(self.svc.credit_summary("CUS-007")["line_status"], "NONE")

    def test_V13_cic_no_record_persona_is_not_declined_and_has_zero_external_obligations(self):
        v = self.thin_file_with_layer2()
        cap = self.svc._capacity(self.svc._app(v["application_id"]))
        self.assertEqual((cap["obligations"]["basis"], cap["obligations"]["monthly_vnd"]), ("CIC_NO_RECORD", 0))
        self.assertEqual((v["status"], v["reason_code"]), ("APPROVED", "R001-L2"))

    def test_V14_cic_outage_then_automatic_retry_restores(self):
        """v6: no officer refresh - the system retries on the servicing cycle and re-runs the rules when CIC answers."""
        self.svc.set_demo_control("cic_unavailable", "1", "t")
        a = self.svc.start_activation("CUS-007", CONSENTS)
        rt = json.loads(self.svc._app(a["application_id"])["routing_json"])
        self.assertEqual((a["reason_code"], a["status"], "R-API-01" in rt["rule_ids"]), ("R010-API", "CHECKING", True))
        self.svc.set_demo_control("cic_unavailable", "0", "t")
        self.svc.refresh_servicing("CUS-007")
        self.assertEqual(self.svc.application_view(a["application_id"])["status"], "APPROVED")

    def test_V15_alternative_data_api_down_retries_automatically(self):
        self.svc.set_demo_control("alt_data_api_unavailable", "1", "t")
        v = self.thin_file_with_layer2()
        self.assertEqual((v["status"], v["band"], v["reason_code"]), ("CHECKING", "AMBER", "R010-API"))

    def test_V16_layer2_identity_mismatch_is_rejected_and_raised_as_a_layer0_signal(self):
        self.svc.db.run("UPDATE customers SET alternative_data_json = json_set(alternative_data_json, '$.open_banking.subject_match', 0) "
                        "WHERE customer_id = 'CUS-005'")
        v = self.thin_file_with_layer2({"open_banking": True, "utility_bills": False})
        res = v["data_acquisition"]["layer2"][0]["results"][0]
        self.assertEqual(res["outcome"], "INVALID")
        self.assertEqual((v["status"], v["reason_code"]), ("IN_REVIEW", "R010-FRD"), "data that belongs to someone else -> fraud review")
        case = self.svc.officer_case(v["application_id"], self.officer())
        checks = case["data_acquisition"]["layer2"][0]["results"][0]["validation"]["checks"]
        self.assertIn(("AV-01", False), [(c["id"], c["passed"]) for c in checks])
        self.assertTrue(case["eligibility"]["fraud_case"])

    def test_V17_layer2_short_history_is_rejected(self):
        self.svc.set_demo_control("alt_data_incomplete", "1", "t")
        v = self.thin_file_with_layer2({"open_banking": True, "utility_bills": False})
        self.assertEqual(v["data_acquisition"]["layer2"][0]["results"][0]["outcome"], "INVALID")
        self.assertEqual((v["status"], v["reason_code"]), ("NOT_APPROVED", "R020-EVD"), "unusable data is never used; appeal open")
        self.assertTrue(v["appeal_available"])

    def test_V18_income_streams_are_summed_once_after_deduplication(self):
        """v6: verified streams are SUMMED, each counted once (v5 used only the single highest source). CUS-007: delivery
        payouts paid into another bank appear in the platform record AND in that bank's Open Banking credits; they are
        counted once (from the platform record, at 0.7) and the transfers from the customer's own HLB account are excluded."""
        a = self.svc.start_activation("CUS-007", CONSENTS, requested_limit=40000000)
        a = self.svc.decline_offer(a["application_id"], "CUS-007", ask_for_more=True)
        v = self.svc.alternative_data_consent(a["application_id"], "CUS-007", {"open_banking": True, "gig_platform": True})
        inc = json.loads(self.svc._app(a["application_id"])["capacity_json"])["income"]
        streams = {x["source"]: x for x in inc["layer2"]["streams"]}
        # platform avg 6,083,333 x 0.7 = 4,258,333; Open Banking: payouts removed (counted from the platform), own transfers
        # excluded -> 0 -> not a stream
        self.assertEqual(list(streams), ["gig_platform"])
        self.assertEqual(streams["gig_platform"]["average_monthly_vnd"], 4258333)
        self.assertEqual(inc["officer_monthly_vnd"], 12998865 + 4258333, "Layer 1 + Layer 2, never the platform twice")
        self.assertEqual(v["status"], "OFFER")

    def test_V19_service_reevaluation_in_review_never_declines(self):
        app = self.to_review("CUS-002")
        self.svc.refresh_servicing("CUS-002")
        self.svc.assess_activation(app)
        v = self.svc.application_view(app, "CUS-002")
        self.assertEqual((v["status"], v["band"]), ("IN_REVIEW", "AMBER"))

    def test_V20_duplicate_identity_routes_to_fraud_review(self):
        nid = json.loads(self.svc._customer("CUS-001")["identity_json"])["national_id"]
        self.svc.db.run("UPDATE customers SET identity_json = json_set(identity_json, '$.national_id', ?) WHERE customer_id = 'CUS-007'",
                        (nid,))
        a = self.svc.start_activation("CUS-007", CONSENTS)
        self.assertEqual((a["status"], a["reason_code"]), ("IN_REVIEW", "R010-FRD"))
        self.assertIsNone(a["approved_limit_vnd"], "no credit decision")
        self.assertIn("FRAUD_SIGNAL_RAISED", self.events(application_id=a["application_id"]))
        o = self.officer()
        c = self.svc.officer_case(a["application_id"], o)
        with self.assertRaises(ServiceError) as e:  # clearing a fraud signal needs an explicit reason code
            self.svc.officer_decide(a["application_id"], o, "approve", "AP-03", "x", c["application"]["version"], 5000000)
        self.assertEqual(e.exception.code, "REASON_CODE_NOT_ALLOWED")
        c = self.svc.officer_decide(a["application_id"], o, "confirm_fraud", "FR-01", "Same CCCD as another customer",
                                    c["application"]["version"])
        self.assertEqual((c["application"]["credit_state"], c["application"]["reason_code"]), ("POLICY_STOP", "HS-05"))

    def test_V21_concurrent_purchases_exactly_one_hold_fits(self):
        pol = copy.deepcopy(POLICY)
        pol["affordability"]["max_total_dti"] = 0.95  # isolate the exposure ledger from affordability (as ExposureConcurrencyTests)
        self.svc, _ = make(pol)
        t0, v0 = self.pay_later("CUS-001", "QR-261016-1133")  # 15.0M held on the (larger) line
        self.assertEqual(v0["pay_later_decision"]["band"], "GREEN")
        left = self.svc.credit_summary("CUS-001")["available_vnd"]
        txs = [self.scan("CUS-001", r) for r in ("QR-261016-0842", "QR-261016-1021")]  # 12.0M + 9.9M
        self.assertTrue(12000000 <= left < 21900000, left)
        out = {}
        ths = [threading.Thread(target=lambda t=t: out.__setitem__(t, self.svc.choose_pay_later(t, "CUS-001"))) for t in txs]
        [t.start() for t in ths]
        [t.join() for t in ths]
        holds = [t for t in txs if out[t]["hold"]]
        self.assertEqual(len(holds), 1, "exactly one of the two parallel purchases is reserved")
        cs = self.svc.credit_summary("CUS-001")
        self.assertLessEqual(cs["held_vnd"], cs["limit_vnd"])

    def test_V22_split_tender_end_to_end(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-1610")  # 30.0M on a 29.5M line
        d = v["pay_later_decision"]
        self.assertEqual(d["reason_code"], "R010-SPLIT")
        v = self.svc.accept_split(tx, "CUS-001")
        pl, pn = v["transaction"]["pay_later_amount_vnd"], v["transaction"]["pay_now_amount_vnd"]
        self.assertEqual((v["outcome"], pl + pn, v["hold"]["amount_vnd"]), ("REVIEW_PAY_LATER", 30000000, pl))
        bal = self.svc._customer("CUS-001")["balance_vnd"]
        v = self.complete_pl("CUS-001", tx)
        self.assertEqual(v["transaction"]["state"], "PAYMENT_COMPLETED")
        self.assertEqual(self.svc._customer("CUS-001")["balance_vnd"], bal - pn)
        self.assertEqual((v["loan"]["principal_vnd"], v["settlement"]["gross_vnd"]), (pl, 30000000))

    def test_V24_line_older_than_90_days_is_renewed_automatically(self):
        self.svc.set_business_date("t", advance_days=95)
        cs = self.svc.credit_summary("CUS-001")
        self.assertEqual((cs["line_status"], cs["assessment_age_days"]), ("ACTIVE", 0), "renewed on refreshed Layer 1 data")
        tx, v = self.pay_later("CUS-001", "QR-261016-2004")
        self.assertEqual(v["pay_later_decision"]["band"], "GREEN")
        self.assertIn("CREDIT_LINE_RENEWED", self.events())

    def test_V24b_renewal_starts_before_expiry_and_never_raises_the_limit(self):
        before = self.svc.credit_summary("CUS-001")["limit_vnd"]
        self.svc.db.run("UPDATE customers SET applicant_json = json_set(applicant_json, '$.monthly_inflow_vnd', 40000000) "
                        "WHERE customer_id = 'CUS-001'")
        self.svc.set_business_date("t", advance_days=76)  # inside the 15-day lead window
        self.assertEqual(self.svc.credit_summary("CUS-001")["limit_vnd"], before, "renewal never raises a limit automatically")
        self.assertEqual(self.svc.credit_summary("CUS-001")["assessment_age_days"], 0)

    def test_V25_arrears_freeze_and_unfreeze(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx, 6)
        loan = self.svc.list_loans("CUS-001")["loans"][0]["loan_ref"]
        self.svc.set_business_date("t", new_date="2026-11-21")  # 5 days overdue
        self.assertEqual(self.svc.credit_summary("CUS-001")["line_status"], "FROZEN")
        self.svc.repay(loan, "CUS-001", [1], self.key())
        self.assertEqual(self.svc.credit_summary("CUS-001")["line_status"], "ACTIVE")

    def test_V26_cure_by_prepayment_does_not_clear_the_flag(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx, 6)
        loan = self.svc.list_loans("CUS-001")["loans"][0]["loan_ref"]
        self.svc.set_business_date("t", new_date="2026-11-21")
        self.svc.repay(loan, "CUS-001", [1], self.key())
        self.svc.repay(loan, "CUS-001", [2, 3, 4], self.key())
        self.assertEqual(self.svc.credit_summary("CUS-001")["internal_delinquency_flag"], 1)

    def test_V27_default_then_arrears_paid_goes_to_audited_reinstatement(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx, 6)
        loan = self.svc.list_loans("CUS-001")["loans"][0]["loan_ref"]
        self.svc.set_business_date("t", advance_days=130)
        self.assertEqual(self.svc.credit_summary("CUS-001")["line_status"], "SUSPENDED")
        with self.assertRaises(ServiceError) as e:
            self.svc.start_activation("CUS-001", CONSENTS)
        self.assertEqual(e.exception.code, "LINE_SUSPENDED")
        overdue = [i["number"] for i in self.svc.get_loan(loan, "CUS-001")["instalments"] if i["status"] == "OVERDUE"]
        self.svc.db.run("UPDATE customers SET balance_vnd = 99000000 WHERE customer_id = 'CUS-001'")
        self.svc.repay(loan, "CUS-001", overdue, self.key())
        self.assertEqual(self.svc.get_loan(loan, "CUS-001")["status"], "ACTIVE", "default cured")
        self.assertEqual(self.svc.credit_summary("CUS-001")["line_status"], "SUSPENDED", "only an officer reinstates")
        case = self.svc.db.one("SELECT * FROM applications WHERE customer_id = 'CUS-001' AND kind = 'LINE_REINSTATEMENT'")
        self.assertEqual((case["credit_state"], case["reason_code"]), ("MANUAL_REVIEW", "R010-REINST"))
        o = self.officer()
        c = self.svc.officer_case(case["application_id"], o)
        mx = c["eligibility"]["max_approvable_vnd"]
        self.assertIsNotNone(mx)
        self.assertLessEqual(mx, 29500000, "never above the previous limit")
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(case["application_id"], o, "approve", "AP-03", "x", c["application"]["version"], mx)
        self.assertEqual(e.exception.code, "REASON_CODE_NOT_ALLOWED")
        self.svc.officer_decide(case["application_id"], o, "approve", "RI-01", "Arrears cleared", c["application"]["version"], mx)
        self.assertEqual(self.svc.credit_summary("CUS-001")["line_status"], "ACTIVE")
        self.assertIn("CREDIT_LINE_REINSTATED", self.events(application_id=case["application_id"]))


class SegmentMatrix(unittest.TestCase):
    """V-28: the full replay by segment (requires the supplied dataset)."""

    @classmethod
    def setUpClass(cls):
        ds = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reference", "hlbvn_dataset.xlsx")
        if not os.path.exists(ds):
            raise unittest.SkipTest("dataset not available")
        import scripts.segment_replay as SR
        cls.result = SR.run(SR.ROOT)

    def test_V28_no_segment_is_auto_declined_without_a_verified_obligations_reason(self):
        s = self.result["summary"]
        self.assertEqual(s["all"]["red"], 0, "first pass: no automatic decline in any segment")
        self.assertEqual(s["all"]["reds_without_verified_obligation_breach"], 0)
        # v6: a customer who declines the Layer 2 request when Layer 1 evidence cannot support a limit gets a verified
        # RED (insufficient evidence / verified capacity breach) - never a segment-based decline. Segment differences in
        # this rate reflect evidence coverage, so the control is on the reason, not on equal rates.
        for seg, b in s["by_segment"].items():
            self.assertEqual(b["red"], 0, seg)
        reasons = set(self.result["red_if_layer2_declined_reasons"])
        self.assertTrue(reasons <= {"R020-EVD", "R020-CAP"}, reasons)

    def test_V28b_no_officer_case_is_a_foregone_rejection_for_a_flag(self):
        self.assertEqual(self.result["summary"]["all"]["flag_cases_unapprovable"], 0, "audit F-19")


if __name__ == "__main__":
    unittest.main()
