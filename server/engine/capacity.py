"""Repayment-capacity limit assignment and per-transaction affordability (v6).

Answers, from VERIFIED inputs only:
  1. income_basis      - recognised monthly income per verified stream: Layer 1 (HLB data / dataset variables) and each
                         validated Layer 2 API stream, recognised by ECONOMIC TYPE with the same factor in every layer, then
                         SUMMED after de-duplication (v6; v5 used only the single highest source);
  2. obligations_basis - existing monthly debt service as an ABSOLUTE VND amount with its provenance;
  3. assign_limit      - the automatic (STP) capacity on the sum of each stream's STRESSED (25th-percentile) month, and the
                         officer maximum on the sum of AVERAGE verified income;
  4. affordability     - which tenures keep total debt service within the ceiling once a purchase is added,
                         tested on the LARGEST instalment of each schedule.

All parameters live in demo_policy.json and are DEMO_ASSUMPTIONs pending HLBVN credit-policy approval. Nothing
here is a validated risk model or an HLBVN lending policy. Pure functions; no I/O.
"""
import math
from statistics import NormalDist
from typing import Any, Dict, List, Optional

from . import terms as T


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ------------------------------------------------------------------ income
def conservative_monthly(avg: Optional[float], cv: Any, series: Optional[List[int]], policy: Dict[str, Any]) -> Dict[str, Any]:
    """Estimated 25th-percentile month. Empirical when a monthly series is available, otherwise the normal
    approximation mean x (1 - z x CV). Never negative, never above the average."""
    cfg = policy["affordability"]["conservative_income"]
    if series and len(series) >= cfg["min_series_months"] and all(_num(x) for x in series):
        s = sorted(series)
        pos = (len(s) - 1) * cfg["percentile"] / 100.0
        lo, hi = math.floor(pos), math.ceil(pos)
        val = s[lo] + (s[hi] - s[lo]) * (pos - lo)
        return {"value_vnd": int(min(val, avg if avg else val)), "method": "EMPIRICAL_P%d" % cfg["percentile"],
                "series_months": len(series)}
    if not _num(avg) or avg <= 0:
        return {"value_vnd": None, "method": "NONE"}
    cv_used, method = (float(cv), "NORMAL_APPROX_P%d" % cfg["percentile"]) if _num(cv) and cv >= 0 \
        else (cfg["cv_when_missing"], "NORMAL_APPROX_P%d_CV_ASSUMED" % cfg["percentile"])
    z = -NormalDist().inv_cdf(cfg["percentile"] / 100.0)  # 25th percentile -> z = 0.6745
    factor = max(0.0, 1.0 - z * cv_used)
    return {"value_vnd": int(avg * factor), "method": method, "cv": round(cv_used, 4), "factor": round(factor, 4)}


def recognition_factor(category: str, policy: Dict[str, Any]) -> float:
    """Income recognition by economic type - identical in Layer 1 and Layer 2 (v6)."""
    rt = policy["income_classification"]["recognition_by_type"].get(category)
    return float(rt["factor"]) if rt else 0.0


def classify_inflow(src: Dict[str, Any], gross: float, policy: Dict[str, Any]) -> Dict[str, Any]:
    """Only genuine income categories count, each at its type factor. HLB core supplies a classified breakdown (monthly
    averages); without one the inflow is used as defined in the data dictionary and labelled UNCLASSIFIED_INFLOW."""
    cfg = policy["income_classification"]
    br = (src or {}).get("inflow_breakdown")
    if isinstance(br, dict) and br:
        types = cfg["recognition_by_type"]
        recognised = {k: int(v * recognition_factor(k, policy)) for k, v in br.items() if k in types and _num(v)}
        excluded = {k: int(v) for k, v in br.items() if k in cfg["excluded_categories"] and _num(v)}
        unknown = {k: int(v) for k, v in br.items() if k not in types and k not in excluded and _num(v)}
        income = sum(recognised.values())
        return {"classified": True, "income_monthly_vnd": income, "recognised": recognised, "excluded": excluded,
                "excluded_total_vnd": sum(excluded.values()) + sum(unknown.values()), "unknown_categories": unknown,
                "factors": {k: recognition_factor(k, policy) for k in recognised},
                "share_of_inflow": round(income / gross, 4) if gross else None}
    return {"classified": False, "income_monthly_vnd": int(gross), "note": "No classified breakdown supplied: monthly inflow used "
            "as defined (average monthly cash inflow), factor 1.0 - UNCLASSIFIED_INFLOW (known limitation)."}


def income_basis(applicant: Dict[str, Any], sources: Dict[str, Any], verified_fields: List[str],
                 policy: Dict[str, Any]) -> Dict[str, Any]:
    """Recognised income per verified stream and the two totals the decision uses:
      stp_monthly_vnd     - sum of each STP-usable stream's stressed (25th-percentile) month;
      officer_monthly_vnd - sum of each valid stream's average month.
    Layer 2 streams come from validated, consented, de-duplicated API results (acquisition.py). Declared values and
    behaviour-only sources (utility, e-wallet) are never income."""
    sources = sources or {}
    tx_src = sources.get("hlb_transactions") or {}
    alt_src = sources.get("alternative_data") or {}
    inflow = applicant.get("monthly_inflow_vnd")
    l1_deduction = int(alt_src.get("layer1_deduction_vnd") or 0) if alt_src.get("verified") else 0
    l1 = None
    if "monthly_inflow_vnd" in verified_fields and tx_src.get("verified") and _num(inflow) and inflow > 0:
        cls = classify_inflow(tx_src, inflow, policy)
        avg = max(0, cls["income_monthly_vnd"] - (0 if cls["classified"] else l1_deduction))
        cv = applicant.get("inflow_cv") if "inflow_cv" in verified_fields else None
        cons = conservative_monthly(avg, cv, tx_src.get("monthly_income_series_vnd"), policy)
        seg_types = policy["income_classification"].get("unclassified_multiplier_type_by_segment") or {}
        mtype = None if cls["classified"] else seg_types.get(applicant.get("customer_profile") or "UNKNOWN", seg_types.get("UNKNOWN"))
        mfactor = 1.0 if cls["classified"] or not mtype else recognition_factor(mtype, policy)
        if avg > 0:
            l1 = {"layer": 1, "basis": "VERIFIED_TRANSACTIONS", "inflow_classified": cls["classified"],
                  "multiplier_type": mtype or "CLASSIFIED", "multiplier_factor": mfactor,
                  "gross_inflow_vnd": int(inflow), "average_monthly_vnd": int(avg), "conservative_monthly_vnd": cons["value_vnd"],
                  "conservative_method": cons, "classification": cls, "verified": True,
                  "layer1_deduction_vnd": l1_deduction if not cls["classified"] else 0}
    streams: List[Dict[str, Any]] = []
    if alt_src.get("verified"):
        for st in alt_src.get("streams") or []:
            avg = int(st.get("average_recognised_vnd") or 0)
            if avg <= 0 or st.get("duplicate_of"):
                continue
            cons = conservative_monthly(avg, st.get("cv"), st.get("series_recognised_vnd"), policy)
            streams.append(dict(st, average_monthly_vnd=avg, conservative_monthly_vnd=cons["value_vnd"], conservative_method=cons))
    l2 = None
    if streams:
        l2 = {"layer": 2, "basis": "ALTERNATIVE_DATA", "streams": streams,
              "average_monthly_vnd": sum(s["average_monthly_vnd"] for s in streams),
              "conservative_monthly_vnd": sum(s["conservative_monthly_vnd"] or 0 for s in streams if s.get("stp_usable")),
              "stp_usable": all(s.get("stp_usable") for s in streams), "source_ids": [s["source"] for s in streams],
              "verified": True}
    stp_monthly = (l1["conservative_monthly_vnd"] or 0 if l1 else 0) + (l2["conservative_monthly_vnd"] if l2 else 0)
    officer_monthly = (l1["average_monthly_vnd"] if l1 else 0) + (l2["average_monthly_vnd"] if l2 else 0)
    # Income-multiple exposure cap on RECOGNISED income (unclassified Layer 1 inflow x the segment's type factor).
    mf = l1["multiplier_factor"] if l1 else 1.0
    stp_mult = int((l1["conservative_monthly_vnd"] or 0) * mf if l1 else 0) + (l2["conservative_monthly_vnd"] if l2 else 0)
    officer_mult = int(l1["average_monthly_vnd"] * mf if l1 else 0) + (l2["average_monthly_vnd"] if l2 else 0)
    out = {"layer1": l1, "layer2": l2, "verified": bool(l1 or l2),
           "basis": "LAYER1+LAYER2" if l1 and l2 else ("VERIFIED_TRANSACTIONS" if l1 else ("ALTERNATIVE_DATA" if l2 else "NONE")),
           "layer": 1 if l1 and not l2 else (2 if l2 and not l1 else (12 if l1 else None)),
           "stp_monthly_vnd": stp_monthly or None, "officer_monthly_vnd": officer_monthly or None,
           "stp_multiplier_base_vnd": stp_mult or None, "officer_multiplier_base_vnd": officer_mult or None,
           "layer2_in_stp": bool(l2 and l2["conservative_monthly_vnd"]),
           "aggregation": "SUM_OF_DEDUPLICATED_STREAMS",
           # compatibility fields (officer view / checkout): what the automatic decision uses
           "gross_monthly_vnd": officer_monthly or None, "recognised_monthly_vnd": stp_monthly or None,
           "source_ids": (l2 or {}).get("source_ids", [])}
    if not out["verified"]:
        out["note"] = ("No verified income: transaction inflow unavailable, unverified or untrusted, and no validated alternative income.")
    return out


# ------------------------------------------------------------------ obligations
def obligations_basis(applicant: Dict[str, Any], verified_fields: List[str], income: Dict[str, Any],
                      hlb_products: Optional[List[Dict[str, Any]]] = None, cic_status: Optional[str] = None) -> Dict[str, Any]:
    """Absolute monthly debt service with provenance. cic_status: OK | NO_RECORD | UNAVAILABLE (None = infer from
    has_formal_cic_history). The dataset defines dti_ratio against the gross monthly inflow, so it converts to VND only
    against that inflow - never against Layer 2 income."""
    cic = applicant.get("has_formal_cic_history")
    status = cic_status or ("OK" if cic == 1 else "NO_RECORD" if cic == 0 else "UNKNOWN")
    dti = applicant.get("dti_ratio")
    inflow = applicant.get("monthly_inflow_vnd")
    dti_ok = "dti_ratio" in verified_fields and _num(dti) and dti >= 0
    inflow_ok = "monthly_inflow_vnd" in verified_fields and _num(inflow) and inflow > 0
    components: List[Dict[str, Any]] = []
    notes: List[str] = []
    if status in ("UNAVAILABLE", "UNKNOWN"):
        return {"basis": "UNKNOWN", "monthly_vnd": None, "known": False, "components": [],
                "reason": "CIC_UNAVAILABLE" if status == "UNAVAILABLE" else "CIC_STATUS_UNKNOWN"}
    if status == "OK":
        if not (dti_ok and inflow_ok):
            return {"basis": "UNKNOWN", "monthly_vnd": None, "known": False, "components": [],
                    "reason": "CIC_OBLIGATIONS_NOT_CONVERTIBLE",
                    "note": "Bureau ratio present but the inflow it is measured on is not verified, so VND obligations are unknown."}
        external = {"lineage": "CIC_REPORTED", "monthly_vnd": int(round(dti * inflow)), "dti_ratio": dti,
                    "measured_on_inflow_vnd": int(inflow)}
    elif applicant.get("has_observable_tx") == 1 and dti_ok and inflow_ok:
        external = {"lineage": "TX_OBSERVED", "monthly_vnd": int(round(dti * inflow)), "dti_ratio": dti,
                    "measured_on_inflow_vnd": int(inflow),
                    "note": "No CIC record; debt payments observed in verified HLB account outflows."}
    else:
        external = {"lineage": "CIC_NO_RECORD", "monthly_vnd": 0,
                    "note": "CIC holds no record and no transactions are observable: verified zero external obligations."}
        if _num(dti):
            notes.append("dti_ratio %s supplied but cannot be sourced (no bureau record, no observable income base): not used." % dti)
    components.append(dict(external, component="EXTERNAL"))
    for p in hlb_products or []:
        m = p.get("monthly_instalment_vnd")
        if not _num(m) or m <= 0:
            continue
        dedup = bool(p.get("included_in_dti")) and external["lineage"] in ("CIC_REPORTED", "TX_OBSERVED")
        components.append({"component": "HLB_INTERNAL", "product": p.get("product"), "monthly_vnd": int(m),
                           "added": not dedup, "lineage": "HLB_LOAN_SYSTEM",
                           "note": "Already in the bureau/observed figure (deduplicated)" if dedup else "Added"})
    total = sum(c["monthly_vnd"] for c in components if c.get("component") == "EXTERNAL" or c.get("added"))
    out = {"basis": external["lineage"], "monthly_vnd": int(total), "known": True, "components": components,
           "external_monthly_vnd": external["monthly_vnd"],
           "hlb_products_included": [c["product"] for c in components if c.get("component") == "HLB_INTERNAL"],
           "notes": notes}
    if _num(dti) and external["lineage"] != "CIC_NO_RECORD":
        out["dti_ratio"] = dti
    return out


def refreshed_obligations(snapshot: Dict[str, Any], hlb_products: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Checkout refresh (F-07): external obligations from the line's last assessment + current HLB internal instalments
    (products already counted at assessment keep their dedup decision; new products are always added)."""
    if not snapshot or not snapshot.get("known"):
        return {"known": False, "monthly_vnd": None, "basis": "UNKNOWN"}
    seen = {c["product"]: c for c in snapshot.get("components", []) if c.get("component") == "HLB_INTERNAL"}
    total = snapshot.get("external_monthly_vnd", 0) or 0
    comps = [c for c in snapshot.get("components", []) if c.get("component") == "EXTERNAL"]
    for p in hlb_products or []:
        m = p.get("monthly_instalment_vnd")
        if not _num(m) or m <= 0:
            continue
        prior = seen.get(p.get("product"))
        add = prior["added"] if prior else True
        comps.append({"component": "HLB_INTERNAL", "product": p.get("product"), "monthly_vnd": int(m), "added": add,
                      "note": "unchanged since assessment" if prior else "new since assessment - added"})
        if add:
            total += int(m)
    return {"known": True, "monthly_vnd": int(total), "basis": snapshot.get("basis"), "components": comps,
            "refreshed": True}


# ------------------------------------------------------------------ limit
def _sizing(income_vnd: int, obligations_vnd: int, cap_vnd: Optional[int], policy: Dict[str, Any],
            multiplier_base_vnd: Optional[int] = None) -> Dict[str, Any]:
    cfg, rt = policy["affordability"], policy["repayment_terms"]
    budget = int(cfg["max_total_dti"] * income_vnd)
    headroom = budget - obligations_vnd
    ref = cfg["reference_tenure_months"]
    factor = ref / (1 + rt["annual_interest_rate_pct"] / 100.0 * ref / 12.0)
    by_capacity = int(max(0, headroom) * factor)
    mult_base = income_vnd if multiplier_base_vnd is None else multiplier_base_vnd
    by_multiplier = int(cfg["income_multiplier_cap"] * mult_base)
    limits = {"CAPACITY": by_capacity, "INCOME_MULTIPLIER": by_multiplier, "PRODUCT_MAXIMUM": cfg["product_max_limit_vnd"]}
    if cap_vnd is not None:
        limits["AGGREGATE_DIGITAL_LIMIT"] = max(0, cap_vnd)
    binding = min(limits, key=lambda k: (limits[k], list(limits).index(k)))
    raw = limits[binding]
    return {"income_vnd": income_vnd, "monthly_debt_service_budget_vnd": budget, "monthly_headroom_vnd": headroom,
            "limit_by_capacity_vnd": by_capacity, "limit_by_income_multiplier_vnd": by_multiplier, "multiplier_base_vnd": mult_base,
            "raw_limit_vnd": raw,
            "binding_constraint": binding, "limit_vnd": round_limit(raw, policy), "meets_minimum": raw >= cfg["min_limit_vnd"]}


def round_limit(raw: int, policy: Dict[str, Any], step: Optional[int] = None) -> int:
    """Round down to the step, but a raw capacity at or above the minimum never rounds below the minimum (F-06), and a raw
    capacity at the product maximum is exactly the product maximum (VND 90.1M is reachable)."""
    cfg = policy["affordability"]
    step = step or cfg["limit_rounding_vnd"]
    if raw >= cfg["product_max_limit_vnd"]:
        return cfg["product_max_limit_vnd"]  # the product maximum is an exact cap, not a computed amount (v6)
    if raw < cfg["min_limit_vnd"]:
        return raw // step * step
    return max(cfg["min_limit_vnd"], raw // step * step)


def min_instalment_vnd(policy: Dict[str, Any]) -> int:
    """Monthly instalment of a minimum-size limit at the reference tenure (used for the Layer 2 plausibility rule)."""
    cfg, rt = policy["affordability"], policy["repayment_terms"]
    ref = cfg["reference_tenure_months"]
    return int(cfg["min_limit_vnd"] * (1 + rt["annual_interest_rate_pct"] / 100.0 * ref / 12.0) / ref)


def assign_limit(income: Dict[str, Any], obligations: Dict[str, Any], policy: Dict[str, Any],
                 aggregate_headroom_vnd: Optional[int] = None) -> Dict[str, Any]:
    """Automatic (STP) capacity on the summed stressed income of STP-usable streams, and the officer maximum on the summed
    average verified income. Returns the computation so officers can see every input."""
    cfg, rt = policy["affordability"], policy["repayment_terms"]
    out: Dict[str, Any] = {"parameters": {
        "max_total_dti": cfg["max_total_dti"], "reference_tenure_months": cfg["reference_tenure_months"],
        "income_multiplier_cap": cfg["income_multiplier_cap"], "product_max_limit_vnd": cfg["product_max_limit_vnd"],
        "min_limit_vnd": cfg["min_limit_vnd"], "rounding_vnd": cfg["limit_rounding_vnd"],
        "stp_max_limit_vnd": cfg["stp_max_limit_vnd"], "conservative_income": cfg["conservative_income"]["method"],
        "annual_interest_rate_pct": rt["annual_interest_rate_pct"], "aggregate_headroom_vnd": aggregate_headroom_vnd,
        "source": cfg["source"], "approval_status": cfg["approval_status"]},
        "income": income, "obligations": obligations,
        "formula": "min( max(0, DTI_max x income - obligations) x T/(1 + r x T/12), multiplier x recognised income, product max, "
                   "aggregate digital-lending headroom ); automatic: income = sum of each STP-usable stream's 25th-percentile "
                   "month (Layer 1 + validated Layer 2, de-duplicated); officer maximum: income = sum of average verified income. "
                   "Minimum tested on raw capacity; rounded down to the step."}
    if not income.get("verified") or not obligations.get("known"):
        out.update({"computable": False, "limit_vnd": None, "officer_max_limit_vnd": None,
                    "reason": "INCOME_NOT_VERIFIED" if not income.get("verified") else "OBLIGATIONS_UNKNOWN"})
        return out
    ob = obligations["monthly_vnd"]
    stp_inc = income.get("stp_monthly_vnd") or 0
    stp = _sizing(stp_inc, ob, aggregate_headroom_vnd, policy, income.get("stp_multiplier_base_vnd")) if stp_inc > 0 else None
    officer = _sizing(income.get("officer_monthly_vnd") or 0, ob, aggregate_headroom_vnd, policy,
                      income.get("officer_multiplier_base_vnd"))
    officer_basis = {"VERIFIED_TRANSACTIONS": "LAYER1_AVERAGE", "ALTERNATIVE_DATA": "LAYER2_AVERAGE",
                     "LAYER1+LAYER2": "LAYER1+LAYER2_AVERAGE"}.get(income.get("basis"), "NONE")
    # Structural over-indebtedness: verified obligations alone leave less than the minimum even on average income.
    breach = not officer["meets_minimum"]
    need = (ob + min_instalment_vnd(policy)) / cfg["max_total_dti"]
    out.update({"computable": True, "stp": stp, "officer": officer, "officer_basis": officer_basis,
                "stp_available": bool(stp), "layer2_in_stp": bool(income.get("layer2_in_stp")),
                "officer_max_limit_vnd": officer["limit_vnd"] if officer["meets_minimum"] else None,
                "verified_obligation_breach": breach,
                "income_needed_for_minimum_vnd": int(need),
                "uplift_ratio_needed": round(need / officer["income_vnd"] - 1, 4) if officer["income_vnd"] else None})
    shown = stp or officer
    out.update({k: shown[k] for k in ("monthly_debt_service_budget_vnd", "monthly_headroom_vnd", "limit_by_capacity_vnd",
                                      "limit_by_income_multiplier_vnd", "binding_constraint", "raw_limit_vnd")})
    out["limit_vnd"] = shown["limit_vnd"]
    out["meets_minimum"] = shown["meets_minimum"]
    out["basis_for_limit"] = ("STP_STRESSED_" + ("LAYER1+LAYER2" if out["layer2_in_stp"] and income.get("layer1") else
                                                  "LAYER2" if out["layer2_in_stp"] else "LAYER1")) if stp else "OFFICER_" + officer_basis
    return out


def max_affordable_principal(income: Dict[str, Any], obligations: Dict[str, Any], smart_credit_monthly_vnd: int,
                             start, policy: Dict[str, Any], upper_vnd: int, step: int = 100000) -> int:
    """Largest principal (multiple of `step`, <= upper, >= product minimum) for which at least one offered tenure is
    affordable. Affordability is monotonic in principal, so a binary search over the step grid is exact."""
    minimum = policy["case_ranges"]["requested_loan_vnd"]["min"]

    def ok(p: int) -> bool:
        return bool(affordable_tenures_fast(p, income, obligations, smart_credit_monthly_vnd, policy))

    lo_k, hi_k = -(-minimum // step), upper_vnd // step  # candidate multiples of step within [minimum, upper]
    if hi_k < lo_k or not ok(lo_k * step):
        return 0
    while lo_k < hi_k:
        mid = (lo_k + hi_k + 1) // 2
        if ok(mid * step):
            lo_k = mid
        else:
            hi_k = mid - 1
    return lo_k * step


def _affordability_income(income: Dict[str, Any]) -> Optional[int]:
    return (income or {}).get("affordability_income_vnd") or (income or {}).get("recognised_monthly_vnd")


def affordable_tenures_fast(amount_vnd: int, income: Dict[str, Any], obligations: Dict[str, Any],
                            smart_credit_monthly_vnd: int, policy: Dict[str, Any]) -> List[int]:
    """Same rule as affordability() (largest instalment, same rounding) without building disclosure offers."""
    inc_vnd = _affordability_income(income)
    if not (income or {}).get("verified") or not (obligations or {}).get("known") or not inc_vnd:
        return []
    cap = policy["affordability"]["max_total_dti"] * inc_vnd - obligations["monthly_vnd"] - smart_credit_monthly_vnd
    return [t for t in policy["repayment_terms"]["offered_tenures"]
            if T.instalment_extremes(amount_vnd, t, policy)["largest_vnd"] <= cap + 1e-9]


def affordability(amount_vnd: int, start, income: Dict[str, Any], obligations: Dict[str, Any],
                  smart_credit_monthly_vnd: int, policy: Dict[str, Any]) -> Dict[str, Any]:
    """Per-tenure check on the LARGEST instalment: (obligations + Smart Credit instalments + largest new instalment) /
    conservative income <= DTI max. Uses the income the line was approved on (stressed Layer 1, or the officer basis)."""
    cfg = policy["affordability"]
    inc_vnd = _affordability_income(income)
    if not (income or {}).get("verified") or not (obligations or {}).get("known") or not inc_vnd:
        return {"computable": False, "offers": [], "affordable_tenures": [],
                "reason": "INCOME_NOT_VERIFIED" if not (income or {}).get("verified") else "OBLIGATIONS_UNKNOWN"}
    offers = []
    for o in T.tenure_offers(amount_vnd, start, policy):
        largest = max(o["monthly_instalment_vnd"], o["last_instalment_vnd"])
        total_ds = obligations["monthly_vnd"] + smart_credit_monthly_vnd + largest
        dti_after = total_ds / inc_vnd
        offers.append(dict(o, tested_instalment_vnd=largest, dti_after=round(dti_after, 4), affordable=dti_after <= cfg["max_total_dti"]))
    ok = [o["tenure_months"] for o in offers if o["affordable"]]
    return {"computable": True, "max_total_dti": cfg["max_total_dti"], "recognised_income_vnd": inc_vnd,
            "existing_obligations_vnd": obligations["monthly_vnd"], "smart_credit_monthly_vnd": smart_credit_monthly_vnd,
            "tested_on": "LARGEST_INSTALMENT", "offers": offers, "affordable_tenures": ok,
            "recommended_tenure": ok[0] if ok else None}
