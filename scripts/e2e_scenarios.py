"""End-to-end scenario runner over real HTTP against a running server.

    python3 run.py --port 8080 --reset          # in one terminal
    python3 scripts/e2e_scenarios.py http://127.0.0.1:8080

Runs every demo scenario (customer + officer + presenter) through the public API,
asserts the expected outcome, and writes a JSON log to docs/test_logs/.
Simulation only - fictional data.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
CONSENTS = {"SC_TERMS": True, "DATA_PROCESSING": True}
LOG, RESULTS = [], []


def call(method, path, body=None, token=None, headers=None, expect=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, method=method, headers=h)) as r:
            st, out = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        st, out = e.code, json.loads(e.read())
    LOG.append({"method": method, "path": path, "status": st, "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": (out.get("error") or {}).get("code") if isinstance(out, dict) else None})
    if expect is not None and st != expect:
        raise AssertionError("%s %s -> %s %s" % (method, path, st, out))
    return st, out


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)


def cust(cid):
    return call("POST", "/api/session/customer", {"customer_id": cid}, expect=201)[1]["token"]


def officer():
    """No access code and no profile: any officer-dashboard session (case assumption)."""
    return call("POST", "/api/session/officer", {}, expect=201)[1]["token"]


def scan(tok, ref, amount=None):
    p = [q for q in call("GET", "/api/qr/demo-codes", token=tok)[1] if q["qr_ref"] == ref][0]["payload"]
    return call("POST", "/api/qr/resolve", {"payload": p, "amount_vnd": amount}, tok)[1]["view"]["transaction"]["transaction_id"]


def auth(tok, tx, purpose):
    ch = call("POST", "/api/transactions/%s/auth" % tx, {"purpose": purpose}, tok, expect=201)[1]
    body = {"face_result": "MATCH"} if ch["method"] == "FACE" else {"code": ch["simulated_authenticator_code"]}
    call("POST", "/api/auth/%s/verify" % ch["challenge_id"], body, tok, expect=200)
    return ch


def pay(tok, tx, purpose):
    ch = auth(tok, tx, purpose)
    return call("POST", "/api/transactions/%s/pay" % tx, {"challenge_id": ch["challenge_id"]}, tok,
                {"Idempotency-Key": str(uuid.uuid4())}, expect=200)[1], ch


def pay_later(tok, tx, consents=None, tenure=None):
    v = call("POST", "/api/transactions/%s/pay-later" % tx, {"activation_consents": consents} if consents else {}, tok, expect=200)[1]
    return v


def finish_pl(tok, tx, tenure=None):
    if tenure:
        call("POST", "/api/transactions/%s/tenure" % tx, {"tenure_months": tenure}, tok, expect=200)
    call("POST", "/api/transactions/%s/agreement" % tx, {"accepted": True}, tok, expect=200)
    return pay(tok, tx, "PAY_LATER_AGREEMENT")


def case(app, off):
    return call("GET", "/api/officer/cases/" + app, token=off, expect=200)[1]


def officer_approve(app, off, limit=None, code="AP-01"):
    c = case(app, off)
    ok(c["eligibility"]["case_eligible"], c["eligibility"]["checks"])
    lim = limit or c["eligibility"]["max_approvable_vnd"]
    return call("POST", "/api/officer/cases/%s/decision" % app, {"action": "approve", "reason_code": code, "note": "Eligible; within capacity",
                "expected_version": c["application"]["version"], "approved_limit_vnd": lim}, off, expect=200)[1]


def scenario(n, title):
    def deco(fn):
        def run(pres):
            call("POST", "/api/presenter/reset", token=pres, expect=200)
            t0 = time.perf_counter()
            try:
                detail = fn(pres) or ""
                RESULTS.append({"id": n, "title": title, "result": "PASS", "detail": detail, "ms": round((time.perf_counter() - t0) * 1000)})
            except Exception as e:  # report and continue
                RESULTS.append({"id": n, "title": title, "result": "FAIL", "detail": str(e)[:400]})
        run.n = n
        return run
    return deco


@scenario(1, "Instant checkout on an active line (GREEN) with face authentication")
def s1(pres):
    t = cust("CUS-001")
    tx = scan(t, "QR-261016-0842")
    v = pay_later(t, tx)
    d = v["pay_later_decision"]
    ok(d["band"] == "GREEN" and 3 not in d["affordable_tenures"], d)
    ok(v["credit"]["held_vnd"] == 12000000, "hold placed")
    v, ch = finish_pl(t, tx)
    ok(v["outcome"] == "PAYMENT_COMPLETED" and ch["method"] == "FACE", (v["outcome"], ch["method"]))
    cr = call("GET", "/api/credit", token=t)[1]
    ok((cr["outstanding_principal_vnd"], cr["held_vnd"]) == (12000000, 0), cr)
    return "monthly %s, available %s" % (v["terms"]["monthly_instalment_vnd"], cr["available_vnd"])


@scenario(2, "Exposure hold + split tender: no silent over-limit decline (concurrent checkouts)")
def s2(pres):
    t = cust("CUS-001")
    bal0 = call("GET", "/api/me", token=t, expect=200)[1]["balance_vnd"]
    a = scan(t, "QR-261016-0842")
    ok(pay_later(t, a)["pay_later_decision"]["band"] == "GREEN", "first checkout green")
    ok(call("GET", "/api/credit", token=t)[1]["held_vnd"] == 12000000, "first checkout holds 12M")
    b = scan(t, "QR-261016-1133")
    d = pay_later(t, b)["pay_later_decision"]
    # 15M fits the remaining 17.5M but no tenure is affordable on top of the held 12M -> split, not a decline (F-04/C-03)
    ok(d["reason_code"] == "R010-SPLIT" and d["split"]["cause"] == "AFFORDABILITY"
       and d["split"]["pay_later_vnd"] + d["split"]["pay_now_vnd"] == 15000000 and d["split"]["pay_later_vnd"] % 100000 == 0, d)
    c = scan(t, "QR-261016-1610")
    dc = pay_later(t, c)["pay_later_decision"]
    ok(dc["reason_code"] == "R010-SPLIT" and dc["split"]["cause"] == "AVAILABLE_LIMIT" and dc["available_vnd"] == 17500000
       and dc["split"]["pay_later_vnd"] <= dc["available_vnd"], "30M above the 17.5M available -> split offered explicitly")
    v = call("POST", "/api/transactions/%s/split" % b, {}, t, expect=200)[1]
    ok(v["transaction"]["method"] == "SPLIT" and v["transaction"]["pay_later_amount_vnd"] == d["split"]["pay_later_vnd"], v["transaction"])
    out, _ = finish_pl(t, b)
    ok(out["transaction"]["state"] == "PAYMENT_COMPLETED", out["transaction"]["state"])
    cr = call("GET", "/api/credit", token=t)[1]
    ok(cr["outstanding_principal_vnd"] == d["split"]["pay_later_vnd"], "only the Pay Later part is booked as credit")
    ok(call("GET", "/api/me", token=t)[1]["balance_vnd"] == bal0 - d["split"]["pay_now_vnd"], "Pay Now part debited")
    call("POST", "/api/transactions/%s/cancel" % a, {}, t, expect=200)
    call("POST", "/api/transactions/%s/cancel" % c, {}, t, expect=200)
    cr = call("GET", "/api/credit", token=t)[1]
    ok(cr["held_vnd"] == 0 and cr["available_vnd"] == 29500000 - d["split"]["pay_later_vnd"], "holds released on cancel")
    return "second checkout split %s PL + %s now; third: split (limit)" % (d["split"]["pay_later_vnd"], d["split"]["pay_now_vnd"])

@scenario(3, "Inline activation GREEN at checkout (no CIC history)")
def s3(pres):
    t = cust("CUS-007")
    tx = scan(t, "QR-261016-1505")
    v = pay_later(t, tx, CONSENTS)
    ok(v["activation"]["status"] == "APPROVED" and v["pay_later_decision"]["band"] == "GREEN", (v["activation"]["status"], v["pay_later_decision"]))
    v, _ = finish_pl(t, tx)
    ok(v["outcome"] == "PAYMENT_COMPLETED", v["outcome"])
    return "limit %s" % v["credit"]["limit_vnd"]


@scenario(4, "Activation AMBER (risk flag) -> Pay Now -> officer approves -> convert within 7 days")
def s4(pres):
    t = cust("CUS-004")
    tx = scan(t, "QR-261016-2004")
    v = pay_later(t, tx, CONSENTS)
    ok(v["pay_later_decision"]["reason_code"] == "R010-PENDING" and v["activation"]["status"] == "IN_REVIEW", v["pay_later_decision"])
    call("POST", "/api/transactions/%s/pay-now" % tx, {}, t, expect=200)
    v, _ = pay(t, tx, "ACCOUNT_PAYMENT")
    ok(v["outcome"] == "PAYMENT_COMPLETED", "paid now, shopper not parked")
    app = v["credit"]["open_application"]["application_id"]
    c = officer_approve(app, officer(), limit=15000000, code="AP-03")
    ok(c["application"]["credit_state"] == "APPROVED", c["application"]["credit_state"])
    offer = call("GET", "/api/transactions/%s/conversion" % tx, token=t)[1]
    ok(offer["eligible"], offer)
    ch = auth(t, tx, "INSTALMENT_CONVERSION")
    v = call("POST", "/api/transactions/%s/convert" % tx, {"tenure_months": offer["affordable_tenures"][0], "accepted": True,
                                                           "challenge_id": ch["challenge_id"]}, t, expect=200)[1]
    ok(v["transaction"]["converted_loan_ref"], "converted")
    return "no uploads; converted to %s months" % offer["affordable_tenures"][0]


@scenario(5, "Thin file: system chooses Layer 2 sources; validated data -> automatic decision; declining -> verified RED with appeal")
def s5(pres):
    t = cust("CUS-005")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    req = a["info_request"]
    ok(a["reason_code"] == "R010-EVD" and req["route"] == "ALTERNATIVE_DATA", a)
    ok([x["id"] for x in req["sources"]] == ["open_banking", "utility_bills"], "income + behaviour only; no e-wallet")
    ok("layer1" not in a["data_acquisition"] and "missing" not in req, "no Layer 1 detail shown to the customer")
    a2 = call("POST", "/api/applications/%s/alternative-data" % a["application_id"], {"consents": {}}, t, expect=200)[1]
    ok((a2["status"], a2["reason_code"], a2["appeal_available"]) == ("NOT_APPROVED", "R020-EVD", True), a2["reason_code"])
    b = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    b = call("POST", "/api/applications/%s/alternative-data" % b["application_id"],
             {"consents": {"open_banking": True, "utility_bills": True}}, t, expect=200)[1]
    ok((b["status"], b["reason_code"]) == ("APPROVED", "R001-L2"), b["reason_code"])
    ok(call("GET", "/api/credit", token=t)[1]["line_status"] == "ACTIVE", "validated Layer 2 supports the automatic decision")
    return "declined -> RED R020-EVD (appeal); granted -> validated Open Banking + utility -> GREEN %s" % b["approved_limit_vnd"]


@scenario(6, "Segment missing: no customer question, decided on verified data")
def s6(pres):
    t = cust("CUS-006")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    ok((a["status"], a["reason_code"]) == ("APPROVED", "R001"), a["reason_code"])
    ok(call("POST", "/api/applications/%s/information" % a["application_id"], {"fields": {}}, t)[0] == 404, "question removed")
    return "GREEN %s, nothing asked" % a["approved_limit_vnd"]


@scenario(7, "Data conflict -> excluded automatically -> automated Open Banking -> automatic decision (no officer)")
def s7(pres):
    t = cust("CUS-003")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    ok((a["status"], a["reason_code"]) == ("ACTION_NEEDED", "R010-EVD"), a["reason_code"])
    off = officer()
    ok(call("GET", "/api/officer/queue", token=off)[1]["queues"]["pending_review"] == [], "no officer case")
    v = call("POST", "/api/applications/%s/alternative-data" % a["application_id"],
             {"consents": {"open_banking": True, "utility_bills": True}}, t, expect=200)[1]
    ok((v["status"], v["reason_code"]) == ("APPROVED", "R001-L2"), v["reason_code"])
    return "approved %s automatically after Layer 2" % v["approved_limit_vnd"]


@scenario(8, "Weak gig profile: below stressed-capacity minimum for STP, officer decides within the officer maximum with a reason code")
def s8(pres):
    t = cust("CUS-002")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    ok(a["reason_code"] == "R010-FLAG", a["reason_code"])
    off = officer()
    app = a["application_id"]
    c = case(app, off)
    el = c["eligibility"]
    # v5: the officer maximum uses average verified income (F-19); the conservative P25 capacity only bounds STP.
    ok(el["case_eligible"] and el["officer_max_limit_vnd"] > el["stp_capacity_vnd"], el)
    mx, ver = el["max_approvable_vnd"], c["application"]["version"]
    body = lambda lim, code: {"action": "approve", "reason_code": code, "note": "x", "expected_version": ver, "approved_limit_vnd": lim}
    st, e1 = call("POST", "/api/officer/cases/%s/decision" % app, body(mx + 100000, "AP-05"), off)
    ok(st == 422 and e1["error"]["code"] == "LIMIT_OUTSIDE_CAPACITY", e1)
    st, e2 = call("POST", "/api/officer/cases/%s/decision" % app, body(el["stp_capacity_vnd"] + 50000, "AP-05"), off)
    ok(st == 422 and e2["error"]["code"] == "LIMIT_OUTSIDE_CAPACITY", "off the VND 100,000 grid")
    above = (el["stp_capacity_vnd"] + mx) // 2 // 100000 * 100000
    st, e3 = call("POST", "/api/officer/cases/%s/decision" % app, body(above, "AP-03"), off)
    ok(st == 422 and e3["error"]["code"] == "REASON_CODE_NOT_ALLOWED", "above stressed capacity needs AP-01/AP-02/AP-05")
    st, _ = call("POST", "/api/officer/cases/%s/decision" % app, {"action": "reject", "note": "x", "expected_version": ver}, off)
    ok(st == 422, "reason code required")
    r = call("POST", "/api/officer/cases/%s/decision" % app, body(above, "AP-05"), off, expect=200)[1]
    ok(r["application"]["credit_state"] == "APPROVED", r["application"]["credit_state"])
    cr = call("GET", "/api/credit", token=t)[1]
    ok(cr["limit_vnd"] == above and cr["line_status"] == "ACTIVE", cr)
    return "STP capacity %s, officer max %s; approved %s with AP-05 (blocked: %s, %s, %s)" % (
        el["stp_capacity_vnd"], mx, above, e1["error"]["code"], e2["error"]["code"], e3["error"]["code"])

@scenario(9, "Ineligible merchant (MCC 6540) -> Pay Now only")
def s9(pres):
    t = cust("CUS-001")
    tx = scan(t, "QR-261016-1418")
    d = pay_later(t, tx)["pay_later_decision"]
    ok((d["reason_code"], d["band"], d["decision_class"]) == ("R030-MER", "N/A", "PRODUCT_SCOPE"), "not a credit decision")


@scenario(10, "Payment failure then retry with fresh authentication")
def s10(pres):
    t = cust("CUS-001")
    tx = scan(t, "QR-261016-1352")
    pay_later(t, tx)
    v, _ = finish_pl(t, tx)
    ok(v["outcome"] == "PAYMENT_FAILED" and v["hold"], "failed, hold kept")
    v, _ = pay(t, tx, "PAY_LATER_AGREEMENT")
    ok(v["outcome"] == "PAYMENT_COMPLETED", v["outcome"])


@scenario(11, "Arrears freeze, Pay Loan, overdue interest, cure only after due dates pass")
def s11(pres):
    t = cust("CUS-001")
    tx = scan(t, "QR-261016-0842")
    pay_later(t, tx)
    finish_pl(t, tx, 6)
    call("POST", "/api/presenter/clock", {"advance_days": 40}, pres, expect=200)
    ok(call("GET", "/api/credit", token=t)[1]["line_status"] == "FROZEN", "frozen")
    tx2 = scan(t, "QR-261016-2230")
    ok(pay_later(t, tx2)["pay_later_decision"]["reason_code"] == "R020-ARR", "blocked")
    loan = call("GET", "/api/loans", token=t)[1]["loans"][0]
    oi = loan["next_due"]["overdue_interest_vnd"]
    # 9 days overdue (Circular 39 Art. 13.4, no late fee): 2M principal at 150% x 12%/yr + 120k unpaid interest at 10%/yr
    ok(oi == round(2000000 * 0.12 * 1.5 * 9 / 365) + round(120000 * 0.10 * 9 / 365) and loan["next_due"]["total_due_vnd"] == 2120000 + oi, loan["next_due"])
    ref = loan["loan_ref"]
    call("POST", "/api/loans/%s/repay" % ref, {"instalments": [1]}, t, {"Idempotency-Key": str(uuid.uuid4())}, expect=200)
    cr = call("GET", "/api/credit", token=t)[1]
    ok(cr["line_status"] == "ACTIVE" and cr["internal_delinquency_flag"] == 1, cr)
    call("POST", "/api/loans/%s/repay" % ref, {"instalments": [2, 3, 4]}, t, {"Idempotency-Key": str(uuid.uuid4())}, expect=200)
    ok(call("GET", "/api/credit", token=t)[1]["internal_delinquency_flag"] == 1, "prepaying future instalments does not cure (F-15)")
    call("POST", "/api/presenter/clock", {"advance_days": 85}, pres, expect=200)   # past the 4th due date (2027-02-16)
    ok(call("GET", "/api/credit", token=t)[1]["internal_delinquency_flag"] == 0, "cured after 3 due dates met on time")
    return "overdue interest %s VND; cured after due dates 2-4 passed" % oi

@scenario(12, "Static counter QR -> Pay Now with Smart OTP")
def s12(pres):
    t = cust("CUS-001")
    tx = scan(t, "QR-STATIC-ANC", 85000)
    call("POST", "/api/transactions/%s/pay-now" % tx, {}, t, expect=200)
    v, ch = pay(t, tx, "ACCOUNT_PAYMENT")
    ok(v["outcome"] == "PAYMENT_COMPLETED" and ch["method"] == "SMART_OTP", (v["outcome"], ch["method"]))


@scenario(13, "Bank system unavailable (fail-safe) and CIC outage")
def s13(pres):
    call("POST", "/api/presenter/controls", {"key": "core_system_unavailable", "value": "1"}, pres, expect=200)
    t = cust("CUS-001")
    ok(pay_later(t, scan(t, "QR-261016-2110"))["pay_later_decision"]["reason_code"] == "R010-API", "fallback")
    call("POST", "/api/presenter/controls", {"key": "cic_unavailable", "value": "1"}, pres, expect=200)
    t7 = cust("CUS-007")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t7, expect=201)[1]
    ok((a["status"], a["reason_code"]) == ("CHECKING", "R010-API"), a["status"])
    call("POST", "/api/presenter/controls", {"key": "cic_unavailable", "value": "0"}, pres, expect=200)
    call("POST", "/api/presenter/clock", {"advance_days": 1}, pres, expect=200)  # next servicing cycle retries automatically
    ok(call("GET", "/api/applications/" + a["application_id"], token=t7)[1]["status"] == "APPROVED", "retried, no officer")


@scenario(14, "Timer expiry releases the hold")
def s14(pres):
    t = cust("CUS-001")
    tx = scan(t, "QR-261016-0915")
    pay_later(t, tx)
    ok(call("GET", "/api/credit", token=t)[1]["held_vnd"] == 6400000, "held")
    call("POST", "/api/presenter/transactions/%s/expire-now" % tx, {}, pres, expect=200)
    ok(call("GET", "/api/transactions/" + tx, token=t)[1]["outcome"] == "EXPIRED", "expired")
    ok(call("GET", "/api/credit", token=t)[1]["held_vnd"] == 0, "released")


@scenario(15, "Security: header impersonation, unauthenticated presenter controls, tampered QR")
def s15(pres):
    ok(call("GET", "/api/me", headers={"X-Demo-Customer": "CUS-001"})[0] == 401, "header identity ignored")
    ok(call("POST", "/api/presenter/clock", {"advance_days": 1})[0] == 401, "clock locked")
    ok(call("GET", "/api/config/policy")[0] == 404, "policy endpoint removed")
    t = cust("CUS-001")
    p = [q for q in call("GET", "/api/qr/demo-codes", token=t)[1] if q["qr_ref"] == "QR-261016-0842"][0]["payload"]
    st, e = call("POST", "/api/qr/resolve", {"payload": p.replace("540812000000", "540811000000")}, t)
    ok(st == 422 and e["error"]["code"] == "QR_CRC_MISMATCH", e)


@scenario(16, "Officer authority = product scope (VND 1.6M-90.1M inclusive) and access without any code")
def s16(pres):
    ok(call("POST", "/api/session/officer", {}, expect=201)[1]["officer"]["approval_authority_max_vnd"] == 90100000, "no code needed")
    ok(call("GET", "/api/public/officers")[0] == 404, "no officer profiles")
    # Over HTTP the persona's capacity is below the product maximum; the 90,100,000 / 90,200,000 boundary with a capacity at
    # the product maximum is covered by tests/test_service.py and tests/test_validation_matrix.py (V-11).
    t = cust("CUS-004")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    off = officer()
    c = case(a["application_id"], off)
    au = c["eligibility"]["authority"]
    ok((au["max_vnd"], au["exclusive"], au["highest_allowed_vnd"]) == (90100000, False, 90100000), au)
    st, e = call("POST", "/api/officer/cases/%s/decision" % a["application_id"], {"action": "approve", "reason_code": "AP-03", "note": "x",
                 "expected_version": c["application"]["version"], "approved_limit_vnd": 90200000}, off)
    ok(st == 422 and e["error"]["code"] == "ABOVE_APPROVAL_AUTHORITY", e)
    return "authority rule %s; 90,200,000 -> %s" % (au["rule"], e["error"]["code"])


@scenario(17, "Layer 2 failures: outage -> automatic retry; incomplete data -> verified RED with appeal")
def s17(pres):
    call("POST", "/api/presenter/controls", {"key": "alt_data_api_unavailable", "value": "1"}, pres, expect=200)
    t = cust("CUS-005")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    v = call("POST", "/api/applications/%s/alternative-data" % a["application_id"],
             {"consents": {"open_banking": True, "utility_bills": True}}, t, expect=200)[1]
    r = v["data_acquisition"]["layer2"][0]["results"][0]
    ok((v["status"], v["reason_code"], r["outcome"]) == ("CHECKING", "R010-API", "UNAVAILABLE"), v)
    call("POST", "/api/presenter/controls", {"key": "alt_data_api_unavailable", "value": "0"}, pres, expect=200)
    call("POST", "/api/presenter/clock", {"advance_days": 1}, pres, expect=200)
    ok(call("GET", "/api/applications/" + a["application_id"], token=t)[1]["status"] == "APPROVED", "retried automatically")
    call("POST", "/api/presenter/reset", token=pres, expect=200)
    call("POST", "/api/presenter/controls", {"key": "alt_data_incomplete", "value": "1"}, pres, expect=200)
    t = cust("CUS-005")
    a = call("POST", "/api/smart-credit/activate", {"consents": CONSENTS}, t, expect=201)[1]
    v = call("POST", "/api/applications/%s/alternative-data" % a["application_id"],
             {"consents": {"open_banking": True, "utility_bills": False}}, t, expect=200)[1]
    ok(v["data_acquisition"]["layer2"][0]["results"][0]["outcome"] == "INVALID" and v["reason_code"] == "R020-EVD", v)
    return "outage -> retried -> GREEN; 3-month data rejected -> RED R020-EVD (appeal)"


def main():
    pres = call("POST", "/api/session/presenter", {}, expect=201)[1]["token"]
    for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13, s14, s15, s16, s17):
        fn(pres)
    call("POST", "/api/presenter/reset", token=pres)
    passed = sum(1 for r in RESULTS if r["result"] == "PASS")
    for r in RESULTS:
        print("%-4s %2d. %s  %s" % (r["result"], r["id"], r["title"], r.get("detail", "")))
    print("\n%d/%d scenarios passed, %d HTTP calls" % (passed, len(RESULTS), len(LOG)))
    os.makedirs(os.path.join(ROOT, "docs", "test_logs"), exist_ok=True)
    dest = os.path.join(ROOT, "docs", "test_logs", "e2e_results.json")
    with open(dest, "w") as f:
        json.dump({"base": BASE, "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "passed": passed, "total": len(RESULTS),
                   "scenarios": RESULTS, "http_calls": len(LOG), "calls": LOG}, f, indent=1)
    print("log:", os.path.relpath(dest, ROOT))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
