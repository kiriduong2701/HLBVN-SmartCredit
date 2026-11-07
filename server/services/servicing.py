"""Credit position (exposure ledger), loan servicing, delinquency freeze/cure,
Pay Loan, reminders, notifications and the simulated business date.
Nothing is reported to CIC."""
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..store.db import now_iso
from .base import _utcnow, ServiceError, _j


class ServicingMixin:
    # ------------------------------------------------------------ credit line + exposure
    def _line(self, customer_id: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM credit_lines WHERE customer_id = ?", (customer_id,))

    def _set_line(self, customer_id: str, status: str, actor: str, actor_id: str, reason: Optional[str] = None,
                  event: str = "CREDIT_LINE_STATUS_CHANGED") -> None:
        line = self._line(customer_id)
        if not line or line["status"] == status:
            return
        self.db.run("UPDATE credit_lines SET status = ?, frozen_reason = ?, version = version + 1, updated_at = ? "
                    "WHERE customer_id = ?", (status, reason if status != "ACTIVE" else None, now_iso(), customer_id))
        self.db.audit(actor, actor_id, event, details={"customer_id": customer_id, "from": line["status"], "to": status,
                                                       "reason": reason})

    def aggregate_available(self, customer_id: str) -> Optional[int]:
        """Headroom under the per-customer digital-lending outstanding limit (Circular 39 Art. 22.2(h); value is a demo
        placeholder): cap - other HLB unsecured outstanding - Smart Credit outstanding principal - active holds."""
        cap = self.policy["aggregate_exposure"]["max_digital_unsecured_outstanding_vnd"]
        if cap is None:
            return None
        ex = self.exposure(customer_id)
        other = sum(int(p.get("outstanding_vnd") or 0) for p in (_j(self._customer(customer_id)["existing_products_json"], []) or []))
        return cap - other - ex["outstanding_principal_vnd"] - ex["held_vnd"]

    def exposure(self, customer_id: str) -> Dict[str, Any]:
        """Single source of truth for available credit: limit - outstanding PRINCIPAL - active holds."""
        rows = self.db.all("SELECT i.loan_ref, i.principal_vnd, i.amount_vnd, i.overdue_interest_vnd FROM loan_instalments i "
                           "JOIN loans l USING(loan_ref) WHERE l.customer_id = ? AND i.status != 'PAID'", (customer_id,))
        principal = sum(r["principal_vnd"] for r in rows)
        total_due = sum(r["amount_vnd"] + r["overdue_interest_vnd"] for r in rows)
        holds = self.db.all("SELECT hold_id, transaction_id, amount_vnd, monthly_instalment_vnd, created_at FROM exposure_holds "
                            "WHERE customer_id = ? AND status = 'HELD'", (customer_id,))
        monthly = 0
        for l in self.db.all("SELECT loan_ref FROM loans WHERE customer_id = ? AND status IN ('ACTIVE', 'DEFAULT')",
                             (customer_id,)):
            nxt = self.db.one("SELECT amount_vnd FROM loan_instalments WHERE loan_ref = ? AND status != 'PAID' "
                              "ORDER BY number LIMIT 1", (l["loan_ref"],))
            monthly += nxt["amount_vnd"] if nxt else 0
        monthly += sum(h["monthly_instalment_vnd"] for h in holds)
        return {"outstanding_principal_vnd": principal, "outstanding_total_vnd": total_due,
                "held_vnd": sum(h["amount_vnd"] for h in holds), "holds": holds,
                "smart_credit_monthly_vnd": monthly}

    def _delinquency(self, customer_id: str) -> Dict[str, Any]:
        today = self.as_of
        worst = 0
        for r in self.db.all("SELECT i.due_date FROM loan_instalments i JOIN loans l USING(loan_ref) "
                             "WHERE l.customer_id = ? AND i.status != 'PAID'", (customer_id,)):
            worst = max(worst, (today - datetime.strptime(r["due_date"], "%Y-%m-%d").date()).days)
        default = bool(self.db.one("SELECT 1 FROM loans WHERE customer_id = ? AND status = 'DEFAULT'", (customer_id,)))
        cfg = self.policy["delinquency"]
        return {"days_overdue": max(0, worst), "arrears": worst > cfg["freeze_when_days_overdue_gt"], "default": default}

    def credit_summary(self, customer_id: str) -> Dict[str, Any]:
        c = self._customer(customer_id)
        line = self._line(customer_id)
        ex = self.exposure(customer_id)
        limit = line["limit_vnd"] if line else 0
        status = line["status"] if line else "NONE"
        available = max(0, limit - ex["outstanding_principal_vnd"] - ex["held_vnd"]) if status == "ACTIVE" else 0
        agg = self.aggregate_available(customer_id)
        if agg is not None:
            available = max(0, min(available, agg))
        open_app = self.db.one("SELECT application_id, kind, credit_state FROM applications WHERE customer_id = ? AND "
                               "credit_state IN ('ASSESSMENT_PENDING', 'STP_ELIGIBLE', 'MORE_INFORMATION_REQUIRED', "
                               "'MANUAL_REVIEW', 'OFFER_PENDING') ORDER BY rowid DESC LIMIT 1", (customer_id,))
        existing = _j(c["existing_products_json"], []) or []
        age = None
        if line and line["assessed_on"]:
            age = (self.as_of - datetime.strptime(line["assessed_on"], "%Y-%m-%d").date()).days
        return {"line_status": status, "limit_vnd": limit, "available_vnd": available,
                "outstanding_principal_vnd": ex["outstanding_principal_vnd"], "outstanding_total_vnd": ex["outstanding_total_vnd"],
                "held_vnd": ex["held_vnd"], "holds": ex["holds"], "smart_credit_monthly_vnd": ex["smart_credit_monthly_vnd"],
                "frozen_reason": line["frozen_reason"] if line else None, "assessed_on": line["assessed_on"] if line else None,
                "assessment_age_days": age, "activation_id": line["activation_id"] if line else None,
                "review_in_progress": bool(open_app and open_app["kind"] == "LINE_REVIEW"),
                "aggregate_available_vnd": agg,
                "internal_delinquency_flag": c["internal_delinquency_flag"], "on_time_streak": c["on_time_streak"],
                "delinquency": self._delinquency(customer_id), "open_application": open_app,
                "other_hlb_exposure": existing,
                "total_hlb_exposure_vnd": ex["outstanding_principal_vnd"] + sum(p.get("outstanding_vnd", 0) for p in existing),
                "basis": "Available = limit - outstanding principal - active holds. Interest and fees are not counted "
                         "against the limit. Fictional data."}

    # ------------------------------------------------------------ business date (presenter control)
    def set_business_date(self, actor: str, new_date: Optional[str] = None, advance_days: Optional[int] = None) -> Dict[str, Any]:
        cur = self.as_of
        if new_date:
            try:
                target = datetime.strptime(new_date, "%Y-%m-%d").date()
            except ValueError:
                raise ServiceError(422, "INVALID_DATE", "Use YYYY-MM-DD.")
        elif isinstance(advance_days, int) and not isinstance(advance_days, bool) and 0 < advance_days <= 400:
            target = cur + timedelta(days=advance_days)
        else:
            raise ServiceError(422, "INVALID_CLOCK", "Give a date or advance_days (1-400).")
        if target < self.demo_date:
            raise ServiceError(422, "INVALID_DATE", "The demo date can't go before %s." % self.demo_date.isoformat())
        with self._unit():
            self.db.set_control("business_date", target.isoformat())
            self.db.audit("PRESENTER", actor, "BUSINESS_DATE_SET", details={"from": cur.isoformat(), "to": target.isoformat()})
        self.refresh_servicing()
        return {"business_date": target.isoformat()}

    # ------------------------------------------------------------ loans
    def _book_loan(self, customer_id: str, tx: Dict[str, Any], terms: Dict[str, Any], merchant_name: str,
                   origin: str = "CHECKOUT") -> str:
        loan_ref = self._new_id("LN", "loan")
        self.db.run("INSERT INTO loans(loan_ref, transaction_id, customer_id, principal_vnd, terms_json, created_at, "
                    "merchant_name, status, origin, purpose) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (loan_ref, tx["transaction_id"], customer_id, terms["principal_vnd"], json.dumps(terms), now_iso(),
                     merchant_name, "ACTIVE", origin, "PURCHASE_FINANCING:" + tx["transaction_id"]))
        for i in terms["instalments"]:
            self.db.run("INSERT INTO loan_instalments(loan_ref, number, due_date, amount_vnd, principal_vnd, interest_fee_vnd) "
                        "VALUES (?,?,?,?,?,?)", (loan_ref, i["number"], i["due_date"], i["amount_vnd"], i["principal_vnd"],
                                                 i["interest_fee_vnd"]))
        self.db.audit("SYSTEM", "simulator", "SIMULATED_LOAN_BOOKED", tx["transaction_id"],
                      details={"loan_ref": loan_ref, "principal_vnd": terms["principal_vnd"], "origin": origin,
                               "instalments": len(terms["instalments"]), "first_due": terms["first_due_date"],
                               "total_repayable_vnd": terms["total_repayable_vnd"]})
        self.notify(customer_id, "LOAN", "loan_booked", {"merchant": merchant_name, "n": len(terms["instalments"]),
                                                         "first_due": terms["first_due_date"]},
                    "#/loans/" + loan_ref, "loan:" + loan_ref)
        return loan_ref

    def refresh_servicing(self, customer_id: Optional[str] = None) -> None:
        """Reminders, overdue interest, internal delinquency flag, line freeze, simulated default, line renewals and
        automatic retries of assessments waiting on a data source."""
        cfg = self.policy["loan_servicing"]
        dq = self.policy["delinquency"]
        today = self.as_of
        sql = ("SELECT i.*, l.customer_id, l.merchant_name FROM loan_instalments i JOIN loans l USING(loan_ref) "
               "WHERE i.status != 'PAID'")
        args: List[Any] = []
        if customer_id:
            sql += " AND l.customer_id = ?"
            args.append(customer_id)
        for i in self.db.all(sql, args):
            due = datetime.strptime(i["due_date"], "%Y-%m-%d").date()
            link = "#/loans/" + i["loan_ref"]
            key = "%s:%s" % (i["loan_ref"], i["number"])
            p = {"merchant": i["merchant_name"], "n": i["number"], "amount": i["amount_vnd"], "due": i["due_date"]}
            with self._unit():
                if due - timedelta(days=cfg["reminder_days_before_due"]) <= today < due:
                    self.notify(i["customer_id"], "REMINDER", "instalment_due_soon", dict(p, days=(due - today).days), link,
                                "remind:" + key)
                elif today == due:
                    self.notify(i["customer_id"], "REMINDER", "instalment_due_today", p, link, "due:" + key)
                elif today > due:
                    accrued = self._overdue_interest(i, (today - due).days)
                    if i["status"] != "OVERDUE":
                        self.db.run("UPDATE loan_instalments SET status = 'OVERDUE', overdue_interest_vnd = ? WHERE loan_ref = ? "
                                    "AND number = ?", (accrued, i["loan_ref"], i["number"]))
                        self.db.run("UPDATE customers SET internal_delinquency_flag = 1, on_time_streak = 0 WHERE customer_id = ?",
                                    (i["customer_id"],))
                        self.db.audit("SYSTEM", "servicing", "INSTALMENT_OVERDUE",
                                      details={"loan_ref": i["loan_ref"], "number": i["number"], "due_date": i["due_date"],
                                               "overdue_interest_vnd": accrued, "late_fee_vnd": 0,
                                               "basis": "Circular 39 Art. 13.4 caps; no flat late fee (Art. 14)",
                                               "internal_flag": "set (simulated, not reported to CIC)"})
                        self.notify(i["customer_id"], "OVERDUE", "instalment_overdue", dict(p, fee=accrued), link, "overdue:" + key)
                    elif accrued != i["overdue_interest_vnd"]:
                        self.db.run("UPDATE loan_instalments SET overdue_interest_vnd = ? WHERE loan_ref = ? AND number = ?",
                                    (accrued, i["loan_ref"], i["number"]))
        # Line freeze while arrears exist (hard stop HS-03), and simulated default (HS-04).
        custs = [customer_id] if customer_id else [r["customer_id"] for r in self.db.all("SELECT customer_id FROM credit_lines")]
        for cid in custs:
            d = self._delinquency(cid)
            with self._unit():
                for row in self.db.all("SELECT DISTINCT l.loan_ref, i.due_date FROM loans l JOIN loan_instalments i USING(loan_ref) "
                                       "WHERE l.customer_id = ? AND l.status = 'ACTIVE' AND i.status != 'PAID'", (cid,)):
                    od = (today - datetime.strptime(row["due_date"], "%Y-%m-%d").date()).days
                    if od > dq["default_after_days_overdue"]:
                        self.db.run("UPDATE loans SET status = 'DEFAULT' WHERE loan_ref = ? AND status = 'ACTIVE'", (row["loan_ref"],))
                        self.db.audit("SYSTEM", "servicing", "SIMULATED_DEFAULT",
                                      details={"loan_ref": row["loan_ref"], "days_overdue": od,
                                               "note": "Demo-only internal status; not reported to CIC. Collections hand-off simulated."})
                        self.notify(cid, "OVERDUE", "loan_default", {"loan": row["loan_ref"]}, "#/loans/" + row["loan_ref"],
                                    "default:" + row["loan_ref"])
                        d["default"] = True
                line = self._line(cid)
                if not line:
                    continue
                if d["default"] and dq["default_suspends_line"] and line["status"] != "SUSPENDED":
                    self._set_line(cid, "SUSPENDED", "SYSTEM", "servicing", "HS-04 default", "CREDIT_LINE_SUSPENDED")
                elif d["arrears"] and line["status"] == "ACTIVE":
                    self._set_line(cid, "FROZEN", "SYSTEM", "servicing", "HS-03 arrears %s day(s)" % d["days_overdue"],
                                   "CREDIT_LINE_FROZEN")
                    self.notify(cid, "OVERDUE", "line_frozen", {"days": d["days_overdue"]}, "#/smart-credit", "frozen:%s:%s"
                                % (cid, today.isoformat()))
                elif not d["arrears"] and not d["default"] and line["status"] == "FROZEN":
                    self._set_line(cid, "ACTIVE", "SYSTEM", "servicing", None, "CREDIT_LINE_UNFROZEN")
                    self.notify(cid, "STATUS", "line_unfrozen", {}, "#/smart-credit", "unfrozen:%s:%s" % (cid, today.isoformat()))
            self._cure_defaults(cid)
            self._recompute_cure(cid)
        if not getattr(self, "_in_line_review", False):
            self._in_line_review = True
            try:
                for cid in custs:
                    self.review_due_lines(cid)
                self.retry_pending_assessments(customer_id)
            finally:
                self._in_line_review = False

    def retry_pending_assessments(self, customer_id: Optional[str] = None) -> int:
        """Automatic data operations (v6): requests waiting on an unavailable Layer 1 or Layer 2 source are retried on every
        servicing cycle and the rules re-run when the source answers. After the retry window the customer is told it is
        taking longer and operations are alerted once. Officers are never involved."""
        from ..engine import routing
        rows = self.db.all("SELECT * FROM applications WHERE credit_state = 'ASSESSMENT_PENDING'"
                           + (" AND customer_id = ?" if customer_id else ""), (customer_id,) if customer_id else ())
        n = 0
        window = self.policy["data_acquisition"]["retry"]["window_hours"]
        for app in rows:
            rt = _j(app["routing_json"], {}) or {}
            if rt.get("route") != routing.ROUTE_RETRY:
                continue
            what = (rt.get("retry") or {}).get("what", "")
            if "Layer 2" in what:
                if not self.retry_layer2(app["application_id"]):
                    self._retry_overdue(app, rt, window)
                    continue
            elif self.flag("cic_unavailable"):
                self._retry_overdue(app, rt, window)
                continue
            self.assess_activation(app["application_id"], actor="SYSTEM")
            n += 1
        return n

    def _retry_overdue(self, app: Dict[str, Any], rt: Dict[str, Any], window_h: int) -> None:
        since = (rt.get("retry") or {}).get("since")
        if not since or (_utcnow() - datetime.fromisoformat(since)).total_seconds() < window_h * 3600:
            return
        if self.db.one("SELECT 1 FROM audit_events WHERE application_id = ? AND event_type = 'DATA_SOURCE_OUTAGE_ESCALATED'",
                       (app["application_id"],)):
            return
        with self._unit():
            self.db.audit("SYSTEM", "data-ops", "DATA_SOURCE_OUTAGE_ESCALATED", None, app["application_id"],
                          details={"since": since, "window_hours": window_h, "to": "operations (not a credit officer)"})
            self.notify(app["customer_id"], "STATUS", "assessment_delayed", {}, "#/smart-credit/app/" + app["application_id"],
                        "delayed:" + app["application_id"])

    def _overdue_interest(self, inst: Dict[str, Any], days: int) -> int:
        """Circular 39 Art. 13.4: overdue principal at <= 150% of the in-term rate; unpaid interest at <= 10%/year.
        Simple daily accrual from the due date (rates are demo values inside the legal caps)."""
        od = self.policy["loan_servicing"]["overdue_interest"]
        rate = self.policy["repayment_terms"]["annual_interest_rate_pct"] / 100.0
        principal = inst["principal_vnd"] * rate * od["overdue_principal_rate_multiplier"] * days / od["day_count"]
        late = inst["interest_fee_vnd"] * od["late_interest_rate_pct_per_year"] / 100.0 * days / od["day_count"]
        return int(round(principal + late))

    def _cure_defaults(self, customer_id: str) -> None:
        """A defaulted loan whose overdue instalments are all paid returns to ACTIVE servicing (default cured). The line
        stays SUSPENDED: an officer reinstatement case decides whether to reopen it (audit F-17)."""
        for ln in self.db.all("SELECT * FROM loans WHERE customer_id = ? AND status = 'DEFAULT'", (customer_id,)):
            overdue = self.db.one("SELECT COUNT(*) n FROM loan_instalments WHERE loan_ref = ? AND status = 'OVERDUE'",
                                  (ln["loan_ref"],))["n"]
            if overdue:
                continue
            with self._unit():
                left = self.db.one("SELECT COUNT(*) n FROM loan_instalments WHERE loan_ref = ? AND status != 'PAID'",
                                   (ln["loan_ref"],))["n"]
                self.db.run("UPDATE loans SET status = ?, default_cured_on = ?, closed_at = ? WHERE loan_ref = ?",
                            ("ACTIVE" if left else "CLOSED", self.as_of.isoformat(), None if left else now_iso(), ln["loan_ref"]))
                self.db.audit("SYSTEM", "servicing", "DEFAULT_CURED", details={"loan_ref": ln["loan_ref"], "remaining": left,
                              "note": "All overdue amounts paid. Line stays suspended until an officer reinstates it."})
        line = self._line(customer_id)
        if line and line["status"] == "SUSPENDED" and not self._delinquency(customer_id)["default"] \
                and not self.db.one("SELECT 1 FROM applications WHERE customer_id = ? AND kind = 'LINE_REINSTATEMENT' "
                                    "AND credit_state IN ('MANUAL_REVIEW', 'MORE_INFORMATION_REQUIRED')", (customer_id,)) \
                and not self.db.one("SELECT 1 FROM applications WHERE customer_id = ? AND kind = 'LINE_REINSTATEMENT' AND reason = ?",
                                    (customer_id, "SUSPENDED_SINCE:" + str(line["updated_at"]))):
            self._open_reinstatement_case(customer_id, line)

    def _open_reinstatement_case(self, customer_id: str, line: Dict[str, Any]) -> None:
        with self._unit():
            app_id = self._new_id("RIN", "app")
            ts = now_iso()
            self.db.run("INSERT INTO applications(application_id, customer_id, kind, origin, requested_limit_vnd, credit_state, band, "
                        "reason_code, reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (app_id, customer_id, "LINE_REINSTATEMENT", "SYSTEM", line["limit_vnd"], "ASSESSMENT_PENDING", "AMBER",
                         "R010-REINST", "SUSPENDED_SINCE:" + str(line["updated_at"]), ts, ts))
            self._move_credit(self._app(app_id), "MANUAL_REVIEW", "DECISION_ENGINE", "servicing", "REINSTATEMENT_TO_REVIEW",
                              ["R-REINST-01"], "Default cured; reinstatement of the suspended line is decided by a credit officer.",
                              reason_code="R010-REINST")
            self._snapshot_for_review(self._app(app_id))
            self.notify(customer_id, "STATUS", "reinstatement_in_review", {}, "#/smart-credit/app/" + app_id, "reinst:" + app_id)

    def _cure_streak(self, customer_id: str) -> int:
        """Consecutive instalments, most recent first, whose due date has passed and that were paid on or before it.
        Prepaying future instalments does not count (audit F-15)."""
        rows = self.db.all("SELECT i.due_date, i.status, i.paid_on FROM loan_instalments i JOIN loans l USING(loan_ref) "
                           "WHERE l.customer_id = ? AND i.due_date < ? ORDER BY i.due_date DESC, i.number DESC",
                           (customer_id, self.as_of.isoformat()))
        n = 0
        for r in rows:
            if r["status"] == "PAID" and r["paid_on"] and r["paid_on"] <= r["due_date"]:
                n += 1
            else:
                break
        return n

    def _recompute_cure(self, customer_id: str) -> None:
        cust = self._customer(customer_id)
        streak = self._cure_streak(customer_id)
        need = self.policy["delinquency"]["cure_on_time_instalments"]
        with self._unit():
            if streak != cust["on_time_streak"]:
                self.db.run("UPDATE customers SET on_time_streak = ? WHERE customer_id = ?", (streak, customer_id))
            if cust["internal_delinquency_flag"] and streak >= need and not self._delinquency(customer_id)["arrears"]:
                self.db.run("UPDATE customers SET internal_delinquency_flag = 0 WHERE customer_id = ?", (customer_id,))
                self.db.audit("SYSTEM", "servicing", "DELINQUENCY_FLAG_CURED",
                              details={"customer_id": customer_id, "on_time_instalments": streak,
                                       "rule": "cure_on_time_instalments=%d, counted only after each due date" % need})
                self.notify(customer_id, "STATUS", "flag_cured", {"n": streak}, "#/smart-credit",
                            "cured:%s:%s" % (customer_id, self.as_of.isoformat()))

    def _loan_view(self, loan: Dict[str, Any]) -> Dict[str, Any]:
        inst = self.db.all("SELECT * FROM loan_instalments WHERE loan_ref = ? ORDER BY number", (loan["loan_ref"],))
        today = self.as_of
        for i in inst:
            i["total_due_vnd"] = i["amount_vnd"] + i["overdue_interest_vnd"]
            if i["status"] == "SCHEDULED":
                due = datetime.strptime(i["due_date"], "%Y-%m-%d").date()
                i["display_status"] = "DUE_TODAY" if due == today else (
                    "DUE_SOON" if 0 < (due - today).days <= self.policy["loan_servicing"]["reminder_days_before_due"] else "SCHEDULED")
            else:
                i["display_status"] = i["status"]
        unpaid = [i for i in inst if i["status"] != "PAID"]
        reps = self.db.all("SELECT * FROM loan_repayments WHERE loan_ref = ? ORDER BY created_at DESC, rowid DESC", (loan["loan_ref"],))
        for r in reps:
            r["instalments"] = _j(r.pop("instalments_json"), [])
            r.pop("idempotency_key", None)
        terms = _j(loan["terms_json"], {})
        principal_left = sum(i["principal_vnd"] for i in unpaid)
        return {
            "loan_ref": loan["loan_ref"], "merchant_name": loan["merchant_name"], "status": loan["status"],
            "transaction_id": loan["transaction_id"], "origin": loan["origin"],
            "principal_vnd": loan["principal_vnd"], "created_at": loan["created_at"], "closed_at": loan["closed_at"],
            "terms": {k: terms.get(k) for k in ("label", "tenure_months", "annual_interest_rate_pct", "rate_basis",
                                                "effective_annual_rate_pct", "equivalent_reducing_rate_pct",
                                                "total_interest_vnd", "total_repayable_vnd", "monthly_instalment_vnd")},
            "outstanding_vnd": sum(i["total_due_vnd"] for i in unpaid), "outstanding_principal_vnd": principal_left,
            # Early-settlement quote: remaining principal + accrued overdue interest; unearned flat interest is waived
            # (DEMO_ASSUMPTION - early-settlement policy to be set by HLBVN).
            "early_settlement_quote_vnd": principal_left + sum(i["overdue_interest_vnd"] for i in unpaid),
            "paid_count": len(inst) - len(unpaid), "instalment_count": len(inst),
            "overdue_count": sum(1 for i in unpaid if i["status"] == "OVERDUE"),
            "collections_status": "HANDED_OFF_TO_COLLECTIONS (simulated)" if loan["status"] == "DEFAULT" else None,
            "next_due": unpaid[0] if unpaid else None, "instalments": inst, "repayments": reps,
            "business_date": today.isoformat(),
        }

    def list_loans(self, customer_id: str) -> Dict[str, Any]:
        self._customer(customer_id)
        self.refresh_servicing(customer_id)
        loans = [self._loan_view(l) for l in self.db.all("SELECT * FROM loans WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC",
                                                          (customer_id,))]
        return {"business_date": self.as_of.isoformat(), "loans": loans, "credit": self.credit_summary(customer_id)}

    def get_loan(self, loan_ref: str, customer_id: str) -> Dict[str, Any]:
        self.refresh_servicing(customer_id)
        loan = self.db.one("SELECT * FROM loans WHERE loan_ref = ?", (loan_ref,))
        if not loan:
            raise ServiceError(404, "LOAN_NOT_FOUND", "Loan %s not found." % loan_ref)
        if loan["customer_id"] != customer_id:
            raise ServiceError(403, "FORBIDDEN", "This loan belongs to another customer.")
        out = self._loan_view(loan)
        out["account"] = {k: self._customer(customer_id)[k] for k in ("account_masked", "balance_vnd")}
        return out

    def repay(self, loan_ref: str, customer_id: str, numbers: List[int], idempotency_key: str) -> Dict[str, Any]:
        """Pay Loan: simulated account debit for the oldest unpaid instalments."""
        if not idempotency_key or len(idempotency_key) > 100:
            raise ServiceError(422, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")
        if not numbers or not all(isinstance(n, int) and not isinstance(n, bool) for n in numbers):
            raise ServiceError(422, "SELECT_INSTALMENTS", "Select at least one instalment to pay.")
        self.refresh_servicing(customer_id)
        prior = self.db.one("SELECT * FROM loan_repayments WHERE idempotency_key = ?", (idempotency_key,))
        if prior:
            if prior["loan_ref"] != loan_ref:
                raise ServiceError(409, "IDEMPOTENCY_KEY_REUSED", "This key was used for another repayment.")
            out = self.get_loan(loan_ref, customer_id)
            out["idempotent_replay"] = True
            return out
        failure = None
        with self._unit():
            loan = self.db.one("SELECT * FROM loans WHERE loan_ref = ?", (loan_ref,))
            if not loan or loan["customer_id"] != customer_id:
                raise ServiceError(404, "LOAN_NOT_FOUND", "Loan %s not found." % loan_ref)
            if loan["status"] not in ("ACTIVE", "DEFAULT"):
                raise ServiceError(409, "LOAN_CLOSED", "This loan is already fully repaid.")
            unpaid = self.db.all("SELECT * FROM loan_instalments WHERE loan_ref = ? AND status != 'PAID' ORDER BY number",
                                 (loan_ref,))
            wanted = sorted(set(numbers))
            expected = [i["number"] for i in unpaid[:len(wanted)]]
            if wanted != expected:
                raise ServiceError(422, "PAY_OLDEST_FIRST", "Please pay the oldest unpaid instalments first (instalment %s)."
                                   % (unpaid[0]["number"] if unpaid else "-"), {"next_unpaid": expected})
            chosen = unpaid[:len(wanted)]
            amount = sum(i["amount_vnd"] + i["overdue_interest_vnd"] for i in chosen)
            cust = self._customer(customer_id)
            rep_id = self._new_id("RP", "repayment")
            if cust["balance_vnd"] < amount:
                self.db.run("INSERT INTO loan_repayments VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (rep_id, loan_ref, customer_id, json.dumps(wanted), amount, idempotency_key, "FAILED",
                             "Insufficient fictional account balance.", self.as_of.isoformat(), now_iso()))
                self.db.audit("CUSTOMER", customer_id, "LOAN_REPAYMENT_FAILED", loan["transaction_id"],
                              details={"loan_ref": loan_ref, "repayment_id": rep_id, "amount_vnd": amount,
                                       "reason": "INSUFFICIENT_FUNDS"})
                failure = ServiceError(409, "INSUFFICIENT_FUNDS", "Your account balance (VND {:,}) is not enough for VND {:,}."
                                       .format(cust["balance_vnd"], amount), {"balance_vnd": cust["balance_vnd"], "amount_vnd": amount})
            else:
                self._apply_repayment(loan, customer_id, chosen, amount, rep_id, idempotency_key)
        if failure:  # the failed attempt is committed (recorded) before reporting it
            raise failure
        self.refresh_servicing(customer_id)
        return self.get_loan(loan_ref, customer_id)

    def _apply_repayment(self, loan: Dict[str, Any], customer_id: str, chosen: List[Dict[str, Any]], amount: int,
                         rep_id: str, idempotency_key: str) -> None:
        loan_ref = loan["loan_ref"]
        ts, today = now_iso(), self.as_of
        self.db.run("UPDATE customers SET balance_vnd = balance_vnd - ? WHERE customer_id = ?", (amount, customer_id))
        on_time = late = 0
        for i in chosen:
            self.db.run("UPDATE loan_instalments SET status = 'PAID', paid_at = ?, paid_on = ?, repayment_id = ? "
                        "WHERE loan_ref = ? AND number = ?", (ts, today.isoformat(), rep_id, loan_ref, i["number"]))
            if i["status"] == "OVERDUE" or today > datetime.strptime(i["due_date"], "%Y-%m-%d").date():
                late += 1
            else:
                on_time += 1
        self.db.run("INSERT INTO loan_repayments VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rep_id, loan_ref, customer_id, json.dumps([i["number"] for i in chosen]), amount, idempotency_key,
                     "SUCCEEDED", None, today.isoformat(), ts))
        left = self.db.one("SELECT COUNT(*) n FROM loan_instalments WHERE loan_ref = ? AND status != 'PAID'", (loan_ref,))["n"]
        if left == 0:
            self.db.run("UPDATE loans SET status = 'CLOSED', closed_at = ? WHERE loan_ref = ?", (ts, loan_ref))
        self.db.audit("CUSTOMER", customer_id, "LOAN_REPAYMENT", loan["transaction_id"],
                      details={"loan_ref": loan_ref, "repayment_id": rep_id, "instalments": [i["number"] for i in chosen],
                               "amount_vnd": amount, "on_time": on_time, "late": late, "remaining_instalments": left})
        # Cure rule (audit F-15): evaluated by refresh_servicing from due dates, never from same-day prepayments.
        self.notify(customer_id, "LOAN", "repayment_received", {"amount": amount, "merchant": loan["merchant_name"],
                                                                "n": len(chosen), "left": left},
                    "#/loans/" + loan_ref, "repay:" + rep_id)

    # ------------------------------------------------------------ notifications
    def notifications(self, customer_id: str) -> Dict[str, Any]:
        self._customer(customer_id)
        self.sweep_expired()
        self.refresh_servicing(customer_id)
        rows = self.db.all("SELECT notification_id, ts, business_date, kind, title_key, params_json, link, read_at "
                           "FROM notifications WHERE customer_id = ? ORDER BY notification_id DESC LIMIT 100", (customer_id,))
        for r in rows:
            r["params"] = _j(r.pop("params_json"), {})
        return {"business_date": self.as_of.isoformat(), "items": rows, "unread": sum(1 for r in rows if not r["read_at"])}

    def mark_notifications_read(self, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            self.db.run("UPDATE notifications SET read_at = ? WHERE customer_id = ? AND read_at IS NULL", (now_iso(), customer_id))
        return self.notifications(customer_id)
