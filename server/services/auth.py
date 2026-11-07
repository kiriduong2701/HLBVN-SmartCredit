"""Sessions (demo identity) and SIMULATED step-up authentication.

Sessions replace the per-request X-Demo-Customer / X-Demo-Officer headers: the
server issues a random bearer token bound to one customer or officer, so a
client can no longer act as anyone by editing a header. Sign-in itself is still
a demo (customer persona picker; officer and navigation pages open a session\nwithout a code, by case assumption) and is NOT real authentication.

Step-up authentication mirrors HLB Connect's published controls (official
HLBVN pages, Decision 2345/QD-NHNN): face authentication from VND 10M per
transaction or 20M total per day, Smart OTP otherwise (OTP valid 2 minutes). It is a
simulation: the "Smart OTP" code is shown on a clearly labelled simulated
authenticator, and face matching is a button. A real integration with HLB
Connect's Smart OTP / eKYC face service is required.
"""
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ..store.db import now_iso
from .base import ACCOUNT, CREDIT_METHODS, PAY_LATER, SPLIT, ServiceError

SESSION_HOURS = 8


class AuthMixin:
    # ------------------------------------------------------------ sessions
    def _new_session(self, role: str, subject: str) -> Dict[str, Any]:
        token = secrets.token_urlsafe(32)
        exp = (self.clock() + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
        with self._unit():
            self.db.run("INSERT INTO sessions VALUES (?,?,?,?,?)", (token, role, subject, now_iso(), exp))
            self.db.audit(role, subject, "SESSION_STARTED", details={"role": role, "auth": "DEMO sign-in, not real authentication"})
        return {"token": token, "role": role, "subject_id": subject, "expires_at": exp}

    def customer_sign_in(self, customer_id: str) -> Dict[str, Any]:
        c = self._customer(customer_id)
        s = self._new_session("CUSTOMER", customer_id)
        s["customer"] = {"customer_id": c["customer_id"], "display_name": c["display_name"], "initials": c["initials"]}
        return s

    def officer_sign_in(self) -> Dict[str, Any]:
        """Open an officer-dashboard session. Case assumption: every user who reaches the officer dashboard is
        already authorised (in production: HLBVN staff SSO), so there is no profile picker and no access code.
        Each session gets its own reference so the audit trail still distinguishes sessions."""
        ref = "OFFICER-" + secrets.token_hex(3).upper()
        s = self._new_session("OFFICER", ref)
        s["officer"] = self._officer_view(ref)
        return s

    def _officer_view(self, ref: str) -> Dict[str, Any]:
        oc = self.policy["officer_controls"]
        return {"officer_id": ref, "role": "CREDIT_OFFICER", "approval_authority_max_vnd": oc["approval_authority_max_vnd"],
                "approval_authority_bound": oc["approval_authority_bound"], "authority_source": oc["approval_authority_source"]}

    def presenter_sign_in(self) -> Dict[str, Any]:
        """Navigation / presenter page session (demo controls). No access code (case assumption); it is a separate
        page from the customer app, and each local instance serves one audience."""
        return self._new_session("PRESENTER", "presenter")

    def resolve_session(self, token: Optional[str], roles: tuple) -> Dict[str, Any]:
        if not token:
            raise ServiceError(401, "SIGN_IN_REQUIRED", "Please sign in.")
        s = self.db.one("SELECT * FROM sessions WHERE token = ?", (token,))
        if not s or datetime.fromisoformat(s["expires_at"]) <= self.clock():
            raise ServiceError(401, "SIGN_IN_REQUIRED", "Your session has ended. Please sign in again.")
        if s["role"] not in roles:
            raise ServiceError(403, "FORBIDDEN_ROLE", "This action needs a %s session." % " or ".join(r.lower() for r in roles))
        return s

    def sign_out(self, token: str) -> Dict[str, Any]:
        with self._unit():
            self.db.run("DELETE FROM sessions WHERE token = ?", (token,))
        return {"signed_out": True}

    def public_customers(self):
        return [{"customer_id": c["customer_id"], "display_name": c["display_name"], "initials": c["initials"]}
                for c in self.db.all("SELECT customer_id, display_name, initials FROM customers ORDER BY customer_id")]

    # ------------------------------------------------------------ step-up authentication
    def _daily_paid(self, customer_id: str) -> int:
        return self.db.one("SELECT COALESCE(SUM(amount_vnd), 0) n FROM transactions WHERE customer_id = ? AND state = "
                           "'PAYMENT_COMPLETED' AND business_date = ?", (customer_id, self.as_of.isoformat()))["n"]

    def _auth_method(self, customer_id: str, amount: int) -> Dict[str, Any]:
        cfg = self.policy["authentication"]
        daily = self._daily_paid(customer_id)
        face = amount >= cfg["face_threshold_per_transaction_vnd"] or daily + amount >= cfg["face_threshold_daily_vnd"]
        return {"method": "FACE" if face else "SMART_OTP", "daily_total_before_vnd": daily,
                "rule": (">= VND {:,} per transaction or >= VND {:,} per day -> face authentication".format(
                    cfg["face_threshold_per_transaction_vnd"], cfg["face_threshold_daily_vnd"]))}

    def auth_preview(self, tx: Dict[str, Any]) -> Dict[str, Any]:
        m = self._auth_method(tx["customer_id"], tx["amount_vnd"])
        return {"method": m["method"], "simulated": True}

    def create_challenge(self, tx_id: str, customer_id: str, purpose: str) -> Dict[str, Any]:
        if purpose not in self.policy["authentication"]["applies_to"]:
            raise ServiceError(422, "INVALID_PURPOSE", "Unknown authentication purpose.")
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if purpose == "ACCOUNT_PAYMENT":
                ok = tx["method"] == ACCOUNT and tx["state"] in ("PAYMENT_METHOD_SELECTED", "PAYMENT_FAILED")
                if ok and self._customer(customer_id)["balance_vnd"] < tx["amount_vnd"]:
                    raise ServiceError(409, "INSUFFICIENT_FUNDS", "Your account balance is not enough for this payment.")
            elif purpose == "PAY_LATER_AGREEMENT":
                ok = tx["method"] in CREDIT_METHODS and tx["state"] in ("TERMS_PENDING_CONFIRMATION", "PAYMENT_FAILED") \
                    and bool(tx["agreement_accepted_at"])
                if ok and tx["method"] == SPLIT and self._customer(customer_id)["balance_vnd"] < tx["pay_now_amount_vnd"]:
                    raise ServiceError(409, "INSUFFICIENT_FUNDS", "Your account balance is not enough for the Pay Now part.")
            else:
                ok = tx["method"] == ACCOUNT and tx["state"] == "PAYMENT_COMPLETED" and not tx["converted_loan_ref"]
            if not ok:
                raise ServiceError(409, "NOTHING_TO_AUTHORISE", "There is nothing to confirm for this purchase right now.")
            m = self._auth_method(customer_id, tx["amount_vnd"])
            cid = self._new_id("AUTH", "auth")
            code = "%06d" % secrets.randbelow(10 ** 6) if m["method"] == "SMART_OTP" else None
            exp = (self.clock() + timedelta(seconds=self.policy["authentication"]["otp_ttl_seconds"])).isoformat(timespec="seconds")
            self.db.run("UPDATE auth_challenges SET status = 'SUPERSEDED' WHERE transaction_id = ? AND purpose = ? "
                        "AND status = 'PENDING'", (tx_id, purpose))
            self.db.run("INSERT INTO auth_challenges(challenge_id, customer_id, transaction_id, purpose, amount_vnd, method, "
                        "demo_code, status, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (cid, customer_id, tx_id, purpose, tx["amount_vnd"], m["method"], code, "PENDING", exp, now_iso()))
            self.db.audit("CUSTOMER", customer_id, "STEP_UP_CHALLENGE_CREATED", tx_id,
                          details={"challenge_id": cid, "method": m["method"], "purpose": purpose, "rule": m["rule"],
                                   "simulated": True})
        out = {"challenge_id": cid, "method": m["method"], "purpose": purpose, "amount_vnd": tx["amount_vnd"],
               "expires_at": exp, "ttl_seconds": self.policy["authentication"]["otp_ttl_seconds"],
               "max_attempts": self.policy["authentication"]["max_attempts"], "simulated": True, "rule": m["rule"]}
        if code:
            out["simulated_authenticator_code"] = code  # stands in for the in-app Smart OTP generator
        return out

    def verify_challenge(self, challenge_id: str, customer_id: str, code: Any = None, face_result: Any = None) -> Dict[str, Any]:
        result: Dict[str, Any]
        with self._unit():
            ch = self.db.one("SELECT * FROM auth_challenges WHERE challenge_id = ?", (challenge_id,))
            if not ch or ch["customer_id"] != customer_id:
                raise ServiceError(404, "CHALLENGE_NOT_FOUND", "Authentication request not found.")
            if ch["status"] != "PENDING":
                raise ServiceError(409, "CHALLENGE_CLOSED", "This authentication request is no longer valid. Start again.")
            if datetime.fromisoformat(ch["expires_at"]) <= self.clock():
                self.db.run("UPDATE auth_challenges SET status = 'EXPIRED' WHERE challenge_id = ?", (challenge_id,))
                self.db.audit("CUSTOMER", customer_id, "STEP_UP_EXPIRED", ch["transaction_id"], details={"challenge_id": challenge_id})
                result = {"error": ServiceError(409, "CHALLENGE_EXPIRED", "The code has expired. Request a new one.")}
            else:
                forced = self.flag("force_next_auth_failure")
                if forced:
                    self.db.set_control("force_next_auth_failure", "0")
                if ch["method"] == "SMART_OTP":
                    ok = not forced and isinstance(code, str) and hmac.compare_digest(code, ch["demo_code"] or "")
                else:
                    ok = not forced and face_result == "MATCH"
                attempts = ch["attempts"] + 1
                if ok:
                    self.db.run("UPDATE auth_challenges SET status = 'VERIFIED', attempts = ?, verified_at = ? WHERE challenge_id = ?",
                                (attempts, now_iso(), challenge_id))
                    self.db.audit("CUSTOMER", customer_id, "STEP_UP_VERIFIED", ch["transaction_id"],
                                  details={"challenge_id": challenge_id, "method": ch["method"], "attempts": attempts})
                    result = {"ok": True}
                else:
                    locked = attempts >= self.policy["authentication"]["max_attempts"]
                    self.db.run("UPDATE auth_challenges SET attempts = ?, status = ? WHERE challenge_id = ?",
                                (attempts, "LOCKED" if locked else "PENDING", challenge_id))
                    self.db.audit("CUSTOMER", customer_id, "STEP_UP_FAILED", ch["transaction_id"],
                                  details={"challenge_id": challenge_id, "method": ch["method"], "attempts": attempts,
                                           "locked": locked, "forced_by_presenter": forced})
                    result = {"error": ServiceError(422, "AUTH_LOCKED" if locked else "AUTH_FAILED",
                                                    "Too many attempts. Start the confirmation again." if locked else
                                                    ("The code is not correct." if ch["method"] == "SMART_OTP" else
                                                     "Face authentication did not match. Try again."),
                                                    {"attempts_left": max(0, self.policy["authentication"]["max_attempts"] - attempts)})}
        if "error" in result:  # failed attempts are committed (audited) before reporting them
            raise result["error"]
        return {"challenge_id": challenge_id, "status": "VERIFIED"}

    def _consume_challenge(self, challenge_id: Optional[str], customer_id: str, tx: Dict[str, Any], purpose: str) -> None:
        ch = self.db.one("SELECT * FROM auth_challenges WHERE challenge_id = ?", (challenge_id or "",))
        if not ch or ch["customer_id"] != customer_id or ch["transaction_id"] != tx["transaction_id"] or ch["purpose"] != purpose \
                or ch["amount_vnd"] != tx["amount_vnd"] or ch["status"] != "VERIFIED":
            raise ServiceError(401, "STEP_UP_REQUIRED", "Please confirm with Smart OTP or face authentication first.",
                               {"method": self._auth_method(customer_id, tx["amount_vnd"])["method"]})
        self.db.run("UPDATE auth_challenges SET status = 'USED', used_at = ? WHERE challenge_id = ?", (now_iso(), challenge_id))
