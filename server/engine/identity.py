"""Layer 0: identity and application-fraud checks (case brief: verify identity and that each application is
genuine, in real time). Runs before any credit rule, at limit activation and at every Pay Later checkout.

A hit is an UNREVIEWED signal: it routes to an officer identity & fraud review (AMBER R010-FRD) and, at the counter,
leaves only Pay Now. It never declines automatically. STOP HS-05 happens only after an officer confirms fraud.
Thresholds are DEMO_ASSUMPTIONs (policy identity_fraud). Pure function; signals are gathered by the service.
"""
from typing import Any, Dict, List


def evaluate(signals: Dict[str, Any], stage: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    """signals: ekyc_status, cccd_match, duplicate_identity_count, device_anomaly, applications_30d,
    pay_later_purchases_today, fraud_confirmed, layer2_identity_mismatch (AV-01 failed on a Layer 2 source). stage: ACTIVATION | CHECKOUT."""
    cfg = policy["identity_fraud"]
    checks: List[Dict[str, Any]] = []
    hits: List[Dict[str, Any]] = []

    def chk(cid: str, ok: Any, detail: str) -> None:
        spec = next(c for c in cfg["checks"] if c["id"] == cid)
        if stage not in spec["applies_to"]:
            return
        res = "PASS" if ok is True else ("NOT_EVALUATED" if ok is None else "HIT")
        checks.append({"id": cid, "label": spec["label"], "result": res, "detail": detail})
        if res == "HIT":
            hits.append({"rule_id": cid, "message": "%s: %s" % (spec["label"], detail)})

    s = signals or {}
    ek = s.get("ekyc_status")
    chk("L0-01", None if ek is None else (ek == "VERIFIED" and s.get("cccd_match") is True),
        "eKYC %s, CCCD match %s" % (ek, s.get("cccd_match")))
    dup = s.get("duplicate_identity_count")
    chk("L0-02", None if dup is None else dup == 0, "%s other customer record(s) with the same national ID" % dup)
    dev = s.get("device_anomaly")
    chk("L0-03", None if dev is None else not dev, "device/session anomaly" if dev else "bound device")
    apps = s.get("applications_30d")
    chk("L0-04", None if apps is None else apps <= cfg["max_applications_30d"],
        "%s application(s) in 30 days (max %s)" % (apps, cfg["max_applications_30d"]))
    pl = s.get("pay_later_purchases_today")
    chk("L0-05", None if pl is None else pl < cfg["max_pay_later_purchases_per_day"],
        "%s Pay Later purchase(s) today (max %s)" % (pl, cfg["max_pay_later_purchases_per_day"]))
    l2 = s.get("layer2_identity_mismatch")
    chk("L0-06", None if l2 is None else not l2,
        "Layer 2 provider record does not match the customer (%s)" % l2 if l2 else "Layer 2 records match the customer")
    return {"stage": stage, "checks": checks, "hits": hits, "clear": not hits,
            "fraud_confirmed": bool(s.get("fraud_confirmed")), "cleared_by_officer": bool(s.get("cleared_by_officer"))}
