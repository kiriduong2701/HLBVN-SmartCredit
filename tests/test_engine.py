"""Unit tests for the pure engines: data readiness, activation routing (GREEN/AMBER/RED),
capacity-based limit + affordability, instant-checkout rules, pricing and the VietQR parser."""
import copy
import json
import os
import unittest
from datetime import date
from statistics import NormalDist

from server.engine import capacity as CAP
from server.engine import checkout_rules as CR
from server.engine import qr as QR
from server.engine import readiness as R
from server.engine import routing, terms as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = json.load(open(os.path.join(ROOT, "server", "policy", "demo_policy.json")))
AS_OF = date(2026, 10, 16)
ALL_CONSENT = {"granted": True, "categories": ["hlb_transactions", "credit_information", "e_wallet_activity"]}
ALL_FIELDS = ["monthly_inflow_vnd", "inflow_cv", "dti_ratio", "unified_credit_score", "on_time_bill_ratio"]


def base_case():
    application = {"application_id": "APP-1", "applicant_id": "CUS-1", "qr_ref": "QR-1", "merchant_id": "M-1",
                   "requested_loan_vnd": 12000000, "loan_tenure_months": 6}
    applicant = {"customer_profile": "Salaried, with credit history", "has_formal_cic_history": 1, "has_observable_tx": 1,
                 "monthly_inflow_vnd": 16969073, "inflow_cv": 0.0681, "unified_credit_score": 681,
                 "casa_balance_vnd": 7096492, "dti_ratio": 0.3145, "on_time_bill_ratio": 0.9715, "e_wallet_topups_pm": 6,
                 "regular_weekly_pattern": 1, "rapid_drain_flag": 0}
    sources = {"hlb_transactions": {"verified": True, "observed_at": "2026-10-15", "period_months": 6},
               "credit_information": {"verified": True, "observed_at": "2026-10-15"},
               "e_wallet_activity": {"verified": True, "observed_at": "2026-10-14"}}
    return application, applicant, sources, copy.deepcopy(ALL_CONSENT)


def run(application, applicant, sources, consent, policy=POLICY):
    return R.evaluate(application, applicant, sources, consent, policy, AS_OF)


def cap_for(applicant, sources, verified=ALL_FIELDS):
    inc = CAP.income_basis(applicant, sources, verified, POLICY)
    return CAP.assign_limit(inc, CAP.obligations_basis(applicant, verified, inc), POLICY)


def route(res, cap=None, delinquency=None, errors=None, attempted=False, outcome=None, unstable=None, in_review=False,
          behaviour=True, income_sources=True, behaviour_source=True, kind=None, requested=None, current=None):
    a, p, s, c = base_case()
    l2 = {"attempted": attempted, "outcome": outcome, "unstable_sources": unstable or [], "income_sources": income_sources,
          "behaviour_source": behaviour_source, "allowed": True}
    return routing.decide(res, {"capacity": cap if cap is not None else cap_for(p, s), "delinquency": delinquency or {},
                                "source_errors": errors or [], "layer2": l2, "behaviour_signal": behaviour,
                                "in_human_review": in_review, "kind": kind, "requested_limit_vnd": requested,
                                "current_limit_vnd": current}, POLICY)


def thin_file():
    """No CIC history, no observable transactions (CUS-005 style)."""
    a, p, s, c = base_case()
    p.update(has_formal_cic_history=0, has_observable_tx=0, customer_profile="Salaried, no credit history")
    for f in ("monthly_inflow_vnd", "inflow_cv", "casa_balance_vnd", "on_time_bill_ratio", "e_wallet_topups_pm", "regular_weekly_pattern"):
        p[f] = None
    return a, p, s, c


def stream(source, monthly, factor=1.0, stp_usable=True, tier="A"):
    rec = [int(x * factor) for x in monthly]
    return {"source": source, "tier": tier, "type": source, "factor": factor, "average_recognised_vnd": int(sum(rec) / len(rec)),
            "series_recognised_vnd": rec, "stp_usable": stp_usable, "dedup": []}


def with_alt_income(p, s, c, income=12000000, cv=0.01, months=6, streams=None):
    """Validated Layer 2 data as acquisition.py writes it: per-stream recognised series + the summed fields."""
    streams = streams if streams is not None else [stream("open_banking", [income] * months)]
    p.update(alt_monthly_income_vnd=sum(x["average_recognised_vnd"] for x in streams), alt_income_cv=cv)
    s["alternative_data"] = {"verified": True, "observed_at": "2026-10-15", "period_months": months, "streams": streams,
                             "source_ids": [x["source"] for x in streams]}
    c["categories"].append("alternative_data")


class ReadinessTests(unittest.TestCase):
    def test_valid_case_is_data_ready(self):
        res = run(*base_case())
        self.assertEqual(res["status"], R.DATA_READY)
        for key in ("status", "validation_errors", "verification_warnings", "evidence_gaps", "review_reasons",
                    "checks_performed", "explanation"):
            self.assertIn(key, res)
        self.assertIn("not a credit approval", res["explanation"])

    def test_missing_mandatory_application_field_is_incomplete(self):
        a, p, s, c = base_case()
        a["qr_ref"] = None
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.INCOMPLETE)
        self.assertIn("qr_ref", res["missing_fields"])

    def test_missing_profile_is_not_a_blocker(self):
        """v6: no customer question - the segment label only chooses Layer 2 sources; income comes from verified data."""
        a, p, s, c = base_case()
        p["customer_profile"] = ""
        self.assertEqual(run(a, p, s, c)["status"], R.DATA_READY)

    def test_amount_bounds(self):
        for amount, expected in [(1600000, R.DATA_READY), (90100000, R.DATA_READY),
                                 (1599999, R.INVALID_DATA), (90100001, R.INVALID_DATA), (12000000.5, R.INVALID_DATA)]:
            a, p, s, c = base_case()
            a["requested_loan_vnd"] = amount
            self.assertEqual(run(a, p, s, c)["status"], expected, amount)

    def test_tenure_must_be_supported(self):
        for tenure in (3, 6, 9, 12, 18, 24):
            a, p, s, c = base_case()
            a["loan_tenure_months"] = tenure
            self.assertEqual(run(a, p, s, c)["status"], R.DATA_READY)
        for tenure in (4, 5, 7, 8, 10, 11, 36, 0):
            a, p, s, c = base_case()
            a["loan_tenure_months"] = tenure
            self.assertEqual(run(a, p, s, c)["status"], R.INVALID_DATA, tenure)

    def test_score_ratio_binary_ranges(self):
        cases = [("unified_credit_score", 349), ("unified_credit_score", 851), ("on_time_bill_ratio", 1.01),
                 ("on_time_bill_ratio", -0.1), ("rapid_drain_flag", 2), ("has_observable_tx", "yes"),
                 ("monthly_inflow_vnd", -5), ("customer_profile", "Astronaut")]
        for field, value in cases:
            a, p, s, c = base_case()
            p[field] = value
            res = run(a, p, s, c)
            self.assertEqual(res["status"], R.INVALID_DATA, (field, value))
            self.assertTrue(any(e["field"] == field for e in res["validation_errors"]))

    def test_observable_tx_zero_with_values_is_excluded_automatically_and_keeps_values(self):
        """v6: the conflicting HLB values are excluded from use automatically (data-quality event), never sent to an officer."""
        a, p, s, c = base_case()
        p["has_observable_tx"] = 0
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY, "CIC history is still an evidence path")
        self.assertTrue(any(w["rule_id"] == "R-VER-01" and w["resolution"] == "EXCLUDED_FROM_USE" for w in res["verification_warnings"]))
        self.assertEqual(p["monthly_inflow_vnd"], 16969073, "input must not be erased")
        self.assertIn("monthly_inflow_vnd", res["fields_not_used"])
        self.assertEqual(res["data_quality_events"][0]["resolution"], "EXCLUDED_FROM_USE")

    def test_unverified_stale_unknown_sources_are_excluded_not_referred(self):
        a, p, s, c = base_case()
        s["credit_information"]["verified"] = False
        res = run(a, p, s, c)
        self.assertNotEqual(res["status"], R.VERIFY_DATA)
        self.assertIn("unified_credit_score", res["fields_not_used"])
        a, p, s, c = base_case()
        s["hlb_transactions"]["observed_at"] = "2026-06-01"
        res = run(a, p, s, c)
        self.assertTrue(any(w["rule_id"] == "R-VER-03" for w in res["verification_warnings"]))
        self.assertIn("monthly_inflow_vnd", res["fields_not_used"])
        a, p, s, c = base_case()
        del s["e_wallet_activity"]
        res = run(a, p, s, c)
        self.assertTrue(any("unknown provenance" in w["message"] for w in res["verification_warnings"]))
        self.assertIn("e_wallet_topups_pm", res["fields_not_used"])

    def test_profile_conflict_is_resolved_by_the_cic_record(self):
        a, p, s, c = base_case()
        p["has_formal_cic_history"] = 0  # profile says "with credit history": CIC is authoritative, label only picks sources
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertTrue(any(w["rule_id"] == "R-VER-04" for w in res["verification_warnings"]))
        self.assertNotIn("has_formal_cic_history", res["fields_not_used"])

    def test_rapid_drain_adds_review_reason_only(self):
        a, p, s, c = base_case()
        p["rapid_drain_flag"] = 1
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertEqual([r["rule_id"] for r in res["review_reasons"]], ["R-REV-01"])

    def test_insufficient_evidence(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Salaried, no credit history", "has_formal_cic_history": 0, "has_observable_tx": 0})
        for f in POLICY["transaction_dependent_fields"]["fields"]:
            p[f] = None
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.INSUFFICIENT_EVIDENCE)
        self.assertEqual(res["evidence_gaps"][0]["rule_id"], "R-EVD-01")

    def test_validated_alternative_data_satisfies_evidence_only_with_consent_and_period(self):
        a, p, s, c = thin_file()
        with_alt_income(p, s, c)
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertEqual(res["evidence_paths_met"], ["EV-ALT"])
        a, p, s, c = thin_file()
        with_alt_income(p, s, c, months=3)
        self.assertEqual(run(a, p, s, c)["status"], R.INSUFFICIENT_EVIDENCE, "3 months is not enough")
        a, p, s, c = thin_file()
        with_alt_income(p, s, c)
        c["categories"].remove("alternative_data")
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.INSUFFICIENT_EVIDENCE, "no consent -> data not used")
        self.assertIn("alt_monthly_income_vnd", res["fields_not_used"])

    def test_no_document_evidence_path_remains(self):
        self.assertNotIn("EV-DOC", [e["id"] for e in POLICY["evidence_requirement"]["any_of"]])
        self.assertNotIn("supporting_documents", POLICY["data_sources"])
        self.assertNotIn("documents", POLICY)

    def test_short_observation_period_is_not_evidence(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Gig / platform workers", "has_formal_cic_history": 0})
        s["hlb_transactions"]["period_months"] = 3
        self.assertEqual(run(a, p, s, c)["status"], R.INSUFFICIENT_EVIDENCE)

    def test_no_cic_with_verified_transactions_is_ready(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Gig / platform workers", "has_formal_cic_history": 0})
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertTrue(any(ch["check_id"] == "C09" and "Not used as a rejection reason" in ch["detail"]
                            for ch in res["checks_performed"]))

    def test_missing_consent_category_drops_source_data(self):
        a, p, s, c = base_case()
        c["categories"] = ["hlb_transactions"]
        res = run(a, p, s, c)
        self.assertEqual(res["missing_consent"], ["credit_information", "e_wallet_activity"])
        self.assertIn("unified_credit_score", res["fields_not_used"])

    def test_outcome_fields_are_excluded(self):
        a, p, s, c = base_case()
        p.update({"is_default_90d": 1, "approved_loan_vnd": 0, "stp_approval_status": 0, "manual_decision_days": 3.2})
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertEqual(sorted(res["excluded_outcome_fields"]),
                         sorted(["is_default_90d", "approved_loan_vnd", "stp_approval_status", "manual_decision_days"]))

    def test_engine_is_deterministic(self):
        self.assertEqual(run(*base_case()), run(*base_case()))

    def test_status_precedence(self):
        a, p, s, c = base_case()
        a["merchant_id"] = None
        p["unified_credit_score"] = 900
        p["has_observable_tx"] = 0
        self.assertEqual(run(a, p, s, c)["status"], R.INCOMPLETE)


class ActivationRoutingTests(unittest.TestCase):
    def test_green_assigns_capacity_based_limit(self):
        dec = route(run(*base_case()))
        self.assertEqual((dec["band"], dec["reason_code"], dec["route"]), ("GREEN", "R001", routing.ROUTE_STP))
        self.assertEqual(dec["state_path"], ["STP_ELIGIBLE", "APPROVED"])
        # v5 (audit F-01): stressed income = 25th-percentile month = 16,969,073 x (1 - 0.6745 x 0.0681); obligations are the
        # absolute 0.3145 x 16,969,073; 0.5 x income - obligations = 2,758,966 headroom -> 29,560,350 -> 29.5M (v4: 27.5M).
        self.assertEqual(dec["approved_limit_vnd"], 29500000)
        self.assertTrue(all(c["passed"] for c in dec["approval_conditions"]))

    def test_risk_flags_go_to_officer_never_decline_no_uploads(self):
        for field, value, rid in (("rapid_drain_flag", 1, "R-REV-01"), ("unified_credit_score", 423, "RF-02"),
                                  ("on_time_bill_ratio", 0.5, "RF-03")):
            a, p, s, c = base_case()
            p[field] = value
            dec = route(run(a, p, s, c))
            self.assertEqual((dec["band"], dec["route"], dec["final_credit_state"], dec["reason_code"]),
                             ("AMBER", routing.ROUTE_REVIEW, "MANUAL_REVIEW", "R010-FLAG"), field)
            self.assertIn(rid, dec["rule_ids"])
            self.assertEqual(dec["requested_items"], [])

    def test_high_dti_alone_no_longer_routes_to_an_officer(self):
        """v6: RF-04 (DTI > dataset P90 0.3695) double-counted DTI, which already enters capacity as absolute obligations
        against the 50% ceiling. DTI 0.37 with CV 0.0681: stressed income 16,969,073 x (1 - 0.6745 x 0.0681) = 16,189,632;
        headroom 0.5 x 16,189,632 - 0.37 x 16,969,073 = 1,816,259 -> capacity ~19.5M: the capacity rule decides, no flag."""
        a, p, s, c = base_case()
        p["dti_ratio"] = 0.37
        res = run(a, p, s, c)
        self.assertNotIn("RF-04", [r["rule_id"] for r in res["review_reasons"]])
        self.assertNotEqual(route(res)["reason_code"], "R010-FLAG")

    def test_profile_flag_boundaries_do_not_trigger(self):
        a, p, s, c = base_case()
        p.update(unified_credit_score=436, on_time_bill_ratio=0.663, dti_ratio=0.3695)
        self.assertEqual(route(run(a, p, s, c))["band"], "GREEN")

    def test_conflict_goes_to_layer2_and_only_invalid_data_to_an_officer(self):
        a, p, s, c = base_case()
        p.update(has_observable_tx=0, has_formal_cic_history=0, customer_profile="Salaried, no credit history")
        res = run(a, p, s, c)
        nu = res["fields_not_used"]
        cap = cap_for(p, s, verified=[f for f in ALL_FIELDS if f not in nu])
        dec = route(res, cap=cap)
        self.assertEqual((dec["band"], dec["route"]), ("AMBER", routing.ROUTE_ALT_DATA), "automated data request, not an officer")
        a, p, s, c = base_case()
        p["unified_credit_score"] = 900
        dec = route(run(a, p, s, c))
        self.assertEqual((dec["band"], dec["final_credit_state"], dec["reason_code"]), ("AMBER", "MANUAL_REVIEW", "R010-VER"))

    def test_layer1_insufficient_requests_layer2_with_missing_items(self):
        a, p, s, c = thin_file()
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.INSUFFICIENT_EVIDENCE)
        cap = cap_for(p, s, verified=["dti_ratio"])
        dec = route(res, cap=cap, behaviour=False)
        self.assertEqual((dec["band"], dec["reason_code"], dec["route"]), ("AMBER", "R010-EVD", routing.ROUTE_ALT_DATA))
        # income + repayment behaviour (no CIC history, no bill ratio) are requested; the customer never sees Layer 1 detail
        self.assertEqual(dec["requested_items"], [{"type": "alternative_data", "missing": ["VERIFIED_INCOME", "REPAYMENT_BEHAVIOUR"]}])
        self.assertEqual(dec["customer_reason"], "MORE_INFORMATION")

    def test_layer2_without_usable_income_is_a_verified_decline_and_outage_retries(self):
        """v6: after Layer 2 gave no usable income, repayment capacity cannot be verified -> RED R020-EVD with appeal (the
        reason is the unverifiable capacity, never the consent answer). A provider outage retries automatically."""
        a, p, s, c = thin_file()
        res, cap = run(a, p, s, c), cap_for(p, s, verified=["dti_ratio"])
        for outcome in ("DECLINED", "NO_USABLE_DATA"):
            dec = route(res, cap=cap, attempted=True, outcome=outcome)
            self.assertEqual((dec["band"], dec["reason_code"], dec["final_credit_state"]), ("RED", "R020-EVD", "DECLINED"))
            self.assertTrue(dec["appeal_allowed"])
            self.assertIn("not because consent was refused", dec["reasons"][-1]["message"])
        dec = route(res, cap=cap, attempted=True, outcome="UNAVAILABLE")
        self.assertEqual((dec["band"], dec["route"], dec["final_credit_state"]), ("AMBER", routing.ROUTE_RETRY, "ASSESSMENT_PENDING"))

    def test_ewallet_activity_is_not_evidence_of_capacity(self):
        """v6: e-wallet top-ups are a Layer 1 behaviour variable, not an evidence path (EV-3P removed) and never income."""
        a, p, s, c = thin_file()
        p["e_wallet_topups_pm"] = 12
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.INSUFFICIENT_EVIDENCE)
        dec = route(res, cap=cap_for(p, s, verified=["dti_ratio", "e_wallet_topups_pm"]))
        self.assertEqual((dec["band"], dec["reason_code"]), ("AMBER", "R010-EVD"))

    def test_validated_layer2_supports_automatic_approval_inside_the_conditions(self):
        """v6: validated, stable Layer 2 income (AV-01..AV-07) can support an automatic approval (R001-L2); unstable or
        declining streams go to an officer (R010-ALT); a capacity below the minimum on Layer 2 data is an officer case too."""
        a, p, s, c = thin_file()
        with_alt_income(p, s, c)
        res = run(a, p, s, c)
        vf = ["dti_ratio", "alt_monthly_income_vnd", "alt_income_cv"]
        cap = cap_for(p, s, verified=vf)
        self.assertEqual((cap["income"]["basis"], cap["income"]["layer"]), ("ALTERNATIVE_DATA", 2))
        dec = route(res, cap=cap, attempted=True, outcome="DATA_ADDED")
        # 12,000,000/month, CIC no record (0 obligations): 0.5 x 12M x 12/1.12 = 64.3M; 2 x 12M = 24M binds -> 24.0M
        self.assertEqual((dec["band"], dec["reason_code"], dec["approved_limit_vnd"]), ("GREEN", "R001-L2", 24000000))
        s["alternative_data"]["streams"][0]["stp_usable"] = False
        cap = cap_for(p, s, verified=vf)
        self.assertIsNone(cap["stp"], "an unstable stream is never in the automatic basis")
        dec = route(res, cap=cap, attempted=True, outcome="DATA_ADDED", unstable=["open_banking"])
        self.assertEqual((dec["band"], dec["reason_code"]), ("AMBER", "R010-ALT"))
        self.assertEqual(dec["indicative_limit_vnd"], cap["officer_max_limit_vnd"])
        a, p, s, c = thin_file()
        with_alt_income(p, s, c, income=700000)
        cap = cap_for(p, s, verified=vf)
        self.assertFalse(cap["meets_minimum"])
        self.assertEqual(route(run(a, p, s, c), cap=cap, attempted=True, outcome="DATA_ADDED")["band"], "RED",
                         "verified income too low even on average after Layer 2 -> R020-CAP")

    def test_alternative_data_does_not_override_hard_stops(self):
        a, p, s, c = thin_file()
        with_alt_income(p, s, c)
        cap = cap_for(p, s, verified=["dti_ratio", "alt_monthly_income_vnd"])
        self.assertEqual(route(run(a, p, s, c), cap=cap, attempted=True, outcome="DATA_ADDED", delinquency={"arrears": True, "days_overdue": 2})["reason_code"], "R020-ARR")
        c["categories"] = ["alternative_data"]
        self.assertEqual(route(run(a, p, s, c), cap=cap, attempted=True, outcome="DATA_ADDED")["band"], "STOP")

    def test_case_already_with_officer_is_never_auto_approved(self):
        dec = route(run(*base_case()), in_review=True)
        self.assertEqual((dec["band"], dec["final_credit_state"], dec["reason_code"]), ("AMBER", "MANUAL_REVIEW", "R010-RECHECK"))

    def test_no_customer_question_route_exists(self):
        """v6: the employment-type question and declared inputs are gone (no MORE_INFORMATION route, no R010-INC / R010-DECL)."""
        self.assertFalse(hasattr(routing, "ROUTE_MORE_INFO"))
        for code in ("R010-INC", "R010-DECL"):
            self.assertNotIn(code, POLICY["reason_codes"]["codes"])

    def test_source_outage_retries_automatically_and_unknown_obligations_go_to_review(self):
        # A Layer 1 outage: automatic retry (no officer, no decision on missing data).
        a, p, s, c = base_case()
        s["credit_information"] = {"unavailable": True, "error": "CIC_ENQUIRY_UNAVAILABLE"}
        res = run(a, p, s, c)
        self.assertEqual((res["status"], res["unavailable_sources"]), (R.DATA_READY, ["credit_information"]))
        dec = route(res, cap=cap_for(p, s, verified=["monthly_inflow_vnd", "inflow_cv"]),
                    errors=[{"source": "credit_information", "code": "CIC_ENQUIRY_UNAVAILABLE"}])
        self.assertEqual((dec["band"], dec["route"], dec["final_credit_state"], dec["reason_code"]),
                         ("AMBER", routing.ROUTE_RETRY, "ASSESSMENT_PENDING", "R010-API"))
        self.assertIn("R-API-01", dec["rule_ids"])
        # Obligations that cannot be converted to VND (bureau ratio without a trusted income base) -> R010-OBL officer.
        a, p, s, c = base_case()
        inc = CAP.income_basis(p, s, ["monthly_inflow_vnd", "inflow_cv"], POLICY)
        ob = CAP.obligations_basis(p, ["monthly_inflow_vnd", "inflow_cv"], inc)
        dec = route(run(a, p, s, c), cap=CAP.assign_limit(inc, ob, POLICY))
        self.assertEqual((ob["known"], dec["reason_code"]), (False, "R010-OBL"))

    def test_capacity_shortfall_and_verified_breach(self):
        # Volatility alone never declines. DTI 0.36 with CV 0.45: stressed headroom < minimum, average capacity ok ->
        # automated Layer 2 request, then an officer judgement (never RED).
        a, p, s, c = base_case()
        p.update(dti_ratio=0.36, inflow_cv=0.45)
        cap = cap_for(p, s)
        self.assertFalse(cap["verified_obligation_breach"])
        dec = route(run(a, p, s, c), cap=cap)
        self.assertEqual((dec["band"], dec["reason_code"], dec["route"]), ("AMBER", "R010-CAPX", routing.ROUTE_ALT_DATA))
        dec = route(run(a, p, s, c), cap=cap, attempted=True, outcome="DECLINED")
        self.assertEqual((dec["final_credit_state"], dec["reason_code"]), ("MANUAL_REVIEW", "R010-CAPX"))
        # Verified obligations above the ceiling on AVERAGE income: Layer 2 first when plausible, then RED R020-CAP.
        p.update(dti_ratio=0.55, inflow_cv=0.05)
        cap = cap_for(p, s)
        self.assertTrue(cap["verified_obligation_breach"])
        # need = (0.55 x 16,969,073 + 1.6M x 1.12/12) / 0.5 = 18,964,980 -> uplift 11.8% <= 1.0 -> Layer 2 is plausible
        self.assertAlmostEqual(cap["uplift_ratio_needed"], 0.1176, places=3)
        self.assertEqual(route(run(a, p, s, c), cap=cap)["reason_code"], "R010-CAPX")
        dec = route(run(a, p, s, c), cap=cap, attempted=True, outcome="DECLINED")
        self.assertEqual((dec["band"], dec["reason_code"], dec["final_credit_state"]), ("RED", "R020-CAP", "DECLINED"))
        self.assertTrue(dec["appeal_allowed"])
        dec = route(run(a, p, s, c), cap=cap, attempted=True, outcome="UNAVAILABLE")
        self.assertEqual((dec["band"], dec["route"]), ("AMBER", routing.ROUTE_RETRY), "a provider outage never declines")
        # Implausible: verified obligations would need more than double the verified income -> RED at once, no data request.
        p.update(dti_ratio=1.2)
        cap = cap_for(p, s)
        self.assertGreater(cap["uplift_ratio_needed"], 1.0)
        dec = route(run(a, p, s, c), cap=cap)
        self.assertEqual((dec["band"], dec["reason_code"]), ("RED", "R020-CAP"))

    def test_arrears_and_default_are_hard_stops(self):
        res = run(*base_case())
        self.assertEqual(route(res, delinquency={"arrears": True, "days_overdue": 3})["reason_code"], "R020-ARR")
        self.assertEqual(route(res, delinquency={"default": True})["reason_code"], "R020-DEF")

    def test_missing_consent_stops(self):
        a, p, s, c = base_case()
        c["categories"] = ["hlb_transactions"]
        dec = route(run(a, p, s, c))
        self.assertEqual((dec["band"], dec["rule_ids"]), ("STOP", ["HS-01"]))

    def test_no_cic_history_is_never_a_decline_reason(self):
        a, p, s, c = base_case()
        p.update(has_formal_cic_history=0, customer_profile="Salaried, no credit history")
        self.assertEqual(route(run(a, p, s, c))["band"], "GREEN")

    def test_thin_file_without_behaviour_signal(self):
        """No CIC history and no bill-payment data: automated utility/telecom request, then an officer (R010-THIN)."""
        res = run(*base_case())
        dec = route(res, behaviour=False)
        self.assertEqual((dec["route"], dec["reason_code"], dec["requested_items"][0]["missing"]),
                         (routing.ROUTE_ALT_DATA, "R010-THIN", ["REPAYMENT_BEHAVIOUR"]))
        dec = route(res, behaviour=False, attempted=True, outcome="NO_USABLE_DATA")
        self.assertEqual((dec["final_credit_state"], dec["reason_code"]), ("MANUAL_REVIEW", "R010-THIN"))

    def test_limit_increase_and_renewal_outcomes(self):
        res = run(*base_case())
        # capacity 29.5M (see test_green_assigns_capacity_based_limit)
        dec = route(res, kind="LIMIT_INCREASE", requested=25000000, current=20000000)
        self.assertEqual((dec["band"], dec["reason_code"], dec["approved_limit_vnd"]), ("GREEN", "R001", 25000000))
        dec = route(res, kind="LIMIT_INCREASE", requested=40000000, current=20000000)
        self.assertEqual((dec["reason_code"], dec["approved_limit_vnd"], dec["final_credit_state"]), ("R001-CO", 29500000, "OFFER_PENDING"))
        dec = route(res, kind="LIMIT_INCREASE", requested=40000000, current=29500000)
        self.assertEqual((dec["route"], dec["reason_code"]), (routing.ROUTE_ALT_DATA, "R010-CAPX"))
        dec = route(res, kind="LIMIT_INCREASE", requested=40000000, current=29500000, attempted=True, outcome="DECLINED")
        self.assertEqual((dec["band"], dec["reason_code"]), ("RED", "R020-INC"))
        dec = route(res, kind="LINE_REVIEW", current=20000000)
        self.assertEqual((dec["band"], dec["approved_limit_vnd"]), ("GREEN", 20000000), "a renewal never raises the limit")

    def test_engine_only_declines_under_explicit_policy(self):
        """Across many perturbations, RED only ever carries an R020 policy code with appeal."""
        for field in ("unified_credit_score", "on_time_bill_ratio", "dti_ratio", "rapid_drain_flag", "has_observable_tx"):
            for v in (0, 1, 0.2, 0.9, 400, 700):
                a, p, s, c = base_case()
                p[field] = v
                for kw in ({}, {"attempted": True, "outcome": "DECLINED"}):
                    dec = route(run(a, p, s, c), cap=cap_for(p, s), **kw)
                    if dec["band"] == "RED":
                        self.assertTrue(dec["reason_code"].startswith("R020") and dec.get("appeal_allowed"))

class CapacityTests(unittest.TestCase):
    def test_limit_formula_and_binding_constraint(self):
        a, p, s, c = base_case()
        cap = cap_for(p, s)
        z = -NormalDist().inv_cdf(0.25)                         # 0.674490 for the 25th percentile
        inc = int(16969073 * (1 - z * 0.0681))                  # 25th-percentile month (normal approximation)
        obligations = int(round(0.3145 * 16969073))             # absolute VND, on the inflow the ratio was measured on
        headroom = int(0.5 * inc) - obligations
        self.assertEqual(cap["obligations"]["monthly_vnd"], obligations)
        self.assertEqual(cap["obligations"]["basis"], "CIC_REPORTED")
        self.assertEqual(cap["monthly_headroom_vnd"], headroom)
        self.assertEqual(cap["limit_by_capacity_vnd"], int(headroom * 12 / 1.12))
        self.assertEqual(cap["binding_constraint"], "CAPACITY")
        self.assertEqual(cap["limit_vnd"] % 500000, 0)
        # officer maximum uses the same obligations against AVERAGE income, so it is never below the automatic capacity
        self.assertEqual(cap["officer"]["monthly_headroom_vnd"], int(0.5 * 16969073) - obligations)
        self.assertGreaterEqual(cap["officer_max_limit_vnd"], cap["limit_vnd"])

    def test_income_multiplier_and_product_max_caps(self):
        a, p, s, c = base_case()
        p.update(dti_ratio=0.0, inflow_cv=0.0)
        self.assertEqual(cap_for(p, s)["binding_constraint"], "INCOME_MULTIPLIER")
        p["monthly_inflow_vnd"] = 500000000
        cap = cap_for(p, s)
        # raw = product maximum 90,100,000: an exact cap (not rounded to the 500k step), equal to the v6 STP ceiling
        self.assertEqual((cap["binding_constraint"], cap["limit_vnd"]), ("PRODUCT_MAXIMUM", 90100000))

    def test_unverified_or_declared_income_is_not_a_basis(self):
        a, p, s, c = base_case()
        self.assertEqual(cap_for(p, s, verified=["dti_ratio"])["reason"], "INCOME_NOT_VERIFIED")
        s["hlb_transactions"]["verified"] = False
        self.assertFalse(cap_for(p, s)["computable"])

    def test_layer1_and_layer2_streams_are_summed_once(self):
        """v6: verified streams are SUMMED after de-duplication; STP uses the sum of stressed months, officers the sum of
        averages; obligations are never re-scaled to the Layer 2 income."""
        a, p, s, c = base_case()
        base = cap_for(p, s)
        with_alt_income(p, s, c, streams=[stream("gig_platform", [6000000] * 6, factor=0.7, tier="B")])
        cap = cap_for(p, s, verified=ALL_FIELDS + ["alt_monthly_income_vnd"])
        self.assertEqual(cap["income"]["basis"], "LAYER1+LAYER2")
        # 6,000,000 x 0.7 = 4,200,000 recognised (series constant -> stressed = average)
        self.assertEqual(cap["income"]["stp_monthly_vnd"], base["income"]["stp_monthly_vnd"] + 4200000)
        self.assertEqual(cap["income"]["officer_monthly_vnd"], 16969073 + 4200000)
        self.assertEqual(cap["obligations"]["monthly_vnd"], base["obligations"]["monthly_vnd"], "absolute obligations unchanged")
        self.assertGreater(cap["limit_vnd"], base["limit_vnd"])
        self.assertEqual(cap["officer_basis"], "LAYER1+LAYER2_AVERAGE")
        # a stream flagged as a duplicate of Layer 1 (payouts into the HLB account) is never added
        s["alternative_data"]["streams"][0]["duplicate_of"] = "LAYER1"
        self.assertEqual(cap_for(p, s, verified=ALL_FIELDS + ["alt_monthly_income_vnd"])["income"]["officer_monthly_vnd"], 16969073)
        # Thin file: CIC 'no record' and no observable transactions -> verified zero obligations; dti_ratio not used.
        a, p, s, c = thin_file()
        with_alt_income(p, s, c, income=20000000, cv=0.1)
        cap = cap_for(p, s, verified=["dti_ratio", "alt_monthly_income_vnd", "alt_income_cv"])
        self.assertEqual((cap["income"]["basis"], cap["obligations"]["basis"], cap["obligations"]["monthly_vnd"]),
                         ("ALTERNATIVE_DATA", "CIC_NO_RECORD", 0))
        s["alternative_data"]["verified"] = False
        self.assertFalse(cap_for(p, s, verified=["dti_ratio", "alt_monthly_income_vnd"])["computable"])

    def test_classified_inflow_uses_type_factors(self):
        """Identified income types carry the same factor in Layer 1 and Layer 2: salary 1.0, platform 0.7, turnover 0.3,
        unidentified credits 0; own-account transfers excluded."""
        a, p, s, c = base_case()
        s["hlb_transactions"]["inflow_breakdown"] = {"salary": 10000000, "platform_payouts": 4000000,
                                                     "merchant_settlements": 2000000, "other_third_party_credits": 500000,
                                                     "own_account_transfers": 469073}
        inc = CAP.income_basis(p, s, ALL_FIELDS, POLICY)
        self.assertEqual(inc["layer1"]["average_monthly_vnd"], 10000000 + 2800000 + 600000)

    def test_affordability_per_tenure_includes_existing_smart_credit(self):
        a, p, s, c = base_case()
        inc = CAP.income_basis(p, s, ALL_FIELDS, POLICY)
        ob = CAP.obligations_basis(p, ALL_FIELDS, inc)
        free = CAP.affordability(12000000, AS_OF, inc, ob, 0, POLICY)
        self.assertEqual(free["affordable_tenures"], [6, 9, 12, 18, 24])
        busy = CAP.affordability(12000000, AS_OF, inc, ob, 2000000, POLICY)
        self.assertLess(len(busy["affordable_tenures"]), len(free["affordable_tenures"]))
        for o in free["offers"]:
            self.assertEqual(o["affordable"], o["dti_after"] <= 0.5)


class CheckoutRuleTests(unittest.TestCase):
    def ctx(self, **kw):
        a, p, s, c = base_case()
        inc = CAP.income_basis(p, s, ALL_FIELDS, POLICY)
        ob = CAP.obligations_basis(p, ALL_FIELDS, inc)
        base = {"amount_vnd": 12000000, "merchant": {"eligible": True}, "line": {"status": "ACTIVE", "limit_vnd": 27500000,
                "assessed_on_age_days": 0}, "exposure": {"outstanding_principal_vnd": 0, "held_vnd": 0},
                "delinquency": {}, "affordability": CAP.affordability(12000000, AS_OF, inc, ob, 0, POLICY),
                "system_available": True, "activation_pending": False}
        base.update(kw)
        return CR.decide(base, POLICY)

    def test_green(self):
        d = self.ctx()
        self.assertEqual((d["band"], d["reason_code"], d["recommended_tenure"]), ("GREEN", "R001", 6))
        self.assertTrue(all(c["result"] == "PASS" for c in d["checks"]))

    def test_holds_count_against_available(self):
        # 27.5M - 10M - 6M = 11.5M available for a 12M purchase: never a silent decline - split tender when a Pay Later part
        # >= the minimum is affordable, otherwise a limit review (both AMBER).
        d = self.ctx(exposure={"outstanding_principal_vnd": 10000000, "held_vnd": 6000000})
        self.assertEqual((d["band"], d["reason_code"], d["shortfall_vnd"]), ("AMBER", "R010-LIMREV", 500000))
        d = self.ctx(exposure={"outstanding_principal_vnd": 10000000, "held_vnd": 6000000}, split_affordable_vnd=11500000)
        self.assertEqual((d["band"], d["reason_code"], d["split"]), ("AMBER", "R010-SPLIT",
                                                                     {"pay_later_vnd": 11500000, "pay_now_vnd": 500000,
                                                                      "cause": "AVAILABLE_LIMIT", "above_product_max": False}))
        self.assertEqual((d["decision_class"], d["credit_outcome"]), ("CREDIT", "PARTIAL"))

    def test_mandatory_checks_and_fallbacks(self):
        d = self.ctx(merchant={"eligible": False, "reason": "x"})
        self.assertEqual((d["band"], d["reason_code"], d["decision_class"]), ("N/A", "R030-MER", "PRODUCT_SCOPE"))
        d = self.ctx(amount_vnd=980000)
        self.assertEqual((d["band"], d["reason_code"], d["credit_outcome"]), ("N/A", "R030-AMT", "NOT_ASSESSED"))
        d = self.ctx(system_available=False)
        self.assertEqual((d["band"], d["decision_class"]), ("AMBER", "OPERATIONAL"))
        d = self.ctx(delinquency={"arrears": True, "days_overdue": 2})
        self.assertEqual((d["reason_code"], d["credit_outcome"], d["payment_options"]), ("R020-ARR", "DECLINED", ["PAY_NOW", "PAY_LOAN"]))
        self.assertEqual(self.ctx(line={"status": None, "limit_vnd": 0})["band"], "NO_LINE")
        self.assertEqual(self.ctx(line={"status": None, "limit_vnd": 0}, activation_pending=True)["reason_code"], "R010-PENDING")
        self.assertEqual(self.ctx(line={"status": "ACTIVE", "limit_vnd": 27500000, "assessed_on_age_days": 91})["reason_code"],
                         "R010-STALE")
        d = self.ctx(identity={"hits": [], "fraud_confirmed": True})
        self.assertEqual((d["band"], d["reason_code"], d["payment_options"]), ("STOP", "HS-05", ["PAY_NOW"]))

    def test_above_product_maximum_splits_the_in_scope_part_only(self):
        d = self.ctx(amount_vnd=95000000, line={"status": "ACTIVE", "limit_vnd": 90100000, "assessed_on_age_days": 0},
                     split_affordable_vnd=95000000 - 1)
        self.assertEqual((d["reason_code"], d["split"]["pay_later_vnd"], d["split"]["pay_now_vnd"], d["split"]["cause"]),
                         ("R010-SPLIT", 90100000, 4900000, "PRODUCT_MAX"))
        d = self.ctx(amount_vnd=95000000, exposure={"outstanding_principal_vnd": 27000000, "held_vnd": 0})
        self.assertEqual((d["band"], d["reason_code"], d["customer_reason"]), ("N/A", "R030-AMT", "AMOUNT_ABOVE_MAX"))

    def test_line_status_maps_to_its_own_reason(self):
        """Audit low-priority item: a SUSPENDED line (after a default) is R020-DEF, not R020-ARR; FROZEN stays R020-ARR."""
        d = self.ctx(line={"status": "SUSPENDED", "limit_vnd": 27500000, "frozen_reason": "DEFAULT"})
        self.assertEqual((d["band"], d["reason_code"], d["customer_reason"]), ("RED", "R020-DEF", "DEFAULT"))
        d = self.ctx(line={"status": "FROZEN", "limit_vnd": 27500000, "frozen_reason": "ARREARS"})
        self.assertEqual((d["band"], d["reason_code"]), ("RED", "R020-ARR"))
        self.assertIn("PAY_NOW", d["alternatives"])

    def test_never_parks_the_shopper(self):
        """Every non-GREEN outcome offers Pay Now."""
        for kw in ({"system_available": False}, {"amount_vnd": 980000}, {"exposure": {"outstanding_principal_vnd": 27000000, "held_vnd": 0}}):
            self.assertIn("PAY_NOW", self.ctx(**kw)["alternatives"])


class QRTests(unittest.TestCase):
    def test_crc_reference_vector(self):
        self.assertEqual(QR.crc16_ccitt("123456789"), "29B1")  # CRC-16/CCITT-FALSE check value

    def test_roundtrip_dynamic_and_static(self):
        dyn = QR.parse(QR.build("970999", "1000012345", "5732", "ABC ELECTRONICS", "HO CHI MINH", 12000000, "A-0842"))
        self.assertEqual((dyn["initiation"], dyn["amount_vnd"], dyn["bill_number"], dyn["mcc"]), ("DYNAMIC", 12000000, "A-0842", "5732"))
        st = QR.parse(QR.build("970999", "1000089012", "5814", "CAFE", "HCM"))
        self.assertEqual((st["initiation"], st["amount_vnd"]), ("STATIC", None))

    def test_tampering_is_detected(self):
        good = QR.build("970999", "1000012345", "5732", "ABC", "HCM", 12000000, "A-1")
        bad = good.replace("540812000000", "540811000000")
        with self.assertRaises(QR.QRError) as e:
            QR.parse(bad)
        self.assertEqual(e.exception.code, "QR_CRC_MISMATCH")
        for junk in ("", "HLBQR|v1|x", "000201" + "x" * 30):
            with self.assertRaises(QR.QRError):
                QR.parse(junk)


class TermsTests(unittest.TestCase):
    def test_zero_interest_schedule(self):
        pol = copy.deepcopy(POLICY)
        pol["repayment_terms"]["annual_interest_rate_pct"] = 0
        t = T.build_terms(12000000, 6, AS_OF, pol)
        self.assertEqual(t["monthly_instalment_vnd"], 2000000)
        self.assertEqual(sum(i["amount_vnd"] for i in t["instalments"]), t["total_repayable_vnd"])
        self.assertEqual(t["first_due_date"], "2026-11-16")
        self.assertEqual(t["last_due_date"], "2027-04-16")

    def test_rounding_remainder_on_last_instalment(self):
        pol = copy.deepcopy(POLICY)
        pol["repayment_terms"]["annual_interest_rate_pct"] = 0
        t = T.build_terms(8500000, 9, AS_OF, pol)
        self.assertEqual(sum(i["amount_vnd"] for i in t["instalments"]), 8500000)
        self.assertEqual(len(t["instalments"]), 9)

    def test_configured_rate_is_applied_consistently(self):
        pol = copy.deepcopy(POLICY)
        pol["repayment_terms"]["annual_interest_rate_pct"] = 12
        t = T.build_terms(12000000, 12, AS_OF, pol)
        self.assertEqual(t["total_interest_vnd"], 1440000)
        self.assertEqual(sum(i["amount_vnd"] for i in t["instalments"]), 13440000)

    def test_unsupported_tenure(self):
        with self.assertRaises(ValueError):
            T.build_terms(12000000, 7, AS_OF, POLICY)


if __name__ == "__main__":
    unittest.main()


class PricingDisclosureTests(unittest.TestCase):
    def test_single_authoritative_calculation(self):
        t = T.build_terms(12000000, 6, AS_OF, POLICY)
        self.assertEqual((t["monthly_instalment_vnd"], t["total_interest_vnd"], t["total_repayable_vnd"]), (2120000, 720000, 12720000))
        self.assertEqual(sum(i["amount_vnd"] for i in t["instalments"]), t["total_repayable_vnd"])
        self.assertEqual(sum(i["principal_vnd"] for i in t["instalments"]), 12000000)
        self.assertEqual(t["effective_annual_rate_pct"], 22.3)   # matches the audit's computed value (B10)
        self.assertEqual(T.build_terms(12000000, 12, AS_OF, POLICY)["effective_annual_rate_pct"], 23.7)
        # Circular 39 Art. 14 has no late-payment fee: overdue interest within the Art. 13.4 caps is disclosed instead.
        self.assertEqual((t["late_fee_vnd"], t["overdue_principal_rate_pct"], t["late_interest_rate_pct"]), (0, 18.0, 10))

    def test_offers_match_full_terms(self):
        for o in T.tenure_offers(9900000, AS_OF, POLICY):
            full = T.build_terms(9900000, o["tenure_months"], AS_OF, POLICY)
            self.assertEqual(o["monthly_instalment_vnd"], full["monthly_instalment_vnd"])
            self.assertEqual(o["total_repayable_vnd"], full["total_repayable_vnd"])
