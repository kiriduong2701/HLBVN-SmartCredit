"""Instant-checkout decision (v6): can this purchase be paid with an ALREADY APPROVED Smart Credit line, right now?

The CREDIT outcome and the PAYMENT options are separate fields: Pay Now is always offered next to the outcome and never
replaces it. Each result carries `decision_class`:
  CREDIT        GREEN R001 (hold placed) | AMBER R010-SPLIT (Pay Later for the eligible part, explicit choice) |
                RED R020-ARR / R020-DEF / R020-AFF (verified decline of Pay Later for this purchase)
  PRODUCT_SCOPE N/A R030-MER / R030-AMT - Pay Later does not apply to this merchant/QR/amount (not a credit decision)
  OPERATIONAL   AMBER R010-API / R010-PENDING / R010-STALE / R010-LIMREV - not decidable instantly; nothing is declined
  FRAUD_HOLD    AMBER R010-FRD (unreviewed Layer 0 signal) | STOP HS-05 (officer-confirmed fraud)
Nothing is ever parked for a human at the counter. Mandatory checks run for every transaction. Pure function; no I/O.
"""
from typing import Any, Dict, List

GREEN, AMBER, RED, NO_LINE, NA, STOP = "GREEN", "AMBER", "RED", "NO_LINE", "N/A", "STOP"
CLASS = {"R001": "CREDIT", "R010-SPLIT": "CREDIT", "R020-ARR": "CREDIT", "R020-DEF": "CREDIT", "R020-AFF": "CREDIT",
         "R030-MER": "PRODUCT_SCOPE", "R030-AMT": "PRODUCT_SCOPE", "R010-FRD": "FRAUD_HOLD", "HS-05": "FRAUD_HOLD",
         "NO_LINE": "NO_LINE"}


def decide(ctx: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    """ctx: amount_vnd, identity (identity.evaluate at CHECKOUT), merchant {eligible, reason}, line {status, limit_vnd,
    assessed_on_age_days, review_in_progress}, exposure {outstanding_principal_vnd, held_vnd}, aggregate_available_vnd,
    delinquency {arrears, default, days_overdue}, affordability (capacity.affordability for the full amount),
    split_affordable_vnd (largest affordable principal <= available, from capacity.max_affordable_principal),
    system_available, activation_pending."""
    checks: List[Dict[str, Any]] = []

    def check(cid: str, label: str, ok: Any, detail: str = "") -> bool:
        checks.append({"id": cid, "label": label, "result": "PASS" if ok is True else ("SKIP" if ok is None else "FAIL"),
                       "detail": detail})
        return ok is True

    def out(band: str, code: str, customer_reason: str, rule: str, message: str, **extra: Any) -> Dict[str, Any]:
        o = {"band": band, "reason_code": code, "customer_reason": customer_reason, "rule_id": rule, "message": message,
             "checks": checks, "offers": [], "affordable_tenures": [], "alternatives": ["PAY_NOW"], "split": None,
             "decision_class": CLASS.get(code, "OPERATIONAL")}
        o.update(extra)
        o["credit_outcome"] = {"GREEN": "APPROVED", "RED": "DECLINED"}.get(band) or (
            "PARTIAL" if code == "R010-SPLIT" else "NOT_ASSESSED")
        # Payment options are separate from the credit outcome: Pay Now from the customer's own account is always one of them.
        opts = (["PAY_LATER"] if band == GREEN else []) + list(o["alternatives"])
        o["payment_options"] = opts if "PAY_NOW" in opts else opts + ["PAY_NOW"]
        return o

    amount = ctx["amount_vnd"]
    rng = policy["case_ranges"]["requested_loan_vnd"]
    m = ctx["merchant"]
    ident = ctx.get("identity") or {}

    if not check("TX-00", "Session, device and identity checks (Layer 0)", not ident.get("hits") and not ident.get("fraud_confirmed"),
                 "; ".join(h["rule_id"] for h in ident.get("hits", [])) or ("fraud confirmed" if ident.get("fraud_confirmed") else "clear")):
        if ident.get("fraud_confirmed"):
            return out(STOP, "HS-05", "IDENTITY_REVIEW", "TX-00", "Fraud confirmed by an officer: no Smart Credit. Pay Now from "
                       "the customer's own account remains subject to the bank's standard payment controls.", alternatives=["PAY_NOW"])
        return out(AMBER, "R010-FRD", "IDENTITY_REVIEW", "TX-00", "Identity / fraud signal: Pay Now only; case sent to an "
                   "officer review. Never an automatic decline.", alternatives=["PAY_NOW"])
    if not check("TX-01", "Merchant eligible for Pay Later", bool(m.get("eligible")), m.get("reason") or ""):
        return out(NA, "R030-MER", "MERCHANT_NOT_ELIGIBLE", "TX-01", m.get("reason") or "Merchant not eligible.")
    above_max = amount > rng["max"]
    label = "Amount within the Pay Later product range"
    if amount < rng["min"]:
        check("TX-02", label, False, "below {:,} VND".format(rng["min"]))
        return out(NA, "R030-AMT", "AMOUNT_BELOW_MIN", "TX-02", "Amount %s below the product minimum." % "{:,}".format(amount))
    check("TX-02", label, None if above_max else True, ("above {:,} VND: only an in-scope part can be split; the rest is out of "
          "scope" if above_max else "{:,}-{:,} VND").format(rng["max"]) if above_max else "{:,}-{:,} VND".format(rng["min"], rng["max"]))
    if not check("TX-03", "Bank systems available", ctx.get("system_available", True) is True):
        return out(AMBER, "R010-API", "SYSTEM_UNAVAILABLE", "TX-03",
                   "Core/credit system unavailable: fail-safe fallback (never auto-approve).",
                   alternatives=["PAY_NOW", "CONVERT_LATER"])

    d = ctx.get("delinquency") or {}
    if not check("TX-04", "No arrears or default on Smart Credit", not (d.get("arrears") or d.get("default")),
                 "days overdue: %s" % d.get("days_overdue", 0)):
        return out(RED, "R020-DEF" if d.get("default") else "R020-ARR", "DEFAULT" if d.get("default") else "ARREARS",
                   "HS-04" if d.get("default") else "HS-03", "Line frozen while arrears/default exist.",
                   alternatives=["PAY_NOW", "PAY_LOAN"])

    line = ctx.get("line") or {}
    status = line.get("status")
    if status != "ACTIVE":
        check("TX-05", "Approved Smart Credit line", False, status or "none")
        if ctx.get("activation_pending"):
            return out(AMBER, "R010-PENDING", "ACTIVATION_IN_REVIEW", "TX-05",
                       "Limit activation is still being reviewed - off the checkout.", alternatives=["PAY_NOW", "CONVERT_LATER"])
        if status == "SUSPENDED":
            return out(RED, "R020-DEF", "DEFAULT", "TX-05", "Line suspended after a default: %s" % (line.get("frozen_reason") or ""),
                       alternatives=["PAY_NOW", "PAY_LOAN"])
        if status == "FROZEN":
            return out(RED, "R020-ARR", "LINE_FROZEN", "TX-05", "Line frozen: %s" % (line.get("frozen_reason") or ""),
                       alternatives=["PAY_NOW", "PAY_LOAN"])
        return out(NO_LINE, "NO_LINE", "NO_LINE", "TX-05", "No approved line; instant activation can be offered.",
                   alternatives=["ACTIVATE", "PAY_NOW"])
    check("TX-05", "Approved Smart Credit line", True, "{:,} VND".format(line["limit_vnd"]))

    cl = policy["credit_line"]
    age = line.get("assessed_on_age_days") or 0
    in_grace = bool(line.get("review_in_progress")) and age <= cl["review_after_days"] + cl["review_grace_days"]
    if not check("TX-06", "Limit assessment current (or renewal in progress within the grace period)",
                 age <= cl["review_after_days"] or in_grace,
                 "%s of %s days%s" % (age, cl["review_after_days"], " · renewal in progress" if in_grace else "")):
        return out(AMBER, "R010-STALE", "LIMIT_REVIEW_DUE", "TX-06",
                   "Limit review due (assessment older than %s days and not renewed)." % cl["review_after_days"],
                   alternatives=["PAY_NOW", "CONVERT_LATER"])

    ex = ctx["exposure"]
    line_available = line["limit_vnd"] - ex["outstanding_principal_vnd"] - ex["held_vnd"]
    agg = ctx.get("aggregate_available_vnd")
    available = max(0, min(line_available, agg) if agg is not None else line_available)
    split_ok = ctx.get("split_affordable_vnd") or 0
    pl_min = rng["min"]
    if above_max:
        available = min(available, rng["max"])  # never finance more than the product maximum on one purchase

    def split(cause: str) -> Dict[str, Any]:
        pl = min(split_ok, available)
        return out(AMBER, "R010-SPLIT", "SPLIT_TENDER", "TX-07" if cause == "AVAILABLE_LIMIT" else "TX-08",
                   "Split tender: Pay Later %s VND, Pay Now %s VND (%s)." % ("{:,}".format(pl), "{:,}".format(amount - pl), cause),
                   split={"pay_later_vnd": pl, "pay_now_vnd": amount - pl, "cause": cause,
                          "above_product_max": above_max}, available_vnd=available,
                   shortfall_vnd=amount - pl, alternatives=["SPLIT", "PAY_NOW", "LIMIT_INCREASE"])

    if not check("TX-07", "Amount within available limit (limit - outstanding principal - holds; aggregate digital-lending cap)",
                 amount <= available, "{:,} <= {:,}".format(amount, available)):
        if min(split_ok, available) >= pl_min:
            return split("PRODUCT_MAX" if above_max and available == rng["max"] else "AVAILABLE_LIMIT")
        if above_max:
            return out(NA, "R030-AMT", "AMOUNT_ABOVE_MAX", "TX-07", "Amount above the product maximum and no in-scope part can "
                       "be financed: Pay Now; larger financing is referred outside Smart Credit.", alternatives=["PAY_NOW"])
        return out(AMBER, "R010-LIMREV", "ABOVE_AVAILABLE", "TX-07",
                   "Available %s VND is below the Pay Later minimum for this purchase: Pay Now; a limit review runs off the "
                   "checkout." % "{:,}".format(available), available_vnd=available, shortfall_vnd=amount - available,
                   alternatives=["PAY_NOW", "LIMIT_INCREASE", "CONVERT_LATER"])

    aff = ctx["affordability"]
    if not aff.get("computable"):
        check("TX-08", "Repayments affordable (DTI on the largest instalment)", False, aff.get("reason", ""))
        return out(AMBER, "R010-OBL", "AFFORDABILITY_DATA_UNAVAILABLE", "TX-08",
                   "Affordability inputs unavailable - fallback.", alternatives=["PAY_NOW", "CONVERT_LATER"])
    if not check("TX-08", "Repayments affordable (DTI on the largest instalment)", bool(aff["affordable_tenures"]),
                 "DTI ceiling %s; affordable tenures %s" % (aff["max_total_dti"], aff["affordable_tenures"])):
        if min(split_ok, available) >= pl_min:
            return split("AFFORDABILITY")
        return out(RED, "R020-AFF", "NOT_AFFORDABLE", "TX-08",
                   "No offered tenure keeps total debt service within DTI %s." % aff["max_total_dti"],
                   offers=aff["offers"], alternatives=["PAY_NOW"])

    return out(GREEN, "R001", "APPROVED", "R001", "Mandatory transaction checks passed; line has capacity.",
               offers=aff["offers"], affordable_tenures=aff["affordable_tenures"],
               recommended_tenure=aff["recommended_tenure"], available_vnd=available, alternatives=[])
