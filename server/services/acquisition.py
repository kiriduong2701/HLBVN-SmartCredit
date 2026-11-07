"""API-based data acquisition for Smart Credit (replaces manual document uploads).

Layer 1 - every variable in the case dataset plus HLB internal data, retrieved automatically (HLB profile, transactions
  & inflows, e-wallet activity variable, HLB exposure & Smart Credit history, CIC report). No per-variable consent; the
  legally mandated data-processing step (DATA_PROCESSING) covers it.
Layer 2 - external API sources (Open Banking, platform earnings, marketplace payouts, e-invoices, utility/telecom
  history), ONLY when Layer 1 is absent or insufficient. The SYSTEM decides when, which sources and in what order
  (policy data_acquisition.layer2_plan), asks an optional unticked consent per source, calls consented sources in
  priority order and stops once the gap is closed, validates (AV-01..AV-07), de-duplicates, and re-runs the rules.
  Outages are retried automatically. Officers never request or refresh data.

EVERY adapter here is a deterministic SIMULATION (labelled `simulated: true`).
No request leaves this server and no real CIC, core-banking, Open API, tax,
marketplace, platform, utility or e-wallet connection exists. Response shapes,
latencies, timeouts (with one retry) and validation are illustrative; real
integrations need provider agreements and HLBVN Legal sign-off (see policy
data_acquisition.layer2_sources[*].legal_note).
"""
import hashlib
import json
import statistics
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..engine import readiness as R
from ..store.db import now_iso
from .base import ServiceError, _j



def _latency(*parts: str) -> int:
    """Deterministic, illustrative latency in ms (no real waiting)."""
    return 120 + int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16) % 380


def _request_id(*parts: str) -> str:
    return "SIM-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:10].upper()


class AcquisitionMixin:
    # ================================================================ Layer 1
    def _layer1_log(self, cust: Dict[str, Any], applicant: Dict[str, Any], sources: Dict[str, Any],
                    app_id: str) -> List[Dict[str, Any]]:
        """Record what each Layer 1 integration returned (simulated). The data itself comes from the
        persona fixtures; this log makes the acquisition explicit and auditable."""
        cfg = {s["id"]: s for s in self.policy["data_acquisition"]["layer1"]}
        timeout = self.policy["data_acquisition"]["timeout_ms"]
        out: List[Dict[str, Any]] = []

        def entry(sid: str, status: str, summary: str, attempts: int = 1, fields: Optional[List[str]] = None) -> None:
            c = cfg[sid]
            out.append({"layer": 1, "source": sid, "label": c["label"], "provider": c["provider"],
                        "legal_basis": c["legal_basis"], "status": status, "summary": summary, "attempts": attempts,
                        "latency_ms": timeout if status == "UNAVAILABLE" else _latency(sid, app_id),
                        "request_id": _request_id(sid, app_id, str(self.as_of)), "fields_returned": fields or [],
                        "simulated": True})

        prof = applicant.get("customer_profile")
        entry("hlb_core_profile", "OK" if prof else "PARTIAL",
              "Customer profile and account status retrieved." + ("" if prof else " Employment profile is missing."),
              fields=["account_status"] + (["customer_profile"] if prof else []))
        tx_fields = [f for f in cfg["hlb_transactions"]["fields"] if applicant.get(f) is not None]
        if applicant.get("has_observable_tx") == 1 and applicant.get("monthly_inflow_vnd") is not None:
            entry("hlb_transactions", "OK", "%s months of account transactions and inflows."
                  % ((sources.get("hlb_transactions") or {}).get("period_months") or "?"), fields=tx_fields)
        elif applicant.get("monthly_inflow_vnd") is not None:
            entry("hlb_transactions", "OK", "Transaction values returned, but the account is marked as having no "
                  "observable transactions (conflict - officer to verify).", fields=tx_fields)
        else:
            entry("hlb_transactions", "NO_DATA", "Account exists but has no observable transaction history.",
                  fields=tx_fields)
        prods = _j(cust["existing_products_json"], []) or []
        entry("hlb_credit_exposure", "OK", "%d other HLB credit product(s); internal delinquency flag %s; %s Smart Credit "
              "on-time instalment(s) in a row." % (len(prods), cust["internal_delinquency_flag"], cust["on_time_streak"]),
              fields=["existing_hlb_products", "internal_delinquency_flag"])
        if self.flag("cic_unavailable"):
            entry("cic", "UNAVAILABLE", "CIC enquiry timed out after one retry. Fields from this source are treated as "
                  "unavailable - never as low or high risk.", attempts=2)
        elif applicant.get("has_formal_cic_history") == 0:
            entry("cic", "NO_RECORD", "CIC responded: no formal credit history (not a decline reason).",
                  fields=["has_formal_cic_history"] + [f for f in ("unified_credit_score", "dti_ratio")
                                                       if applicant.get(f) is not None])
        else:
            entry("cic", "OK", "CIC report retrieved.", fields=[f for f in cfg["cic"]["fields"] if applicant.get(f) is not None])
        return out

    # ================================================================ Layer 2: automatic source selection (v6)
    def _segment(self, app: Dict[str, Any]) -> Optional[str]:
        return (_j(self._customer(app["customer_id"])["applicant_json"], {}) or {}).get("customer_profile")

    def _l2_sources(self) -> Dict[str, Dict[str, Any]]:
        return {s["id"]: s for s in self.policy["data_acquisition"]["layer2_sources"]}

    def _alt_plan(self, app: Dict[str, Any], missing: List[str]) -> List[Dict[str, Any]]:
        """Which sources to request, in which order (policy data_acquisition.layer2_plan). Income items -> income sources
        eligible for the segment (unknown segment -> Open Banking only); REPAYMENT_BEHAVIOUR -> utility/telecom history.
        Sources with no decision use (e-wallet) are never requested."""
        seg = self._segment(app)
        order = self.policy["data_acquisition"]["layer2_plan"]["order"]
        cfg = self._l2_sources()
        want_income = bool({"VERIFIED_INCOME", "ADDITIONAL_INCOME"} & set(missing))
        out = []
        for sid in order:
            s = cfg.get(sid)
            if not s or s.get("requested") is False:
                continue
            if s.get("stp_income") and want_income:
                if s.get("eligible_profiles") and seg not in s["eligible_profiles"]:
                    continue
                out.append(s)
            elif not s.get("stp_income") and "REPAYMENT_BEHAVIOUR" in missing:
                out.append(s)
        return out

    def _public_source(self, s: Dict[str, Any]) -> Dict[str, Any]:
        """What the customer sees for one requested source: its name, purpose and data requested - nothing about Layer 1."""
        return {k: s.get(k) for k in ("id", "label", "category", "purpose", "data_requested", "period_months", "tier",
                                      "feasibility", "legal_note", "status")}

    def _alt_request(self, app: Dict[str, Any], missing: List[str], requested_by: str, note: Optional[str] = None,
                     target_vnd: Optional[int] = None) -> Dict[str, Any]:
        expl = self.policy["data_acquisition"]["layer2_missing_items"]
        offer = [self._public_source(s) for s in self._alt_plan(app, missing)]
        return {"requested_by": requested_by, "ts": now_iso(), "route": "ALTERNATIVE_DATA", "reason": "ALTERNATIVE_DATA",
                "note": note, "items": [{"type": "alternative_data", "missing": missing}],
                "missing": [{"item": m, "why": expl[m]} for m in missing if m in expl], "sources": offer,
                "target_vnd": target_vnd, "guarantees": self.policy["data_acquisition"]["guarantees"]}

    # ================================================================ Layer 2: adapters (SIMULATED)
    def _call_alt_adapter(self, src: Dict[str, Any], customer_id: str, app_id: str) -> Dict[str, Any]:
        """One simulated provider call with timeout + one retry. Returns a provider-style response."""
        cfg = self.policy["data_acquisition"]
        rid = _request_id(src["id"], customer_id, app_id, now_iso())
        base = {"source": src["id"], "label": src["label"], "request_id": rid, "simulated": True,
                "provider": "SIMULATED adapter - no real connection"}
        if self.flag("alt_data_api_unavailable"):
            attempts = [{"attempt": n, "result": "TIMEOUT", "latency_ms": cfg["timeout_ms"]} for n in range(1, cfg["retries"] + 2)]
            return dict(base, status="UNAVAILABLE", http_status=504, attempts=attempts,
                        error={"code": "UPSTREAM_TIMEOUT", "message": "Provider did not respond within %s ms (retried once)."
                               % cfg["timeout_ms"]})
        rec = (_j(self._customer(customer_id)["alternative_data_json"], {}) or {}).get(src["id"])
        attempts = [{"attempt": 1, "result": "OK", "latency_ms": _latency(src["id"], customer_id)}]
        if not rec:
            return dict(base, status="NOT_FOUND", http_status=404, attempts=attempts,
                        error={"code": "NO_RECORD", "message": "The provider holds no matching record for this customer."})
        months = src["period_months"]
        if self.flag("alt_data_incomplete"):
            months = 3  # simulated partial history -> fails validation
        end = self.as_of.replace(day=1) - timedelta(days=1)
        start = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
        data: Dict[str, Any] = {}
        if "monthly" in rec:
            data = {"monthly_amounts_vnd": rec["monthly"][-months:], "currency": "VND"}
            if rec.get("credits_by_category"):
                data["credits_by_category"] = {k: v[-months:] for k, v in rec["credits_by_category"].items()}
            if rec.get("payout_destination"):
                data["payout_destination"] = rec["payout_destination"]
        elif "on_time" in rec:
            data = {"on_time_payments": rec["on_time"], "total_bills": rec["of"]}
        elif "topups_pm" in rec:
            data = {"avg_topups_per_month": rec["topups_pm"]}
        return dict(base, status="OK", http_status=200, attempts=attempts, provider_name=rec.get("provider"),
                    subject_match=bool(rec.get("subject_match")), match_on=rec.get("match_on") or [],
                    observed_at=(self.as_of - timedelta(days=1)).isoformat(),
                    period={"from": start.isoformat(), "to": end.isoformat(), "months": months}, data=data)

    def _validate_alt(self, src: Dict[str, Any], resp: Dict[str, Any]) -> Dict[str, Any]:
        """AV-01..AV-04 decide whether the data can be used at all; AV-05 (activity) and AV-06 (trend) decide whether an
        income stream may be used AUTOMATICALLY (else officer). AV-07 (de-duplication) is applied when streams are built."""
        v = self.policy["data_acquisition"]["validation"]
        checks = []

        def chk(cid: str, label: str, ok: bool, blocking: bool = True) -> None:
            checks.append({"id": cid, "label": label, "passed": bool(ok), "blocking": blocking})

        match_on = set(resp.get("match_on") or [])
        id_ok = bool(resp.get("subject_match")) and "national_id" in match_on and len(match_on) >= 2
        chk("AV-01", "Provider record matches the customer on national ID + one more identifier", id_ok or not v["require_subject_match"])
        income_src = bool(src.get("stp_income"))
        need = max(v["min_period_months"], src["period_months"]) if income_src else src["period_months"]
        chk("AV-02", "Covers at least %s months" % need, (resp.get("period") or {}).get("months", 0) >= need)
        obs = resp.get("observed_at")
        chk("AV-03", "Data is at most %s days old" % v["max_age_days"],
            bool(obs) and (self.as_of - date.fromisoformat(obs)).days <= v["max_age_days"])
        d = resp.get("data") or {}
        summary: Dict[str, Any] = {}
        if "monthly_amounts_vnd" in d:
            series = d["monthly_amounts_vnd"]
            ok = bool(series) and all(isinstance(x, int) and x >= 0 for x in series)
            chk("AV-04", "Monthly amounts are whole, non-negative VND values", ok)
            if ok:
                active = sum(1 for x in series[-6:] if x > 0)
                chk("AV-05", "Income received in at least %s of the last 6 months" % v["min_active_months"],
                    active >= v["min_active_months"], blocking=False)
                last3, prev3 = series[-3:], series[-6:-3]
                trend = (sum(last3) / len(last3)) / (sum(prev3) / len(prev3)) if prev3 and sum(prev3) else 1.0
                chk("AV-06", "Last 3 months at least %d%% of the previous 3" % round(100 * v["min_trend_ratio"]),
                    trend >= v["min_trend_ratio"], blocking=False)
                summary = {"series_vnd": series, "trend_ratio": round(trend, 3), "active_months": active,
                           "credits_by_category": d.get("credits_by_category"), "payout_destination": d.get("payout_destination")}
        elif "on_time_payments" in d:
            ok = 0 <= d["on_time_payments"] <= d["total_bills"] and d["total_bills"] > 0
            chk("AV-04", "Payment counts are consistent", ok)
            if ok:
                summary = {"on_time_ratio": round(d["on_time_payments"] / d["total_bills"], 4),
                           "on_time_payments": d["on_time_payments"], "total_bills": d["total_bills"]}
        elif "avg_topups_per_month" in d:
            ok = isinstance(d["avg_topups_per_month"], int) and d["avg_topups_per_month"] >= 0
            chk("AV-04", "Activity count is valid", ok)
            if ok:
                summary = {"avg_topups_per_month": d["avg_topups_per_month"]}
        valid = all(c["passed"] for c in checks if c["blocking"])
        return {"valid": valid, "stp_usable": valid and all(c["passed"] for c in checks),
                "identity_mismatch": not id_ok, "checks": checks, "summary": summary}

    # ================================================================ Layer 2: streams, de-duplication (AV-07)
    def _build_streams(self, results: List[Dict[str, Any]], layer1_income: bool) -> Tuple[List[Dict[str, Any]], int]:
        """Recognised income streams from accepted results, counted ONCE:
          * platform / marketplace payouts paid into the HLB account are already Layer 1 -> not added;
          * payouts paid into another bank come from the platform record and are removed from Open Banking credits;
          * Open Banking transfers from the customer's own HLB account are excluded;
          * Open Banking transfers INTO HLB (counted in an unclassified Layer 1 inflow) are deducted from Layer 1.
        Returns (streams, layer1_deduction_vnd)."""
        cfg = self._l2_sources()
        acc = [r for r in results if r["outcome"] == "ACCEPTED" and cfg[r["source"]].get("stp_income")]
        platform_other_bank = {r["source"]: r for r in acc if r["source"] != "open_banking"
                               and (r["validation"]["summary"].get("payout_destination") or "OTHER_BANK") != "HLB"}
        streams, deduction = [], 0
        for r in acc:
            sm, src = r["validation"]["summary"], cfg[r["source"]]
            n = len(sm["series_vnd"])
            if r["source"] == "open_banking":
                cats = sm.get("credits_by_category") or {"salary": sm["series_vnd"]}
                monthly = [0] * n
                dedup_notes = []
                for cat, series in cats.items():
                    if cat == "platform_payouts" and any(k in platform_other_bank for k in ("gig_platform",)):
                        dedup_notes.append("platform_payouts counted from the platform record (not twice)")
                        continue
                    if cat == "merchant_settlements" and any(k in platform_other_bank for k in ("marketplace",)):
                        dedup_notes.append("marketplace payouts counted from the marketplace record (not twice)")
                        continue
                    if cat == "transfers_to_hlb":
                        deduction += int(sum(series) / len(series))
                        dedup_notes.append("transfers into HLB deducted from the Layer 1 inflow")
                        continue
                    f = self._factor(cat)
                    if f == 0:
                        dedup_notes.append("%s excluded (not income)" % cat)
                        continue
                    monthly = [m + int(x * f) for m, x in zip(monthly, series)]
                avg = int(sum(monthly) / n) if n else 0
                streams.append({"source": "open_banking", "tier": src["tier"], "type": "BY_CATEGORY", "factor": "by category",
                                "average_recognised_vnd": avg, "series_recognised_vnd": monthly,
                                "stp_usable": r["validation"]["stp_usable"], "dedup": dedup_notes})
                continue
            dest = sm.get("payout_destination") or "OTHER_BANK"
            f = src["income_recognition_factor"]
            rec = [int(x * f) for x in sm["series_vnd"]]
            st = {"source": r["source"], "tier": src["tier"], "type": src["category"], "factor": f,
                  "average_recognised_vnd": int(sum(rec) / n) if n else 0, "series_recognised_vnd": rec,
                  "stp_usable": r["validation"]["stp_usable"], "payout_destination": dest, "dedup": []}
            if dest == "HLB" and layer1_income:
                st.update(duplicate_of="LAYER1", average_recognised_vnd=0)
                st["dedup"].append("paid into the HLB account: already in Layer 1 - not added again")
            streams.append(st)
        return streams, deduction

    def _factor(self, category: str) -> float:
        rt = self.policy["income_classification"]["recognition_by_type"].get(category)
        return float(rt["factor"]) if rt else 0.0

    def _apply_alt_results(self, submitted: Dict[str, Any], results: List[Dict[str, Any]], layer1_income: bool) -> Dict[str, Any]:
        """Write validated Layer 2 data into the application's submitted inputs (fields + alternative_data source)."""
        submitted = submitted or {"fields": {}, "sources": {}}
        submitted.setdefault("fields", {})
        submitted.setdefault("sources", {})
        streams, deduction = self._build_streams(results, layer1_income)
        live = [s for s in streams if not s.get("duplicate_of") and s["average_recognised_vnd"] > 0]
        obs = (self.as_of - timedelta(days=1)).isoformat()
        for f in ("alt_monthly_income_vnd", "alt_income_cv", "alt_on_time_bill_ratio"):
            submitted["fields"].pop(f, None)
        alt: Dict[str, Any] = {"verified": True, "observed_at": obs, "method": "LAYER2_API_RETRIEVAL", "streams": streams,
                               "layer1_deduction_vnd": deduction, "source_ids": [s["source"] for s in live],
                               "period_months": min([len(s["series_recognised_vnd"]) for s in live] or [0])}
        if live:
            total = [sum(x) for x in zip(*[s["series_recognised_vnd"] for s in live])]
            avg = int(sum(total) / len(total))
            submitted["fields"]["alt_monthly_income_vnd"] = avg
            submitted["fields"]["alt_income_cv"] = round(statistics.pstdev(total) / avg, 4) if avg else 0.0
        beh = next((r for r in results if r["outcome"] == "ACCEPTED" and r["source"] == "utility_bills"), None)
        if beh:
            submitted["fields"]["alt_on_time_bill_ratio"] = beh["validation"]["summary"]["on_time_ratio"]
            alt["behaviour_source"] = "utility_bills"
        if live or beh or streams:
            submitted["sources"]["alternative_data"] = alt
        else:
            submitted["sources"].pop("alternative_data", None)
        return submitted

    # ================================================================ Layer 2: customer consent + retrieval
    def alternative_data_consent(self, app_id: str, customer_id: str, consents: Dict[str, Any]) -> Dict[str, Any]:
        """Explicit, optional, per-source consent (unticked by default; unticked = not allowed). Consented sources are
        called in priority order and calling STOPS once the gap is closed (data minimisation). Declining everything, no
        record or invalid data is never itself a decline reason: the rules re-run on what is verified. A provider outage
        keeps the request in automatic assessment (retried later)."""
        if not isinstance(consents, dict):
            raise ServiceError(422, "CONSENT_REQUIRED", "Tick the sources you agree to share (or none).")
        with self._unit():
            app = self._app(app_id, customer_id)
            req = _j(app["info_request_json"], {}) or {}
            if app["credit_state"] != "MORE_INFORMATION_REQUIRED" or req.get("route") != "ALTERNATIVE_DATA":
                raise ServiceError(409, "ALTERNATIVE_DATA_NOT_REQUESTED", "Additional data hasn't been requested for this application.")
            offered = {s["id"]: s for s in req.get("sources", [])}
            unknown = [k for k in consents if k not in offered]
            if unknown:
                raise ServiceError(422, "SOURCE_NOT_OFFERED", "These data sources weren't requested: %s" % ", ".join(unknown))
            bad = [k for k, v in consents.items() if not isinstance(v, bool)]
            if bad:
                raise ServiceError(422, "CONSENT_REQUIRED", "Each answer must be a tick (true) or no tick (false).", {"invalid": bad})
            answers = {sid: consents.get(sid) is True for sid in offered}  # unticked = not allowed
            cfg = self._l2_sources()
            ts = now_iso()
            for sid in offered:
                self.db.run("INSERT INTO consents(consent_id, application_id, customer_id, granted, categories_json, "
                            "policy_version, recorded_at, kind, purpose) VALUES (?,?,?,?,?,?,?,?,?)",
                            (self._new_id("CNS", "consent"), app_id, customer_id, 1 if answers[sid] else 0,
                             json.dumps(["alternative_data"]), self.policy["policy_version"], ts, "ALT_DATA:" + sid, cfg[sid]["purpose"]))
            granted = [sid for sid in offered if answers[sid]]
            self.db.audit("CUSTOMER", customer_id, "ALTERNATIVE_DATA_CONSENT", None, app_id,
                          details={"granted": granted, "declined": [s for s in offered if not answers[s]],
                                   "purpose_specific": True, "one_time": True, "default": "unticked"})
            acq = _j(app["acquisition_json"], {}) or {}
            rnd = {"requested_by": req.get("requested_by"), "requested_at": req.get("ts"), "answered_at": ts,
                   "missing": req.get("missing"), "offered": list(offered), "consents": answers, "results": [],
                   "target_vnd": req.get("target_vnd")}
            acq.setdefault("layer2", []).append(rnd)
            self._run_layer2_round(app, rnd, granted)
            self._move_credit(app, "ASSESSMENT_PENDING", "CUSTOMER", customer_id, "ALTERNATIVE_DATA_ANSWERED",
                              extra={"info_submitted_json": app["info_submitted_json"], "acquisition_json": json.dumps(acq)})
        return self.reevaluate_after_layer2(app_id, customer_id)

    def _run_layer2_round(self, app: Dict[str, Any], rnd: Dict[str, Any], to_call: List[str]) -> None:
        """Call `to_call` in priority order; stop income calls once the gap is closed. Mutates rnd and app (submitted)."""
        cfg = self._l2_sources()
        order = self.policy["data_acquisition"]["layer2_plan"]["order"]
        app_id, customer_id = app["application_id"], app["customer_id"]
        submitted = _j(app["info_submitted_json"], {}) or {"fields": {}, "sources": {}}
        layer1_income = bool((self._capacity_with(app, submitted).get("income") or {}).get("layer1"))
        done = {r["source"]: r for r in rnd["results"] if r["outcome"] in ("ACCEPTED", "INVALID", "NOT_FOUND")}
        results = [r for r in rnd["results"] if r["source"] in done]
        closed = False
        for sid in sorted(to_call, key=lambda x: order.index(x) if x in order else 99):
            if sid in done:
                continue
            if closed and cfg[sid].get("stp_income"):
                results.append({"source": sid, "label": cfg[sid]["label"], "outcome": "NOT_CALLED_GAP_CLOSED", "response": None,
                                "validation": None})
                continue
            resp = self._call_alt_adapter(cfg[sid], customer_id, app_id)
            val = self._validate_alt(cfg[sid], resp) if resp["status"] == "OK" else None
            outcome = "ACCEPTED" if val and val["valid"] else ("INVALID" if val else resp["status"])
            results.append({"source": sid, "label": cfg[sid]["label"], "outcome": outcome, "response": resp, "validation": val})
            self.db.audit("SYSTEM", "alt-data-adapter", "ALTERNATIVE_DATA_RETRIEVED" if outcome == "ACCEPTED"
                          else "ALTERNATIVE_DATA_NOT_USED", None, app_id,
                          rule_ids=[] if outcome == "ACCEPTED" else ["R-API-01" if outcome == "UNAVAILABLE" else "R-ALT-03"],
                          details={"source": sid, "outcome": outcome, "request_id": resp["request_id"],
                                   "attempts": resp.get("attempts"), "simulated": True,
                                   "validation": [(c["id"], c["passed"]) for c in (val or {}).get("checks", [])]})
            if outcome == "ACCEPTED" and cfg[sid].get("stp_income"):
                submitted = self._apply_alt_results(submitted, results, layer1_income)
                cap = self._capacity_with(app, submitted)
                target = rnd.get("target_vnd") or self.policy["affordability"]["min_limit_vnd"]
                stp = cap.get("stp") or {}
                closed = bool(cap.get("computable") and not cap.get("verified_obligation_breach") and stp.get("meets_minimum")
                              and stp.get("raw_limit_vnd", 0) >= target)
        submitted = self._apply_alt_results(submitted, results, layer1_income)
        rnd["results"] = results
        called = [r for r in results if r["outcome"] not in ("NOT_CALLED_GAP_CLOSED",)]
        rnd["identity_mismatch"] = [r["source"] for r in results if r.get("validation") and r["validation"]["identity_mismatch"]]
        rnd["unstable"] = [r["source"] for r in results if r["outcome"] == "ACCEPTED" and cfg[r["source"]].get("stp_income")
                           and not r["validation"]["stp_usable"]]
        rnd["outcome"] = ("DECLINED" if not to_call else
                          "UNAVAILABLE" if called and all(r["outcome"] == "UNAVAILABLE" for r in called) else
                          "DATA_ADDED" if any(r["outcome"] == "ACCEPTED" for r in results) else "NO_USABLE_DATA")
        if rnd["outcome"] == "UNAVAILABLE":
            rnd["pending_retry"] = [r["source"] for r in called if r["outcome"] == "UNAVAILABLE"]
        app["info_submitted_json"] = json.dumps(submitted)

    def _capacity_with(self, app: Dict[str, Any], submitted: Dict[str, Any]) -> Dict[str, Any]:
        """Provisional capacity with the given submitted Layer 2 inputs (used for the stop rule)."""
        a = dict(app, info_submitted_json=json.dumps(submitted))
        inp = self._activation_inputs(a)
        pol = dict(self.policy)
        pol["mandatory_fields"] = dict(self.policy["mandatory_fields"], application=self.policy["mandatory_fields"]["activation_application"])
        res = R.evaluate(inp["application"], inp["applicant"], inp["sources"], inp["consent"], pol, self.as_of)
        inp["verified_fields"] = self._verified_fields(inp, res)
        return self._capacity(dict(a, input_snapshot_json=json.dumps(inp)))

    def retry_layer2(self, app_id: str) -> bool:
        """Automatic retry of Layer 2 sources that were unavailable (consent already given). True if re-assessed."""
        with self._unit():
            app = self._app(app_id)
            acq = _j(app["acquisition_json"], {}) or {}
            rnd = (acq.get("layer2") or [None])[-1]
            if not rnd or rnd.get("outcome") != "UNAVAILABLE" or self.flag("alt_data_api_unavailable"):
                return False
            pending = rnd.get("pending_retry") or []
            rnd["results"] = [r for r in rnd["results"] if r["source"] not in pending]
            self._run_layer2_round(app, rnd, [s for s, ok in rnd["consents"].items() if ok])
            rnd["retried_at"] = now_iso()
            self.db.run("UPDATE applications SET info_submitted_json = ?, acquisition_json = ?, version = version + 1, updated_at = ? "
                        "WHERE application_id = ?", (app["info_submitted_json"], json.dumps(acq), now_iso(), app_id))
            self.db.audit("SYSTEM", "alt-data-adapter", "LAYER2_RETRIED", None, app_id, details={"sources": pending,
                          "outcome": rnd["outcome"]})
        return True

    def reevaluate_after_layer2(self, app_id: str, customer_id: str) -> Dict[str, Any]:
        return self.assess_activation(app_id)

    # ================================================================ views
    def acquisition_view(self, app: Dict[str, Any], audience: str = "CUSTOMER") -> Dict[str, Any]:
        acq = _j(app["acquisition_json"], {}) or {}
        l1 = acq.get("layer1", [])
        rounds = acq.get("layer2", [])
        if audience == "CUSTOMER":
            # v6: no Layer 1 detail in the customer app (which data was assessed or missing is internal).
            return {"layer2": [{"answered_at": r["answered_at"], "outcome": r.get("outcome"),
                                "results": [{"source": x["source"], "label": x["label"], "outcome": x["outcome"]}
                                            for x in r["results"]],
                                "declined": [k for k, v in r["consents"].items() if not v]} for r in rounds]}
        return {"layer1": l1, "layer2": rounds, "layer1_sufficient": acq.get("layer1_sufficient"),
                "excluded_sources": self.policy["data_acquisition"]["excluded_sources"],
                "note": self.policy["data_acquisition"]["note"]}

    def _layer2_state(self, app: Dict[str, Any]) -> Dict[str, Any]:
        """Routing context for Layer 2. Data withdrawn by the customer no longer counts as used."""
        acq = _j(app["acquisition_json"], {}) or {}
        rounds = acq.get("layer2", [])
        submitted = _j(app["info_submitted_json"], {}) or {}
        alt = (submitted.get("sources") or {}).get("alternative_data") or {}
        last = rounds[-1] if rounds else {}
        withdrawn = set(alt.get("withdrawn") or [])
        unstable = [s for s in (last.get("unstable") or []) if s not in withdrawn]
        return {"attempted": bool(rounds), "outcome": last.get("outcome"), "used": bool(alt.get("source_ids")),
                "unstable_sources": unstable, "identity_mismatch": last.get("identity_mismatch") or [],
                "income_sources": bool(self._alt_plan(app, ["ADDITIONAL_INCOME"])),
                "behaviour_source": bool(self._alt_plan(app, ["REPAYMENT_BEHAVIOUR"])), "allowed": True}

    def _layer2_used(self, app: Dict[str, Any]) -> Tuple[bool, bool]:
        st = self._layer2_state(app)
        return st["attempted"], st["used"]

    # ================================================================ Layer 2: per-source consent withdrawal (audit F-18)
    def withdraw_alt_source(self, app_id: str, customer_id: str, source_id: str) -> Dict[str, Any]:
        """Withdraw consent for one Layer 2 source: the consent is marked withdrawn and that source's data is purged from the
        application (its stream is removed and the totals recomputed). An open application is re-assessed without it; a
        decision already taken is not reversed. Legal deadline: respond within 2 working days, implement within 15 days
        (Decree 356/2025) - implemented immediately here."""
        with self._unit():
            app = self._app(app_id, customer_id)
            rows = self.db.all("SELECT * FROM consents WHERE application_id = ? AND kind = ? AND granted = 1 AND withdrawn_at IS NULL",
                               (app_id, "ALT_DATA:" + str(source_id)))
            if not rows:
                raise ServiceError(404, "CONSENT_NOT_FOUND", "There is no active permission for this data source on this request.")
            ts = now_iso()
            self.db.run("UPDATE consents SET withdrawn_at = ? WHERE application_id = ? AND kind = ? AND withdrawn_at IS NULL",
                        (ts, app_id, "ALT_DATA:" + source_id))
            acq = _j(app["acquisition_json"], {}) or {}
            purged = []
            for rnd in acq.get("layer2", []):
                for r in rnd.get("results", []):
                    if r["source"] == source_id:
                        if r.get("response"):
                            r["response"] = {k: r["response"].get(k) for k in ("source", "label", "request_id", "status", "simulated")}
                        r["validation"] = None
                        r["outcome"] = "WITHDRAWN"
                        r["withdrawn_at"] = ts
                        purged.append(source_id)
                rnd["unstable"] = [s for s in (rnd.get("unstable") or []) if s != source_id]
            submitted = _j(app["info_submitted_json"], {}) or {"fields": {}, "sources": {}}
            last = (acq.get("layer2") or [{}])[-1]
            layer1_income = bool((self._capacity_with(app, {"fields": {}, "sources": {}}).get("income") or {}).get("layer1"))
            submitted = self._apply_alt_results(submitted, last.get("results", []), layer1_income)
            if submitted["sources"].get("alternative_data") is not None:
                submitted["sources"]["alternative_data"]["withdrawn"] = sorted(set(
                    (submitted["sources"]["alternative_data"].get("withdrawn") or []) + [source_id]))
            self.db.run("UPDATE applications SET info_submitted_json = ?, acquisition_json = ?, version = version + 1, updated_at = ? "
                        "WHERE application_id = ?", (json.dumps(submitted), json.dumps(acq), ts, app_id))
            wd = self.policy["consent"]["withdrawal"]
            self.db.audit("CUSTOMER", customer_id, "ALTERNATIVE_DATA_CONSENT_WITHDRAWN", None, app_id,
                          details={"source": source_id, "data_purged": bool(purged), "decision_reversed": False,
                                   "respond_within_working_days": wd["respond_within_working_days"],
                                   "implement_within_days": wd["implement_within_days"], "implemented": "IMMEDIATELY"})
            app = self._app(app_id)
            if app["credit_state"] == "MANUAL_REVIEW":
                self._snapshot_for_review(app)
        return self.application_view(app_id, customer_id)
