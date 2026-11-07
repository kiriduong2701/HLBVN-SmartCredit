"""JSON HTTP API + static file server (stdlib http.server).

Identity: every customer / officer / presenter call carries a server-issued
bearer token (Authorization: Bearer ...). Roles are enforced per route. The
customer app only receives the configuration it needs (no credit cut-offs);
presenter controls (clock, failures, outages, reset) need a presenter or
officer session. Unexpected errors return a generic message plus an error id.
"""
import json
import mimetypes
import os
import re
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..services.smart_credit import ServiceError, SmartCreditService
from ..store.db import seed

HOTLINE = "1900 633 068"  # HLBVN contact centre, from the official HLB Connect page


class App:
    def __init__(self, service: SmartCreditService, web_root: str, reference_dir: str):
        self.service = service
        self.web_root = web_root
        self.reference_dir = reference_dir
        self.reset_lock = threading.Lock()

    def reset(self, actor: str) -> int:
        """Re-seed the demo in place. The append-only audit log, id counters and sessions survive."""
        with self.reset_lock:
            svc = self.service
            with svc.db.lock:
                epoch = int(svc.db.control("demo_epoch", "0") or 0) + 1
                svc.db.wipe()
                seed(svc.db, svc.fixtures, epoch)
                svc.bootstrap()
                with svc._unit():
                    svc.db.audit("PRESENTER", actor, "DEMO_RESET", details={"epoch": epoch, "audit_log": "preserved"})
            return epoch


Route = Tuple[str, "re.Pattern[str]", Callable[..., Any]]


def public_config(svc: SmartCreditService) -> Dict[str, Any]:
    p = svc.policy
    return {
        "policy_version": p["policy_version"],
        "amount_range": {k: p["case_ranges"]["requested_loan_vnd"][k] for k in ("min", "max")},
        "tenures": p["repayment_terms"]["offered_tenures"],
        "pricing": {"annual_interest_rate_pct": p["repayment_terms"]["annual_interest_rate_pct"],
                    "rate_basis": p["repayment_terms"]["rate_basis"], "upfront_fee_vnd": p["repayment_terms"]["upfront_fee_vnd"],
                    "late_fee_vnd": 0,
                    "overdue_principal_rate_pct": round(p["repayment_terms"]["annual_interest_rate_pct"]
                                                        * p["loan_servicing"]["overdue_interest"]["overdue_principal_rate_multiplier"], 2),
                    "late_interest_rate_pct": p["loan_servicing"]["overdue_interest"]["late_interest_rate_pct_per_year"],
                    "label": p["repayment_terms"]["label"]},
        "policy_hash": svc.policy_hash,
        "stp_max_limit_vnd": p["affordability"]["stp_max_limit_vnd"],
        "min_limit_vnd": p["affordability"]["min_limit_vnd"],
        "approval_authority": {"max_vnd": p["officer_controls"]["approval_authority_max_vnd"],
                               "bound": p["officer_controls"]["approval_authority_bound"]},
        "activation_consents": p["consent"]["activation_consents"],
        "conversion_window_days": p["instalment_conversion"]["window_days"],
        "auth": {"face_per_tx_vnd": p["authentication"]["face_threshold_per_transaction_vnd"],
                 "face_daily_vnd": p["authentication"]["face_threshold_daily_vnd"],
                 "otp_ttl_seconds": p["authentication"]["otp_ttl_seconds"]},
        "expected_decision_hours": p["officer_controls"]["customer_expected_decision_text_hours"],
        "checkout_timeout_seconds": p["checkout"]["timeout_seconds"],
        "hotline": HOTLINE,
    }


def build_routes(app: App) -> List[Route]:
    svc = lambda: app.service  # noqa: E731

    def session(h: "Handler", *roles: str) -> Dict[str, Any]:
        auth = h.headers.get("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else None
        return svc().resolve_session(token, roles)

    def cust(h):
        return session(h, "CUSTOMER")["subject_id"]

    def officer(h):
        return session(h, "OFFICER")["subject_id"]

    def presenter(h):
        s = session(h, "PRESENTER", "OFFICER")
        return "%s:%s" % (s["role"], s["subject_id"])

    def body_int(b: Dict[str, Any], key: str, required: bool = False) -> Optional[int]:
        v = b.get(key)
        if v is None:
            if required:
                raise ServiceError(422, "FIELD_REQUIRED", "'%s' is required." % key)
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ServiceError(422, "INVALID_FIELD", "'%s' must be an integer." % key)
        return v

    def reference(h, _b, _m):
        officer(h)
        path = os.path.join(app.reference_dir, "dataset_audit.json")
        if not os.path.exists(path):
            raise ServiceError(404, "NOT_GENERATED", "Run scripts/dataset_audit.py to generate dataset baselines.")
        with open(path) as f:
            data = json.load(f)
        data.pop("columns", None)
        return 200, data

    def me(h, _b, _m):
        cid = cust(h)
        c = svc().get_customer(cid)
        return 200, c

    def reset(h, _b, _m):
        actor = presenter(h)
        return 200, {"reset": True, "epoch": app.reset(actor), "note": "Demo re-seeded. Audit log preserved."}

    def presenter_state(h, _b, _m):
        presenter(h)
        out = svc().demo_controls()
        out["personas"] = svc().presenter_personas()
        out["scenarios"] = svc().fixtures["scenarios"]
        return 200, out

    def timeline(h, _b, m):
        cid = cust(h)
        svc().get_view(m["tx"], cid)
        keep = ("event_id", "ts", "event_type", "from_state", "to_state")
        return 200, [{k: e[k] for k in keep} for e in svc().audit_log(transaction_id=m["tx"])
                     if not e["event_type"].startswith(("CHECKOUT_DECISION", "VALIDATION"))]

    R: List[Tuple[str, str, Callable[..., Any]]] = [
        ("GET", r"/api/health", lambda h, b, m: (200, {"ok": True, "simulated": True})),
        ("GET", r"/api/public/config", lambda h, b, m: (200, public_config(svc()))),
        ("GET", r"/api/public/customers", lambda h, b, m: (200, svc().public_customers())),
        ("POST", r"/api/session/customer", lambda h, b, m: (201, svc().customer_sign_in(str(b.get("customer_id") or "")))),
        ("POST", r"/api/session/officer", lambda h, b, m: (201, svc().officer_sign_in())),
        ("POST", r"/api/session/presenter", lambda h, b, m: (201, svc().presenter_sign_in())),
        ("POST", r"/api/session/sign-out", lambda h, b, m: (200, svc().sign_out(
            (h.headers.get("Authorization") or "")[7:]))),

        # ---- customer
        ("GET", r"/api/me", me),
        ("GET", r"/api/credit", lambda h, b, m: (200, svc().credit_summary(cust(h)))),
        ("GET", r"/api/qr/demo-codes", lambda h, b, m: (200, svc().list_qr_codes(cust(h)))),
        ("POST", r"/api/qr/resolve", lambda h, b, m: _created(svc().resolve_qr(cust(h), b.get("payload"), b.get("amount_vnd")))),
        ("GET", r"/api/transactions", lambda h, b, m: (200, svc().history(cust(h)))),
        ("GET", r"/api/transactions/(?P<tx>[\w-]+)", lambda h, b, m: (200, svc().get_view(m["tx"], cust(h)))),
        ("GET", r"/api/transactions/(?P<tx>[\w-]+)/timeline", timeline),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/pay-now", lambda h, b, m: (200, svc().choose_pay_now(m["tx"], cust(h)))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/pay-later", lambda h, b, m: (200, svc().choose_pay_later(
            m["tx"], cust(h), b.get("activation_consents")))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/split", lambda h, b, m: (200, svc().accept_split(m["tx"], cust(h)))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/consent-pause", lambda h, b, m: (200, svc().pause_for_consent(
            m["tx"], cust(h), bool(b.get("resume"))))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/tenure", lambda h, b, m: (200, svc().select_tenure(
            m["tx"], cust(h), body_int(b, "tenure_months", True)))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/agreement", lambda h, b, m: (200, svc().accept_agreement(
            m["tx"], cust(h), b.get("accepted")))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/auth", lambda h, b, m: (201, svc().create_challenge(
            m["tx"], cust(h), str(b.get("purpose") or "")))),
        ("POST", r"/api/auth/(?P<ch>[\w-]+)/verify", lambda h, b, m: (200, svc().verify_challenge(
            m["ch"], cust(h), b.get("code"), b.get("face_result")))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/pay", lambda h, b, m: (200, svc().pay(
            m["tx"], cust(h), h.headers.get("Idempotency-Key") or b.get("idempotency_key"), b.get("challenge_id")))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/cancel", lambda h, b, m: (200, svc().cancel(m["tx"], cust(h)))),
        ("GET", r"/api/transactions/(?P<tx>[\w-]+)/conversion", lambda h, b, m: (200, svc().conversion_offer(m["tx"], cust(h)))),
        ("POST", r"/api/transactions/(?P<tx>[\w-]+)/convert", lambda h, b, m: (200, svc().convert_to_instalments(
            m["tx"], cust(h), body_int(b, "tenure_months", True), b.get("accepted"), b.get("challenge_id")))),

        ("GET", r"/api/smart-credit/requirements", lambda h, b, m: (200, svc().activation_requirements(cust(h)))),
        ("POST", r"/api/smart-credit/activate", lambda h, b, m: (201, svc().start_activation(
            cust(h), b.get("consents") or {}, requested_limit=body_int(b, "requested_limit_vnd")))),
        ("POST", r"/api/smart-credit/limit-increase", lambda h, b, m: (201, svc().create_limit_increase(
            cust(h), b.get("requested_limit_vnd"), b.get("reason") or "", b.get("transaction_id")))),
        ("GET", r"/api/applications", lambda h, b, m: (200, svc().list_applications(cust(h)))),
        ("GET", r"/api/applications/(?P<app>[\w-]+)", lambda h, b, m: (200, svc().application_view(m["app"], cust(h)))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/alternative-data", lambda h, b, m: (200, svc().alternative_data_consent(
            m["app"], cust(h), b.get("consents")))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/appeal", lambda h, b, m: (200, svc().appeal_decline(
            m["app"], cust(h), b.get("reason") or ""))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/cancel", lambda h, b, m: (200, svc().cancel_application(m["app"], cust(h)))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/withdraw-consent", lambda h, b, m: (200, svc().withdraw_consent(
            m["app"], cust(h)))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/accept-offer", lambda h, b, m: (200, svc().accept_offer(m["app"], cust(h)))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/decline-offer", lambda h, b, m: (200, svc().decline_offer(
            m["app"], cust(h), b.get("ask_for_more") is True))),
        ("POST", r"/api/applications/(?P<app>[\w-]+)/alternative-data/(?P<src>[\w-]+)/withdraw", lambda h, b, m: (200,
            svc().withdraw_alt_source(m["app"], cust(h), m["src"]))),
        ("POST", r"/api/smart-credit/withdraw-consent", lambda h, b, m: (200, svc().withdraw_line_consent(cust(h)))),

        ("GET", r"/api/loans", lambda h, b, m: (200, svc().list_loans(cust(h)))),
        ("GET", r"/api/loans/(?P<loan>[\w-]+)", lambda h, b, m: (200, svc().get_loan(m["loan"], cust(h)))),
        ("POST", r"/api/loans/(?P<loan>[\w-]+)/repay", lambda h, b, m: (200, svc().repay(
            m["loan"], cust(h), b.get("instalments") or [], h.headers.get("Idempotency-Key") or b.get("idempotency_key")))),
        ("GET", r"/api/notifications", lambda h, b, m: (200, svc().notifications(cust(h)))),
        ("POST", r"/api/notifications/read", lambda h, b, m: (200, svc().mark_notifications_read(cust(h)))),

        # ---- officer
        ("GET", r"/api/officer/queue", lambda h, b, m: (200, svc().officer_queue(officer(h)))),
        ("GET", r"/api/officer/cases/(?P<app>[\w-]+)", lambda h, b, m: (200, svc().officer_case(m["app"], officer(h)))),
        ("POST", r"/api/officer/cases/(?P<app>[\w-]+)/decision", lambda h, b, m: (200, svc().officer_decide(
            m["app"], officer(h), b.get("action"), b.get("reason_code"), b.get("note"), b.get("expected_version"),
            b.get("approved_limit_vnd"), b.get("requested_sources")))),
        ("POST", r"/api/officer/cases/(?P<app>[\w-]+)/qa", lambda h, b, m: (200, svc().officer_qa_review(
            m["app"], officer(h), b.get("outcome"), b.get("note")))),
        ("GET", r"/api/officer/policy", lambda h, b, m: (200, (officer(h), svc().policy)[1])),
        ("GET", r"/api/officer/audit", lambda h, b, m: (200, (officer(h), svc().audit_log())[1])),
        ("GET", r"/api/reference/dataset", reference),

        # ---- presenter (presenter or officer session)
        ("GET", r"/api/presenter/state", presenter_state),
        ("POST", r"/api/presenter/controls", lambda h, b, m: (200, svc().set_demo_control(
            str(b.get("key")), str(b.get("value")), presenter(h)))),
        ("POST", r"/api/presenter/clock", lambda h, b, m: (200, svc().set_business_date(
            presenter(h), b.get("date"), b.get("advance_days")))),
        ("POST", r"/api/presenter/reset", reset),
        ("GET", r"/api/presenter/live-demo", lambda h, b, m: (presenter(h), (200, svc().live_demo_config()))[1]),
        ("POST", r"/api/presenter/live-demo", lambda h, b, m: (201, svc().create_live_customer(b, presenter(h)))),
        ("GET", r"/api/presenter/live-demo/(?P<cid>[\w-]+)", lambda h, b, m: (presenter(h),
                                                                              (200, svc().live_customer_status(m["cid"])))[1]),
        ("GET", r"/api/presenter/scenario-lab", lambda h, b, m: (presenter(h), (200, svc().scenario_lab_config()))[1]),
        ("GET", r"/api/presenter/personas", lambda h, b, m: (presenter(h), (200, svc().persona_showcase()))[1]),
        ("POST", r"/api/presenter/simulate", lambda h, b, m: (presenter(h), (200, svc().simulate_scenario(b)))[1]),
        ("GET", r"/api/presenter/risk", lambda h, b, m: (presenter(h), (200, svc().risk_round_config()))[1]),
        ("POST", r"/api/presenter/risk/overload", lambda h, b, m: (presenter(h), (200, svc().risk_overload(b)))[1]),
        ("POST", r"/api/presenter/risk/rules", lambda h, b, m: (presenter(h), (200, svc().risk_rule_case(b)))[1]),
        ("POST", r"/api/presenter/risk/data", lambda h, b, m: (presenter(h), (200, svc().risk_data_case(b)))[1]),
        ("GET", r"/api/presenter/open-checkouts", lambda h, b, m: (presenter(h), (200, svc().presenter_open_checkouts()))[1]),
        ("POST", r"/api/presenter/transactions/(?P<tx>[\w-]+)/expire-now", lambda h, b, m: (200, svc().expire_now(
            m["tx"], presenter(h)))),
    ]
    return [(meth, re.compile("^" + pat + "$"), fn) for meth, pat, fn in R]


def _created(result: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return (200 if result.get("resumed") else 201), result


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' "
                               "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; frame-ancestors 'none'",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartCreditDemo/2.0"
    routes: List[Route] = []
    app: App = None  # type: ignore
    query: Dict[str, List[str]] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("SMART_CREDIT_QUIET") != "1":
            super().log_message(fmt, *args)

    def _headers(self) -> None:
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._headers()
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        self.query = parse_qs(parsed.query)
        if not parsed.path.startswith("/api/"):
            return self._static(parsed.path) if method == "GET" else self._send(405, {"error": {"code": "METHOD_NOT_ALLOWED"}})
        body: Dict[str, Any] = {}
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 64 * 1024:
                return self._send(413, {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request too large."}})
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._send(400, {"error": {"code": "INVALID_JSON", "message": "Request body is not valid JSON."}})
                if not isinstance(body, dict):
                    return self._send(400, {"error": {"code": "INVALID_JSON", "message": "Body must be a JSON object."}})
        path_matched = False
        for meth, pattern, fn in self.routes:
            m = pattern.match(parsed.path)
            if not m:
                continue
            path_matched = True
            if meth != method:
                continue
            try:
                status, payload = fn(self, body, m.groupdict())
                return self._send(status, payload)
            except ServiceError as e:
                return self._send(e.http_status, {"error": {"code": e.code, "message": e.message, "details": e.details}})
            except Exception:  # never leak internals to the client
                err_id = secrets.token_hex(4)
                print("ERROR %s %s %s" % (err_id, method, parsed.path))
                traceback.print_exc()
                return self._send(500, {"error": {"code": "INTERNAL_ERROR", "error_id": err_id,
                                                  "message": "Something went wrong on our side. Nothing was charged. "
                                                             "Please try again (ref %s)." % err_id}})
        if path_matched:
            return self._send(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "Method not allowed."}})
        return self._send(404, {"error": {"code": "NOT_FOUND", "message": "Unknown endpoint."}})

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(self.app.web_root, path.lstrip("/")))
        if not full.startswith(os.path.realpath(self.app.web_root) + os.sep) or not os.path.isfile(full):
            self.send_response(404)
            self._headers()
            self.end_headers()
            return
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith((".html", ".css", ".js", ".svg")):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._headers()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"routes": build_routes(app), "app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
