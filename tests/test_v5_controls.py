"""v5 controls not covered one-to-one by the audit matrix, plus seeded property tests over the pure engines."""
import copy
import json
import os
import random
import unittest
from datetime import date

from server.engine import capacity as CAP
from server.engine import checkout_rules as CR
from server.engine import policy_integrity as PI
from server.engine import routing
from tests.test_engine import POLICY, base_case, cap_for, run, thin_file
from tests.test_service import CONSENTS, Base, ServiceError, make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = POLICY["reason_codes"]["codes"]
AS_OF = date(2026, 10, 16)


class PolicyIntegrityTests(unittest.TestCase):
    def test_policy_change_without_version_bump_fails(self):
        lock = json.load(open(os.path.join(ROOT, "server", "policy", "policy.lock.json")))
        self.assertEqual(lock["policy_version"], POLICY["policy_version"], "lock records the current version")
        self.assertEqual(lock["sha256"], PI.policy_hash(POLICY),
                         "policy content changed: bump policy_version and run scripts/policy_lock.py")
        versions = [h["policy_version"] for h in lock["history"]]
        self.assertEqual(len(versions), len(set(versions)), "one hash per version")

    def test_every_decision_records_version_and_hash(self):
        svc, _ = make()
        self.addCleanup(lambda: svc.db.close())
        a = svc.start_activation("CUS-007", CONSENTS)
        rt = json.loads(svc._app(a["application_id"])["routing_json"])
        self.assertEqual((rt["policy_version"], rt["policy_hash"]), (POLICY["policy_version"], PI.policy_hash(POLICY)))
        line = json.loads(svc._line("CUS-007")["basis_json"])
        self.assertEqual(line["policy_hash"], PI.policy_hash(POLICY))

    def test_configuration_inside_legal_caps_and_mandates(self):
        self.assertEqual(PI.validate(POLICY), [])
        for path, value, needle in ((("loan_servicing", "overdue_interest", "overdue_principal_rate_multiplier"), 1.6, "150%"),
                                    (("loan_servicing", "overdue_interest", "late_interest_rate_pct_per_year"), 12, "10%/year"),
                                    (("affordability", "stp_max_limit_vnd"), 95000000, "officer approval authority")):
            p = copy.deepcopy(POLICY)
            node = p
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = value
            self.assertTrue(any(needle in e for e in PI.validate(p)), path)
        p = copy.deepcopy(POLICY)
        p["loan_servicing"]["late_fee_vnd"] = 50000
        self.assertTrue(any("Art. 14" in e for e in PI.validate(p)))
        self.assertNotIn("Decree 13/2023/ND-CP)", POLICY["consent"]["note"].replace("replacing Decree 13/2023/ND-CP", ""))


class CapacityControlTests(unittest.TestCase):
    def test_classified_inflow_excludes_self_transfers_loan_proceeds_refunds(self):
        a, p, s, c = base_case()
        s["hlb_transactions"]["inflow_breakdown"] = {"salary": 12000000, "own_account_transfers": 3000000,
                                                     "loan_proceeds": 1500000, "refunds_reversals": 469073}
        cap = cap_for(p, s)
        l1 = cap["income"]["layer1"]
        self.assertEqual((l1["average_monthly_vnd"], l1["classification"]["excluded_total_vnd"]), (12000000, 4969073))
        self.assertEqual(cap["obligations"]["monthly_vnd"], int(round(0.3145 * 16969073)), "DTI stays on the inflow it was measured on")
        self.assertLess(cap["limit_vnd"], cap_for(*base_case()[1:3])["limit_vnd"])

    def test_hlb_internal_obligations_are_deduplicated(self):
        a, p, s, c = base_case()
        inc = CAP.income_basis(p, s, ["monthly_inflow_vnd", "dti_ratio"], POLICY)
        dup = CAP.obligations_basis(p, ["monthly_inflow_vnd", "dti_ratio"], inc, [{"product": "PL", "monthly_instalment_vnd": 1650000,
                                                                                   "included_in_dti": True}])
        add = CAP.obligations_basis(p, ["monthly_inflow_vnd", "dti_ratio"], inc, [{"product": "PL", "monthly_instalment_vnd": 1650000}])
        self.assertEqual(add["monthly_vnd"] - dup["monthly_vnd"], 1650000)
        refreshed = CAP.refreshed_obligations(dup, [{"product": "PL", "monthly_instalment_vnd": 1650000},
                                                    {"product": "NEW_LOAN", "monthly_instalment_vnd": 800000}])
        self.assertEqual(refreshed["monthly_vnd"], dup["monthly_vnd"] + 800000, "a loan taken after assessment is added at checkout")

    def test_aggregate_digital_lending_limit_binds(self):
        a, p, s, c = base_case()
        inc = CAP.income_basis(p, s, ["monthly_inflow_vnd", "inflow_cv", "dti_ratio"], POLICY)
        ob = CAP.obligations_basis(p, ["monthly_inflow_vnd", "inflow_cv", "dti_ratio"], inc)
        cap = CAP.assign_limit(inc, ob, POLICY, aggregate_headroom_vnd=8000000)
        self.assertEqual((cap["binding_constraint"], cap["limit_vnd"]), ("AGGREGATE_DIGITAL_LIMIT", 8000000))


class ServiceControlTests(Base):
    def test_new_hlb_loan_after_activation_reduces_checkout_affordability(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.assertIn(6, v["pay_later_decision"]["affordable_tenures"])
        self.svc.cancel(tx, "CUS-001")
        self.svc.db.run("UPDATE customers SET existing_products_json = ? WHERE customer_id = 'CUS-001'",
                        (json.dumps([{"product": "HLB_PERSONAL_LOAN", "outstanding_vnd": 30000000, "monthly_instalment_vnd": 1500000}]),))
        tx, v = self.pay_later("CUS-001", "QR-261016-0915")  # 6.4M, would have been affordable at 3 months
        self.assertNotIn(3, v["pay_later_decision"]["affordable_tenures"], "refreshed obligations include the new HLB loan")

    def test_classified_persona_shows_excluded_own_transfers(self):
        a = self.svc.start_activation("CUS-004", CONSENTS)
        cap = self.svc.officer_case(a["application_id"], self.officer())["capacity"]
        cls = cap["income"]["layer1"]["classification"]
        self.assertEqual((cls["classified"], cls["excluded"]), (True, {"own_account_transfers": 1461654}))
        self.assertEqual([c["note"] for c in cap["obligations"]["components"] if c["component"] == "HLB_INTERNAL"],
                         ["Already in the bureau/observed figure (deduplicated)"])

    def test_device_anomaly_at_checkout_is_pay_now_only_never_decline(self):
        self.svc.set_demo_control("device_anomaly", "1", "t")
        tx, v = self.pay_later("CUS-001", "QR-261016-0915")
        d = v["pay_later_decision"]
        self.assertEqual((d["band"], d["reason_code"], d["alternatives"]), ("AMBER", "R010-FRD", ["PAY_NOW"]))
        self.assertIsNone(v["hold"])
        self.assertEqual(self.pay_now("CUS-001", tx)["transaction"]["state"], "PAYMENT_COMPLETED")
        self.assertIn("FRAUD_SIGNAL_RAISED", self.events(transaction_id=tx))

    def test_application_velocity_routes_to_fraud_review(self):
        for _ in range(POLICY["identity_fraud"]["max_applications_30d"]):
            a = self.svc.start_activation("CUS-002", CONSENTS)
            self.svc.cancel_application(a["application_id"], "CUS-002")
        a = self.svc.start_activation("CUS-002", CONSENTS)
        self.assertEqual(a["reason_code"], "R010-FRD")

    def test_aggregate_cap_limits_checkout_available(self):
        self.svc.db.run("UPDATE customers SET existing_products_json = ? WHERE customer_id = 'CUS-001'",
                        (json.dumps([{"product": "HLB_PERSONAL_LOAN", "outstanding_vnd": 92000000, "monthly_instalment_vnd": 0}]),))
        cs = self.svc.credit_summary("CUS-001")
        self.assertEqual(cs["available_vnd"], 8000000, "100M digital-lending cap - 92M other HLB outstanding")
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.assertEqual(v["pay_later_decision"]["reason_code"], "R010-SPLIT")
        self.assertLessEqual(v["pay_later_decision"]["split"]["pay_later_vnd"], 8000000)

    def test_inline_activation_consent_is_not_under_the_countdown(self):
        tx = self.scan("CUS-007", "QR-261016-1505")
        v = self.svc.pause_for_consent(tx, "CUS-007")
        self.assertTrue(v["timer_paused"])
        self.assertGreater(v["seconds_left"], POLICY["checkout"]["timeout_seconds"])
        self.clock.now = self.clock.now.replace(minute=10)  # 10 minutes later: still open
        v = self.svc.choose_pay_later(tx, "CUS-007", CONSENTS)
        self.assertEqual(v["transaction"]["state"], "TERMS_PENDING_CONFIRMATION")
        self.assertFalse(v["timer_paused"])
        self.assertLessEqual(v["seconds_left"], POLICY["checkout"]["timeout_seconds"], "a fresh countdown after consent")
        self.assertIn("CHECKOUT_TIMER_PAUSED_FOR_CONSENT", self.events(transaction_id=tx))

    def test_withdraw_one_layer2_source_purges_its_data(self):
        v = self.thin_file_with_layer2()
        app = v["application_id"]
        snap = self.svc._app(app)["info_submitted_json"]
        self.assertIn("alt_monthly_income_vnd", snap)
        v = self.svc.withdraw_alt_source(app, "CUS-005", "open_banking")
        sub = json.loads(self.svc._app(app)["info_submitted_json"])
        self.assertNotIn("alt_monthly_income_vnd", sub["fields"])
        self.assertEqual(sub["sources"]["alternative_data"]["streams"], [], "the Open Banking stream is purged")
        self.assertIn("alt_on_time_bill_ratio", sub["fields"], "the utility source (not withdrawn) is kept")
        cons = {c["kind"]: c["withdrawn_at"] for c in self.svc.db.all("SELECT kind, withdrawn_at FROM consents WHERE application_id = ?", (app,))}
        self.assertIsNotNone(cons["ALT_DATA:open_banking"])
        self.assertIsNone(cons["ALT_DATA:utility_bills"], "other sources untouched")
        self.assertIn("ALTERNATIVE_DATA_CONSENT_WITHDRAWN", self.events(application_id=app))
        with self.assertRaises(ServiceError):
            self.svc.withdraw_alt_source(app, "CUS-005", "open_banking")

    def test_green_qa_disagreement_opens_a_line_review(self):
        self.svc.db.run("UPDATE applications SET qa_status = 'PENDING' WHERE customer_id = 'CUS-001'")
        app = self.svc.db.one("SELECT application_id FROM applications WHERE customer_id = 'CUS-001'")["application_id"]
        self.svc.officer_qa_review(app, self.officer(), "DISAGREE", "Inflow looks inflated")
        lr = self.svc.db.one("SELECT * FROM applications WHERE customer_id = 'CUS-001' AND kind = 'LINE_REVIEW'")
        self.assertEqual((lr["origin"], lr["credit_state"], lr["reason_code"]), ("QA", "MANUAL_REVIEW", "R010-REVIEW"))
        self.assertEqual(self.svc.credit_summary("CUS-001")["line_status"], "ACTIVE", "limit unchanged until decided")
        self.assertIn("QA_REMEDIATION_OPENED", self.events(application_id=app))

    def test_conversion_is_recorded_as_purchase_financing(self):
        tx, v = self.pay_later("CUS-004", "QR-261016-2004", consents=CONSENTS)
        self.pay_now("CUS-004", tx)
        app = v["activation"]["application_id"]
        o = self.officer()
        self.svc.officer_decide(app, o, "approve", "AP-03", "ok", self.svc.officer_case(app, o)["application"]["version"], 15000000)
        offer = self.svc.conversion_offer(tx, "CUS-004")
        ch = self.auth("CUS-004", tx, "INSTALMENT_CONVERSION")
        self.svc.convert_to_instalments(tx, "CUS-004", offer["affordable_tenures"][0], True, ch["challenge_id"])
        ev = [e for e in self.svc.audit_log(transaction_id=tx) if e["event_type"] == "PURCHASE_CONVERTED_TO_INSTALMENTS"][0]
        self.assertEqual((ev["details"]["purpose"], ev["details"]["disbursement"], ev["details"]["reversed_vnd"]),
                         ("PURCHASE_FINANCING", "REVERSAL_OF_PURCHASE_DEBIT", 4500000))
        loan = self.svc.db.one("SELECT purpose FROM loans WHERE transaction_id = ?", (tx,))
        self.assertEqual(loan["purpose"], "PURCHASE_FINANCING:" + tx)

    def test_overdue_interest_never_exceeds_the_legal_caps(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.complete_pl("CUS-001", tx, 6)
        loan = self.svc.list_loans("CUS-001")["loans"][0]["loan_ref"]
        for days in (5, 30, 80):
            self.svc.set_business_date("t", new_date=(date(2026, 11, 16).toordinal() + days and
                                                      date.fromordinal(date(2026, 11, 16).toordinal() + days).isoformat()))
            inst = self.svc.get_loan(loan, "CUS-001")["instalments"][0]
            cap = inst["principal_vnd"] * 0.12 * 1.5 * days / 365 + inst["interest_fee_vnd"] * 0.10 * days / 365
            self.assertLessEqual(inst["overdue_interest_vnd"], round(cap) + 1, days)
            self.assertGreater(inst["overdue_interest_vnd"], 0)


class PropertyTests(unittest.TestCase):
    """Seeded random inputs (no extra dependency). 600 activation cases, 400 checkout cases."""

    def random_case(self, rnd):
        a, p, s, c = base_case()
        p.update(customer_profile=rnd.choice(POLICY["case_ranges"]["customer_profiles"]["allowed"]),
                 has_formal_cic_history=rnd.choice([0, 1]), has_observable_tx=rnd.choice([0, 1, 1, 1]),
                 monthly_inflow_vnd=rnd.choice([None, rnd.randint(500, 80000) * 1000]), inflow_cv=round(rnd.uniform(0, 0.8), 3),
                 dti_ratio=round(rnd.uniform(0, 0.9), 3), unified_credit_score=rnd.randint(350, 850),
                 on_time_bill_ratio=round(rnd.uniform(0.3, 1), 3), rapid_drain_flag=rnd.choice([0, 0, 0, 1]))
        if p["customer_profile"] == "Salaried, with credit history":
            p["has_formal_cic_history"] = 1
        if rnd.random() < 0.1:
            c["categories"] = ["hlb_transactions"]
        if rnd.random() < 0.15:
            s["hlb_transactions"]["verified"] = False
        return a, p, s, c

    def test_activation_properties(self):
        rnd = random.Random(20261106)
        for i in range(600):
            a, p, s, c = self.random_case(rnd)
            res = run(a, p, s, c)
            cap = cap_for(p, s)
            ctx = {"capacity": cap, "requested_limit_vnd": rnd.choice([None, rnd.randint(16, 900) * 100000]),
                   "layer2_attempted": rnd.choice([True, False]), "layer2_outcome": rnd.choice([None, "DECLINED", "UNAVAILABLE"]),
                   "alternative_data_used": False, "in_human_review": rnd.random() < 0.1,
                   "declared_fields": ["customer_profile"] if rnd.random() < 0.05 else [], "layer2_income_source_available": True}
            d = routing.decide(res, ctx, POLICY)
            # every outcome carries exactly one primary code that exists, plus its triggered rule ids
            self.assertIn(d["reason_code"], CODES, i)
            self.assertEqual(CODES[d["reason_code"]]["band"], d["band"], (i, d["reason_code"]))
            self.assertIsInstance(d["rule_ids"], list)
            if d["band"] == "GREEN":
                lim = d["approved_limit_vnd"]
                self.assertEqual(res["status"], "DATA_READY", i)
                self.assertFalse(ctx["declared_fields"] or ctx["in_human_review"], i)
                self.assertTrue(POLICY["affordability"]["min_limit_vnd"] <= lim <= POLICY["affordability"]["stp_max_limit_vnd"], i)
                self.assertLessEqual(lim, cap["stp"]["limit_vnd"], i)
                if ctx["requested_limit_vnd"]:
                    self.assertLessEqual(lim, ctx["requested_limit_vnd"], i)
                    self.assertEqual(d["reason_code"] == "R001-CO", lim < ctx["requested_limit_vnd"], i)
            if d["band"] == "RED" and d["reason_code"] == "R020-CAP":
                self.assertTrue(cap["verified_obligation_breach"], i)
                self.assertNotEqual(ctx["layer2_outcome"], "UNAVAILABLE", "outage never declines")
                self.assertTrue(d.get("appeal_allowed"), i)
            if ctx["in_human_review"] and d["band"] not in ("STOP", "RED"):
                self.assertEqual(d["reason_code"], "R010-RECHECK", i)
            if cap.get("computable"):
                self.assertGreaterEqual(cap["officer"]["raw_limit_vnd"], (cap["stp"] or {}).get("raw_limit_vnd", 0), i)

    def test_no_green_on_layer2_or_declared_or_unverified_data(self):
        rnd = random.Random(7)
        for i in range(200):
            a, p, s, c = thin_file()
            p.update(alt_monthly_income_vnd=rnd.randint(2, 60) * 1000000, alt_income_cv=round(rnd.uniform(0, 0.5), 2))
            s["alternative_data"] = {"verified": True, "observed_at": "2026-10-15", "period_months": 6, "source_ids": ["x"]}
            c["categories"].append("alternative_data")
            cap = cap_for(p, s, verified=["dti_ratio", "alt_monthly_income_vnd", "alt_income_cv"])
            d = routing.decide(run(a, p, s, c), {"capacity": cap, "layer2_attempted": True, "alternative_data_used": True}, POLICY)
            self.assertNotEqual(d["band"], "GREEN", i)
            self.assertNotEqual(d["band"], "RED", i)

    def test_checkout_properties(self):
        rnd = random.Random(99)
        a, p, s, c = base_case()
        inc = CAP.income_basis(p, s, ["monthly_inflow_vnd", "inflow_cv", "dti_ratio"], POLICY)
        ob = CAP.obligations_basis(p, ["monthly_inflow_vnd", "inflow_cv", "dti_ratio"], inc)
        for i in range(400):
            limit = rnd.randint(16, 500) * 100000
            out = rnd.randint(0, limit // 100000) * 100000
            held = rnd.randint(0, max(0, (limit - out) // 100000)) * 100000
            amount = rnd.randint(16, 901) * 100000
            sc = rnd.randint(0, 8) * 250000
            avail = limit - out - held
            split_ok = CAP.max_affordable_principal(inc, ob, sc, AS_OF, POLICY, min(avail, amount - 1)) if avail > 0 else 0
            d = CR.decide({"amount_vnd": amount, "merchant": {"eligible": True}, "delinquency": {}, "system_available": True,
                           "line": {"status": "ACTIVE", "limit_vnd": limit, "assessed_on_age_days": 1},
                           "exposure": {"outstanding_principal_vnd": out, "held_vnd": held},
                           "affordability": CAP.affordability(amount, AS_OF, inc, ob, sc, POLICY), "split_affordable_vnd": split_ok},
                          POLICY)
            self.assertIn(d["reason_code"], CODES, i)
            self.assertGreaterEqual(d.get("available_vnd", 0), 0, "available never negative")
            if d["band"] == "GREEN":
                self.assertLessEqual(amount, avail, i)
                self.assertTrue(d["affordable_tenures"], i)
            if d["reason_code"] == "R010-SPLIT":
                sp = d["split"]
                self.assertEqual(sp["pay_later_vnd"] + sp["pay_now_vnd"], amount, i)
                self.assertTrue(POLICY["case_ranges"]["requested_loan_vnd"]["min"] <= sp["pay_later_vnd"] <= avail, i)
                self.assertTrue(CAP.affordable_tenures_fast(sp["pay_later_vnd"], inc, ob, sc, POLICY), i)
            if d["band"] != "GREEN":
                self.assertIn("PAY_NOW", d["alternatives"], "never parks the shopper")


if __name__ == "__main__":
    unittest.main()
