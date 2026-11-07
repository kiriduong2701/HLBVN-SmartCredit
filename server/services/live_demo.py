"""Live judge demo: create ONE hypothetical customer from numbers typed on the navigation page during the pitch, plus a
merchant order (dynamic QR) for the purchase amount, then follow that customer's result in the real customer app.
(The Live Scenario Simulator in scenario_lab.py reuses FIELDS / _validate_live / create_live_customer inside a sandbox.)

Nothing here makes or bends a credit decision. The customer created here goes through exactly the same journey as every
seeded persona: the customer app (Pay Later -> legal data step -> Layer 0/1 -> readiness -> capacity -> routing), checkout
rules, Layer 2 consent and the officer workbench. This module only (a) validates the typed inputs, (b) writes a fictional
customer record whose Layer 1 sources are marked verified and fresh as of the simulated business date, and (c) reports
progress back to the presenter in plain language.

Input bounds: documented ranges where the case defines them (credit score 350-850, ratios 0-1, segment labels); where the
data definitions give no upper bound (inflow, CV, DTI) a plausibility bound wider than the dataset's observed range is used
for input hygiene only - it is NOT a credit threshold.
"""
import json
from typing import Any, Dict, List, Optional

from ..engine import qr as QR
from ..store.db import now_iso
from .base import ServiceError, _j

LIVE_PREFIX = "CUS-J"
SEGMENTS = ["Salaried, with credit history", "Salaried, no credit history", "Gig / platform workers",
            "First-time borrowers, digitally active", "Online merchants (MSME)"]
# Layer 2 (Open Banking) income category implied by the segment, used only to simulate the other-bank record when the
# presenter says the customer has no observable HLB transactions (same recognition factors as everywhere else).
SEGMENT_INCOME_CATEGORY = {"Salaried, with credit history": "salary", "Salaried, no credit history": "salary",
                           "First-time borrowers, digitally active": "salary", "Gig / platform workers": "platform_payouts",
                           "Online merchants (MSME)": "merchant_settlements"}
LIVE_MERCHANTS = ["M-ABC", "M-MKM", "M-SHF", "M-SLA", "M-FPL"]  # partner merchants with an eligible MCC

# name: (kind, min, max, required, default)
FIELDS: Dict[str, Dict[str, Any]] = {
    "display_name": {"kind": "text", "max_len": 40, "required": False},
    "customer_profile": {"kind": "enum", "allowed": SEGMENTS, "required": True},
    "monthly_inflow_vnd": {"kind": "int", "min": 1_000_000, "max": 500_000_000, "required": True,
                           "observed": [5_441_227, 130_994_130]},
    "inflow_cv": {"kind": "ratio", "min": 0.0, "max": 1.0, "required": True, "observed": [0.045, 0.70]},
    "has_formal_cic_history": {"kind": "flag", "required": True},
    "unified_credit_score": {"kind": "int", "min": 350, "max": 850, "required": True, "observed": [350, 768]},
    "dti_ratio": {"kind": "ratio", "min": 0.0, "max": 1.0, "required": True, "observed": [0.1219, 0.5284]},
    "on_time_bill_ratio": {"kind": "ratio", "min": 0.0, "max": 1.0, "required": True, "observed": [0.2785, 0.9988]},
    "purchase_amount_vnd": {"kind": "int", "min": 10_000, "max": 300_000_000, "required": True},
    "merchant_id": {"kind": "enum", "allowed": LIVE_MERCHANTS, "required": True},
    "has_observable_tx": {"kind": "flag", "required": False, "default": 1},
    "rapid_drain_flag": {"kind": "flag", "required": False, "default": 0},
    "regular_weekly_pattern": {"kind": "flag", "required": False, "default": 1},
    "e_wallet_topups_pm": {"kind": "int", "min": 0, "max": 100, "required": False, "default": 6, "observed": [0, 33]},
    "casa_balance_vnd": {"kind": "int", "min": 0, "max": 2_000_000_000, "required": False,
                         "observed": [119_497, 203_719_964]},
}

_Z = [-1.34, 0.45, 1.12, -0.67, 1.0, -0.56]  # fixed shape for a simulated 6-month series (normalised below)


def _initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").split() if p]
    return ((parts[0][0] + parts[-1][0]) if len(parts) > 1 else (parts[0][:2] if parts else "KH")).upper()


def _series(mean: float, cv: float) -> List[int]:
    m = sum(_Z) / len(_Z)
    sd = (sum((z - m) ** 2 for z in _Z) / len(_Z)) ** 0.5
    zs = [(z - m) / sd for z in _Z]
    return [max(0, int(round(mean * (1 + cv * z), -3))) for z in zs]


class LiveDemoMixin:
    # ------------------------------------------------------------ config for the navigation page
    def live_demo_config(self) -> Dict[str, Any]:
        merchants = {m["merchant_id"]: m for m in self.fixtures["merchants"]}
        # Starting points (personas and failure scenarios) now come from scenario_lab (GET /api/presenter/scenario-lab).
        return {"fields": FIELDS, "segments": SEGMENTS,
                "merchants": [{"merchant_id": i, "name": merchants[i]["name"], "category": merchants[i]["category"]}
                              for i in LIVE_MERCHANTS],
                "product_scope": {"min_vnd": self.policy["affordability"]["min_limit_vnd"],
                                  "max_vnd": self.policy["affordability"]["product_max_limit_vnd"]},
                "recent": self.live_customers()}

    def live_customers(self) -> List[Dict[str, Any]]:
        rows = self.db.all("SELECT customer_id, display_name FROM customers WHERE customer_id LIKE ? ORDER BY rowid DESC LIMIT 5",
                           (LIVE_PREFIX + "%",))
        return rows

    # ------------------------------------------------------------ validation (same spec is enforced in the browser)
    def _validate_live(self, body: Dict[str, Any]) -> Dict[str, Any]:
        errors: Dict[str, Dict[str, Any]] = {}
        out: Dict[str, Any] = {}
        for name, spec in FIELDS.items():
            v = body.get(name)
            if v is None or v == "":
                if spec.get("required"):
                    errors[name] = {"code": "REQUIRED"}
                elif "default" in spec:
                    out[name] = spec["default"]
                continue
            kind = spec["kind"]
            if kind == "text":
                if not isinstance(v, str) or len(v.strip()) > spec["max_len"]:
                    errors[name] = {"code": "TOO_LONG", "max": spec["max_len"]}
                else:
                    out[name] = v.strip()
            elif kind == "enum":
                if v not in spec["allowed"]:
                    errors[name] = {"code": "NOT_ALLOWED"}
                else:
                    out[name] = v
            elif kind == "flag":
                if v not in (0, 1) or isinstance(v, bool):
                    errors[name] = {"code": "NOT_ALLOWED"}
                else:
                    out[name] = v
            elif kind == "int":
                if isinstance(v, bool) or not isinstance(v, int):
                    errors[name] = {"code": "NOT_INTEGER"}
                elif not spec["min"] <= v <= spec["max"]:
                    errors[name] = {"code": "OUT_OF_RANGE", "min": spec["min"], "max": spec["max"]}
                else:
                    out[name] = v
            elif kind == "ratio":
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    errors[name] = {"code": "NOT_NUMBER"}
                elif not spec["min"] <= float(v) <= spec["max"]:
                    errors[name] = {"code": "OUT_OF_RANGE", "min": spec["min"], "max": spec["max"]}
                else:
                    out[name] = round(float(v), 4)
        if errors:
            raise ServiceError(422, "INVALID_LIVE_INPUT", "Some inputs need fixing.", {"fields": errors})
        if "casa_balance_vnd" not in out:
            out["casa_balance_vnd"] = int(round(out["monthly_inflow_vnd"] * 0.4, -3))
        return out

    # ------------------------------------------------------------ create
    def create_live_customer(self, body: Dict[str, Any], actor: str) -> Dict[str, Any]:
        inp = self._validate_live(body or {})
        merchant = next(m for m in self.fixtures["merchants"] if m["merchant_id"] == inp["merchant_id"])
        as_of = self.as_of
        observed = as_of.isoformat()
        with self._unit():
            seq = self.db.next_id("live_customer")
            cid = "%s%02d" % (LIVE_PREFIX, seq)
            name = inp.get("display_name") or ("Khách hàng demo %d" % seq)
            tx_obs = inp["has_observable_tx"] == 1
            applicant = {"customer_profile": inp["customer_profile"], "has_formal_cic_history": inp["has_formal_cic_history"],
                         "has_observable_tx": inp["has_observable_tx"], "unified_credit_score": inp["unified_credit_score"],
                         "dti_ratio": inp["dti_ratio"], "e_wallet_topups_pm": inp["e_wallet_topups_pm"],
                         "rapid_drain_flag": inp["rapid_drain_flag"]}
            alternative: Dict[str, Any] = {}
            if tx_obs:
                applicant.update(monthly_inflow_vnd=inp["monthly_inflow_vnd"], inflow_cv=inp["inflow_cv"],
                                 casa_balance_vnd=inp["casa_balance_vnd"], regular_weekly_pattern=inp["regular_weekly_pattern"],
                                 on_time_bill_ratio=inp["on_time_bill_ratio"])
            else:
                # No observable HLB transactions: transaction-dependent fields stay empty (policy transaction_dependent_fields).
                # The typed income becomes a SIMULATED other-bank (Open Banking) record and the typed bill ratio a SIMULATED
                # utility record; both are used only if the engine asks for Layer 2 and the customer ticks the source.
                applicant.update(monthly_inflow_vnd=None, inflow_cv=None, casa_balance_vnd=None, regular_weekly_pattern=None,
                                 on_time_bill_ratio=None)
                series = _series(inp["monthly_inflow_vnd"], inp["inflow_cv"])
                alternative["open_banking"] = {"monthly": series, "subject_match": True, "match_on": ["national_id", "full_name"],
                                               "provider": "Other bank via Open API (simulated from live input)",
                                               "credits_by_category": {SEGMENT_INCOME_CATEGORY[inp["customer_profile"]]: series}}
                alternative["utility_bills"] = {"on_time": int(round(inp["on_time_bill_ratio"] * 12)), "of": 12,
                                                "subject_match": True, "match_on": ["national_id", "full_name"],
                                                "provider": "Utility aggregator (simulated from live input)"}
            sources = {"hlb_transactions": {"verified": True, "observed_at": observed, "period_months": 6,
                                            "method": "SIMULATED_LIVE_INPUT"},
                       "credit_information": {"verified": True, "observed_at": observed, "method": "SIMULATED_LIVE_INPUT"},
                       "e_wallet_activity": {"verified": True, "observed_at": observed, "method": "SIMULATED_LIVE_INPUT"}}
            identity = {"national_id": "0792%08d" % (900000 + seq), "id_type": "CCCD (chip)", "ekyc_status": "VERIFIED",
                        "cccd_match": True, "ekyc_method": "NFC chip read + face match (SIMULATED)", "ekyc_on": observed,
                        "note": "Fictional number for a hypothetical customer typed in during the pitch."}
            balance = inp["purchase_amount_vnd"] + 5_000_000  # enough to pay now or pay the split remainder (fictional)
            self.db.run("INSERT INTO customers(customer_id, display_name, initials, account_masked, balance_vnd, derived_from, "
                        "fixture_adjustment, persona_note, applicant_json, sources_json, alternative_data_json, "
                        "existing_products_json, identity_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, name, _initials(name), "•••• %04d" % (9000 + seq), balance, "LIVE_INPUT", None,
                         "Hypothetical customer entered live on the navigation page.", json.dumps(applicant),
                         json.dumps(sources), json.dumps(alternative), "[]", json.dumps(identity)))
            qr_ref = "QR-LIVE-%02d" % seq
            bill = "J-%04d" % seq
            payload = QR.build(merchant["acquirer_bin"], merchant["merchant_account"], merchant["mcc"], merchant["qr_name"],
                               merchant["qr_city"], inp["purchase_amount_vnd"], bill)
            desc = "Đơn hàng demo · %s" % name
            self.db.run("INSERT INTO qr_codes VALUES (?,?,?,?,?,?,?,?)",
                        (qr_ref, merchant["merchant_id"], "DYNAMIC", inp["purchase_amount_vnd"], desc, bill, payload, 0))
            self.db.audit("PRESENTER", actor, "LIVE_DEMO_CUSTOMER_CREATED",
                          details={"customer_id": cid, "qr_ref": qr_ref, "inputs": inp, "applicant": applicant,
                                   "simulated_layer2_records": sorted(alternative),
                                   "note": "Hypothetical customer; decided by the same engine as every persona."})
        return {"customer_id": cid, "display_name": name, "qr_ref": qr_ref, "purchase_amount_vnd": inp["purchase_amount_vnd"],
                "merchant_name": merchant["name"], "status": self.live_customer_status(cid)}

    # ------------------------------------------------------------ progress, in plain stages
    def live_customer_status(self, customer_id: str) -> Dict[str, Any]:
        if not customer_id.startswith(LIVE_PREFIX):
            raise ServiceError(404, "NOT_FOUND", "Not a live demo customer.")
        cust = self._customer(customer_id)
        self.refresh_servicing(customer_id)
        app = self.db.one("SELECT * FROM applications WHERE customer_id = ? ORDER BY rowid DESC LIMIT 1", (customer_id,))
        tx = self.db.one("SELECT * FROM transactions WHERE customer_id = ? ORDER BY rowid DESC LIMIT 1", (customer_id,))
        cs = self.credit_summary(customer_id)
        a = None
        if app:
            rt = _j(app["routing_json"], {}) or {}
            cap = _j(app["capacity_json"], {}) or {}
            a = {"application_id": app["application_id"], "credit_state": app["credit_state"], "band": app["band"],
                 "reason_code": app["reason_code"], "reason": (rt.get("reasons") or [{}])[0].get("message"),
                 "approved_limit_vnd": app["approved_limit_vnd"] or rt.get("approved_limit_vnd"),
                 "capacity_limit_vnd": cap.get("limit_vnd"), "binding_constraint": cap.get("binding_constraint"),
                 "decided_by": app["decided_by"]}
        t = None
        if tx:
            dec = _j(tx["pl_decision_json"], {}) or {}
            t = {"transaction_id": tx["transaction_id"], "state": tx["state"], "method": tx["method"],
                 "amount_vnd": tx["amount_vnd"], "pay_later_vnd": tx["pay_later_amount_vnd"], "pay_now_vnd": tx["pay_now_amount_vnd"],
                 "checkout_band": dec.get("band"), "checkout_reason_code": dec.get("reason_code"),
                 "split": dec.get("split")}
        stage = "WAITING_CUSTOMER"
        if a:
            stage = {"APPROVED": "APPROVED", "OFFER_PENDING": "COUNTER_OFFER", "MANUAL_REVIEW": "OFFICER_REVIEW",
                     "MORE_INFORMATION_REQUIRED": "DATA_REQUESTED", "DECLINED": "DECLINED", "REJECTED": "OFFICER_REJECTED",
                     "POLICY_STOP": "STOPPED", "CANCELLED": "CANCELLED"}.get(a["credit_state"], "ASSESSING")
            if a["credit_state"] == "ASSESSMENT_PENDING" and a["reason_code"] == "R010-API":
                stage = "RETRYING"
            if a["credit_state"] == "APPROVED" and str(a["decided_by"] or "").startswith("OFFICER"):
                stage = "OFFICER_APPROVED"
        if t and t["state"] == "PAYMENT_COMPLETED":
            stage = "PAID"
        return {"customer_id": customer_id, "display_name": cust["display_name"], "stage": stage, "application": a,
                "checkout": t, "line": {k: cs[k] for k in ("line_status", "limit_vnd", "available_vnd")},
                "business_date": self.as_of.isoformat(), "checked_at": now_iso()}
