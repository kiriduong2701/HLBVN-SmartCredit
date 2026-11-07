"""Shared service infrastructure: transactions, ids, clocks, state moves,
exposure-hold release, notifications and checkout expiry."""
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..domain import states as S
from ..engine import policy_integrity as PI
from ..store.db import Database, now_iso


class ServiceError(Exception):
    def __init__(self, http_status: int, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.http_status, self.code, self.message, self.details = http_status, code, message, details or {}


PAY_LATER, ACCOUNT, SPLIT = "PAY_LATER", "ACCOUNT", "SPLIT"
CREDIT_METHODS = (PAY_LATER, SPLIT)  # methods that spend the Smart Credit line
APPLICANT_FIELDS = ["customer_profile", "has_formal_cic_history", "has_observable_tx", "monthly_inflow_vnd", "inflow_cv",
                    "unified_credit_score", "casa_balance_vnd", "dti_ratio", "on_time_bill_ratio", "e_wallet_topups_pm",
                    "regular_weekly_pattern", "rapid_drain_flag"]


def _j(value: Optional[str], default: Any = None) -> Any:
    return json.loads(value) if value else default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def vnd(n: Any) -> str:
    return "{:,}".format(int(n or 0))


class BaseService:
    def __init__(self, db: Database, policy: Dict[str, Any], fixtures: Dict[str, Any],
                 payment_delay_ms: Optional[int] = None, clock: Callable[[], datetime] = _utcnow):
        errors = PI.validate(policy)
        if errors:
            raise ValueError("Invalid policy configuration: " + "; ".join(errors))
        self.db = db
        self.policy = policy
        self.policy_hash = PI.policy_hash(policy)
        self.fixtures = fixtures
        self.demo_date = datetime.strptime(fixtures["demo_date"], "%Y-%m-%d").date()
        self.delay_ms = policy["payment_simulation"]["processing_delay_ms"] if payment_delay_ms is None else payment_delay_ms
        self.clock = clock  # wall clock (checkout timer, OTP expiry); tests may replace it
        self.bootstrap()

    def bootstrap(self) -> None:  # overridden by ActivationMixin (pre-activated personas)
        pass

    # ------------------------------------------------------------ clocks
    @property
    def as_of(self) -> date:
        """Simulated business date (loan due dates, freshness). Advanced only by a presenter control."""
        v = self.db.control("business_date")
        return datetime.strptime(v, "%Y-%m-%d").date() if v else self.demo_date

    def _deadline(self, seconds: Optional[int] = None) -> str:
        return (self.clock() + timedelta(seconds=seconds or self.policy["checkout"]["timeout_seconds"])).isoformat(
            timespec="seconds")

    def flag(self, key: str) -> bool:
        return self.db.control(key) == "1"

    # ------------------------------------------------------------ infra
    @contextmanager
    def _unit(self):
        with self.db.lock:
            self.db.begin()
            try:
                yield
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def _new_id(self, prefix: str, counter: str) -> str:
        return "%s-%s-%04d" % (prefix, self.demo_date.strftime("%y%m%d"), self.db.next_id(counter))

    def _tx(self, tx_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        tx = self.db.one("SELECT * FROM transactions WHERE transaction_id = ?", (tx_id,))
        if not tx:
            raise ServiceError(404, "TRANSACTION_NOT_FOUND", "Transaction %s not found." % tx_id)
        if customer_id is not None and tx["customer_id"] != customer_id:
            raise ServiceError(403, "FORBIDDEN", "This transaction belongs to another customer.")
        return tx

    def _app(self, app_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        app = self.db.one("SELECT * FROM applications WHERE application_id = ?", (app_id,))
        if not app:
            raise ServiceError(404, "APPLICATION_NOT_FOUND", "Application %s not found." % app_id)
        if customer_id is not None and app["customer_id"] != customer_id:
            raise ServiceError(403, "FORBIDDEN", "This application belongs to another customer.")
        return app

    def _customer(self, customer_id: str) -> Dict[str, Any]:
        c = self.db.one("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
        if not c:
            raise ServiceError(404, "CUSTOMER_NOT_FOUND", "Customer %s not found." % customer_id)
        return c

    def _merchant(self, merchant_id: str) -> Dict[str, Any]:
        return self.db.one("SELECT * FROM merchants WHERE merchant_id = ?", (merchant_id,))

    # ------------------------------------------------------------ state moves
    def _move_tx(self, tx: Dict[str, Any], target: str, actor: str, actor_id: Optional[str],
                 extra: Optional[Dict[str, Any]] = None, details: Optional[Dict[str, Any]] = None,
                 event: str = "TRANSACTION_STATE_CHANGED") -> Dict[str, Any]:
        try:
            S.check_tx(tx["state"], target)
        except S.InvalidTransition as e:
            raise ServiceError(409, "INVALID_TRANSACTION_STATE", str(e), {"current_state": tx["state"]})
        fields: Dict[str, Any] = {"state": target}
        # Timer: the checkout has one 5-minute window; reviewing the Pay Later
        # offer and retrying after a failed payment each open a fresh window.
        if target in ("TERMS_PENDING_CONFIRMATION", "PAYMENT_FAILED") or \
                (target in S.TX_EXPIRABLE and not tx.get("expires_at")):
            fields["expires_at"] = self._deadline()
        elif target not in S.TX_EXPIRABLE:
            fields["expires_at"] = None
        if tx.get("timer_paused_at"):  # consent pause ends on the next step: a fresh countdown starts
            fields["timer_paused_at"] = None
            if target in S.TX_EXPIRABLE:
                fields["expires_at"] = self._deadline()
        fields.update(extra or {})
        if self.db.update("transactions", "transaction_id", tx["transaction_id"], fields, tx["version"]) != 1:
            raise ServiceError(409, "CONCURRENT_UPDATE", "The transaction changed in the meantime. Please refresh.")
        self.db.audit(actor, actor_id, event, tx["transaction_id"], None, tx["state"], target, details=details)
        # Exposure ledger: leaving the Pay Later confirmation path releases the hold.
        if target in ("CANCELLED", "EXPIRED", "QR_CREATED", "PAYMENT_METHOD_SELECTED"):
            self._release_hold(tx["transaction_id"], actor, actor_id, target)
        return self._tx(tx["transaction_id"])

    def _move_credit(self, app: Dict[str, Any], target: str, actor: str, actor_id: Optional[str], action: str,
                     rule_ids: Optional[List[str]] = None, note: Optional[str] = None,
                     extra: Optional[Dict[str, Any]] = None, expected_version: Optional[int] = None,
                     reason_code: Optional[str] = None) -> Dict[str, Any]:
        try:
            S.check_credit(app["credit_state"], target)
        except S.InvalidTransition as e:
            raise ServiceError(409, "INVALID_CREDIT_STATE", str(e), {"current_state": app["credit_state"]})
        fields: Dict[str, Any] = {"credit_state": target}
        if target == "MANUAL_REVIEW" and not app.get("review_started_at"):
            fields["review_started_at"] = now_iso()
            fields["sla_due_at"] = (_utcnow() + timedelta(hours=self.policy["officer_controls"]["sla_hours"])).astimezone() \
                .isoformat(timespec="seconds")
        fields.update(extra or {})
        version = app["version"] if expected_version is None else expected_version
        if self.db.update("applications", "application_id", app["application_id"], fields, version) != 1:
            raise ServiceError(409, "STALE_DECISION", "The case was updated by someone else. Reload before acting.")
        self.db.run("INSERT INTO decisions(application_id, ts, actor_type, actor_id, action, from_state, to_state, "
                    "rule_ids_json, note, reason_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (app["application_id"], now_iso(), actor, actor_id, action, app["credit_state"], target,
                     json.dumps(rule_ids or []), note, reason_code))
        self.db.audit(actor, actor_id, "CREDIT_STATE_CHANGED", app.get("origin_transaction_id"), app["application_id"],
                      app["credit_state"], target, rule_ids, {"action": action, "note": note, "reason_code": reason_code,
                                                              "kind": app["kind"]})
        return self._app(app["application_id"])

    # ------------------------------------------------------------ exposure holds
    def _active_hold(self, tx_id: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM exposure_holds WHERE transaction_id = ? AND status = 'HELD'", (tx_id,))

    def _release_hold(self, tx_id: str, actor: str, actor_id: Optional[str], why: str) -> None:
        h = self._active_hold(tx_id)
        if not h:
            return
        S.check_hold("HELD", "RELEASED")
        self.db.run("UPDATE exposure_holds SET status = 'RELEASED', closed_at = ?, reason = ? WHERE hold_id = ?",
                    (now_iso(), why, h["hold_id"]))
        self.db.audit(actor, actor_id, "EXPOSURE_HOLD_RELEASED", tx_id,
                      details={"hold_id": h["hold_id"], "amount_vnd": h["amount_vnd"], "reason": why})

    # ------------------------------------------------------------ notifications (i18n by key)
    def notify(self, customer_id: str, kind: str, key: str, params: Optional[Dict[str, Any]] = None,
               link: Optional[str] = None, dedupe_key: Optional[str] = None) -> None:
        self.db.run("INSERT OR IGNORE INTO notifications(customer_id, ts, business_date, kind, title_key, params_json, "
                    "link, dedupe_key) VALUES (?,?,?,?,?,?,?,?)",
                    (customer_id, now_iso(), self.as_of.isoformat(), kind, key,
                     json.dumps(params or {}, ensure_ascii=False), link, dedupe_key))

    # ------------------------------------------------------------ checkout expiry
    def reconcile_stuck_payments(self) -> int:
        """A payment left in DISBURSEMENT_PROCESSING beyond checkout.payment_processing_timeout_seconds (e.g. the process died
        between the two payment steps) is reconciled as FAILED. Nothing is posted before the final commit - the account
        debit, loan booking, hold booking and merchant settlement are one database transaction - so no compensation is
        needed: no duplicate charge, no loan without a purchase. The hold stays (retry possible) until the checkout expires."""
        limit = self.policy["checkout"].get("payment_processing_timeout_seconds", 120)
        now = self.clock()
        n = 0
        for r in self.db.all("SELECT p.payment_id, p.transaction_id, p.created_at FROM payments p JOIN transactions t "
                             "USING(transaction_id) WHERE p.status = 'PROCESSING' AND t.state = 'DISBURSEMENT_PROCESSING'"):
            if (now - datetime.fromisoformat(r["created_at"])).total_seconds() < limit:
                continue
            with self._unit():
                tx = self._tx(r["transaction_id"])
                if tx["state"] != "DISBURSEMENT_PROCESSING":
                    continue
                self.db.run("UPDATE payments SET status = 'FAILED', failure_code = 'RECONCILED_TIMEOUT', failure_message = ?, "
                            "completed_at = ? WHERE payment_id = ? AND status = 'PROCESSING'",
                            ("No result within %s s: reconciled as failed; nothing was posted." % limit, now_iso(), r["payment_id"]))
                self._move_tx(tx, "PAYMENT_FAILED", "SYSTEM", "reconciliation", None,
                              {"payment_id": r["payment_id"], "reason": "PROCESSING_TIMEOUT", "posted": "nothing", "hold": "kept"},
                              "PAYMENT_RECONCILED_FAILED")
                n += 1
        return n

    def sweep_expired(self) -> int:
        """Expire unfinished customer-side checkouts whose 5-minute window passed (releases any hold); reconcile stuck
        payments first so their holds follow the same expiry."""
        self.reconcile_stuck_payments()
        now = self.clock()
        states = sorted(S.TX_EXPIRABLE)
        rows = self.db.all("SELECT transaction_id, expires_at FROM transactions WHERE expires_at IS NOT NULL AND state IN (%s)"
                           % ",".join("?" * len(states)), states)
        due = [r["transaction_id"] for r in rows if datetime.fromisoformat(r["expires_at"]) <= now]
        n = 0
        for tx_id in due:
            with self._unit():
                tx = self._tx(tx_id)
                if tx["state"] not in S.TX_EXPIRABLE or not tx["expires_at"] or datetime.fromisoformat(tx["expires_at"]) > now:
                    continue
                self._move_tx(tx, "EXPIRED", "SYSTEM", "checkout-timer", {"closure_reason": "CHECKOUT_EXPIRED"},
                              {"timeout_seconds": self.policy["checkout"]["timeout_seconds"]}, "CHECKOUT_EXPIRED")
                self.notify(tx["customer_id"], "STATUS", "checkout_expired", {"merchant": self._merchant(tx["merchant_id"])["name"]},
                            "#/tx/" + tx_id, "expired:" + tx_id)
                n += 1
        return n

    def expire_now(self, tx_id: str, actor: str) -> Dict[str, Any]:
        """Presenter control: move the deadline into the past and run the sweeper."""
        with self._unit():
            tx = self._tx(tx_id)
            if tx["state"] not in S.TX_EXPIRABLE:
                raise ServiceError(409, "NOT_EXPIRABLE", "Only unfinished checkouts can expire (state %s)." % tx["state"])
            self.db.run("UPDATE transactions SET expires_at = ? WHERE transaction_id = ?",
                        ((self.clock() - timedelta(seconds=1)).isoformat(timespec="seconds"), tx_id))
            self.db.audit("PRESENTER", actor, "DEMO_EXPIRE_NOW", tx_id)
        self.sweep_expired()
        return {"transaction_id": tx_id, "state": self._tx(tx_id)["state"]}
