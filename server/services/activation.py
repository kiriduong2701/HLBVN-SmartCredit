"""Limit Activation (journey 1), counter-offers, automatic line renewal, limit increases, reinstatement and consent.

All credit assessment lives here, OFF the merchant counter: the legally mandated data-processing step, Layer 0 identity &
fraud checks, automatic Layer 1 acquisition (every dataset variable + HLB data - see acquisition.py), readiness,
capacity-based limit assignment, an automated Layer 2 request when Layer 1 is absent or insufficient, automatic retries
after outages, and officer review only for judgement cases. The customer is never asked a question and never uploads
documents. A GREEN result opens the credit line instantly; a GREEN below the request is a counter-offer that opens only
when the customer accepts; requests above VND 90.1M are capped and the excess referred out of scope.
"""
import copy
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..domain import states as S
from ..engine import capacity as CAP
from ..engine import identity as IDN
from ..engine import readiness as R
from ..engine import routing
from ..store.db import now_iso
from .base import APPLICANT_FIELDS, CREDIT_METHODS, ServiceError, _j

LINE_KINDS = ("LIMIT_INCREASE", "LINE_REVIEW", "LINE_REINSTATEMENT")  # assessed under the line's activation consents


class ActivationMixin:
    # ------------------------------------------------------------ bootstrap (pre-activated personas)
    def bootstrap(self) -> None:
        for c in self.fixtures["customers"]:
            if not c.get("pre_activated") or self._line(c["customer_id"]):
                continue
            if self.db.one("SELECT 1 FROM applications WHERE customer_id = ?", (c["customer_id"],)):
                continue
            consents = {x["id"]: True for x in self.policy["consent"]["activation_consents"]}
            self.start_activation(c["customer_id"], consents, origin="SEED", actor="SYSTEM", note="Pre-activated persona (seed)")

    # ------------------------------------------------------------ consent text for the UI
    def activation_requirements(self, customer_id: str) -> Dict[str, Any]:
        cs = self.credit_summary(customer_id)
        return {"consents": self.policy["consent"]["activation_consents"], "credit": cs,
                "can_activate": cs["line_status"] in ("NONE", "CLOSED") and not cs["open_application"]
                and not cs["delinquency"]["arrears"],
                "terms_version": self.policy["terms_and_conditions"]["version"]}

    # ------------------------------------------------------------ start
    def start_activation(self, customer_id: str, consents: Dict[str, Any], origin: str = "PRODUCTS",
                         origin_tx_id: Optional[str] = None, requested_limit: Optional[int] = None,
                         actor: str = "CUSTOMER", note: Optional[str] = None) -> Dict[str, Any]:
        consents = consents or {}
        missing = [c["id"] for c in self.policy["consent"]["activation_consents"] if c["required"] and consents.get(c["id"]) is not True]
        if missing:
            raise ServiceError(422, "CONSENT_REQUIRED", "Please give the required permissions to check your limit.",
                               {"missing": missing})
        if requested_limit is not None and (not isinstance(requested_limit, int) or isinstance(requested_limit, bool)
                                            or requested_limit < self.policy["affordability"]["min_limit_vnd"]):
            raise ServiceError(422, "INVALID_LIMIT", "Requested limit is not valid (minimum VND {:,}).".format(
                self.policy["affordability"]["min_limit_vnd"]))
        self.refresh_servicing(customer_id)
        with self._unit():
            cs = self.credit_summary(customer_id)
            if cs["open_application"]:
                raise ServiceError(409, "APPLICATION_OPEN", "You already have a Smart Credit request in progress.",
                                   {"application_id": cs["open_application"]["application_id"]})
            if cs["line_status"] in ("ACTIVE", "FROZEN"):
                raise ServiceError(409, "LINE_ALREADY_ACTIVE", "Your Smart Credit limit is already active.")
            if cs["line_status"] == "SUSPENDED":
                raise ServiceError(409, "LINE_SUSPENDED", "Your Smart Credit line is suspended after a default. Once the overdue "
                                   "amounts are paid, a credit officer reviews reinstatement.")
            app_id = self._new_id("ACT", "app")
            ts = now_iso()
            self.db.run("INSERT INTO applications(application_id, customer_id, kind, origin, origin_transaction_id, "
                        "requested_limit_vnd, credit_state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (app_id, customer_id, "LIMIT_ACTIVATION", origin, origin_tx_id, requested_limit,
                         "ASSESSMENT_PENDING", ts, ts))
            self.db.audit(actor, customer_id, "LIMIT_ACTIVATION_STARTED", origin_tx_id, app_id, to_state="ASSESSMENT_PENDING",
                          details={"origin": origin, "requested_limit_vnd": requested_limit, "note": note})
            self._refer_out_of_scope(app_id, customer_id, requested_limit)
            for c in self.policy["consent"]["activation_consents"]:
                self.db.run("INSERT INTO consents(consent_id, application_id, customer_id, granted, categories_json, "
                            "policy_version, recorded_at, kind, purpose) VALUES (?,?,?,?,?,?,?,?,?)",
                            (self._new_id("CNS", "consent"), app_id, customer_id, 1 if consents.get(c["id"]) is True else 0,
                             json.dumps(c["categories"]), self.policy["terms_and_conditions"]["version"], ts, c["id"],
                             c["purpose"] + (" [%s]" % note if note else "")))
            self.db.audit(actor, customer_id, "ACTIVATION_CONSENTS_RECORDED", origin_tx_id, app_id,
                          details={"consents": {c["id"]: consents.get(c["id"]) is True
                                                for c in self.policy["consent"]["activation_consents"]},
                                   "separate_purpose_specific": True})
        return self.assess_activation(app_id, actor=actor)

    # ------------------------------------------------------------ product scope (VND 1.6M - 90.1M)
    def _in_scope(self, requested: Optional[int]) -> Optional[int]:
        mx = self.policy["affordability"]["product_max_limit_vnd"]
        return None if requested is None else min(requested, mx)

    def _refer_out_of_scope(self, app_id: str, customer_id: str, requested: Optional[int]) -> None:
        """Above the product maximum: never processed as eligible. The in-scope part is assessed here; the request is
        referred to HLB lending for human review outside Smart Credit (R030-OOS)."""
        mx = self.policy["affordability"]["product_max_limit_vnd"]
        if requested is None or requested <= mx:
            return
        self.db.audit("DECISION_ENGINE", "engine", "OUT_OF_SCOPE_REFERRAL", None, app_id, rule_ids=["R030-OOS"],
                      details={"requested_vnd": requested, "in_scope_max_vnd": mx, "excess_vnd": requested - mx,
                               "referred_to": "HLB lending (human review outside Smart Credit - simulated)"})
        self.notify(customer_id, "STATUS", "out_of_scope_referral", {"max": mx}, "#/smart-credit/app/" + app_id, "oos:" + app_id)

    # ------------------------------------------------------------ inputs
    def _repulled_sources(self, sources: Dict[str, Any]) -> Dict[str, Any]:
        """Simulated Layer 1 re-pull: the persona fixtures are a snapshot taken on the demo date, so when the business
        date moves on, every fixture source is re-read and its observation date moves with it (relative staleness kept)."""
        days = (self.as_of - self.demo_date).days
        if days <= 0:
            return sources
        for s in sources.values():
            if isinstance(s, dict) and s.get("observed_at") and str(s.get("method", "")).startswith("SIMULATED"):
                s["observed_at"] = (date.fromisoformat(s["observed_at"][:10]) + timedelta(days=days)).isoformat()
                s["repulled"] = True
        return sources

    def _activation_inputs(self, app: Dict[str, Any]) -> Dict[str, Any]:
        cust = self._customer(app["customer_id"])
        applicant = {k: v for k, v in _j(cust["applicant_json"], {}).items() if k in APPLICANT_FIELDS}
        applicant["internal_delinquency_flag"] = cust["internal_delinquency_flag"]
        sources = self._repulled_sources(copy.deepcopy(_j(cust["sources_json"], {})))
        submitted = _j(app["info_submitted_json"], {}) or {}
        declared = _j(app["declared_json"], {}) or {}
        for f, d in declared.items():
            applicant[f] = d["value"]
        applicant.update(submitted.get("fields", {}))
        sources.update(submitted.get("sources", {}))
        source_errors: List[Dict[str, str]] = []
        cic = applicant.get("has_formal_cic_history")
        cic_status = "OK" if cic == 1 else ("NO_RECORD" if cic == 0 else None)
        if self.flag("cic_unavailable"):
            # CIC enquiry failed after one retry: the source is UNAVAILABLE (audit C-01) - its fields are neither low nor
            # high risk, not "missing" and not "unknown provenance".
            sources["credit_information"] = {"unavailable": True, "error": "CIC_ENQUIRY_UNAVAILABLE", "attempts": 2}
            source_errors.append({"source": "credit_information", "code": "CIC_ENQUIRY_UNAVAILABLE"})
            cic_status = "UNAVAILABLE"
        cats = set()
        consent_apps = [app["application_id"]]
        line = self._line(app["customer_id"])
        if app["kind"] in LINE_KINDS and line and line["activation_id"]:
            consent_apps.append(line["activation_id"])  # the activation consent covers assessing AND monitoring the limit
        for aid in consent_apps:
            for c in self.db.all("SELECT * FROM consents WHERE application_id = ? AND granted = 1 AND withdrawn_at IS NULL",
                                 (aid,)):
                cats |= set(_j(c["categories_json"], []))
        layer1 = self._layer1_log(cust, applicant, sources, app["application_id"])
        return {"application": {"application_id": app["application_id"], "applicant_id": cust["customer_id"]},
                "applicant": applicant, "sources": sources, "consent": {"granted": bool(cats), "categories": sorted(cats)},
                "declared_fields": sorted(declared), "source_errors": source_errors, "as_of": self.as_of.isoformat(),
                "layer1": layer1, "cic_status": cic_status,
                "hlb_products": _j(cust["existing_products_json"], []) or []}

    def _verified_fields(self, inp: Dict[str, Any], res: Dict[str, Any]) -> List[str]:
        """Fields usable for capacity: present, trusted by readiness, from a verified + fresh source, not declared."""
        out = []
        not_used = res.get("fields_not_used") or {}
        stale = {w["field"] for w in res.get("verification_warnings", []) if w["rule_id"] in ("R-VER-02", "R-VER-03")}
        for src_id, cfg in self.policy["data_sources"].items():
            src = inp["sources"].get(src_id) or {}
            if not src.get("verified") or src_id in stale:
                continue
            for f in cfg["fields"]:
                v = inp["applicant"].get(f)
                if v is not None and f not in not_used and f not in inp["declared_fields"]:
                    out.append(f)
        return sorted(out)

    def _aggregate_headroom(self, products: List[Dict[str, Any]]) -> Optional[int]:
        """Circular 39 Art. 22.2(h) (Circular 29/2026): per-customer outstanding limit for digital lending, set by the bank."""
        cap = self.policy["aggregate_exposure"]["max_digital_unsecured_outstanding_vnd"]
        if cap is None:
            return None
        return cap - sum(int(p.get("outstanding_vnd") or 0) for p in products or [])

    def _capacity(self, app: Dict[str, Any]) -> Dict[str, Any]:
        snap = _j(app["input_snapshot_json"], {}) or {}
        applicant, sources, vf = snap.get("applicant", {}), snap.get("sources", {}), snap.get("verified_fields", [])
        products = snap.get("hlb_products", [])
        inc = CAP.income_basis(applicant, sources, vf, self.policy)
        ob = CAP.obligations_basis(applicant, vf, inc, products, snap.get("cic_status"))
        return CAP.assign_limit(inc, ob, self.policy, self._aggregate_headroom(products))

    def _store_layer1(self, app: Dict[str, Any], inp: Dict[str, Any], sufficient: Optional[bool]) -> str:
        acq = _j(app["acquisition_json"], {}) or {}
        acq["layer1"] = inp.get("layer1", [])
        acq["layer1_at"] = now_iso()
        acq["layer1_sufficient"] = sufficient
        return json.dumps(acq)

    # ------------------------------------------------------------ Layer 0 signals (simulated eKYC + DB-derived)
    def _identity_signals(self, customer_id: str) -> Dict[str, Any]:
        cust = self._customer(customer_id)
        idt = _j(cust["identity_json"], {}) or {}
        nid = idt.get("national_id")
        dup = None
        if nid:
            dup = sum(1 for r in self.db.all("SELECT customer_id, identity_json FROM customers WHERE customer_id != ?", (customer_id,))
                      if (_j(r["identity_json"], {}) or {}).get("national_id") == nid)
        since = (datetime.now(timezone.utc) - timedelta(days=30)).astimezone().isoformat(timespec="seconds")
        apps = self.db.one("SELECT COUNT(*) n FROM applications WHERE customer_id = ? AND origin != 'SEED' AND origin != 'SYSTEM' "
                           "AND kind IN ('LIMIT_ACTIVATION', 'LIMIT_INCREASE') AND created_at >= ?", (customer_id, since))["n"]
        pl = self.db.one("SELECT COUNT(*) n FROM transactions WHERE customer_id = ? AND method IN (%s) AND state = 'PAYMENT_COMPLETED' "
                         "AND business_date = ?" % ",".join("?" * len(CREDIT_METHODS)),
                         (customer_id,) + CREDIT_METHODS + (self.as_of.isoformat(),))["n"]
        return {"ekyc_status": idt.get("ekyc_status"), "cccd_match": idt.get("cccd_match"), "duplicate_identity_count": dup,
                "device_anomaly": self.flag("device_anomaly"), "applications_30d": apps, "pay_later_purchases_today": pl,
                "fraud_confirmed": bool(cust["fraud_confirmed"])}

    # ------------------------------------------------------------ assessment
    def assess_activation(self, app_id: str, actor: str = "CUSTOMER") -> Dict[str, Any]:
        with self._unit():
            app = self._app(app_id)
            if app["credit_state"] != "ASSESSMENT_PENDING":
                return self.application_view(app_id)
            started = time.perf_counter()
            inp = self._activation_inputs(app)
            pol = dict(self.policy)
            pol["mandatory_fields"] = dict(self.policy["mandatory_fields"], application=self.policy["mandatory_fields"]["activation_application"])
            res = R.evaluate(inp["application"], inp["applicant"], inp["sources"], inp["consent"], pol, self.as_of)
            inp["verified_fields"] = self._verified_fields(inp, res)
            app_for_cap = dict(app, input_snapshot_json=json.dumps(inp))
            cap = self._capacity(app_for_cap)
            dq = self._delinquency(app["customer_id"])
            ident = IDN.evaluate(self._identity_signals(app["customer_id"]), "ACTIVATION", self.policy)
            l2 = self._layer2_state(app)
            if l2["identity_mismatch"]:
                sig = self._identity_signals(app["customer_id"])
                sig["layer2_identity_mismatch"] = ", ".join(l2["identity_mismatch"])
                ident = IDN.evaluate(sig, "ACTIVATION", self.policy)
            line = self._line(app["customer_id"])
            if app["kind"] == "LINE_REVIEW":
                l2["allowed"] = False  # a renewal never prompts for alternative data; anything short goes to an officer
            vf = set(inp["verified_fields"])
            behaviour = (inp["applicant"].get("has_formal_cic_history") == 1 and "has_formal_cic_history" in vf) \
                or "on_time_bill_ratio" in vf or "alt_on_time_bill_ratio" in vf
            requested = self._in_scope(app["requested_limit_vnd"])
            dec = routing.decide(res, {"capacity": cap, "identity": ident, "delinquency": dq, "source_errors": inp["source_errors"],
                                       "requested_limit_vnd": requested, "layer2": l2, "behaviour_signal": behaviour,
                                       "in_human_review": bool(app["review_started_at"]), "kind": app["kind"],
                                       "current_limit_vnd": line["limit_vnd"] if line else None}, self.policy)
            alt_req = None
            if dec["route"] == routing.ROUTE_ALT_DATA:
                target = requested if app["kind"] == "LIMIT_INCREASE" else None
                alt_req = self._alt_request(app, dec["requested_items"][0]["missing"], "DECISION_ENGINE", target_vnd=target)
                if not alt_req["sources"]:  # nothing relevant to request for this profile -> decide without Layer 2
                    l2.update(allowed=False)
                    dec = routing.decide(res, {"capacity": cap, "identity": ident, "delinquency": dq,
                                               "source_errors": inp["source_errors"], "requested_limit_vnd": requested,
                                               "layer2": l2, "behaviour_signal": behaviour,
                                               "in_human_review": bool(app["review_started_at"]), "kind": app["kind"],
                                               "current_limit_vnd": line["limit_vnd"] if line else None}, self.policy)
                    alt_req = None
            dec["engine_ms"] = round((time.perf_counter() - started) * 1000, 2)
            dec["policy_version"], dec["policy_hash"] = self.policy["policy_version"], self.policy_hash
            dec["identity"] = ident
            self.db.audit("DECISION_ENGINE", "engine", "ACTIVATION_ASSESSED", app["origin_transaction_id"], app_id,
                          rule_ids=dec["rule_ids"],
                          details={"band": dec["band"], "reason_code": dec["reason_code"], "secondary_codes": dec["secondary_codes"],
                                   "kind": app["kind"], "readiness": res["status"], "limit_vnd": cap.get("limit_vnd"),
                                   "officer_max_limit_vnd": cap.get("officer_max_limit_vnd"),
                                   "binding_constraint": cap.get("binding_constraint"), "engine_ms": dec["engine_ms"],
                                   "policy_version": self.policy["policy_version"], "policy_hash": self.policy_hash,
                                   "layer0": [(c["id"], c["result"]) for c in ident["checks"]],
                                   "checks": [(c["check_id"], c["result"]) for c in res["checks_performed"]]})
            extra = {"readiness_status": res["status"], "readiness_json": json.dumps(res), "routing_json": json.dumps(dec),
                     "capacity_json": json.dumps(cap), "input_snapshot_json": json.dumps(inp), "band": dec["band"],
                     "reason_code": dec["reason_code"], "decided_by": dec["decided_by"],
                     "assessment_count": app["assessment_count"] + 1,
                     "acquisition_json": self._store_layer1(app, inp, dec["route"] != routing.ROUTE_ALT_DATA and not l2["used"])}
            if alt_req:
                extra["info_request_json"] = json.dumps(alt_req)
            elif dec["final_credit_state"] == "MORE_INFORMATION_REQUIRED":
                extra["info_request_json"] = json.dumps({"requested_by": "DECISION_ENGINE", "ts": now_iso(), "route": dec["route"],
                                                         "items": dec["requested_items"], "reason": dec["customer_reason"]})
            if dec["route"] == routing.ROUTE_RETRY:
                # Automatic retry: no state change, no officer. The servicing cycle re-runs the assessment.
                rt_prev = _j(app["routing_json"], {}) or {}
                dec["retry"]["since"] = (rt_prev.get("retry") or {}).get("since") or now_iso()
                extra["routing_json"] = json.dumps(dec)
                self.db.update("applications", "application_id", app_id, extra, app["version"])
                self.db.audit("DECISION_ENGINE", "engine", "AUTOMATIC_RETRY_SCHEDULED", app["origin_transaction_id"], app_id,
                              rule_ids=dec["rule_ids"], details={"what": dec["retry"]["what"], "since": dec["retry"]["since"]})
                return self.application_view(app_id)
            for i, target in enumerate(dec["state_path"]):
                app = self._move_credit(app, target, dec["decided_by"] if target in ("APPROVED", "OFFER_PENDING") else "DECISION_ENGINE",
                                        "engine", "ENGINE_ROUTING", dec["rule_ids"], dec["reasons"][0]["message"] if dec["reasons"] else None,
                                        extra if i == 0 else None, reason_code=dec["reason_code"])
            link = "#/smart-credit/app/" + app_id
            final = dec["final_credit_state"]
            if final == "APPROVED":
                if app["kind"] == "LINE_REVIEW":
                    self._renew_line(app, dec["approved_limit_vnd"], dec["decided_by"], "engine", cap)
                else:
                    self._open_line(app, dec["approved_limit_vnd"], dec["decided_by"], "engine", {"capacity": cap, "band": "GREEN"})
                oc = self.policy["officer_controls"]
                pct = oc["layer2_qa_sample_pct"] if dec.get("layer2_used") else oc["green_qa_sample_pct"]
                if app["origin"] not in ("SEED", "SYSTEM") and int(hashlib.sha256(app_id.encode()).hexdigest(), 16) % 100 < pct:
                    self.db.run("UPDATE applications SET qa_status = 'PENDING' WHERE application_id = ?", (app_id,))
                    self.db.audit("SYSTEM", "qa-sampler", "GREEN_QA_SAMPLE_SELECTED", app["origin_transaction_id"], app_id,
                                  details={"sample_pct": pct, "method": "sha256(application_id) % 100"})
            elif final == "OFFER_PENDING":
                self.db.audit("DECISION_ENGINE", "engine", "COUNTER_OFFER_MADE", app["origin_transaction_id"], app_id,
                              details={"requested_vnd": app["requested_limit_vnd"], "offered_vnd": dec["approved_limit_vnd"]})
                self.notify(app["customer_id"], "ACTION", "activation_offer", {"limit": dec["approved_limit_vnd"]}, link,
                            "offer:%s:%s" % (app_id, app["assessment_count"]))
            elif final == "MANUAL_REVIEW":
                self.db.audit("DECISION_ENGINE", "engine", "REVIEW_CASE_CREATED", app["origin_transaction_id"], app_id,
                              rule_ids=dec["rule_ids"], details={"reason_code": dec["reason_code"], "kind": app["kind"]})
                if dec["reason_code"] == "R010-FRD":
                    self.db.audit("DECISION_ENGINE", "layer0", "FRAUD_SIGNAL_RAISED", app["origin_transaction_id"], app_id,
                                  rule_ids=[h["rule_id"] for h in ident["hits"]], details={"stage": "ACTIVATION"})
                if app["kind"] != "LINE_REVIEW":
                    self.notify(app["customer_id"], "STATUS", "activation_in_review",
                                {"hours": self.policy["officer_controls"]["customer_expected_decision_text_hours"]}, link,
                                "review:%s:%s" % (app_id, app["assessment_count"]))
            elif final == "MORE_INFORMATION_REQUIRED":
                self.notify(app["customer_id"], "ACTION", "activation_action_needed", {"reason": dec["customer_reason"]}, link,
                            "action:%s:%s" % (app_id, app["assessment_count"]))
            elif final == "DECLINED":
                self.notify(app["customer_id"], "STATUS", "activation_declined", {"reason": dec["customer_reason"]}, link,
                            "declined:" + app_id)
        return self.application_view(app_id)

    def _line_basis(self, cap: Dict[str, Any], limit: int, band: str, **extra: Any) -> Dict[str, Any]:
        """What the line was approved on. Checkout affordability uses the same income: the stressed Layer 1 income when the
        limit is within automatic capacity, otherwise the officer basis (average verified income)."""
        stp = cap.get("stp") or {}
        within_stp = bool(stp) and limit <= (stp.get("limit_vnd") or 0)
        inc = stp.get("income_vnd") if within_stp else (cap.get("officer") or {}).get("income_vnd")
        out = {"capacity": cap, "band": band, "affordability_income_vnd": inc,
               "affordability_basis": "STP_CONSERVATIVE_LAYER1" if within_stp else "OFFICER_" + str(cap.get("officer_basis")),
               "obligations_snapshot": cap.get("obligations"), "policy_version": self.policy["policy_version"],
               "policy_hash": self.policy_hash}
        out.update(extra)
        return out

    def _open_line(self, app: Dict[str, Any], limit: int, actor: str, actor_id: str, basis: Dict[str, Any]) -> None:
        line = self._line(app["customer_id"])
        ts = now_iso()
        basis = self._line_basis(basis.get("capacity") or {}, limit, basis.get("band", ""),
                                 **{k: v for k, v in basis.items() if k not in ("capacity", "band")})
        if app["kind"] in ("LIMIT_INCREASE", "LINE_REVIEW") and line:
            self.db.run("UPDATE credit_lines SET limit_vnd = ?, assessed_on = ?, basis_json = ?, version = version + 1, "
                        "updated_at = ? WHERE customer_id = ?",
                        (limit, self.as_of.isoformat(), json.dumps(basis), ts, app["customer_id"]))
            self.db.audit(actor, actor_id, "CREDIT_LIMIT_CHANGED" if app["kind"] == "LIMIT_INCREASE" else "CREDIT_LINE_RENEWED",
                          None, app["application_id"], details={"from_vnd": line["limit_vnd"], "to_vnd": limit})
            if app["kind"] == "LIMIT_INCREASE":
                self.notify(app["customer_id"], "STATUS", "limit_increased", {"limit": limit}, "#/smart-credit",
                            "limit-up:" + app["application_id"])
            else:
                self.notify(app["customer_id"], "STATUS", "line_renewed" if limit >= line["limit_vnd"] else "line_reduced",
                            {"limit": limit}, "#/smart-credit", "renew:" + app["application_id"])
        else:
            if line:
                self.db.run("UPDATE credit_lines SET status = 'ACTIVE', limit_vnd = ?, activation_id = ?, approved_at = ?, "
                            "assessed_on = ?, basis_json = ?, frozen_reason = NULL, version = version + 1, updated_at = ? "
                            "WHERE customer_id = ?", (limit, line["activation_id"] if app["kind"] == "LINE_REINSTATEMENT"
                                                      else app["application_id"], ts, self.as_of.isoformat(),
                                                      json.dumps(basis), ts, app["customer_id"]))
            else:
                self.db.run("INSERT INTO credit_lines(customer_id, status, limit_vnd, activation_id, approved_at, assessed_on, "
                            "basis_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                            (app["customer_id"], "ACTIVE", limit, app["application_id"], ts, self.as_of.isoformat(),
                             json.dumps(basis), ts))
            self.db.audit(actor, actor_id, "CREDIT_LINE_REINSTATED" if app["kind"] == "LINE_REINSTATEMENT" else "CREDIT_LINE_ACTIVATED",
                          app["origin_transaction_id"], app["application_id"],
                          details={"limit_vnd": limit, "assessed_on": self.as_of.isoformat()})
            self.notify(app["customer_id"], "STATUS", "line_reinstated" if app["kind"] == "LINE_REINSTATEMENT" else "activation_approved",
                        {"limit": limit}, "#/smart-credit", "activated:" + app["application_id"])
        self.db.run("UPDATE applications SET approved_limit_vnd = ? WHERE application_id = ?", (limit, app["application_id"]))

    def _renew_line(self, app: Dict[str, Any], limit: int, actor: str, actor_id: str, cap: Dict[str, Any]) -> None:
        self._open_line(app, limit, actor, actor_id, {"capacity": cap, "band": "GREEN_RENEWAL"})

    # ------------------------------------------------------------ counter-offer (audit F-04)
    def accept_offer(self, app_id: str, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            app = self._app(app_id, customer_id)
            if app["credit_state"] != "OFFER_PENDING":
                raise ServiceError(409, "NO_OFFER_PENDING", "There is no limit offer waiting for your answer.",
                                   {"current_state": app["credit_state"]})
            rt = _j(app["routing_json"], {}) or {}
            limit = rt.get("approved_limit_vnd")
            app = self._move_credit(app, "APPROVED", "CUSTOMER", customer_id, "COUNTER_OFFER_ACCEPTED", ["R001-CO"],
                                    "Customer accepted the counter-offer.", reason_code="R001-CO")
            self.db.audit("CUSTOMER", customer_id, "COUNTER_OFFER_ACCEPTED", app["origin_transaction_id"], app_id,
                          details={"requested_vnd": app["requested_limit_vnd"], "accepted_vnd": limit})
            self._open_line(app, limit, "STP_ENGINE", "engine", {"capacity": _j(app["capacity_json"], {}) or {},
                                                                 "band": "GREEN_COUNTER_OFFER_ACCEPTED"})
        return self.application_view(app_id, customer_id)

    def decline_offer(self, app_id: str, customer_id: str, ask_for_more: bool = False) -> Dict[str, Any]:
        """Decline the counter-offer: either close the request, or ask for more by sharing income held elsewhere
        (Layer 2, consent per source) for a credit officer to consider."""
        with self._unit():
            app = self._app(app_id, customer_id)
            if app["credit_state"] != "OFFER_PENDING":
                raise ServiceError(409, "NO_OFFER_PENDING", "There is no limit offer waiting for your answer.")
            self.db.audit("CUSTOMER", customer_id, "COUNTER_OFFER_DECLINED", app["origin_transaction_id"], app_id,
                          details={"ask_for_more": bool(ask_for_more)})
            if not ask_for_more:
                self._move_credit(app, "CANCELLED", "CUSTOMER", customer_id, "COUNTER_OFFER_DECLINED", ["R001-CO"])
            else:
                # The customer asked for more: the system requests the Layer 2 income sources for the segment (automatic
                # selection; target = the requested limit). Never an officer data request.
                req = self._alt_request(app, ["ADDITIONAL_INCOME"], "CUSTOMER", target_vnd=self._in_scope(app["requested_limit_vnd"]))
                if not req["sources"]:
                    raise ServiceError(409, "NO_SOURCE_AVAILABLE", "No additional data source is available for your profile.")
                self._move_credit(app, "MORE_INFORMATION_REQUIRED", "CUSTOMER", customer_id, "COUNTER_OFFER_ASK_FOR_MORE",
                                  ["R010-CAPX"], extra={"info_request_json": json.dumps(req)}, reason_code="R010-CAPX")
        return self.application_view(app_id, customer_id)

    # ------------------------------------------------------------ automatic line renewal (audit F-08)
    def review_due_lines(self, customer_id: Optional[str] = None) -> int:
        """From day (review_after - lead) re-run the activation rules on refreshed Layer 1 data. One renewal per
        assessment date; skipped while any request is open or the line is not ACTIVE."""
        cl = self.policy["credit_line"]
        due_age = cl["review_after_days"] - cl["renewal_lead_days"]
        rows = self.db.all("SELECT * FROM credit_lines WHERE status = 'ACTIVE'" + (" AND customer_id = ?" if customer_id else ""),
                           (customer_id,) if customer_id else ())
        n = 0
        for line in rows:
            if not line["assessed_on"]:
                continue
            age = (self.as_of - datetime.strptime(line["assessed_on"], "%Y-%m-%d").date()).days
            marker = "RENEWAL:" + line["assessed_on"]
            if age < due_age or self.db.one("SELECT 1 FROM applications WHERE customer_id = ? AND credit_state IN (%s)"
                                            % ",".join("?" * len(S.CREDIT_OPEN)), (line["customer_id"],) + tuple(S.CREDIT_OPEN)):
                continue
            if self.db.one("SELECT 1 FROM applications WHERE customer_id = ? AND kind = 'LINE_REVIEW' AND reason = ?",
                           (line["customer_id"], marker)):
                continue
            self.start_line_review(line["customer_id"], "SYSTEM", marker)
            n += 1
        return n

    def start_line_review(self, customer_id: str, origin: str, marker: str, note: Optional[str] = None) -> Dict[str, Any]:
        line = self._line(customer_id)
        with self._unit():
            app_id = self._new_id("LRV", "app")
            ts = now_iso()
            self.db.run("INSERT INTO applications(application_id, customer_id, kind, origin, requested_limit_vnd, credit_state, "
                        "reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (app_id, customer_id, "LINE_REVIEW", origin, line["limit_vnd"], "ASSESSMENT_PENDING", marker, ts, ts))
            self.db.audit("SYSTEM", "line-review", "LINE_REVIEW_STARTED", None, app_id,
                          details={"origin": origin, "assessed_on": line["assessed_on"], "limit_vnd": line["limit_vnd"], "note": note})
            if origin == "QA":
                # GREEN QA disagreement: the officer reviews the line; the limit is unchanged until a decision.
                self._move_credit(self._app(app_id), "MANUAL_REVIEW", "DECISION_ENGINE", "qa", "QA_FOLLOW_UP", ["QA-01"],
                                  note, extra={"band": "AMBER", "reason_code": "R010-REVIEW"}, reason_code="R010-REVIEW")
                self._snapshot_for_review(self._app(app_id))
        if origin == "QA":
            return self.application_view(app_id)
        return self.assess_activation(app_id, actor="SYSTEM")

    # ------------------------------------------------------------ customer actions (off the counter)
    def _options(self, app: Dict[str, Any]) -> List[str]:
        if app["credit_state"] == "OFFER_PENDING":
            return ["accept_offer", "request_more", "decline_offer"]
        req = _j(app["info_request_json"], {}) or {}
        if req.get("route") == "ALTERNATIVE_DATA":
            return ["alternative_data"]
        return []

    def appeal_decline(self, app_id: str, customer_id: str, reason: str) -> Dict[str, Any]:
        if not (reason or "").strip():
            raise ServiceError(422, "REASON_REQUIRED", "Tell us briefly why you'd like us to reconsider.")
        with self._unit():
            app = self._app(app_id, customer_id)
            if app["credit_state"] != "DECLINED" or app["appeal_count"] >= 1:
                raise ServiceError(409, "APPEAL_NOT_AVAILABLE", "This decision can't be appealed here. Please contact us.")
            self._move_credit(app, "MANUAL_REVIEW", "CUSTOMER", customer_id, "CUSTOMER_APPEAL", ["APPEAL"], reason[:500],
                              extra={"appeal_count": app["appeal_count"] + 1, "reason": reason[:500]}, reason_code=app["reason_code"])
            self.db.audit("CUSTOMER", customer_id, "APPEAL_LODGED", None, app_id, details={"reason": reason[:500]})
        return self.application_view(app_id, customer_id)

    def cancel_application(self, app_id: str, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            app = self._app(app_id, customer_id)
            if app["credit_state"] not in S.CREDIT_OPEN:
                raise ServiceError(409, "CANNOT_CANCEL", "This request can't be cancelled now.")
            self._move_credit(app, "CANCELLED", "CUSTOMER", customer_id, "CUSTOMER_CANCELLED")
        return self.application_view(app_id, customer_id)

    def withdraw_consent(self, app_id: str, customer_id: str) -> Dict[str, Any]:
        """Withdraw data permission for an open request: assessment stops (HS-01)."""
        with self._unit():
            app = self._app(app_id, customer_id)
            if app["credit_state"] not in S.CREDIT_OPEN:
                raise ServiceError(409, "CANNOT_WITHDRAW", "Consent for a decided request is withdrawn from your Smart Credit "
                                   "page (it closes the line to new purchases).")
            self.db.run("UPDATE consents SET withdrawn_at = ? WHERE application_id = ? AND withdrawn_at IS NULL", (now_iso(), app_id))
            self.db.audit("CUSTOMER", customer_id, "CONSENT_WITHDRAWN", None, app_id)
            self._move_credit(app, "POLICY_STOP", "DECISION_ENGINE", "routing", "HARD_STOP", ["HS-01"],
                              "Data-use consent withdrawn by customer.", {"reason_code": "HS-01", "band": "STOP"}, reason_code="HS-01")
        return self.application_view(app_id, customer_id)

    def withdraw_line_consent(self, customer_id: str) -> Dict[str, Any]:
        """Withdraw the assessment/monitoring consent of an ACTIVE line (audit F-18). Monitoring stops and the line closes to
        new drawdowns at once; existing instalment loans continue to be serviced. Legal deadline (Decree 356/2025): respond
        within 2 working days, implement within 15 days - the prototype implements immediately."""
        self.refresh_servicing(customer_id)
        with self._unit():
            line = self._line(customer_id)
            if not line or line["status"] not in ("ACTIVE", "FROZEN"):
                raise ServiceError(409, "NO_ACTIVE_LINE", "There is no active Smart Credit line to withdraw consent from.")
            ts = now_iso()
            n = self.db.run("UPDATE consents SET withdrawn_at = ? WHERE application_id = ? AND withdrawn_at IS NULL",
                            (ts, line["activation_id"])).rowcount
            for a in self.db.all("SELECT * FROM applications WHERE customer_id = ? AND credit_state IN (%s)"
                                 % ",".join("?" * len(S.CREDIT_OPEN)), (customer_id,) + tuple(S.CREDIT_OPEN)):
                self._move_credit(a, "POLICY_STOP" if a["credit_state"] != "ASSESSMENT_PENDING" else "CANCELLED", "DECISION_ENGINE",
                                  "routing", "HARD_STOP", ["HS-01"], "Line consent withdrawn by customer.",
                                  {"reason_code": "HS-01", "band": "STOP"} if a["credit_state"] != "ASSESSMENT_PENDING" else None,
                                  reason_code="HS-01")
            for h in self.db.all("SELECT transaction_id FROM exposure_holds WHERE customer_id = ? AND status = 'HELD'", (customer_id,)):
                self._release_hold(h["transaction_id"], "CUSTOMER", customer_id, "CONSENT_WITHDRAWN")
            self._set_line(customer_id, "CLOSED", "CUSTOMER", customer_id, "HS-01 consent withdrawn: no new drawdowns",
                           "CREDIT_LINE_CLOSED")
            wd = self.policy["consent"]["withdrawal"]
            self.db.audit("CUSTOMER", customer_id, "LINE_CONSENT_WITHDRAWN", None, line["activation_id"],
                          rule_ids=["HS-01"], details={"consents_withdrawn": n, "monitoring": "STOPPED", "new_drawdowns": "BLOCKED",
                                                       "existing_loans": "CONTINUE", "respond_within_working_days":
                                                       wd["respond_within_working_days"], "implement_within_days":
                                                       wd["implement_within_days"], "implemented": "IMMEDIATELY"})
            self.notify(customer_id, "STATUS", "line_consent_withdrawn", {}, "#/smart-credit", "lcw:%s:%s" % (customer_id, ts))
        return self.credit_summary(customer_id)

    # ------------------------------------------------------------ limit increase
    def create_limit_increase(self, customer_id: str, requested_limit: Any, reason: str,
                              linked_tx: Optional[str] = None) -> Dict[str, Any]:
        self.refresh_servicing(customer_id)
        cs = self.credit_summary(customer_id)
        if cs["line_status"] != "ACTIVE":
            raise ServiceError(409, "LINE_NOT_ACTIVE", "Activate your Smart Credit limit first.")
        if cs["open_application"]:
            raise ServiceError(409, "APPLICATION_OPEN", "You already have a Smart Credit request in progress.",
                               {"application_id": cs["open_application"]["application_id"]})
        if not isinstance(requested_limit, int) or isinstance(requested_limit, bool) or requested_limit <= cs["limit_vnd"]:
            raise ServiceError(422, "INVALID_LIMIT", "Requested limit must be above your current limit (VND {:,}).".format(cs["limit_vnd"]))
        if linked_tx:
            self._tx(linked_tx, customer_id)
        with self._unit():
            app_id = self._new_id("CLR", "app")
            ts = now_iso()
            self.db.run("INSERT INTO applications(application_id, customer_id, kind, origin, origin_transaction_id, "
                        "requested_limit_vnd, credit_state, band, reason_code, reason, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (app_id, customer_id, "LIMIT_INCREASE", "CHECKOUT" if linked_tx else "PRODUCTS", linked_tx,
                         requested_limit, "ASSESSMENT_PENDING", "AMBER", "R010-INCR", (reason or "")[:500], ts, ts))
            self.db.audit("CUSTOMER", customer_id, "LIMIT_INCREASE_REQUESTED", linked_tx, app_id,
                          details={"current_limit_vnd": cs["limit_vnd"], "requested_limit_vnd": requested_limit})
            self._refer_out_of_scope(app_id, customer_id, requested_limit)
        # v6: assessed automatically on refreshed Layer 1 (+ an automated Layer 2 request when Layer 1 is short of the request).
        return self.assess_activation(app_id, actor="CUSTOMER")

    def _snapshot_for_review(self, app: Dict[str, Any]) -> None:
        """Re-run Layer 1 acquisition + readiness + capacity for the officer (limit increases, officer refresh,
        reinstatement, QA follow-up). Uses only the consents the customer gave; keeps validated Layer 2 data."""
        inp = self._activation_inputs(app)
        pol = dict(self.policy)
        pol["mandatory_fields"] = dict(self.policy["mandatory_fields"], application=self.policy["mandatory_fields"]["activation_application"])
        res = R.evaluate(inp["application"], inp["applicant"], inp["sources"], inp["consent"], pol, self.as_of)
        inp["verified_fields"] = self._verified_fields(inp, res)
        cap = self._capacity(dict(app, input_snapshot_json=json.dumps(inp)))
        self.db.run("UPDATE applications SET input_snapshot_json = ?, readiness_json = ?, readiness_status = ?, capacity_json = ?, "
                    "acquisition_json = ? WHERE application_id = ?",
                    (json.dumps(inp), json.dumps(res), res["status"], json.dumps(cap),
                     self._store_layer1(app, inp, None), app["application_id"]))

    # ------------------------------------------------------------ views
    def list_applications(self, customer_id: str) -> List[Dict[str, Any]]:
        return [self.application_view(r["application_id"], customer_id) for r in
                self.db.all("SELECT application_id FROM applications WHERE customer_id = ? AND origin != 'SEED' "
                            "ORDER BY created_at DESC, rowid DESC", (customer_id,))]

    def application_view(self, app_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        app = self._app(app_id, customer_id)
        rd = _j(app["readiness_json"], {}) or {}
        rt = _j(app["routing_json"], {}) or {}
        info = _j(app["info_request_json"]) if app["credit_state"] == "MORE_INFORMATION_REQUIRED" else None
        status = {"APPROVED": "APPROVED", "MORE_INFORMATION_REQUIRED": "ACTION_NEEDED", "MANUAL_REVIEW": "IN_REVIEW",
                  "ASSESSMENT_PENDING": "CHECKING", "STP_ELIGIBLE": "CHECKING", "OFFER_PENDING": "OFFER",
                  "DECLINED": "NOT_APPROVED", "REJECTED": "NOT_APPROVED", "POLICY_STOP": "STOPPED",
                  "CANCELLED": "CANCELLED"}[app["credit_state"]]
        offered = rt.get("approved_limit_vnd") if app["credit_state"] == "OFFER_PENDING" else None
        return {
            "application_id": app["application_id"], "kind": app["kind"], "origin": app["origin"],
            "status": status, "credit_state": app["credit_state"], "band": app["band"],
            "reason_code": app["decision_reason_code"] or app["reason_code"], "customer_reason": rt.get("customer_reason")
            if app["credit_state"] not in ("REJECTED",) else "OFFICER_DECLINED",
            "requested_limit_vnd": app["requested_limit_vnd"], "approved_limit_vnd": app["approved_limit_vnd"],
            "offer": {"limit_vnd": offered, "requested_vnd": app["requested_limit_vnd"]} if offered else None,
            "counter_offer": bool(app["approved_limit_vnd"] and app["requested_limit_vnd"]
                                  and app["approved_limit_vnd"] < app["requested_limit_vnd"]),
            "options": self._options(app),
            # v6: the customer sees only WHICH sources are requested and WHY each is useful - never which Layer 1 data was
            # assessed or found missing.
            "info_request": {"route": info.get("route"), "requested_by": "SYSTEM",
                             "purposes": [m["item"] for m in info.get("missing", [])], "sources": info.get("sources", []),
                             "options": self._options(app)} if info else None,
            "out_of_scope": {"requested_vnd": app["requested_limit_vnd"],
                             "max_vnd": self.policy["affordability"]["product_max_limit_vnd"]}
            if (app["requested_limit_vnd"] or 0) > self.policy["affordability"]["product_max_limit_vnd"] else None,
            "checking_reason": "SOURCE_RETRY" if app["credit_state"] == "ASSESSMENT_PENDING"
            and (rt.get("route") == routing.ROUTE_RETRY) else None,
            "data_acquisition": self.acquisition_view(app, "CUSTOMER"),
            "appeal_available": app["credit_state"] == "DECLINED" and app["appeal_count"] < 1,
            "expected_decision_hours": self.policy["officer_controls"]["customer_expected_decision_text_hours"]
            if app["credit_state"] == "MANUAL_REVIEW" else None,
            "created_at": app["created_at"], "updated_at": app["updated_at"], "version": app["version"],
            "credit": self.credit_summary(app["customer_id"]),
        }
