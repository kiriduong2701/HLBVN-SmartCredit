"""Instant Checkout (journey 2): QR -> Pay Now or Pay Later on an approved line ->
review & confirm -> step-up authentication -> simulated payment -> settlement.

No credit assessment and no human review happens here. Pay Later only spends an
already-approved line, under mandatory transaction checks and an exposure hold
taken in the same database transaction as the check (BEGIN IMMEDIATE), and
re-checked inside the payment transaction. Anything not instantly decidable falls
back to Pay Now (with the option to convert to instalments within 7 days).
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..domain import states as S
from ..engine import capacity as CAP
from ..engine import checkout_rules as CR
from ..engine import identity as IDN
from ..engine import qr as QR
from ..engine import terms as T
from ..store.db import now_iso
from . import payment_gateway
from .base import ACCOUNT, CREDIT_METHODS, PAY_LATER, SPLIT, ServiceError, _j


class CheckoutMixin:
    def get_customer(self, customer_id: str) -> Dict[str, Any]:
        self.refresh_servicing(customer_id)
        c = self._customer(customer_id)
        return {"customer_id": c["customer_id"], "display_name": c["display_name"], "initials": c["initials"],
                "account_masked": c["account_masked"], "balance_vnd": c["balance_vnd"],
                "credit": self.credit_summary(customer_id), "business_date": self.as_of.isoformat(),
                "unread_notifications": self.db.one("SELECT COUNT(*) n FROM notifications WHERE customer_id = ? AND read_at IS NULL",
                                                    (customer_id,))["n"]}

    # ------------------------------------------------------------ catalogue (simulated camera)
    def list_qr_codes(self, customer_id: str) -> List[Dict[str, Any]]:
        self._customer(customer_id)
        rows = self.db.all("SELECT q.qr_ref, q.kind, q.amount_vnd, q.description, q.payload, m.name AS merchant_name, "
                           "m.category, m.initials FROM qr_codes q JOIN merchants m USING(merchant_id) ORDER BY q.qr_ref")
        # A live-demo order (QR-LIVE-NN) belongs to the hypothetical customer CUS-JNN typed in on the presenter page: that
        # customer sees only their own order (one clear story on stage) and nobody else sees it.
        own = "QR-LIVE-" + customer_id[5:] if customer_id.startswith("CUS-J") else None
        rows = [r for r in rows if r["qr_ref"] == own] if own else [r for r in rows if not r["qr_ref"].startswith("QR-LIVE-")]
        for r in rows:
            r["already_paid"] = bool(self.db.one("SELECT 1 FROM transactions WHERE qr_ref = ? AND state = 'PAYMENT_COMPLETED' "
                                                 "AND qr_kind = 'DYNAMIC'", (r["qr_ref"],)))
        return rows

    # ------------------------------------------------------------ merchant eligibility (MCC + partner registry)
    def merchant_eligibility(self, merchant: Dict[str, Any], qr_kind: str) -> Dict[str, Any]:
        cfg = self.policy["merchant_eligibility"]
        if merchant["mcc"] in cfg["excluded_mcc"]:
            return {"eligible": False, "code": "EXCLUDED_MCC", "reason": "MCC %s (%s) is excluded from Pay Later."
                    % (merchant["mcc"], cfg["excluded_mcc"][merchant["mcc"]])}
        if cfg["require_partner_merchant"] and not merchant["partner_merchant"]:
            return {"eligible": False, "code": "NOT_PARTNER", "reason": "Merchant is not a Smart Credit partner merchant."}
        if qr_kind != "DYNAMIC":
            return {"eligible": False, "code": "STATIC_QR", "reason": "Static QR: the order and amount cannot be verified with "
                    "the merchant, so Pay Later is not offered."}
        return {"eligible": True, "code": "ELIGIBLE", "reason": "Partner merchant (%s), MCC %s." % (merchant["partner_program"],
                                                                                                merchant["mcc"])}

    # ------------------------------------------------------------ 1. QR
    def resolve_qr(self, customer_id: str, payload: str, amount_vnd: Any = None) -> Dict[str, Any]:
        self.sweep_expired()
        self._customer(customer_id)
        try:
            parsed = QR.parse(payload)
        except QR.QRError as e:
            with self._unit():
                self.db.audit("CUSTOMER", customer_id, "QR_REJECTED", details={"code": e.code, "reason": str(e)})
            raise ServiceError(422, e.code, str(e) + " Nothing was charged.")
        with self._unit():
            m = self.db.one("SELECT * FROM merchants WHERE acquirer_bin = ? AND merchant_account = ?",
                            (parsed["acquirer_bin"], parsed["merchant_account"]))
            if not m:
                self.db.audit("CUSTOMER", customer_id, "QR_REJECTED", details={"reason": "unknown merchant account"})
                raise ServiceError(422, "QR_MERCHANT_UNKNOWN", "This merchant could not be identified. Nothing was charged.")
            if parsed["initiation"] == "DYNAMIC":
                # Simulated merchant-order API (PROPOSED): the order record, not the QR text, is the source of truth.
                order = self.db.one("SELECT * FROM qr_codes WHERE merchant_id = ? AND bill_number = ? AND kind = 'DYNAMIC'",
                                    (m["merchant_id"], parsed["bill_number"]))
                if not order or order["amount_vnd"] != parsed["amount_vnd"]:
                    self.db.audit("CUSTOMER", customer_id, "QR_REJECTED",
                                  details={"bill_number": parsed["bill_number"], "qr_amount": parsed["amount_vnd"],
                                           "reason": "amount mismatch with merchant order" if order else "unknown order"})
                    raise ServiceError(422, "QR_INVALID", "The QR details don't match the merchant's order. Nothing was charged.")
                amount, source = order["amount_vnd"], "MERCHANT_ORDER_RECORD"
                existing = self.db.one("SELECT * FROM transactions WHERE qr_ref = ? AND state NOT IN ('CANCELLED', 'EXPIRED')",
                                       (order["qr_ref"],))
                if existing:
                    if existing["state"] == "PAYMENT_COMPLETED":
                        self.db.audit("CUSTOMER", customer_id, "DUPLICATE_QR_BLOCKED", existing["transaction_id"])
                        raise ServiceError(409, "QR_ALREADY_PAID", "This order has already been paid. No second payment will "
                                           "be made.", {"transaction_id": existing["transaction_id"]})
                    if existing["customer_id"] != customer_id:
                        raise ServiceError(409, "QR_IN_USE", "This order is being paid by another customer.")
                    self.db.audit("CUSTOMER", customer_id, "QR_TRANSACTION_RESUMED", existing["transaction_id"])
                    return {"resumed": True, "view": self._customer_view(existing["transaction_id"])}
            else:
                if not isinstance(amount_vnd, int) or isinstance(amount_vnd, bool) or not (1000 <= amount_vnd <= 500000000):
                    raise ServiceError(422, "AMOUNT_REQUIRED", "Enter the amount to pay.", {"static_qr": True,
                                                                                         "merchant_name": m["name"]})
                order = self.db.one("SELECT * FROM qr_codes WHERE merchant_id = ? AND kind = 'STATIC'", (m["merchant_id"],))
                if not order:
                    raise ServiceError(422, "QR_MERCHANT_UNKNOWN", "This merchant QR is not registered.")
                amount, source = amount_vnd, "CUSTOMER_ENTERED"
            tx_id = self._new_id("TXN", "tx")
            ts = now_iso()
            self.db.run("INSERT INTO transactions(transaction_id, qr_ref, qr_kind, customer_id, merchant_id, amount_vnd, "
                        "amount_source, description, state, expires_at, business_date, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tx_id, order["qr_ref"], order["kind"], customer_id, m["merchant_id"], amount, source,
                         order["description"], "QR_CREATED", self._deadline(), self.as_of.isoformat(), ts, ts))
            self.db.audit("CUSTOMER", customer_id, "QR_TRANSACTION_CREATED", tx_id, to_state="QR_CREATED",
                          details={"qr_ref": order["qr_ref"], "scheme": parsed["scheme"], "initiation": parsed["initiation"],
                                   "mcc": parsed["mcc"], "amount_vnd": amount, "amount_source": source,
                                   "crc": "valid (integrity only, not authenticity)"})
        return {"resumed": False, "view": self._customer_view(tx_id)}

    # ------------------------------------------------------------ 2a. Pay Now
    def choose_pay_now(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        self.sweep_expired()
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] not in ("QR_CREATED", "TERMS_PENDING_CONFIRMATION", "PAYMENT_FAILED", "PAYMENT_METHOD_SELECTED"):
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "The payment method can't be changed now.",
                                   {"current_state": tx["state"]})
            if tx["state"] == "PAYMENT_METHOD_SELECTED" and tx["method"] == ACCOUNT:
                return self._customer_view(tx_id)
            self._move_tx(tx, "PAYMENT_METHOD_SELECTED", "CUSTOMER", customer_id,
                          {"method": ACCOUNT, "tenure_months": None, "terms_json": None, "agreement_accepted_at": None,
                           "pay_later_amount_vnd": None, "pay_now_amount_vnd": None},
                          {"method": ACCOUNT}, "PAYMENT_METHOD_SELECTED")
        return self._customer_view(tx_id)

    # ------------------------------------------------------------ 2b. Pay Later (instant; optional inline activation)
    def _credit_inputs(self, customer_id: str) -> Dict[str, Any]:
        """Income and obligations for checkout affordability. External obligations come from the line's last assessment;
        current HLB internal instalments are re-read and added (deduplicated) at every checkout (audit F-07); the income
        is the one the line was approved on (stressed Layer 1, or the officer's average-income basis)."""
        line = self._line(customer_id)
        income = {"verified": False}
        obligations = {"known": False}
        if line:
            basis = _j(line["basis_json"], {}) or {}
            cap = basis.get("capacity") or {}
            income = dict(cap.get("income") or income)
            income["affordability_income_vnd"] = basis.get("affordability_income_vnd") or income.get("recognised_monthly_vnd")
            products = _j(self._customer(customer_id)["existing_products_json"], []) or []
            obligations = CAP.refreshed_obligations(basis.get("obligations_snapshot") or cap.get("obligations"), products)
        return {"income": income, "obligations": obligations}

    def _checkout_context(self, tx: Dict[str, Any]) -> Dict[str, Any]:
        cid = tx["customer_id"]
        merchant = self._merchant(tx["merchant_id"])
        cs = self.credit_summary(cid)
        line = self._line(cid)
        ci = self._credit_inputs(cid)
        ex = self.exposure(cid)
        amount = tx["amount_vnd"]
        aff = CAP.affordability(amount, self.as_of, ci["income"], ci["obligations"], ex["smart_credit_monthly_vnd"], self.policy)
        agg = self.aggregate_available(cid)
        split_ok = 0
        if line and line["status"] == "ACTIVE" and aff.get("computable"):
            available = line["limit_vnd"] - ex["outstanding_principal_vnd"] - ex["held_vnd"]
            if agg is not None:
                available = min(available, agg)
            if amount > available or not aff["affordable_tenures"]:
                split_ok = CAP.max_affordable_principal(ci["income"], ci["obligations"], ex["smart_credit_monthly_vnd"], self.as_of,
                                                        self.policy, min(available, amount - 1))
        return {"amount_vnd": amount, "merchant": self.merchant_eligibility(merchant, tx["qr_kind"]),
                "identity": IDN.evaluate(self._identity_signals(cid), "CHECKOUT", self.policy),
                "line": {"status": line["status"] if line else None, "limit_vnd": line["limit_vnd"] if line else 0,
                         "frozen_reason": line["frozen_reason"] if line else None,
                         "assessed_on_age_days": cs["assessment_age_days"], "review_in_progress": cs["review_in_progress"]},
                "exposure": ex, "aggregate_available_vnd": agg, "delinquency": cs["delinquency"], "affordability": aff,
                "split_affordable_vnd": split_ok, "credit_inputs": ci,
                "system_available": not self.flag("core_system_unavailable"),
                "activation_pending": bool(cs["open_application"] and cs["open_application"]["kind"] == "LIMIT_ACTIVATION")}

    def pause_for_consent(self, tx_id: str, customer_id: str, resume: bool = False) -> Dict[str, Any]:
        """Inline activation: suspend the 5-minute checkout countdown while the customer reads and gives purpose-specific
        consent, so consent is never captured under time pressure (audit F-18 / C-07). Safety expiry applies."""
        self.sweep_expired()
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] not in ("QR_CREATED", "PAYMENT_METHOD_SELECTED"):
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "The checkout can't be paused now.")
            if resume:
                if tx["timer_paused_at"]:
                    self.db.run("UPDATE transactions SET expires_at = ?, timer_paused_at = NULL, version = version + 1, updated_at = ? "
                                "WHERE transaction_id = ?", (self._deadline(), now_iso(), tx_id))
                    self.db.audit("CUSTOMER", customer_id, "CHECKOUT_TIMER_RESUMED", tx_id)
                return self._customer_view(tx_id)
            secs = self.policy["checkout"]["consent_pause_seconds"]
            self.db.run("UPDATE transactions SET expires_at = ?, timer_paused_at = ?, version = version + 1, updated_at = ? "
                        "WHERE transaction_id = ?", (self._deadline(secs), now_iso(), now_iso(), tx_id))
            self.db.audit("CUSTOMER", customer_id, "CHECKOUT_TIMER_PAUSED_FOR_CONSENT", tx_id,
                          details={"safety_expiry_seconds": secs, "reason": "consent is never captured under a countdown"})
        return self._customer_view(tx_id)

    def choose_pay_later(self, tx_id: str, customer_id: str, activation_consents: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Any]:
        self.sweep_expired()
        self.refresh_servicing(customer_id)
        tx = self._tx(tx_id, customer_id)
        if tx["state"] not in ("QR_CREATED", "PAYMENT_METHOD_SELECTED"):
            raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Pay Later can't be chosen now.", {"current_state": tx["state"]})
        activation = None
        if activation_consents is not None and (not self._line(customer_id) or self._line(customer_id)["status"] == "CLOSED"):
            # Inline activation: the same engine as from Products. GREEN continues in this checkout;
            # anything else stays off the counter and the shopper can pay now. The countdown restarts after consent.
            activation = self.start_activation(customer_id, activation_consents, origin="CHECKOUT", origin_tx_id=tx_id)
            with self._unit():
                self.db.run("UPDATE transactions SET activation_id = ?, expires_at = ?, timer_paused_at = NULL WHERE transaction_id = ?",
                            (activation["application_id"], self._deadline(), tx_id))
        started = time.perf_counter()
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] not in ("QR_CREATED", "PAYMENT_METHOD_SELECTED"):
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Pay Later can't be chosen now.")
            dec = CR.decide(self._checkout_context(tx), self.policy)
            dec["engine_ms"] = round((time.perf_counter() - started) * 1000, 2)
            dec["evaluated_at"] = now_iso()
            dec["activation_id"] = activation["application_id"] if activation else None
            self.db.audit("DECISION_ENGINE", "checkout-rules", "CHECKOUT_DECISION", tx_id,
                          rule_ids=[c["id"] for c in dec["checks"]],
                          details={"band": dec["band"], "reason_code": dec["reason_code"], "message": dec["message"],
                                   "checks": [(c["id"], c["result"]) for c in dec["checks"]], "engine_ms": dec["engine_ms"]})
            if dec["reason_code"] == "R010-FRD":
                self.db.audit("DECISION_ENGINE", "layer0", "FRAUD_SIGNAL_RAISED", tx_id,
                              rule_ids=[h["rule_id"] for h in self._checkout_context(tx)["identity"]["hits"]],
                              details={"stage": "CHECKOUT", "fallback": "PAY_NOW_ONLY"})
            if dec["band"] == CR.GREEN:
                tenure = dec["recommended_tenure"]
                terms = T.build_terms(tx["amount_vnd"], tenure, self.as_of, self.policy)
                hold_id = self._new_id("HLD", "hold")
                self.db.run("INSERT INTO exposure_holds(hold_id, customer_id, transaction_id, amount_vnd, monthly_instalment_vnd, "
                            "status, created_at) VALUES (?,?,?,?,?,?,?)",
                            (hold_id, customer_id, tx_id, tx["amount_vnd"], max(terms["monthly_instalment_vnd"],
                                                                                terms["last_instalment_vnd"]), "HELD", now_iso()))
                self.db.audit("SYSTEM", "exposure-ledger", "EXPOSURE_HOLD_PLACED", tx_id,
                              details={"hold_id": hold_id, "amount_vnd": tx["amount_vnd"], "available_before_vnd": dec["available_vnd"]})
                self._move_tx(tx, "TERMS_PENDING_CONFIRMATION", "DECISION_ENGINE", "checkout-rules",
                              {"method": PAY_LATER, "tenure_months": tenure, "pl_decision_json": json.dumps(dec),
                               "terms_json": json.dumps(terms), "agreement_accepted_at": None,
                               "pay_later_amount_vnd": None, "pay_now_amount_vnd": None},
                              {"band": "GREEN", "hold_id": hold_id})
            else:
                self.db.run("UPDATE transactions SET pl_decision_json = ?, version = version + 1, updated_at = ? "
                            "WHERE transaction_id = ?", (json.dumps(dec), now_iso(), tx_id))
        view = self._customer_view(tx_id)
        view["activation"] = activation
        return view

    def accept_split(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        """Split tender (audit F-04 / C-03): Pay Later for the available and affordable part, Pay Now from the account for
        the rest. Re-decided inside the transaction; the hold covers only the Pay Later part."""
        self.sweep_expired()
        self.refresh_servicing(customer_id)
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] not in ("QR_CREATED", "PAYMENT_METHOD_SELECTED"):
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Split payment can't be chosen now.", {"current_state": tx["state"]})
            ctx = self._checkout_context(tx)
            dec = CR.decide(ctx, self.policy)
            if dec["reason_code"] != "R010-SPLIT":
                raise ServiceError(409, "SPLIT_NOT_AVAILABLE", "Split payment is not available for this purchase now.",
                                   {"band": dec["band"], "reason_code": dec["reason_code"]})
            pl, pn = dec["split"]["pay_later_vnd"], dec["split"]["pay_now_vnd"]
            if self._customer(customer_id)["balance_vnd"] < pn:
                raise ServiceError(409, "INSUFFICIENT_FUNDS", "Your account balance is not enough for the Pay Now part (VND {:,})."
                                   .format(pn), {"pay_now_vnd": pn})
            ci = ctx["credit_inputs"]
            aff = CAP.affordability(pl, self.as_of, ci["income"], ci["obligations"], ctx["exposure"]["smart_credit_monthly_vnd"],
                                    self.policy)
            dec.update(offers=aff["offers"], affordable_tenures=aff["affordable_tenures"], recommended_tenure=aff["recommended_tenure"],
                       evaluated_at=now_iso())
            terms = T.build_terms(pl, aff["recommended_tenure"], self.as_of, self.policy)
            hold_id = self._new_id("HLD", "hold")
            self.db.run("INSERT INTO exposure_holds(hold_id, customer_id, transaction_id, amount_vnd, monthly_instalment_vnd, "
                        "status, created_at) VALUES (?,?,?,?,?,?,?)",
                        (hold_id, customer_id, tx_id, pl, max(terms["monthly_instalment_vnd"], terms["last_instalment_vnd"]),
                         "HELD", now_iso()))
            self.db.audit("SYSTEM", "exposure-ledger", "EXPOSURE_HOLD_PLACED", tx_id,
                          details={"hold_id": hold_id, "amount_vnd": pl, "split": True, "pay_now_vnd": pn})
            self._move_tx(tx, "TERMS_PENDING_CONFIRMATION", "CUSTOMER", customer_id,
                          {"method": SPLIT, "tenure_months": aff["recommended_tenure"], "pl_decision_json": json.dumps(dec),
                           "terms_json": json.dumps(terms), "agreement_accepted_at": None, "pay_later_amount_vnd": pl,
                           "pay_now_amount_vnd": pn}, {"band": "AMBER", "reason_code": "R010-SPLIT", "hold_id": hold_id,
                                                       "pay_later_vnd": pl, "pay_now_vnd": pn}, "SPLIT_TENDER_ACCEPTED")
        return self._customer_view(tx_id)

    def select_tenure(self, tx_id: str, customer_id: str, tenure: Any) -> Dict[str, Any]:
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] != "TERMS_PENDING_CONFIRMATION":
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "No Pay Later offer to change.")
            dec = _j(tx["pl_decision_json"], {})
            if tenure not in dec.get("affordable_tenures", []):
                raise ServiceError(422, "TENURE_NOT_AFFORDABLE", "This repayment period isn't available for this purchase.",
                                   {"affordable_tenures": dec.get("affordable_tenures", [])})
            terms = T.build_terms(tx["pay_later_amount_vnd"] or tx["amount_vnd"], tenure, self.as_of, self.policy)
            self.db.run("UPDATE transactions SET tenure_months = ?, terms_json = ?, agreement_accepted_at = NULL, "
                        "version = version + 1, updated_at = ? WHERE transaction_id = ?",
                        (tenure, json.dumps(terms), now_iso(), tx_id))
            self.db.run("UPDATE exposure_holds SET monthly_instalment_vnd = ? WHERE transaction_id = ? AND status = 'HELD'",
                        (max(terms["monthly_instalment_vnd"], terms["last_instalment_vnd"]), tx_id))
            self.db.audit("CUSTOMER", customer_id, "TENURE_SELECTED", tx_id, details={"tenure_months": tenure,
                          "monthly_instalment_vnd": terms["monthly_instalment_vnd"]})
        return self._customer_view(tx_id)

    def accept_agreement(self, tx_id: str, customer_id: str, accepted: Any) -> Dict[str, Any]:
        if accepted is not True:
            raise ServiceError(422, "AGREEMENT_NOT_ACCEPTED", "Please confirm you agree to the instalment agreement.")
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] != "TERMS_PENDING_CONFIRMATION":
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "There is no agreement to accept.")
            terms = _j(tx["terms_json"], {})
            self.db.run("UPDATE transactions SET agreement_accepted_at = ?, version = version + 1, updated_at = ? "
                        "WHERE transaction_id = ?", (now_iso(), now_iso(), tx_id))
            self.db.audit("CUSTOMER", customer_id, "INSTALMENT_AGREEMENT_ACCEPTED", tx_id,
                          details={k: terms.get(k) for k in ("tenure_months", "monthly_instalment_vnd", "total_repayable_vnd",
                                                             "total_interest_vnd", "effective_annual_rate_pct", "calculated_by")})
        return self._customer_view(tx_id)

    # ------------------------------------------------------------ 3. simulated payment
    def pay(self, tx_id: str, customer_id: str, idempotency_key: str, challenge_id: Optional[str]) -> Dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 100:
            raise ServiceError(422, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")
        self.sweep_expired()
        self.refresh_servicing(customer_id)
        with self._unit():
            prior = self.db.one("SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,))
            if prior:
                if prior["transaction_id"] != tx_id:
                    raise ServiceError(409, "IDEMPOTENCY_KEY_REUSED", "This idempotency key was used for another payment.")
                self.db.audit("CUSTOMER", customer_id, "DUPLICATE_PAYMENT_REQUEST", tx_id, details={"payment_id": prior["payment_id"]})
                view = self._customer_view(tx_id)
                view["idempotent_replay"] = True
                return view
            tx = self._tx(tx_id, customer_id)
            if tx["state"] == "PAYMENT_COMPLETED":
                self.db.audit("CUSTOMER", customer_id, "DUPLICATE_PAYMENT_BLOCKED", tx_id)
                raise ServiceError(409, "DUPLICATE_PAYMENT", "This purchase has already been paid. No second payment was made.")
            if tx["state"] == "DISBURSEMENT_PROCESSING":
                raise ServiceError(409, "PAYMENT_IN_PROGRESS", "This payment is already being processed.")
            if tx["state"] in ("CANCELLED", "EXPIRED"):
                raise ServiceError(409, "TRANSACTION_CLOSED", "This purchase was %s and can't be paid." % tx["state"].lower(),
                                   {"current_state": tx["state"]})
            if tx["method"] in CREDIT_METHODS:
                if tx["state"] not in ("TERMS_PENDING_CONFIRMATION", "PAYMENT_FAILED"):
                    raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Payment can't start in state %s." % tx["state"])
                if not tx["agreement_accepted_at"]:
                    raise ServiceError(409, "AGREEMENT_NOT_ACCEPTED", "Please accept the instalment agreement first.")
                self._recheck_line(tx)  # transactional re-check of the exposure ledger
            elif tx["method"] == ACCOUNT:
                if tx["state"] not in ("PAYMENT_METHOD_SELECTED", "PAYMENT_FAILED"):
                    raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Payment can't start in state %s." % tx["state"])
            else:
                raise ServiceError(409, "NO_METHOD", "Choose a payment method first.")
            purpose = "PAY_LATER_AGREEMENT" if tx["method"] in CREDIT_METHODS else "ACCOUNT_PAYMENT"
            self._consume_challenge(challenge_id, customer_id, tx, purpose)
            attempts = self.db.one("SELECT COUNT(*) AS n FROM payments WHERE transaction_id = ?", (tx_id,))["n"]
            if attempts > self.policy["payment_simulation"]["max_retries"]:
                raise ServiceError(409, "RETRY_LIMIT", "Retry limit reached. Please cancel and try again later.")
            payment_id = self._new_id("PAY", "payment")
            order_key = tx["qr_ref"] if tx["qr_kind"] == "DYNAMIC" else tx_id
            self.db.run("INSERT INTO payments(payment_id, transaction_id, order_key, idempotency_key, attempt_no, method, "
                        "amount_vnd, status, auth_challenge_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (payment_id, tx_id, order_key, idempotency_key, attempts + 1, tx["method"], tx["amount_vnd"],
                         "PROCESSING", challenge_id, now_iso()))
            self._move_tx(tx, "DISBURSEMENT_PROCESSING", "CUSTOMER", customer_id, event="PAYMENT_PROCESSING_STARTED",
                          details={"payment_id": payment_id, "attempt_no": attempts + 1, "auth_challenge_id": challenge_id,
                                   "operation": {"PAY_LATER": "SIMULATED_DISBURSEMENT_TO_MERCHANT",
                                                 "SPLIT": "SIMULATED_DISBURSEMENT_TO_MERCHANT + SIMULATED_ACCOUNT_DEBIT"}
                                   .get(tx["method"], "SIMULATED_ACCOUNT_DEBIT")})
            force = self.flag("force_next_payment_failure")
            if force:
                self.db.set_control("force_next_payment_failure", "0")
            qr = self.db.one("SELECT * FROM qr_codes WHERE qr_ref = ?", (tx["qr_ref"],))
        if self.delay_ms:  # simulated network latency, outside the lock
            time.sleep(self.delay_ms / 1000.0)
        with self._unit():
            tx = self._tx(tx_id)
            cust = self._customer(customer_id)
            if tx["method"] == SPLIT:  # the account part must clear for the purchase to complete
                result = payment_gateway.process(ACCOUNT, tx["pay_now_amount_vnd"], attempts + 1, qr, cust["balance_vnd"], force)
            else:
                result = payment_gateway.process(tx["method"], tx["amount_vnd"], attempts + 1, qr, cust["balance_vnd"], force)
            if result["ok"]:
                try:
                    self.db.run("UPDATE payments SET status = 'SUCCEEDED', completed_at = ? WHERE payment_id = ?",
                                (now_iso(), payment_id))
                except sqlite3.IntegrityError:
                    result = {"ok": False, "code": "DUPLICATE_BLOCKED", "message": "A payment for this order already succeeded."}
            merchant = self._merchant(tx["merchant_id"])
            if result["ok"]:
                if tx["method"] == ACCOUNT:
                    self.db.run("UPDATE customers SET balance_vnd = balance_vnd - ? WHERE customer_id = ?",
                                (tx["amount_vnd"], customer_id))
                else:
                    if tx["method"] == SPLIT:
                        self.db.run("UPDATE customers SET balance_vnd = balance_vnd - ? WHERE customer_id = ?",
                                    (tx["pay_now_amount_vnd"], customer_id))
                    terms = _j(tx["terms_json"])
                    loan_ref = self._book_loan(customer_id, tx, terms, merchant["name"])
                    h = self._active_hold(tx_id)
                    S.check_hold("HELD", "BOOKED")
                    self.db.run("UPDATE exposure_holds SET status = 'BOOKED', closed_at = ?, reason = ? WHERE hold_id = ?",
                                (now_iso(), loan_ref, h["hold_id"]))
                    self.db.audit("SYSTEM", "exposure-ledger", "EXPOSURE_HOLD_BOOKED", tx_id,
                                  details={"hold_id": h["hold_id"], "loan_ref": loan_ref})
                self._settle(tx, merchant)
                self._move_tx(tx, "PAYMENT_COMPLETED", "SYSTEM", "payment-simulator", {"payment_ref": payment_id},
                              {"payment_id": payment_id, "result": result}, "PAYMENT_COMPLETED")
                self.notify(customer_id, "STATUS", "payment_completed", {"amount": tx["amount_vnd"], "merchant": merchant["name"]},
                            "#/tx/" + tx_id, "paid:" + tx_id)
            else:
                self.db.run("UPDATE payments SET status = 'FAILED', failure_code = ?, failure_message = ?, completed_at = ? "
                            "WHERE payment_id = ?", (result["code"], result["message"], now_iso(), payment_id))
                self._move_tx(tx, "PAYMENT_FAILED", "SYSTEM", "payment-simulator", None,
                              {"payment_id": payment_id, "result": result, "hold": "kept"}, "PAYMENT_FAILED")
                self.notify(customer_id, "STATUS", "payment_failed", {"code": result["code"]}, "#/tx/" + tx_id,
                            "failed:" + payment_id)
        return self._customer_view(tx_id)

    def _recheck_line(self, tx: Dict[str, Any]) -> None:
        """Defence in depth: the hold must still exist and the line must still be usable."""
        hold = self._active_hold(tx["transaction_id"])
        if not hold or hold["amount_vnd"] != (tx["pay_later_amount_vnd"] if tx["method"] == SPLIT else tx["amount_vnd"]):
            raise ServiceError(409, "HOLD_MISSING", "This Pay Later approval is no longer reserved. Please start again.")
        line = self._line(tx["customer_id"])
        d = self._delinquency(tx["customer_id"])
        if not line or line["status"] != "ACTIVE" or d["arrears"] or d["default"]:
            raise ServiceError(409, "LINE_NOT_AVAILABLE", "Pay Later is no longer available on your limit. Please pay now.")
        ex = self.exposure(tx["customer_id"])
        if ex["outstanding_principal_vnd"] + ex["held_vnd"] > line["limit_vnd"]:
            self.db.audit("SYSTEM", "exposure-ledger", "LIMIT_BREACH_PREVENTED", tx["transaction_id"],
                          details={"limit": line["limit_vnd"], "outstanding": ex["outstanding_principal_vnd"], "held": ex["held_vnd"]})
            raise ServiceError(409, "LIMIT_EXCEEDED", "This purchase would exceed your limit. Please pay now.")
        agg = self.aggregate_available(tx["customer_id"])
        if agg is not None and agg < 0:
            self.db.audit("SYSTEM", "exposure-ledger", "AGGREGATE_LIMIT_BREACH_PREVENTED", tx["transaction_id"], details={"headroom": agg})
            raise ServiceError(409, "LIMIT_EXCEEDED", "This purchase would exceed your total HLB digital-lending limit. Please pay now.")

    def _settle(self, tx: Dict[str, Any], merchant: Dict[str, Any]) -> None:
        cfg = self.policy["settlement"]
        settle_on = (self.as_of + timedelta(days=cfg["settlement_days"])).isoformat() if tx["method"] in CREDIT_METHODS \
            else self.as_of.isoformat()
        self.db.run("INSERT OR IGNORE INTO merchant_settlements VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (self._new_id("STL", "settlement"), tx["transaction_id"], merchant["merchant_id"], tx["method"],
                     tx["amount_vnd"], None, None, settle_on, "SCHEDULED (simulated)", now_iso()))
        self.db.audit("SYSTEM", "settlement-simulator", "MERCHANT_SETTLEMENT_SCHEDULED", tx["transaction_id"],
                      details={"merchant_id": merchant["merchant_id"], "gross_vnd": tx["amount_vnd"], "settle_on": settle_on,
                               "discount_rate": "UNRESOLVED", "integration": "PROPOSED - not integrated"})

    def cancel(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        self.sweep_expired()
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] not in S.TX_ACTIVE_BEFORE_PAYMENT:
                raise ServiceError(409, "CANNOT_CANCEL", "This transaction can't be cancelled now (%s)." % tx["state"],
                                   {"current_state": tx["state"]})
            self._move_tx(tx, "CANCELLED", "CUSTOMER", customer_id, {"closure_reason": "CANCELLED_BY_CUSTOMER"},
                          event="TRANSACTION_CANCELLED")
            self.notify(customer_id, "STATUS", "purchase_cancelled", {"merchant": self._merchant(tx["merchant_id"])["name"]},
                        "#/tx/" + tx_id, "cancel:" + tx_id)
        return self._customer_view(tx_id)

    # ------------------------------------------------------------ convert a Pay Now purchase within N days
    def conversion_offer(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        self.refresh_servicing(customer_id)
        tx = self._tx(tx_id, customer_id)
        cfg = self.policy["instalment_conversion"]
        out: Dict[str, Any] = {"eligible": False, "window_days": cfg["window_days"]}
        if tx["state"] != "PAYMENT_COMPLETED" or tx["method"] != ACCOUNT or tx["converted_loan_ref"]:
            out["reason"] = "NOT_A_PAY_NOW_PURCHASE" if not tx["converted_loan_ref"] else "ALREADY_CONVERTED"
            return out
        bdate = datetime.strptime(tx["business_date"], "%Y-%m-%d").date()
        until = bdate + timedelta(days=cfg["window_days"])
        out["until"] = until.isoformat()
        if self.as_of > until:
            out["reason"] = "WINDOW_CLOSED"
            return out
        ctx = self._checkout_context(tx)
        ctx["merchant"] = self.merchant_eligibility(self._merchant(tx["merchant_id"]), tx["qr_kind"])
        dec = CR.decide(ctx, self.policy)
        out.update({"band": dec["band"], "reason_code": dec["reason_code"], "customer_reason": dec["customer_reason"],
                    "offers": dec["offers"], "affordable_tenures": dec["affordable_tenures"],
                    "eligible": dec["band"] == CR.GREEN})
        return out

    def convert_to_instalments(self, tx_id: str, customer_id: str, tenure: Any, accepted: Any,
                               challenge_id: Optional[str]) -> Dict[str, Any]:
        if accepted is not True:
            raise ServiceError(422, "AGREEMENT_NOT_ACCEPTED", "Please confirm you agree to the instalment agreement.")
        offer = self.conversion_offer(tx_id, customer_id)
        if not offer["eligible"]:
            raise ServiceError(409, "CONVERSION_NOT_AVAILABLE", "This purchase can't be converted to instalments now.",
                               {"reason": offer.get("reason") or offer.get("reason_code")})
        if tenure not in offer["affordable_tenures"]:
            raise ServiceError(422, "TENURE_NOT_AFFORDABLE", "This repayment period isn't available.")
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["converted_loan_ref"]:
                raise ServiceError(409, "CONVERSION_NOT_AVAILABLE", "Already converted.")
            self._consume_challenge(challenge_id, customer_id, tx, "INSTALMENT_CONVERSION")
            line = self._line(customer_id)
            ex = self.exposure(customer_id)
            if ex["outstanding_principal_vnd"] + ex["held_vnd"] + tx["amount_vnd"] > line["limit_vnd"]:
                raise ServiceError(409, "LIMIT_EXCEEDED", "Not enough available limit to convert this purchase.")
            terms = T.build_terms(tx["amount_vnd"], tenure, self.as_of, self.policy)
            merchant = self._merchant(tx["merchant_id"])
            if not self.policy["instalment_conversion"].get("enabled", True):
                raise ServiceError(409, "CONVERSION_NOT_AVAILABLE", "Instalment conversion is not available.")
            loan_ref = self._book_loan(customer_id, tx, terms, merchant["name"], origin="CONVERSION")
            paid = self.db.one("SELECT payment_id, amount_vnd FROM payments WHERE transaction_id = ? AND status = 'SUCCEEDED'", (tx_id,))
            # Purchase financing of one already-settled merchant purchase: the disbursement reverses that purchase's account
            # debit (exactly its amount, once) - recorded against the original payment and the merchant settlement.
            self.db.run("UPDATE customers SET balance_vnd = balance_vnd + ? WHERE customer_id = ?", (paid["amount_vnd"], customer_id))
            self.db.run("UPDATE transactions SET converted_loan_ref = ?, version = version + 1, updated_at = ? WHERE transaction_id = ?",
                        (loan_ref, now_iso(), tx_id))
            self.db.run("UPDATE merchant_settlements SET status = status || ' · financed by ' || ? WHERE transaction_id = ?",
                        (loan_ref, tx_id))
            self.db.audit("CUSTOMER", customer_id, "PURCHASE_CONVERTED_TO_INSTALMENTS", tx_id,
                          details={"loan_ref": loan_ref, "tenure_months": tenure, "purpose": "PURCHASE_FINANCING",
                                   "disbursement": "REVERSAL_OF_PURCHASE_DEBIT", "original_payment_id": paid["payment_id"],
                                   "reversed_vnd": paid["amount_vnd"], "merchant_id": merchant["merchant_id"],
                                   "legal_dependency": "Circular 39 Art. 8.10 suspended since 2023-09-01 - Legal to confirm",
                                   "simulated": True})
            self.notify(customer_id, "LOAN", "converted", {"merchant": merchant["name"], "amount": tx["amount_vnd"]},
                        "#/loans/" + loan_ref, "convert:" + tx_id)
        return self._customer_view(tx_id)

    # ------------------------------------------------------------ views
    def history(self, customer_id: str) -> List[Dict[str, Any]]:
        self.sweep_expired()
        self._customer(customer_id)
        return [self._customer_view(r["transaction_id"]) for r in
                self.db.all("SELECT transaction_id FROM transactions WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC",
                            (customer_id,))]

    def audit_log(self, transaction_id: Optional[str] = None, application_id: Optional[str] = None,
                  limit: int = 300) -> List[Dict[str, Any]]:
        if transaction_id:
            rows = self.db.all("SELECT * FROM audit_events WHERE transaction_id = ? ORDER BY event_id", (transaction_id,))
        elif application_id:
            rows = self.db.all("SELECT * FROM audit_events WHERE application_id = ? ORDER BY event_id", (application_id,))
        else:
            rows = self.db.all("SELECT * FROM audit_events ORDER BY event_id DESC LIMIT ?", (limit,))
        for r in rows:
            r["rule_ids"] = _j(r.pop("rule_ids_json"), [])
            r["details"] = _j(r.pop("details_json"), {})
        return rows

    def get_view(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        self.sweep_expired()
        self._tx(tx_id, customer_id)
        return self._customer_view(tx_id)

    def _customer_view(self, tx_id: str) -> Dict[str, Any]:
        tx = self._tx(tx_id)
        merchant = self._merchant(tx["merchant_id"])
        elig = self.merchant_eligibility(merchant, tx["qr_kind"])
        payments = self.db.all("SELECT payment_id, attempt_no, method, status, failure_code, created_at, completed_at "
                               "FROM payments WHERE transaction_id = ? ORDER BY attempt_no", (tx_id,))
        state = tx["state"]
        outcome = {"QR_CREATED": "CHOOSE_METHOD", "PAYMENT_METHOD_SELECTED": "REVIEW_PAY_NOW",
                   "TERMS_PENDING_CONFIRMATION": "REVIEW_PAY_LATER", "DISBURSEMENT_PROCESSING": "PROCESSING",
                   "PAYMENT_COMPLETED": "PAYMENT_COMPLETED", "PAYMENT_FAILED": "PAYMENT_FAILED", "EXPIRED": "EXPIRED",
                   "CANCELLED": "CANCELLED"}[state]
        cust = self._customer(tx["customer_id"])
        seconds_left = None
        if tx["expires_at"] and state in S.TX_EXPIRABLE:
            seconds_left = max(0, int((datetime.fromisoformat(tx["expires_at"]) - self.clock()).total_seconds()))
        dec = _j(tx["pl_decision_json"])
        loan = self.db.one("SELECT loan_ref, principal_vnd, status, origin FROM loans WHERE transaction_id = ?", (tx_id,))
        settlement = self.db.one("SELECT settle_on, status, gross_vnd FROM merchant_settlements WHERE transaction_id = ?", (tx_id,))
        return {
            "transaction": {k: tx[k] for k in ("transaction_id", "qr_ref", "qr_kind", "amount_vnd", "amount_source", "description",
                                                "method", "tenure_months", "state", "closure_reason", "payment_ref", "created_at",
                                                "updated_at", "expires_at", "agreement_accepted_at", "activation_id",
                                                "business_date", "converted_loan_ref", "pay_later_amount_vnd",
                                                "pay_now_amount_vnd")},
            "timer_paused": bool(tx.get("timer_paused_at")) and state in ("QR_CREATED", "PAYMENT_METHOD_SELECTED"),
            "merchant": {"merchant_id": merchant["merchant_id"], "name": merchant["name"], "category": merchant["category"],
                         "city": merchant["city"], "initials": merchant["initials"], "mcc": merchant["mcc"],
                         "pay_later_eligible": elig["eligible"], "eligibility_code": elig["code"]},
            "pay_later_decision": {k: dec.get(k) for k in ("band", "reason_code", "customer_reason", "offers", "affordable_tenures",
                                                           "recommended_tenure", "alternatives", "checks", "available_vnd",
                                                           "shortfall_vnd", "activation_id", "split", "decision_class",
                                                           "credit_outcome", "payment_options")} if dec else None,
            "terms": _j(tx["terms_json"]),
            "hold": self._active_hold(tx_id),
            "auth_required": self.auth_preview(tx) if state in ("PAYMENT_METHOD_SELECTED", "TERMS_PENDING_CONFIRMATION",
                                                                "PAYMENT_FAILED") else None,
            "payments": payments, "loan": loan, "settlement": settlement, "outcome": outcome, "seconds_left": seconds_left,
            "account": {"account_masked": cust["account_masked"], "balance_vnd": cust["balance_vnd"]},
            "credit": self.credit_summary(tx["customer_id"]),
            "simulated": True,
        }
