"""Limit-activation routing (v6): Layer 0 + readiness + capacity + Layer 2 state -> GREEN / AMBER / RED / STOP.

Operating model (one, coherent): the engine appraises every case and DECIDES automatically wherever the approved policy
rules give a verified answer (GREEN approve, RED decline); it ACQUIRES missing evidence automatically (Layer 2 request
with per-source consent, automatic retries after outages); a credit officer decides only cases that need human judgement
(fraud signal, invalid source data, risk flag, validated data outside the automatic conditions, stressed-capacity
shortfall after Layer 2, appeals, reinstatement). Officers never collect data.

Rule order (first match wins; every other triggered rule is recorded in `secondary_codes`):
  0a STOP  HS-01        legally required data-processing consent missing or withdrawn
  0b STOP  HS-05        fraud confirmed by an officer   | AMBER R010-FRD  unreviewed Layer 0 signal (incl. L0-06)
  1  RED   R020-DEF / R020-ARR  Smart Credit default / arrears (verified; appeal allowed)
  G  AMBER R010-RECHECK case already with an officer: only 0a, 0b and 1 may override
  2  AMBER R010-API     Layer 1 (or Layer 2) source outage -> automatic retry, no officer (route SYSTEM_RETRY)
  3  AMBER R010-VER     invalid source data (impossible values) -> officer (data integrity)
  4  AMBER R010-EVD / R010-INCOME  no evidence path / no verified income -> automated Layer 2 request;
     RED   R020-EVD     ... after Layer 2 gave no usable income (declined / not found / invalid): capacity unverifiable
  5  AMBER R010-OBL     obligations not convertible to VND (bureau ratio without a trusted income base) -> officer
  6  AMBER R010-CAPX    verified obligations exceed the ceiling even on average income and Layer 2 is plausible
     RED   R020-CAP     same breach, Layer 2 exhausted or implausible (extra income needed > max_uplift_ratio)
  7  AMBER R010-FRD/ALT validated Layer 2 data outside the automatic conditions (unstable, declining) -> officer
  8  AMBER R010-THIN    no repayment-behaviour signal: Layer 2 utility request, then officer
  9  AMBER R010-FLAG    profile risk flag (rapid drain, internal delinquency, score / bill-payment percentile) -> officer
 10  AMBER R010-CAPX    stressed capacity below the minimum -> automated Layer 2 request, then officer (never RED)
 11  GREEN R001 / R001-L2 / R001-CO  automatic approval up to min(capacity, product maximum); counter-offer below the
                        request; LIMIT_INCREASE: approve / counter-offer / Layer 2 / RED R020-INC; LINE_REVIEW: <= current
Pure function; no I/O.
"""
from typing import Any, Dict, List, Optional

from . import readiness as R

ROUTE_STP = "STP"
ROUTE_DECLINE = "POLICY_DECLINE"
ROUTE_REVIEW = "HUMAN_REVIEW"
ROUTE_ALT_DATA = "ALTERNATIVE_DATA"
ROUTE_RETRY = "SYSTEM_RETRY"
ROUTE_POLICY_STOP = "POLICY_STOP"
ROUTE_FRAUD = "FRAUD_REVIEW"

GREEN, AMBER, RED, STOP = "GREEN", "AMBER", "RED", "STOP"


def _vnd(n: Any) -> str:
    return "{:,}".format(int(n or 0))


def _base(band: str, route: str, path: List[str], reason_code: str, rule_ids: List[str], reasons: List[Dict[str, Any]],
          customer_reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"band": band, "route": route, "state_path": path, "final_credit_state": path[-1], "reason_code": reason_code,
           "rule_ids": sorted(set(rule_ids)), "reasons": reasons, "customer_reason": customer_reason,
           "approval_conditions": [], "requested_items": [], "decided_by": "DECISION_ENGINE", "secondary_codes": []}
    out.update(extra)
    return out


def _alt_request(code: str, missing: List[str], why: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    """Automated Layer 2 request: the customer sees only the sources and their purposes (never which Layer 1 data was short)."""
    return _base(AMBER, ROUTE_ALT_DATA, ["MORE_INFORMATION_REQUIRED"], code, [w["rule_id"] for w in why], why,
                 "MORE_INFORMATION", requested_items=[{"type": "alternative_data", "missing": missing}], **extra)


def _retry(why: List[Dict[str, Any]], what: str) -> Dict[str, Any]:
    return _base(AMBER, ROUTE_RETRY, ["ASSESSMENT_PENDING"], "R010-API", [w["rule_id"] for w in why] + ["R-API-02"],
                 why + [{"rule_id": "R-API-02", "message": "Automatic retry: the request stays in assessment and the rules re-run "
                         "when %s answers. No officer is involved; the customer can cancel or pay now." % what}],
                 "CHECKING", retry={"what": what})


def _review(code: str, rule_ids: List[str], reasons: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return _base(AMBER, ROUTE_REVIEW, ["MANUAL_REVIEW"], code, rule_ids, reasons, "VERIFYING_DATA", **extra)


def triggered_codes(readiness: Dict[str, Any], context: Dict[str, Any], policy: Dict[str, Any]) -> List[str]:
    """Every rule that would fire for this input, independent of precedence (recorded as secondary codes)."""
    cap = context.get("capacity") or {}
    ident = context.get("identity") or {}
    dq = context.get("delinquency") or {}
    out: List[str] = []
    if readiness.get("missing_consent"):
        out.append("HS-01")
    if ident.get("fraud_confirmed"):
        out.append("HS-05")
    if ident.get("hits"):
        out.append("R010-FRD")
    if dq.get("default"):
        out.append("R020-DEF")
    if dq.get("arrears"):
        out.append("R020-ARR")
    if readiness.get("unavailable_sources") or context.get("source_errors"):
        out.append("R010-API")
    if readiness.get("status") == R.INVALID_DATA:
        out.append("R010-VER")
    if readiness.get("status") == R.INSUFFICIENT_EVIDENCE:
        out.append("R010-EVD")
    if readiness.get("review_reasons"):
        out.append("R010-FLAG")
    if cap.get("computable"):
        if cap.get("verified_obligation_breach"):
            out.append("R020-CAP")
        elif not (cap.get("stp") or {}).get("meets_minimum", False):
            out.append("R010-CAPX")
    elif cap.get("reason") == "INCOME_NOT_VERIFIED":
        out.append("R010-INCOME")
    elif cap:
        out.append("R010-OBL")
    if context.get("behaviour_signal") is False:
        out.append("R010-THIN")
    return out


def decide(readiness: Dict[str, Any], context: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    """context keys:
      capacity (capacity.assign_limit), identity (identity.evaluate), delinquency {arrears, default, days_overdue},
      source_errors [{source, code}] (Layer 1 outage), requested_limit_vnd (already capped at the product maximum),
      kind (LIMIT_ACTIVATION | LIMIT_INCREASE | LINE_REVIEW | LINE_REINSTATEMENT), current_limit_vnd, in_human_review,
      layer2: {attempted, outcome (DATA_ADDED | DECLINED | NO_USABLE_DATA | UNAVAILABLE), used, unstable_sources [ids],
               income_sources (an income source exists for this segment), behaviour_source (a behaviour source exists),
               allowed (False for renewals: never prompt)},
      behaviour_signal (CIC history, verified bill ratio or validated utility history present)."""
    dec = _decide(readiness, context, policy)
    dec["secondary_codes"] = [c for c in triggered_codes(readiness, context, policy) if c != dec["reason_code"]]
    return dec


def _decide(readiness: Dict[str, Any], context: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    cap = context.get("capacity") or {}
    ident = context.get("identity") or {}
    delinquency = context.get("delinquency") or {}
    source_errors = context.get("source_errors") or []
    l2 = context.get("layer2") or {}
    l2_open = l2.get("allowed", True) and not l2.get("attempted")
    l2_down = l2.get("outcome") == "UNAVAILABLE"
    cfg = policy["affordability"]
    plan = policy["data_acquisition"]["layer2_plan"]
    kind = context.get("kind") or "LIMIT_ACTIVATION"

    # 0a. Legal data-processing basis missing -> nothing can be assessed.
    if readiness.get("missing_consent"):
        return _base(STOP, ROUTE_POLICY_STOP, ["POLICY_STOP"], "HS-01", ["HS-01"],
                     [{"rule_id": "HS-01", "message": "Required data-processing consent missing: " + ", ".join(readiness["missing_consent"])}],
                     "CONSENT_MISSING")
    # 0b. Layer 0 identity & fraud.
    if ident.get("fraud_confirmed"):
        return _base(STOP, ROUTE_POLICY_STOP, ["POLICY_STOP"], "HS-05", ["HS-05"],
                     [{"rule_id": "HS-05", "message": "Fraud confirmed by a credit officer on this customer record."}],
                     "IDENTITY_REVIEW")
    if ident.get("hits") and not ident.get("cleared_by_officer"):
        return _base(AMBER, ROUTE_FRAUD, ["MANUAL_REVIEW"], "R010-FRD", [h["rule_id"] for h in ident["hits"]],
                     ident["hits"] + [{"rule_id": "R-FRD-00", "message": "Unreviewed identity / fraud signal: officer review "
                                       "before any credit decision. Never an automatic decline."}], "IDENTITY_REVIEW")
    # 1. Verified credit hard stops -> RED (appeal allowed).
    if delinquency.get("default"):
        return _base(RED, ROUTE_DECLINE, ["DECLINED"], "R020-DEF", ["HS-04"],
                     [{"rule_id": "HS-04", "message": "A Smart Credit loan is in (simulated) default. New credit is blocked "
                       "until it is resolved."}], "DEFAULT", appeal_allowed=True)
    if delinquency.get("arrears"):
        return _base(RED, ROUTE_DECLINE, ["DECLINED"], "R020-ARR", ["HS-03"],
                     [{"rule_id": "HS-03", "message": "Instalment overdue by %s day(s). New credit is blocked while "
                       "arrears exist." % delinquency.get("days_overdue")}], "ARREARS", appeal_allowed=True)

    # G. Review guard: once a credit officer has the case, the engine never takes it back.
    if context.get("in_human_review"):
        msg = "Re-evaluated while in officer review: the decision stays with the officer."
        if cap.get("computable"):
            msg += " Automatic capacity %s VND; officer maximum %s VND." % (
                _vnd((cap.get("stp") or {}).get("limit_vnd")), _vnd(cap.get("officer_max_limit_vnd")))
        return _review("R010-RECHECK", ["R-REV-00"], [{"rule_id": "R-REV-00", "message": msg}])

    # 2. Source outage -> automatic retry (never an officer, never a decision on missing data).
    if readiness.get("unavailable_sources") or source_errors:
        why = [{"rule_id": "R-API-01", "message": "Layer 1 source unavailable after one retry: %s." % ", ".join(
            sorted({e["source"] for e in source_errors} | set(readiness.get("unavailable_sources") or [])))}]
        return _retry(why, "the Layer 1 source")

    status = readiness["status"]
    # 3. Invalid source data (impossible values) -> data-integrity review by an officer.
    if status == R.INVALID_DATA:
        reasons = [{"rule_id": f["rule_id"], "message": f["message"]} for f in readiness["validation_errors"]]
        return _review("R010-VER", [r["rule_id"] for r in reasons], reasons)

    def layer2_or(code: str, missing: List[str], why: List[Dict[str, Any]], after):
        """Automated Layer 2 if a relevant source exists and none was tried; retry on outage; else `after()`."""
        has_source = l2.get("behaviour_source") if missing == ["REPAYMENT_BEHAVIOUR"] else l2.get("income_sources", True)
        if l2_open and has_source:
            return _alt_request(code, missing, why)
        if l2_down:
            return _retry(why, "the Layer 2 provider")
        return after()

    behaviour_missing = context.get("behaviour_signal") is False
    # 4. No evidence path / no verified income -> Layer 2; afterwards a verified RED (capacity cannot be verified).
    income_missing = not cap.get("computable") and cap.get("reason") == "INCOME_NOT_VERIFIED"
    if status == R.INSUFFICIENT_EVIDENCE or income_missing:
        why = [{"rule_id": f["rule_id"], "message": f["message"]} for f in readiness.get("evidence_gaps", [])]
        if income_missing:
            why.append({"rule_id": "R-CAP-01", "message": "No verified income from Layer 1 data."})
        missing = ["VERIFIED_INCOME"] + (["REPAYMENT_BEHAVIOUR"] if behaviour_missing and l2.get("behaviour_source") else [])
        return layer2_or("R010-EVD" if status == R.INSUFFICIENT_EVIDENCE else "R010-INCOME", missing, why, lambda: _base(
            RED, ROUTE_DECLINE, ["DECLINED"], "R020-EVD", [w["rule_id"] for w in why] + ["R-EVD-02"], why + [
                {"rule_id": "R-EVD-02", "message": "No usable Layer 2 income (%s). Repayment capacity cannot be verified "
                 "(Circular 39/2016 Art. 7), so the request is declined - not because consent was refused. The customer can "
                 "appeal or re-apply sharing data." % (l2.get("outcome") or "no source for this profile")}],
            "CAPACITY_UNVERIFIED", appeal_allowed=True))
    # 5. Obligations not convertible to VND -> officer.
    if not cap.get("computable"):
        return _review("R010-OBL", ["R-CAP-02"], [{"rule_id": "R-CAP-02", "message": "Existing debt obligations could not be "
                       "confirmed in VND from a verified source (%s)." % cap["obligations"].get("reason")}])

    stp = cap.get("stp") or {}
    officer_max = cap.get("officer_max_limit_vnd")
    # 6. Structural over-indebtedness on verified data.
    if cap.get("verified_obligation_breach"):
        uplift = cap.get("uplift_ratio_needed")
        why = [{"rule_id": "R-CAP-03", "message": "Verified obligations %s VND/month leave headroom %s VND even on average "
                "verified income %s VND (DTI ceiling %s): capacity %s VND is below the minimum %s VND. Extra income needed: %s%%."
                % (_vnd(cap["obligations"]["monthly_vnd"]), _vnd(cap["officer"]["monthly_headroom_vnd"]),
                   _vnd(cap["officer"]["income_vnd"]), cfg["max_total_dti"], _vnd(cap["officer"]["raw_limit_vnd"]),
                   _vnd(cfg["min_limit_vnd"]), round(100 * (uplift or 0), 1))}]
        red = lambda: _base(RED, ROUTE_DECLINE, ["DECLINED"], "R020-CAP", ["R-CAP-03", "R-CAP-04"], why + [
            {"rule_id": "R-CAP-04", "message": "Decided on verified obligations: %s. An appeal can add validated income." % (
                "the extra income needed exceeds %s x verified income, so no data request was made" % plan["max_uplift_ratio"]
                if uplift is not None and uplift > plan["max_uplift_ratio"] else "Layer 2 did not add enough income")}],
            "CAPACITY", appeal_allowed=True)
        if uplift is not None and uplift > plan["max_uplift_ratio"]:
            return red()
        return layer2_or("R010-CAPX", ["ADDITIONAL_INCOME"], why, red)

    # 7. Validated Layer 2 data outside the automatic conditions -> officer judgement.
    if l2.get("unstable_sources"):
        return _review("R010-ALT", ["R-ALT-01"], [{"rule_id": "R-ALT-01", "message": "Validated Layer 2 income from %s fails the "
                       "automatic stability/trend conditions (AV-05/AV-06). Officer maximum %s VND (basis %s)."
                       % (", ".join(l2["unstable_sources"]), _vnd(officer_max), cap.get("officer_basis"))}],
                       indicative_limit_vnd=officer_max)
    # 8. No repayment-behaviour signal (no CIC history, no bill-payment data).
    if behaviour_missing:
        why = [{"rule_id": "R-THIN-01", "message": "No repayment-behaviour signal: no CIC history, no verified bill-payment ratio "
                "and no validated utility/telecom history."}]
        return layer2_or("R010-THIN", ["REPAYMENT_BEHAVIOUR"], why,
                         lambda: _review("R010-THIN", ["R-THIN-01"], why, indicative_limit_vnd=officer_max))
    # 9. Profile risk flags -> officer judgement.
    if readiness["review_reasons"]:
        why = [{"rule_id": f["rule_id"], "message": f["message"]} for f in readiness["review_reasons"]]
        return _review("R010-FLAG", [w["rule_id"] for w in why], why, indicative_limit_vnd=officer_max)
    # 10. Stressed capacity below the minimum (volatile or partial income) -> Layer 2, then officer. Never RED.
    if not stp.get("meets_minimum"):
        why = [{"rule_id": "R-CAP-05", "message": "Automatic capacity on the 25th-percentile month (%s VND) is %s VND, below "
                "the minimum %s VND; on average income an officer could approve up to %s VND."
                % (_vnd(stp.get("income_vnd")), _vnd(stp.get("raw_limit_vnd")), _vnd(cfg["min_limit_vnd"]), _vnd(officer_max))}]
        return layer2_or("R010-CAPX", ["ADDITIONAL_INCOME"], why,
                         lambda: _review("R010-CAPX", ["R-CAP-05"], why, indicative_limit_vnd=officer_max))

    # 11. Automatic decision inside the policy.
    ceiling = cfg["stp_max_limit_vnd"]
    capacity_limit = stp["limit_vnd"]
    limit = min(capacity_limit, ceiling)
    requested = context.get("requested_limit_vnd")
    l2_used = bool(cap.get("layer2_in_stp"))
    conditions = [
        {"id": "AC-01", "label": "Layer 0 identity & fraud checks clear", "passed": True},
        {"id": "AC-02", "label": "No arrears or default on Smart Credit", "passed": True},
        {"id": "AC-03", "label": "Used data valid, verified, fresh and de-duplicated", "passed": True,
         "detail": "%d value(s) excluded by data-quality checks" % len(readiness.get("data_quality_events") or [])},
        {"id": "AC-04", "label": "Income and obligations verified; capacity computed", "passed": True},
        {"id": "AC-05", "label": "Stressed capacity at or above the product minimum (raw capacity)", "passed": True,
         "detail": "{:,} VND".format(stp["raw_limit_vnd"])},
        {"id": "AC-06", "label": "No profile risk flag requiring judgement", "passed": True},
        {"id": "AC-07", "label": "Repayment-behaviour signal available", "passed": True},
        {"id": "AC-08", "label": "Every Layer 2 income source used passes AV-01..AV-07", "passed": True,
         "detail": ", ".join(((cap.get("income") or {}).get("layer2") or {}).get("source_ids", [])) or "no Layer 2 data used"},
    ]
    code = "R001-L2" if l2_used else "R001"
    reasons = [{"rule_id": code, "message": "Automatic approval inside the policy rules; capacity-based limit assigned%s."
                % (" on Layer 1 + validated Layer 2 income" if l2_used else "")}]
    extra = {"approval_conditions": conditions, "decided_by": "STP_ENGINE", "layer2_used": l2_used}
    if kind == "LINE_REVIEW":
        current = context.get("current_limit_vnd") or 0
        limit = min(limit, current)  # a renewal never raises the limit automatically
        return _base(GREEN, ROUTE_STP, ["STP_ELIGIBLE", "APPROVED"], code, [c["id"] for c in conditions], reasons, "APPROVED",
                     approved_limit_vnd=limit, **extra)
    if kind == "LIMIT_INCREASE":
        current = context.get("current_limit_vnd") or 0
        if requested and limit >= requested:
            return _base(GREEN, ROUTE_STP, ["STP_ELIGIBLE", "APPROVED"], code, [c["id"] for c in conditions], reasons + [
                {"rule_id": "R-INCR-02", "message": "Increase to %s VND supported by verified capacity." % _vnd(requested)}],
                "APPROVED", approved_limit_vnd=requested, **extra)
        why = [{"rule_id": "R-INCR-03", "message": "Verified capacity %s VND is below the requested %s VND (current limit %s VND)."
                % (_vnd(limit), _vnd(requested), _vnd(current))}]
        if limit > current and limit >= cfg["min_limit_vnd"]:
            return _base(GREEN, ROUTE_STP, ["STP_ELIGIBLE", "OFFER_PENDING"], "R001-CO", [c["id"] for c in conditions] + ["R001-CO"],
                         reasons + why, "COUNTER_OFFER", approved_limit_vnd=limit, counter_offer=True, **extra)
        return layer2_or("R010-CAPX", ["ADDITIONAL_INCOME"], why, lambda: _base(
            RED, ROUTE_DECLINE, ["DECLINED"], "R020-INC", ["R-INCR-03"], why + [{"rule_id": "R-INCR-04", "message":
                "Increase declined on verified data; the current line is unchanged. Appeal allowed."}],
            "INCREASE_NOT_SUPPORTED", appeal_allowed=True))
    counter = bool(requested and limit < requested)
    if requested:
        limit = min(limit, requested)
    if counter:
        reasons.append({"rule_id": "R001-CO", "message": "Requested %s VND; capacity supports %s VND. Counter-offer: the line opens "
                        "only when the customer accepts (asking for more starts an automated Layer 2 request)."
                        % (_vnd(requested), _vnd(limit))})
        return _base(GREEN, ROUTE_STP, ["STP_ELIGIBLE", "OFFER_PENDING"], "R001-CO", [c["id"] for c in conditions] + ["R001-CO"],
                     reasons, "COUNTER_OFFER", approved_limit_vnd=limit, counter_offer=True, **extra)
    return _base(GREEN, ROUTE_STP, ["STP_ELIGIBLE", "APPROVED"], code, [c["id"] for c in conditions], reasons, "APPROVED",
                 approved_limit_vnd=limit, **extra)
