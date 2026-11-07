"""Credit-officer workbench (v6): judgement queue, consolidated case summary, single exposure view, approval-eligibility
guard, decisions and SLA. Officers decide; they never request or refresh data (Layer 1 / Layer 2 acquisition, retries and
re-runs are automatic - see acquisition.py and servicing.retry_pending_assessments).

Case assumption (NOT HLBVN's delegated-authority policy): every authorised officer-dashboard user has full decision
authority (approve, reduce, decline) over referred cases within the product scope VND 1,600,000 - 90,100,000 inclusive
(policy officer_controls.approval_authority_*). No profiles, bands, maker-checker or escalation tiers in the prototype.

Controls (audit B6 / R6; Proposal slide 8):
  * officer session comes from the server, never from a header; each session has its own reference;
  * approve, reject and confirm-fraud need a reason code AND a note;
  * approval is blocked unless EVERY eligibility check passes (case in Amber review, consent valid, no
    arrears/default, a RED hard stop has actually cleared, capacity computable and at least the minimum,
    limit within capacity, limit below the approval authority) - the amount alone never makes a case approvable;
  * Green (automatic) decisions are not touched here except for the 5% QA sample;
  * case age is tracked against an SLA.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..store.db import now_iso
from .base import APPLICANT_FIELDS, ServiceError, _j, vnd

DEMO_CONTROLS = ("force_next_payment_failure", "force_next_auth_failure", "core_system_unavailable", "cic_unavailable",
                 "alt_data_api_unavailable", "alt_data_incomplete", "device_anomaly")
PLAIN_REASON = {
    "R010-VER": "Invalid source data", "R010-EVD": "Waiting for customer data (Layer 2)", "R010-FLAG": "Risk flag",
    "R010-INCOME": "Waiting for customer data (Layer 2)", "R010-OBL": "Obligations unconfirmed",
    "R010-API": "Data source retrying (automatic)", "R010-ALT": "Layer 2 data outside automatic conditions",
    "R010-THIN": "No repayment-behaviour signal", "R010-RECHECK": "Re-evaluated in review",
    "R010-INCR": "Limit increase", "R020-CAP": "Obligations exceed ceiling", "R020-EVD": "Capacity unverifiable",
    "R020-INC": "Increase not supported", "R020-ARR": "Arrears", "R020-DEF": "Default",
    "R010-CAPX": "Stressed capacity short", "R010-FRD": "Identity / fraud review",
    "R010-REVIEW": "Line review", "R010-REINST": "Reinstatement after default", "R001-CO": "Counter-offer",
}
LINE_KINDS = ("LIMIT_INCREASE", "LINE_REVIEW", "LINE_REINSTATEMENT")


def _age_hours(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    return round((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600.0, 1)


class OfficerMixin:
    def _officer(self, officer_id: str) -> Dict[str, Any]:
        return self._officer_view(officer_id)

    # ------------------------------------------------------------ approval eligibility (single, configurable rule)
    def _authority(self) -> Dict[str, Any]:
        oc = self.policy["officer_controls"]
        mx, step = oc["approval_authority_max_vnd"], self.policy["affordability"]["officer_limit_step_vnd"]
        exclusive = oc["approval_authority_bound"] == "EXCLUSIVE"
        highest = (mx - 1 if exclusive else mx) // step * step
        return {"max_vnd": mx, "exclusive": exclusive, "highest_allowed_vnd": highest, "step_vnd": step,
                "stp_max_limit_vnd": self.policy["affordability"]["stp_max_limit_vnd"],
                "source": oc["approval_authority_source"], "rounding_vnd": step,
                "rule": ("approved limit < VND {:,}" if exclusive else "approved limit <= VND {:,}").format(mx)}

    def _within_authority(self, limit: int) -> bool:
        a = self._authority()
        return limit < a["max_vnd"] if a["exclusive"] else limit <= a["max_vnd"]

    def _approval_eligibility(self, app: Dict[str, Any], limit: Optional[int] = None,
                              reason_code: Optional[str] = None) -> Dict[str, Any]:
        """Every check must pass before an officer can approve. Also returned to the UI so the frontend shows
        exactly the same rule the backend enforces."""
        checks: List[Dict[str, Any]] = []

        def chk(cid: str, label: str, ok: bool, detail: str = "") -> None:
            checks.append({"id": cid, "label": label, "passed": bool(ok), "detail": detail})

        auth = self._authority()
        oc = self.policy["officer_controls"]
        aff = self.policy["affordability"]
        step = aff["officer_limit_step_vnd"]
        chk("EL-01", "Case is in Amber credit-officer review", app["credit_state"] == "MANUAL_REVIEW",
            "Current state: " + app["credit_state"])
        req = [c["id"] for c in self.policy["consent"]["activation_consents"] if c["required"]]
        consent_app = app["application_id"]
        line = self._line(app["customer_id"])
        if app["kind"] in LINE_KINDS:
            consent_app = (line or {}).get("activation_id") or consent_app
        rows = {r["kind"]: r for r in self.db.all("SELECT kind, granted, withdrawn_at FROM consents WHERE application_id = ?",
                                                    (consent_app,))}
        ok_consent = all(k in rows and rows[k]["granted"] == 1 and not rows[k]["withdrawn_at"] for k in req)
        chk("EL-02", "Required consents given and not withdrawn", ok_consent)
        dq = self._delinquency(app["customer_id"])
        chk("EL-03", "No arrears or default on Smart Credit (hard stop)", not dq.get("arrears") and not dq.get("default"),
            "Overdue %s day(s)" % dq.get("days_overdue") if dq.get("arrears") else "")
        cap = self._capacity(app) if app["input_snapshot_json"] else {}
        officer_max = cap.get("officer_max_limit_vnd")
        stp_limit = ((cap.get("stp") or {}).get("limit_vnd") if (cap.get("stp") or {}).get("meets_minimum") else 0) or 0
        red = str(app["reason_code"] or "").startswith("R020") or app["band"] == "RED"
        if red:
            # Appeal of a RED decision: approvable only if the RED condition itself has cleared. For R020-CAP that may be on
            # verified Layer 1 data OR validated Layer 2 income (audit F-02) - a human decides, never the engine.
            cleared = not dq.get("arrears") and not dq.get("default")
            if app["reason_code"] == "R020-CAP":
                cleared = bool(cap.get("computable") and officer_max)
            chk("EL-04", "Original RED policy reason has cleared on verified data", cleared, app["reason_code"] or "")
        else:
            chk("EL-04", "Not a RED / hard-stop case", app["band"] in ("AMBER", "GREEN", None), "Band: %s" % app["band"])
        chk("EL-05", "Repayment capacity computed from verified data", bool(cap.get("computable")),
            "" if cap.get("computable") else "Reason: %s" % (cap.get("reason") or "no assessment"))
        chk("EL-06", "Officer maximum (average verified income) at least the product minimum", bool(officer_max),
            "Automatic capacity VND %s; officer maximum VND %s (%s)" % (vnd(stp_limit), vnd(officer_max), cap.get("officer_basis"))
            if cap.get("computable") else "")
        lo = aff["min_limit_vnd"]
        hi = min(x for x in (officer_max or 0, auth["highest_allowed_vnd"],
                             (line or {}).get("limit_vnd") if app["kind"] in ("LINE_REVIEW", "LINE_REINSTATEMENT") and line else None)
                 if x is not None) if officer_max else 0
        hi = hi // step * step
        if app["kind"] == "LIMIT_INCREASE":
            lo = max(lo, (line or {}).get("limit_vnd", 0) + step)
            chk("EL-09", "Officer maximum supports a limit above the current limit (VND %s)" % vnd(lo - step), (officer_max or 0) >= lo,
                "Officer maximum VND %s" % vnd(officer_max))
        lo = -(-lo // step) * step
        routing_json = _j(app["routing_json"], {}) or {}
        fraud_case = app["reason_code"] == "R010-FRD" or bool((routing_json.get("identity") or {}).get("hits"))
        if limit is not None:
            chk("EL-08", "Limit within officer approval authority (%s)" % auth["rule"], self._within_authority(limit),
                "Case assumption, not HLBVN policy")
            chk("EL-07", "Limit within VND %s - VND %s on the VND %s grid" % (vnd(lo), vnd(hi), vnd(step)),
                lo <= limit <= hi and limit % step == 0)
            need: List[str] = []
            if limit > stp_limit:
                need = list(oc["above_stressed_capacity_requires"])
            if fraud_case:
                need = ["AP-06"]
            if app["kind"] == "LINE_REINSTATEMENT":
                need = ["RI-01"]
            if need:
                chk("EL-10", "Reason code fits the basis (%s)" % ", ".join(need), reason_code in need,
                    "above automatic capacity VND %s" % vnd(stp_limit) if limit > stp_limit and not fraud_case else
                    ("identity / fraud signal must be cleared explicitly" if fraud_case else "reinstatement"))
        eligible_case = all(c["passed"] for c in checks if c["id"] in ("EL-01", "EL-02", "EL-03", "EL-04", "EL-05", "EL-06", "EL-09"))
        return {"eligible": eligible_case and hi >= lo and all(c["passed"] for c in checks),
                "case_eligible": eligible_case, "checks": checks, "min_limit_vnd": lo, "step_vnd": step,
                "capacity_limit_vnd": stp_limit or None, "officer_max_limit_vnd": officer_max,
                "max_approvable_vnd": hi if eligible_case and hi >= lo else None,
                "stp_capacity_vnd": stp_limit, "above_stp_reason_codes": oc["above_stressed_capacity_requires"],
                "fraud_case": fraud_case, "authority": auth,
                "capacity_above_authority": bool(officer_max and not self._within_authority(officer_max))}

    # ------------------------------------------------------------ profile + single exposure view
    def _profile_panel(self, customer_id: str) -> Dict[str, Any]:
        cust = self._customer(customer_id)
        applicant = {k: v for k, v in _j(cust["applicant_json"], {}).items() if k in APPLICANT_FIELDS}
        applicant["internal_delinquency_flag"] = cust["internal_delinquency_flag"]
        ops = {"eq": lambda a, b: a == b, "lt": lambda a, b: a < b, "gt": lambda a, b: a > b}
        indicators = []
        for rr in self.policy["review_rules"]:
            v = applicant.get(rr["field"])
            hit = isinstance(v, (int, float)) and ops[rr["op"]](v, rr["value"])
            indicators.append({"rule_id": rr["rule_id"], "field": rr["field"], "value": v, "op": rr["op"],
                               "threshold": rr["value"], "source": rr["source"], "triggered": hit})
        self.refresh_servicing(customer_id)
        loans = [self._loan_view(l) for l in self.db.all("SELECT * FROM loans WHERE customer_id = ? ORDER BY created_at",
                                                          (customer_id,))]
        cs = self.credit_summary(customer_id)
        exposure = [{"product": "SMART_CREDIT", "label": "Smart Credit line", "limit_vnd": cs["limit_vnd"],
                     "outstanding_vnd": cs["outstanding_principal_vnd"], "held_vnd": cs["held_vnd"],
                     "monthly_instalment_vnd": cs["smart_credit_monthly_vnd"], "status": cs["line_status"]}]
        for p in cs["other_hlb_exposure"]:
            exposure.append({"product": p["product"], "label": p["label"], "limit_vnd": None,
                             "outstanding_vnd": p["outstanding_vnd"], "held_vnd": 0,
                             "monthly_instalment_vnd": p["monthly_instalment_vnd"], "status": "ACTIVE", "note": p.get("note")})
        return {"applicant": applicant, "risk_indicators": indicators, "credit": cs, "exposure_view": exposure,
                "total_hlb_exposure_vnd": cs["total_hlb_exposure_vnd"],
                "repayment_history": {"loans": [{k: l[k] for k in ("loan_ref", "merchant_name", "status", "principal_vnd",
                                                                   "outstanding_vnd", "paid_count", "instalment_count",
                                                                   "overdue_count")} for l in loans],
                                      "internal_delinquency_flag": cust["internal_delinquency_flag"],
                                      "on_time_streak": cust["on_time_streak"],
                                      "note": "Internal simulated history only - nothing is read from or reported to CIC."},
                "customer": {"customer_id": cust["customer_id"], "display_name": cust["display_name"],
                             "derived_from": cust["derived_from"], "fixture_adjustment": cust["fixture_adjustment"]}}

    # ------------------------------------------------------------ consolidated case summary (v6)
    def _case_summary(self, app: Dict[str, Any], cap: Dict[str, Any], elig: Dict[str, Any]) -> Dict[str, Any]:
        """Everything the officer needs to decide, in one place: verified evidence with lineage, data excluded by quality
        checks, risk indicators, the capacity calculation, the recommended limit, triggered rules and unresolved issues.
        Built from the automated assessment; the officer never has to collect anything."""
        rd = _j(app["readiness_json"], {}) or {}
        rt = _j(app["routing_json"], {}) or {}
        inp = _j(app["input_snapshot_json"], {}) or {}
        acq = _j(app["acquisition_json"], {}) or {}
        applicant, sources, vf = inp.get("applicant", {}), inp.get("sources", {}), set(inp.get("verified_fields", []))
        evidence = []
        for sid, scfg in self.policy["data_sources"].items():
            src = sources.get(sid) or {}
            for f in scfg["fields"]:
                if applicant.get(f) is None:
                    continue
                evidence.append({"field": f, "value": applicant.get(f), "source": sid, "layer": scfg.get("layer"),
                                 "verified": f in vf, "observed_at": src.get("observed_at"),
                                 "excluded": (rd.get("fields_not_used") or {}).get(f)})
        inc = (cap or {}).get("income") or {}
        streams = []
        if inc.get("layer1"):
            l1 = inc["layer1"]
            streams.append({"stream": "HLB account inflow (Layer 1)", "lineage": "hlb_transactions",
                            "classified": l1.get("inflow_classified"), "multiplier_type": l1.get("multiplier_type"),
                            "multiplier_factor": l1.get("multiplier_factor"), "average_vnd": l1["average_monthly_vnd"],
                            "stressed_vnd": l1["conservative_monthly_vnd"], "stp_usable": True,
                            "deduction_vnd": l1.get("layer1_deduction_vnd", 0)})
        for st in ((inc.get("layer2") or {}).get("streams") or []):
            streams.append({"stream": st["source"] + " (Layer 2)", "lineage": "API " + st["source"], "tier": st.get("tier"),
                            "factor": st.get("factor"), "average_vnd": st["average_monthly_vnd"],
                            "stressed_vnd": st["conservative_monthly_vnd"], "stp_usable": st.get("stp_usable"),
                            "dedup": st.get("dedup")})
        indicators = [{"rule_id": r["rule_id"], "message": r["message"]} for r in rd.get("review_reasons", [])]
        for info in self.policy.get("risk_indicators_info_only", []):
            v = applicant.get(info["field"])
            if isinstance(v, (int, float)) and v > info["value"]:
                indicators.append({"rule_id": info["rule_id"], "message": info["message"], "info_only": True})
        unresolved = []
        if rt.get("reason_code") in ("R010-FRD",):
            unresolved.append("Identity / fraud signal must be cleared or confirmed (AP-06 / FR-01).")
        if rt.get("reason_code") == "R010-FLAG":
            unresolved.append("Risk flag(s) need a judgement: " + ", ".join(i["rule_id"] for i in indicators if not i.get("info_only")))
        if rt.get("reason_code") == "R010-CAPX":
            unresolved.append("Stressed (25th-percentile) capacity is below the minimum after Layer 2; decide whether average "
                              "verified income justifies a limit (AP-01 / AP-05).")
        if rt.get("reason_code") == "R010-ALT":
            unresolved.append("Layer 2 income failed the stability/trend conditions: " + ", ".join(
                (self._layer2_state(app) or {}).get("unstable_sources") or []))
        if rt.get("reason_code") == "R010-THIN":
            unresolved.append("No repayment-behaviour signal (no CIC history, no bill-payment data).")
        if rt.get("reason_code") == "R010-VER":
            unresolved.append("Source values failed integrity checks: " + ", ".join(e["message"] for e in rd.get("validation_errors", [])))
        if str(app["reason_code"] or "").startswith("R020"):
            unresolved.append("Appeal of an automatic decline: approvable only if the decline reason has cleared (EL-04).")
        return {"recommended_limit_vnd": elig.get("max_approvable_vnd") if elig.get("case_eligible") else None,
                "automatic_capacity_vnd": elig.get("stp_capacity_vnd"), "officer_max_limit_vnd": elig.get("officer_max_limit_vnd"),
                "income_streams": streams, "obligations": (cap or {}).get("obligations"),
                "binding_constraint": (cap or {}).get("binding_constraint"), "evidence": evidence,
                "data_quality_events": rd.get("data_quality_events", []), "risk_indicators": indicators,
                "triggered_rules": [rt.get("reason_code")] + list(rt.get("secondary_codes") or []),
                "reasons": rt.get("reasons", []), "unresolved_issues": unresolved,
                "automated_data_operations": {"layer1_calls": len(acq.get("layer1", [])),
                                              "layer2_rounds": [{"outcome": r.get("outcome"), "consents": r.get("consents"),
                                                                 "results": [(x["source"], x["outcome"]) for x in r.get("results", [])]}
                                                                for r in acq.get("layer2", [])]},
                "note": "Assembled automatically. Officers decide; they do not collect data."}

    # ------------------------------------------------------------ queue
    def _queue_item(self, r: Dict[str, Any]) -> Dict[str, Any]:
        due = r["sla_due_at"]
        breached = bool(due and r["credit_state"] == "MANUAL_REVIEW" and datetime.fromisoformat(due) < datetime.now(timezone.utc))
        cap = _j(r["capacity_json"], {}) or {}
        acq = _j(r["acquisition_json"], {}) or {}
        return {"application_id": r["application_id"], "kind": r["kind"], "customer_id": r["customer_id"],
                "customer_name": r["display_name"], "credit_state": r["credit_state"], "band": r["band"],
                "reason_code": r["reason_code"], "reason_label": PLAIN_REASON.get(r["reason_code"], r["reason_code"]),
                "requested_limit_vnd": r["requested_limit_vnd"], "capacity_limit_vnd": cap.get("limit_vnd"),
                "capacity_basis": (cap.get("income") or {}).get("basis"),
                "alternative_data_rounds": len(acq.get("layer2", [])),
                "age_hours": _age_hours(r["review_started_at"] or r["created_at"]), "sla_due_at": due, "sla_breached": breached,
                "is_appeal": bool(r["appeal_count"]), "updated_at": r["updated_at"], "version": r["version"],
                "qa_status": r["qa_status"], **self._approvability(r)}

    def _approvability(self, r: Dict[str, Any]) -> Dict[str, Any]:
        """Shown on every queue item (audit F-19): can this case be approved as it stands, and up to how much?"""
        if r["credit_state"] != "MANUAL_REVIEW" or not r["input_snapshot_json"]:
            return {"approvable": None, "max_approvable_vnd": None}
        el = self._approval_eligibility(r)
        return {"approvable": bool(el["max_approvable_vnd"]), "max_approvable_vnd": el["max_approvable_vnd"]}

    def officer_queue(self, officer_id: str) -> Dict[str, Any]:
        officer = self._officer(officer_id)
        self.sweep_expired()
        rows = self.db.all("SELECT a.*, c.display_name FROM applications a JOIN customers c USING(customer_id) "
                           "WHERE a.origin != 'SEED' ORDER BY COALESCE(a.review_started_at, a.created_at), a.rowid")
        g: Dict[str, List[Dict[str, Any]]] = {"pending_review": [], "awaiting_customer": [], "qa_sample": [], "decided": []}
        for r in rows:
            item = self._queue_item(r)
            if r["qa_status"] == "PENDING":
                g["qa_sample"].append(item)
            key = {"MANUAL_REVIEW": "pending_review", "MORE_INFORMATION_REQUIRED": "awaiting_customer",
                   "ASSESSMENT_PENDING": "awaiting_customer"}.get(r["credit_state"], "decided")
            g[key].append(item)
        g["decided"] = sorted(g["decided"], key=lambda x: x["updated_at"], reverse=True)[:20]
        cfg = self.policy["officer_controls"]
        referred_today = self.db.one("SELECT COUNT(DISTINCT application_id) n FROM decisions WHERE to_state = 'MANUAL_REVIEW' "
                                     "AND ts LIKE ?", (datetime.now().astimezone().date().isoformat() + "%",))["n"]
        open_review = len(g["pending_review"])
        qa = self.db.all("SELECT qa_status, COUNT(*) n FROM applications WHERE qa_status IS NOT NULL GROUP BY qa_status")
        stats = {"open_reviews": open_review, "qa": {r["qa_status"]: r["n"] for r in qa},
                 "sla_breaches": sum(1 for x in g["pending_review"] if x["sla_breached"]),
                 "oldest_age_hours": max([x["age_hours"] or 0 for x in g["pending_review"]] or [0]),
                 "referred_today": referred_today, "daily_review_capacity": cfg["daily_review_capacity"],
                 "capacity_alert": referred_today > cfg["daily_review_capacity"] or open_review > cfg["daily_review_capacity"],
                 "sla_hours": cfg["sla_hours"], "business_date": self.as_of.isoformat(),
                 "capacity_note": cfg["daily_review_capacity_source"] + ". Proposal slide 9: expansion pauses if backlog "
                                  "breaches threshold - routing is never changed to clear a queue."}
        return {"officer": officer, "authority": self._authority(), "queues": g, "stats": stats}

    # ------------------------------------------------------------ case
    def officer_case(self, app_id: str, officer_id: str) -> Dict[str, Any]:
        officer = self._officer(officer_id)
        self.sweep_expired()
        app = self._app(app_id)
        consents = self.db.all("SELECT * FROM consents WHERE application_id = ? ORDER BY rowid", (app_id,))
        for c in consents:
            c["categories"] = _j(c.pop("categories_json"), [])
        decisions = self.db.all("SELECT * FROM decisions WHERE application_id = ? ORDER BY decision_id", (app_id,))
        for d in decisions:
            d["rule_ids"] = _j(d.pop("rule_ids_json"), [])
        cap = self._capacity(app) if app["input_snapshot_json"] else {}
        cs = self.credit_summary(app["customer_id"])
        state = app["credit_state"]
        cfg = self.policy["officer_controls"]
        elig = self._approval_eligibility(app)
        return {
            "application": {k: app[k] for k in ("application_id", "kind", "origin", "origin_transaction_id", "customer_id",
                                                 "requested_limit_vnd", "approved_limit_vnd", "credit_state", "band",
                                                 "reason_code", "readiness_status", "decided_by", "decision_reason_code",
                                                 "decision_note", "appeal_count", "reason", "assessment_count", "version",
                                                 "created_at", "updated_at", "review_started_at", "sla_due_at", "qa_status",
                                                 "qa_by", "qa_note")},
            "age_hours": _age_hours(app["review_started_at"] or app["created_at"]),
            "sla_breached": bool(app["sla_due_at"] and state == "MANUAL_REVIEW"
                                 and datetime.fromisoformat(app["sla_due_at"]) < datetime.now(timezone.utc)),
            "reason_label": PLAIN_REASON.get(app["reason_code"], app["reason_code"]),
            "permissions": {"can_review": state == "MANUAL_REVIEW", "officer": officer,
                            "current_line_limit_vnd": cs["limit_vnd"]},
            "eligibility": elig,
            "reason_codes": {"approve": cfg["approval_reason_codes"], "reject": cfg["rejection_reason_codes"],
                             "fraud": cfg["fraud_reason_codes"]},
            "governance": cfg["governance"],
            "case_summary": self._case_summary(app, cap, elig),
            "profile": self._profile_panel(app["customer_id"]),
            "readiness": _j(app["readiness_json"]), "routing": _j(app["routing_json"]), "capacity": cap,
            "data_acquisition": self.acquisition_view(app, "OFFICER"),
            "inputs": _j(app["input_snapshot_json"]), "declared": _j(app["declared_json"], {}) or {},
            "info_request": _j(app["info_request_json"]),
            "consents": consents, "decisions": decisions, "audit": self.audit_log(application_id=app_id),
            "policy_parameters": {"affordability": self.policy["affordability"], "officer_controls": cfg},
        }

    # ------------------------------------------------------------ decisions
    def officer_decide(self, app_id: str, officer_id: str, action: str, reason_code: Optional[str], note: Optional[str],
                       expected_version: Any, approved_limit: Any = None,
                       requested_sources: Optional[List[str]] = None) -> Dict[str, Any]:
        """approve | reject | confirm_fraud (identity / fraud review concluded as fraud -> STOP HS-05). v6: no data actions -
        officers never request or refresh data (automatic)."""
        self._officer(officer_id)
        cfg = self.policy["officer_controls"]
        if action in ("request_data", "refresh_data"):
            raise ServiceError(422, "DATA_ACTIONS_AUTOMATIC", "Data is acquired, retried and re-assessed automatically; officers "
                               "decide on the case summary.")
        if action not in ("approve", "reject", "confirm_fraud"):
            raise ServiceError(422, "INVALID_ACTION", "Unknown action.")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ServiceError(422, "VERSION_REQUIRED", "expected_version is required to prevent stale decisions.")
        note = (note or "").strip()
        if not note:
            raise ServiceError(422, "NOTE_REQUIRED", "A note is required for every officer decision.")
        codes = {"approve": cfg["approval_reason_codes"], "reject": cfg["rejection_reason_codes"],
                 "confirm_fraud": cfg["fraud_reason_codes"]}.get(action)
        if codes is not None and reason_code not in codes:
            raise ServiceError(422, "REASON_CODE_REQUIRED", "Choose a reason code.", {"allowed": sorted(codes)})
        self.refresh_servicing(self._app(app_id)["customer_id"])
        self.sweep_expired()
        with self._unit():
            app = self._app(app_id)
            if app["version"] != expected_version:
                raise ServiceError(409, "STALE_DECISION", "This case changed since you opened it. Reload before deciding.",
                                   {"current_version": app["version"]})
            if app["credit_state"] != "MANUAL_REVIEW":
                raise ServiceError(409, "INVALID_CREDIT_STATE", "Only cases in review can be decided.",
                                   {"current_state": app["credit_state"]})
            link = "#/smart-credit/app/" + app_id
            self.db.audit("OFFICER", officer_id, "OFFICER_ACTION", None, app_id,
                          details={"action": action, "reason_code": reason_code, "note": note,
                                   "authority": self._authority()["rule"], "auth": "DEMO officer-dashboard session"})
            if action == "approve":
                if not isinstance(approved_limit, int) or isinstance(approved_limit, bool):
                    raise ServiceError(422, "LIMIT_REQUIRED", "Enter the limit to approve.")
                el = self._approval_eligibility(app, approved_limit, reason_code)
                failed = [c for c in el["checks"] if not c["passed"]]
                if failed:
                    first = failed[0]
                    code = {"EL-07": "LIMIT_OUTSIDE_CAPACITY", "EL-08": "ABOVE_APPROVAL_AUTHORITY",
                            "EL-05": "CAPACITY_NOT_COMPUTABLE", "EL-10": "REASON_CODE_NOT_ALLOWED"}.get(first["id"],
                                                                                                    "NOT_ELIGIBLE_FOR_APPROVAL")
                    raise ServiceError(422, code, "Approval blocked: %s." % first["label"],
                                       {"failed_checks": failed, "max_approvable_vnd": el["max_approvable_vnd"],
                                        "authority": el["authority"]})
                cap = self._capacity(app)
                a = self._move_credit(app, "APPROVED", "OFFICER", officer_id, "OFFICER_APPROVE", note=note,
                                      reason_code=reason_code, expected_version=expected_version,
                                      extra={"decided_by": "OFFICER:" + officer_id, "decision_reason_code": reason_code,
                                             "decision_note": note, "capacity_json": json.dumps(cap)})
                self._open_line(a, approved_limit, "OFFICER", officer_id, {"capacity": cap, "band": "AMBER_APPROVED",
                                                                           "approved_by": officer_id, "reason_code": reason_code,
                                                                           "eligibility": [(c["id"], c["passed"]) for c in el["checks"]]})
                if el["fraud_case"]:
                    self.db.audit("OFFICER", officer_id, "IDENTITY_SIGNAL_CLEARED", None, app_id, details={"note": note,
                                  "production": "four-eyes required"})
                if a["requested_limit_vnd"] and approved_limit < a["requested_limit_vnd"]:
                    self.db.audit("OFFICER", officer_id, "COUNTER_OFFER", None, app_id,
                                  details={"requested_vnd": a["requested_limit_vnd"], "approved_vnd": approved_limit})
            elif action == "reject":
                self._move_credit(app, "REJECTED", "OFFICER", officer_id, "OFFICER_REJECT", note=note, reason_code=reason_code,
                                  expected_version=expected_version,
                                  extra={"decided_by": "OFFICER:" + officer_id, "decision_reason_code": reason_code,
                                         "decision_note": note})
                if app["kind"] in ("LINE_REINSTATEMENT", "LINE_REVIEW"):
                    self._set_line(app["customer_id"], "CLOSED", "OFFICER", officer_id,
                                   "%s rejected (%s): no new drawdowns" % (app["kind"].replace("_", " ").lower(), reason_code),
                                   "CREDIT_LINE_CLOSED")
                self.notify(app["customer_id"], "STATUS", "activation_rejected" if app["kind"] == "LIMIT_ACTIVATION"
                            else "limit_increase_rejected", {}, link, "officer-reject:" + app_id)
            elif action == "confirm_fraud":
                self._move_credit(app, "POLICY_STOP", "OFFICER", officer_id, "OFFICER_CONFIRM_FRAUD", ["HS-05"], note=note,
                                  reason_code="HS-05", expected_version=expected_version,
                                  extra={"decided_by": "OFFICER:" + officer_id, "decision_reason_code": reason_code,
                                         "decision_note": note, "band": "STOP", "reason_code": "HS-05"})
                self.db.run("UPDATE customers SET fraud_confirmed = 1 WHERE customer_id = ?", (app["customer_id"],))
                line = self._line(app["customer_id"])
                if line and line["status"] in ("ACTIVE", "FROZEN"):
                    self._set_line(app["customer_id"], "SUSPENDED", "OFFICER", officer_id, "HS-05 fraud confirmed", "CREDIT_LINE_SUSPENDED")
                self.db.audit("OFFICER", officer_id, "FRAUD_CONFIRMED", None, app_id, rule_ids=["HS-05"],
                              details={"reason_code": reason_code, "production": "four-eyes approval required"})
        if self._app(app_id)["credit_state"] == "APPROVED":
            self._offer_conversions(self._app(app_id)["customer_id"])  # after commit: conversion offers re-check servicing
        return self.officer_case(app_id, officer_id)

    def _offer_conversions(self, customer_id: str) -> None:
        """Tell the customer which recent Pay Now purchases can now be converted (within the window)."""
        for t in self.db.all("SELECT transaction_id FROM transactions WHERE customer_id = ? AND method = 'ACCOUNT' AND "
                             "state = 'PAYMENT_COMPLETED' AND converted_loan_ref IS NULL", (customer_id,)):
            try:
                if self.conversion_offer(t["transaction_id"], customer_id)["eligible"]:
                    self.notify(customer_id, "ACTION", "conversion_available", {}, "#/tx/" + t["transaction_id"],
                                "conv-offer:" + t["transaction_id"])
            except ServiceError:
                pass

    # ------------------------------------------------------------ 5% QA sample of GREEN decisions (Proposal)
    def officer_qa_review(self, app_id: str, officer_id: str, outcome: str, note: Optional[str]) -> Dict[str, Any]:
        """Post-decision QA of an automatic approval. DISAGREE is recorded and surfaced for routing-accuracy
        monitoring; it does not silently change the customer's limit (that needs a separate, audited action)."""
        self._officer(officer_id)
        if outcome not in ("AGREE", "DISAGREE"):
            raise ServiceError(422, "INVALID_OUTCOME", "Outcome must be AGREE or DISAGREE.")
        if not (note or "").strip():
            raise ServiceError(422, "NOTE_REQUIRED", "A note is required for a QA review.")
        with self._unit():
            app = self._app(app_id)
            if app["qa_status"] != "PENDING":
                raise ServiceError(409, "NOT_IN_QA_SAMPLE", "This case is not awaiting QA review.")
            self.db.run("UPDATE applications SET qa_status = ?, qa_by = ?, qa_note = ?, version = version + 1, updated_at = ? "
                        "WHERE application_id = ?", (outcome, officer_id, note[:500], now_iso(), app_id))
            self.db.run("INSERT INTO decisions(application_id, ts, actor_type, actor_id, action, from_state, to_state, note) "
                        "VALUES (?,?,?,?,?,?,?,?)", (app_id, now_iso(), "OFFICER", officer_id, "QA_" + outcome,
                                                     app["credit_state"], app["credit_state"], note[:500]))
            self.db.audit("OFFICER", officer_id, "GREEN_QA_REVIEWED", None, app_id, details={"outcome": outcome, "note": note})
        if outcome == "DISAGREE":
            # Remediation (audit low-priority item): open a line review for an officer; the limit is unchanged until decided.
            cs = self.credit_summary(app["customer_id"])
            if cs["line_status"] == "ACTIVE" and not cs["open_application"]:
                r = self.start_line_review(app["customer_id"], "QA", "QA:" + app_id, note="GREEN QA disagreement: " + note[:200])
                with self._unit():
                    self.db.audit("OFFICER", officer_id, "QA_REMEDIATION_OPENED", None, app_id,
                                  details={"line_review_id": r["application_id"]})
        return self.officer_case(app_id, officer_id)

    # ------------------------------------------------------------ presenter controls
    def set_demo_control(self, key: str, value: str, actor: str) -> Dict[str, Any]:
        allowed = DEMO_CONTROLS
        if key not in allowed or value not in ("0", "1"):
            raise ServiceError(422, "INVALID_CONTROL", "Unknown demo control.")
        with self._unit():
            self.db.set_control(key, value)
            self.db.audit("PRESENTER", actor, "DEMO_CONTROL_SET", details={key: value})
        return self.demo_controls()

    def demo_controls(self) -> Dict[str, Any]:
        return {"controls": {k: self.flag(k) for k in DEMO_CONTROLS}, "business_date": self.as_of.isoformat(),
                "demo_date": self.demo_date.isoformat()}

    def presenter_personas(self) -> List[Dict[str, Any]]:
        out = []
        for c in self.db.all("SELECT customer_id, display_name, persona_note, derived_from, fixture_adjustment FROM customers"):
            c["credit"] = {k: v for k, v in self.credit_summary(c["customer_id"]).items()
                           if k in ("line_status", "limit_vnd", "available_vnd", "held_vnd")}
            out.append(c)
        return out

    def presenter_open_checkouts(self) -> List[Dict[str, Any]]:
        """Unfinished checkouts (any customer) so the standalone navigation page can expire one."""
        from ..domain import states as S
        st = sorted(S.TX_EXPIRABLE)
        return self.db.all("SELECT t.transaction_id, t.customer_id, t.amount_vnd, t.state, m.name AS merchant_name "
                           "FROM transactions t JOIN merchants m USING(merchant_id) WHERE t.state IN (%s) "
                           "ORDER BY t.created_at DESC, t.rowid DESC" % ",".join("?" * len(st)), st)
