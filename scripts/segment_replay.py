"""Segment-level replay of the limit-activation engine and the purchase it was requested for.

Usage:
  python3 scripts/segment_replay.py                         # current engine -> docs/test_logs/segment_replay.json
  python3 scripts/segment_replay.py --compare BASELINE_ROOT # also replays the engine in BASELINE_ROOT (e.g. a git
                                                            # worktree of the audited commit) and writes a before/after table

What it does, for every one of the 10,000 dataset rows, on PRE-ASSESSMENT fields only (outcome columns are never passed):
  1. readiness -> capacity -> routing (first pass: as the customer would meet it at activation);
  2. for v5, the outcome if the customer then DECLINES the Layer 2 offer (shows when a RED can occur);
  3. the purchase the row asked for (requested_loan_vnd): financed in full instantly, split tender (v5), or not financed
     instantly - using the engine's own checkout affordability rule.
Assumptions (as in dataset_audit.py, because the dataset has no provenance metadata): every Layer 1 source is verified and
fresh, consent is given, no Layer 2 data exists, no other HLB products. Indicative engine behaviour on simulated data -
NOT evidence of credit performance.
"""
import argparse
import importlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRE = ["customer_profile", "has_formal_cic_history", "has_observable_tx", "monthly_inflow_vnd", "inflow_cv",
       "unified_credit_score", "casa_balance_vnd", "dti_ratio", "on_time_bill_ratio", "e_wallet_topups_pm",
       "regular_weekly_pattern", "rapid_drain_flag"]
AS_OF = date(2026, 10, 16)
SEGMENTS = ["Salaried, with credit history", "Salaried, no credit history", "First-time borrowers, digitally active",
            "Online merchants (MSME)", "Gig / platform workers"]


def load_engine(root):
    """Import server.engine.* from `root` in isolation (so two engine versions can be compared in one process)."""
    for k in [k for k in sys.modules if k == "server" or k.startswith("server.")]:
        del sys.modules[k]
    sys.path.insert(0, root)
    try:
        mods = {n: importlib.import_module("server.engine." + n) for n in ("readiness", "capacity", "routing", "terms")}
    finally:
        sys.path.remove(root)
    policy = json.load(open(os.path.join(root, "server", "policy", "demo_policy.json")))
    mods["policy"] = policy
    mods["v5"] = hasattr(mods["capacity"], "conservative_monthly")
    mods["v6"] = hasattr(mods["routing"], "ROUTE_RETRY")
    return mods


def read_rows():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import dataset_audit as D  # noqa: E402  (xlsx reader, stdlib only)
    sheets = D.read_xlsx(os.path.join(ROOT, "data", "reference", "hlbvn_dataset.xlsx"))
    header, *body = sheets["dataset"]
    return [dict(zip(header, r)) for r in body]


def replay_row(E, r):
    R, CAP, RT, P = E["readiness"], E["capacity"], E["routing"], E["policy"]
    applicant = {k: r[k] for k in PRE}
    sources = {"hlb_transactions": {"verified": True, "observed_at": AS_OF.isoformat(), "period_months": 6},
               "credit_information": {"verified": True, "observed_at": AS_OF.isoformat()},
               "e_wallet_activity": {"verified": True, "observed_at": AS_OF.isoformat()}}
    consent = {"granted": True, "categories": P["consent"]["required_categories"] + ["e_wallet_activity"]}
    pol = dict(P)
    pol["mandatory_fields"] = dict(P["mandatory_fields"], application=P["mandatory_fields"]["activation_application"])
    res = R.evaluate({"application_id": "REPLAY", "applicant_id": r["applicant_id"]}, applicant, sources, consent, pol, AS_OF)
    not_used = res.get("fields_not_used") or {}
    verified = [k for k, v in applicant.items() if v is not None and k not in not_used]
    inc = CAP.income_basis(applicant, sources, verified, P)
    out = {"segment": r["customer_profile"], "requested": r["requested_loan_vnd"], "readiness": res["status"]}
    if E.get("v6"):
        ob = CAP.obligations_basis(applicant, verified, inc, [], None)
        agg = P["aggregate_exposure"]["max_digital_unsecured_outstanding_vnd"]
        cap = CAP.assign_limit(inc, ob, P, agg)
        behaviour = (applicant.get("has_formal_cic_history") == 1) or ("on_time_bill_ratio" in verified)
        l2 = {"attempted": False, "income_sources": True, "behaviour_source": True, "allowed": True}
        ctx = {"capacity": cap, "delinquency": {}, "identity": {}, "layer2": l2, "behaviour_signal": behaviour}
        dec = RT.decide(res, ctx, P)
        if dec["route"] == RT.ROUTE_ALT_DATA:
            d2 = RT.decide(res, dict(ctx, layer2=dict(l2, attempted=True, outcome="DECLINED")), P)
            out["if_layer2_declined"] = {"band": d2["band"], "reason_code": d2["reason_code"], "route": d2["route"]}
        out["verified_obligation_breach"] = bool(cap.get("verified_obligation_breach"))
        out["officer_max_vnd"] = cap.get("officer_max_limit_vnd")
        out["officer_can_approve"] = bool(cap.get("computable") and cap.get("officer_max_limit_vnd"))
        out["binding"] = (cap.get("stp") or {}).get("binding_constraint")
    elif E["v5"]:
        ob = CAP.obligations_basis(applicant, verified, inc, [], None)
        agg = P["aggregate_exposure"]["max_digital_unsecured_outstanding_vnd"]
        cap = CAP.assign_limit(inc, ob, P, agg)
        ctx = {"capacity": cap, "declared_fields": [], "delinquency": {}, "identity": {}, "layer2_attempted": False,
               "layer2_income_source_available": True, "alternative_data_used": False}
        dec = RT.decide(res, ctx, P)
        if dec["route"] == RT.ROUTE_ALT_DATA:
            d2 = RT.decide(res, dict(ctx, layer2_attempted=True, layer2_outcome="DECLINED"), P)
            out["if_layer2_declined"] = {"band": d2["band"], "reason_code": d2["reason_code"], "route": d2["route"]}
        out["verified_obligation_breach"] = bool(cap.get("verified_obligation_breach"))
        out["officer_max_vnd"] = cap.get("officer_max_limit_vnd")
        out["officer_can_approve"] = bool(cap.get("computable") and cap.get("officer_max_limit_vnd"))
    else:
        ob = CAP.obligations_basis(applicant, verified, inc)
        cap = CAP.assign_limit(inc, ob, P)
        dec = RT.decide(res, {"capacity": cap, "declared_fields": [], "delinquency": {}, "layer2_attempted": False,
                              "alternative_data_used": False}, P)
        out["officer_can_approve"] = bool(cap.get("computable") and cap.get("meets_minimum"))
    out.update(band=dec["band"], reason_code=dec["reason_code"], limit=dec.get("approved_limit_vnd"),
               dti=r["dti_ratio"], cv=r["inflow_cv"], route=dec["route"])
    # purchase level
    out["purchase"] = "NOT_INSTANT"
    if dec["band"] == "GREEN" and out["limit"]:
        lim, amt = out["limit"], r["requested_loan_vnd"]
        if E["v5"]:
            income = dict(inc, affordability_income_vnd=cap["stp"]["income_vnd"])
            ok_full = amt <= lim and bool(CAP.affordable_tenures_fast(amt, income, ob, 0, P))
            if ok_full:
                out["purchase"] = "FULL"
            else:
                pl = CAP.max_affordable_principal(income, ob, 0, AS_OF, P, min(lim, amt - 1))
                if pl >= P["case_ranges"]["requested_loan_vnd"]["min"]:
                    out["purchase"], out["split_pay_later_vnd"] = "SPLIT", pl
        else:
            aff = CAP.affordability(amt, AS_OF, inc, ob, 0, P) if amt <= lim else {"affordable_tenures": []}
            out["purchase"] = "FULL" if amt <= lim and aff["affordable_tenures"] else "NOT_INSTANT"
    return out


def _pct(vals, q):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(q / 100 * (len(vals) - 1))))]


def summarise(results, rows):
    by = defaultdict(list)
    for x in results:
        by[x["segment"]].append(x)
    manual = defaultdict(list)
    for r in rows:
        manual[r["customer_profile"]].append(r["manual_approval_status"])

    def block(xs, seg=None):
        n = len(xs)
        bands = Counter(x["band"] for x in xs)
        pur = Counter(x["purchase"] for x in xs)
        reasons = Counter(x["reason_code"] for x in xs if x["band"] == "AMBER")
        red_if_declined = sum(1 for x in xs if (x.get("if_layer2_declined") or {}).get("band") == "RED")
        b = {"n": n, "green": bands.get("GREEN", 0), "amber": bands.get("AMBER", 0), "red": bands.get("RED", 0),
             "green_pct": round(100 * bands.get("GREEN", 0) / n, 1), "amber_pct": round(100 * bands.get("AMBER", 0) / n, 1),
             "red_pct": round(100 * bands.get("RED", 0) / n, 1),
             "auto_decided_pct": round(100 * (bands.get("GREEN", 0) + bands.get("RED", 0)) / n, 1),
             "purchase_full_pct": round(100 * pur.get("FULL", 0) / n, 1),
             "purchase_split_pct": round(100 * pur.get("SPLIT", 0) / n, 1),
             "purchase_instant_any_pct": round(100 * (pur.get("FULL", 0) + pur.get("SPLIT", 0)) / n, 1),
             "green_but_purchase_not_full_pct_of_green": round(100 * sum(1 for x in xs if x["band"] == "GREEN"
                                                                         and x["purchase"] != "FULL") / max(1, bands.get("GREEN", 0)), 1),
             "red_if_layer2_declined": red_if_declined, "red_if_layer2_declined_pct": round(100 * red_if_declined / n, 1),
             "amber_reasons": dict(reasons.most_common()),
             "officer_cases_unapprovable": sum(1 for x in xs if x["band"] == "AMBER" and x["reason_code"] in ("R010-FLAG", "R010-THIN", "R010-ALT",
                                                   "R010-CAPX") and not x.get("officer_can_approve")),
             "flag_cases": sum(1 for x in xs if x["reason_code"] == "R010-FLAG"),
             "flag_cases_unapprovable": sum(1 for x in xs if x["reason_code"] == "R010-FLAG" and not x.get("officer_can_approve")),
             "reds_without_verified_obligation_breach": sum(1 for x in xs if x["band"] == "RED" and not x.get("verified_obligation_breach")
                                                           and x["reason_code"] == "R020-CAP"),
             # first-pass routes: who acts next
             "officer_referral_pct": round(100 * sum(1 for x in xs if x.get("route") in ("HUMAN_REVIEW", "FRAUD_REVIEW")) / n, 1),
             "layer2_request_pct": round(100 * sum(1 for x in xs if x.get("route") == "ALTERNATIVE_DATA") / n, 1),
             "customer_question_pct": round(100 * sum(1 for x in xs if x.get("route") == "MORE_INFORMATION") / n, 1),
             # if every Layer 2 request were declined / found nothing (worst case for automation)
             "officer_if_layer2_declined_pct": round(100 * sum(1 for x in xs if x.get("route") in ("HUMAN_REVIEW", "FRAUD_REVIEW")
                                                               or (x.get("if_layer2_declined") or {}).get("route") in
                                                               ("HUMAN_REVIEW", "FRAUD_REVIEW")) / n, 1),
             "green_at_or_above_50m": sum(1 for x in xs if x["band"] == "GREEN" and (x.get("limit") or 0) > 50000000),
             "green_limit_median_vnd": _pct([x["limit"] for x in xs if x["band"] == "GREEN" and x.get("limit")], 50),
             "green_limit_p90_vnd": _pct([x["limit"] for x in xs if x["band"] == "GREEN" and x.get("limit")], 90),
             "green_at_product_max": sum(1 for x in xs if x["band"] == "GREEN" and (x.get("limit") or 0) >= 90100000),
             "green_binding_constraint": dict(Counter(x.get("binding") for x in xs if x["band"] == "GREEN" and x.get("binding")))}
        if seg:
            b["manual_approval_baseline_pct"] = round(100 * sum(manual[seg]) / len(manual[seg]), 1)
        return b
    return {"all": block(results), "by_segment": {s: block(by[s], s) for s in SEGMENTS if by[s]}}


def run(root):
    E = load_engine(root)
    rows = read_rows()
    res = [replay_row(E, r) for r in rows]
    return {"engine_root": root, "policy_version": E["policy"]["policy_version"], "v5": E["v5"], "v6": E.get("v6"), "rows": len(rows),
            "summary": summarise(res, rows),
            "red_if_layer2_declined_reasons": dict(Counter((x.get("if_layer2_declined") or {}).get("reason_code") for x in res
                                                           if (x.get("if_layer2_declined") or {}).get("band") == "RED"))}


def table(before, after):
    lines = ["| Segment | n | Manual approval (dataset) | GREEN before → after | Officer referral (1st pass) before → after | "
             "Layer 2 request before → after | Auto-decline RED before → after | Purchase financed instantly before → after "
             "(full + split) | RED if Layer 2 declined (after) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS + ["all"]:
        b = before["summary"]["by_segment"].get(seg) if seg != "all" else before["summary"]["all"]
        a = after["summary"]["by_segment"].get(seg) if seg != "all" else after["summary"]["all"]
        lines.append("| %s | %d | %s | %.1f%% → %.1f%% | %.1f%% → %.1f%% | %.1f%% → %.1f%% | %.1f%% → %.1f%% | %.1f%% → %.1f%% "
                     "(%.1f%% + %.1f%%) | %.1f%% |" % (
            "**All**" if seg == "all" else seg, a["n"], ("%.1f%%" % a["manual_approval_baseline_pct"]) if seg != "all" else "—",
            b["green_pct"], a["green_pct"], b["officer_referral_pct"], a["officer_referral_pct"], b["layer2_request_pct"],
            a["layer2_request_pct"], b["red_pct"], a["red_pct"], b["purchase_instant_any_pct"], a["purchase_instant_any_pct"],
            a["purchase_full_pct"], a["purchase_split_pct"], a["red_if_layer2_declined_pct"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", help="root of the baseline (audited) engine to replay as 'before'")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "test_logs", "segment_replay.json"))
    ap.add_argument("--sensitivity", action="store_true", help="also replay the current engine at P10 and P50 income")
    a = ap.parse_args()
    after = run(ROOT)
    sens = {}
    if a.sensitivity:
        for pct in (10, 50):
            E = load_engine(ROOT)
            E["policy"]["affordability"]["conservative_income"]["percentile"] = pct
            rows = read_rows()
            sm = summarise([replay_row(E, r) for r in rows], rows)
            sens["P%d" % pct] = {"all": {k: sm["all"][k] for k in ("green_pct", "red_pct", "purchase_full_pct", "purchase_split_pct",
                                                                    "purchase_instant_any_pct")},
                                 "gig_green_pct": sm["by_segment"]["Gig / platform workers"]["green_pct"],
                                 "msme_green_pct": sm["by_segment"]["Online merchants (MSME)"]["green_pct"]}
        print("sensitivity:", json.dumps(sens))
    out = {"note": __doc__.strip().splitlines()[0], "assumptions": "Every Layer 1 source verified and fresh, consent given, "
           "no Layer 2 data, no other HLB products. Indicative engine behaviour on simulated data, not credit performance.",
           "after": after, "sensitivity": sens}
    if a.compare:
        before = run(os.path.abspath(a.compare))
        out["before"] = before
        out["table_md"] = table(before, after)
        print(out["table_md"])
    with open(a.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(json.dumps(after["summary"]["all"], indent=1))
    print("written", a.out)


if __name__ == "__main__":
    main()
