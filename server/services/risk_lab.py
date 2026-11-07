"""Presenter page, section 3: the three risks of the risk round, demonstrated on the ACTUAL engine.

R1 System overload
    A burst of checkout decisions is pushed through a simulated decision gateway with a bounded worker pool, a bounded
    queue (admission control) and a decision-time budget. Queueing, timeouts and shedding are computed by a deterministic
    discrete-event model (labelled assumptions, not HLBVN figures). Every request answered in time gets a REAL engine
    decision (persona profiles run through the sandbox); every request that cannot be answered in time ends in the safe
    state the rules already define for checkout: no credit decision, Pay Later unavailable for this purchase, Pay Now
    offered (TX-03 / R010-API pattern; checkout never waits; conversion within 7 days once a line is active). The same
    burst is also run WITHOUT the safeguards, to show what they prevent.

R2 Rule & evidence errors (the rule-based equivalent of "AI uncertainty / incorrect outputs")
    The engine has no model, so it cannot be statistically uncertain. Its equivalent failure is a confidently WRONG output:
    (a) the rules are configured wrongly, or (b) the rules are fed unreliable evidence, or (c) several rules fire at once.
    Misconfigurations are applied to a COPY of the policy only: start-up validation, the version lock (content hash) and a
    shadow replay on the 10,000 dataset rows show how each one is caught and what it would have changed. The live service
    keeps the locked policy; nothing on this page can change it.

R3 Missing & anomalous data
    The sandbox customer record is altered (missing evidence, stale source, conflicting record, impossible value, incomplete
    Layer 2 data, missing field); the unchanged engine then shows how readiness checks, automatic re-acquisition, AMBER
    routing or a verified RED handle it.

Nothing here changes a threshold, formula, route, reason code or the policy file.
"""
import bisect
import copy
import heapq
import json
import os
import random
import statistics
import sys
import threading
from collections import Counter
from typing import Any, Dict, List, Optional

from ..engine import capacity as CAP
from ..engine import policy_integrity as PI
from ..engine import readiness as RD
from ..engine import routing as RT
from ..engine import terms as TM
from .base import ServiceError, _j
from .scenario_lab import PERSONAS, persona_inputs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROWS: Dict[str, Any] = {}
_ROWS_LOCK = threading.Lock()

# ------------------------------------------------------------------------------------------------ R1 overload
OVERLOAD_ASSUMPTIONS = {
    "downstream_median_ms": 350, "downstream_spread": 0.35,
    "note": "SIMULATED latency model: each decision waits on downstream calls (core banking, CIC, eKYC) with a median of "
            "350 ms and log-normal spread. The engine's own compute time is measured, not assumed. Pool size, queue "
            "length and time budget are gateway settings to be sized with HLBVN IT; none of these numbers is HLBVN data. "
            "Safeguards modelled: bounded worker pool, bounded queue (requests beyond it are answered at once), and "
            "deadline-aware dispatch (a request that cannot finish inside its budget is answered with the fallback "
            "instead of starting).",
}
OVERLOAD_PRESETS = [
    {"id": "normal", "title": {"vi": "Ngày thường", "en": "Normal day"}, "requests": 300, "window_s": 60, "workers": 16, "queue_max": 120, "budget_ms": 3000},
    {"id": "peak", "title": {"vi": "Cao điểm khuyến mãi", "en": "Promotion peak"}, "requests": 2400, "window_s": 60, "workers": 16, "queue_max": 120, "budget_ms": 3000},
    {"id": "spike", "title": {"vi": "Đột biến flash-sale", "en": "Flash-sale spike"}, "requests": 6000, "window_s": 60, "workers": 16, "queue_max": 120, "budget_ms": 3000},
]
OVERLOAD_BOUNDS = {"requests": (1, 20000), "window_s": (1, 600), "workers": (1, 256), "queue_max": (0, 5000), "budget_ms": (200, 30000)}
WAIT_AT_COUNTER_S = 30  # "would a shopper still be standing at the counter?" - illustrative, for the unprotected comparison

# ------------------------------------------------------------------------------------------------ R2 rules & evidence
RULE_CASES = [
    {"id": "M1", "kind": "CONFIG",
     "title": {"vi": "Gõ nhầm ngưỡng: điểm tín dụng 436 → 643", "en": "Typo in a threshold: credit-score cut-off 436 → 643"},
     "what": {"vi": "Một giá trị hợp lệ về hình thức nhưng sai: hệ thống sẽ tự tin chuyển hàng loạt hồ sơ tốt sang chuyên viên.",
              "en": "A well-formed but wrong value: the engine would confidently send many good files to officers."},
     "change": {"path": ["review_rules", {"rule_id": "RF-02"}, "value"], "to": 643}},
    {"id": "M2", "kind": "CONFIG",
     "title": {"vi": "Nới trần nghĩa vụ nợ 50% → 85% khi chưa được duyệt", "en": "Debt-service ceiling loosened 50% → 85% without approval"},
     "what": {"vi": "Kết quả trông hợp lý nhưng rủi ro hơn: nhiều hồ sơ được tự duyệt và hạn mức cao hơn.",
              "en": "Plausible-looking but riskier outputs: more automatic approvals and higher limits."},
     "change": {"path": ["affordability", "max_total_dti"], "to": 0.85}},
    {"id": "M3", "kind": "CONFIG",
     "title": {"vi": "Cấu hình mâu thuẫn: trần tự duyệt 500 triệu > thẩm quyền 90,1 triệu", "en": "Contradictory configuration: automatic ceiling 500M > authority 90.1M"},
     "what": {"vi": "Hai tham số mâu thuẫn nhau: máy được duyệt nhiều hơn con người có thẩm quyền.",
              "en": "Two parameters contradict each other: the machine could approve more than any officer may."},
     "change": {"path": ["affordability", "stp_max_limit_vnd"], "to": 500000000}},
    {"id": "E1", "kind": "EVIDENCE", "persona": "P3", "branch": "share",
     "title": {"vi": "Bằng chứng không đáng tin: dữ liệu ngân hàng khác không khớp danh tính", "en": "Unreliable evidence: other-bank record does not match the identity"},
     "what": {"vi": "Nếu dùng, bản ghi của người khác sẽ dẫn tới một quyết định duyệt sai.",
              "en": "If used, someone else's record would produce a wrong approval."},
     "mutation": "L2_IDENTITY_MISMATCH"},
    {"id": "E2", "kind": "EVIDENCE", "persona": "P5", "overrides": {"dti_ratio": 1.0},
     "title": {"vi": "Quy tắc xung đột: cờ rủi ro + nợ đã xác minh vượt khả năng trả", "en": "Conflicting rules: risk flags + verified debts above capacity"},
     "what": {"vi": "Nhiều quy tắc cùng kích hoạt: kết quả phải xác định, giải thích được và không phụ thuộc thứ tự ngẫu nhiên.",
              "en": "Several rules fire at once: the outcome must be deterministic, explainable and order-independent."}},
]

# ------------------------------------------------------------------------------------------------ R3 data
DATA_CASES = [
    {"id": "D1", "persona": "P3", "kind": "MISSING",
     "title": {"vi": "Thiếu bằng chứng thu nhập (không CIC, không giao dịch)", "en": "Missing income evidence (no CIC, no transactions)"},
     "change": {"vi": "Hồ sơ gốc P3 – không đổi gì", "en": "Persona P3 as is – nothing changed"}},
    {"id": "D2", "persona": "P1", "kind": "STALE", "mutation": "STALE_TX",
     "title": {"vi": "Dữ liệu cũ: giao dịch HLB đọc cách đây 200 ngày", "en": "Stale data: HLB transactions read 200 days ago"},
     "change": {"vi": "observed_at của nguồn hlb_transactions lùi 200 ngày (ngưỡng độ mới 90 ngày)", "en": "hlb_transactions observed_at moved back 200 days (freshness limit 90 days)"}},
    {"id": "D3", "persona": "P3", "kind": "INCONSISTENT", "mutation": "CONFLICT_TX",
     "title": {"vi": "Bản ghi mâu thuẫn: không có giao dịch nhưng có số liệu giao dịch", "en": "Inconsistent record: no transactions, yet transaction values present"},
     "change": {"vi": "Đưa lại thu nhập, biến động, số dư, tỷ lệ hóa đơn của bộ dữ liệu vào hồ sơ dù has_observable_tx = 0 (mẫu của 1.943 dòng)",
                "en": "Dataset inflow, CV, balance and bill ratio put back on the record although has_observable_tx = 0 (the pattern of 1,943 rows)"}},
    {"id": "D4", "persona": "P1", "kind": "INVALID", "mutation": "INVALID_SCORE",
     "title": {"vi": "Giá trị bất khả: điểm tín dụng 920 (thang 350–850)", "en": "Impossible value: credit score 920 (scale 350–850)"},
     "change": {"vi": "unified_credit_score = 920 trong bản ghi nguồn", "en": "unified_credit_score = 920 in the source record"}},
    {"id": "D5", "persona": "P3", "kind": "INCOMPLETE", "switches": ["alt_data_incomplete"],
     "title": {"vi": "Dữ liệu bổ sung không đủ kỳ: chỉ 3 tháng", "en": "Incomplete extra data: only 3 months"},
     "change": {"vi": "Nhà cung cấp Layer 2 giả lập chỉ trả 3 tháng (cần ≥ 6)", "en": "The simulated Layer 2 provider returns only 3 months (6 needed)"}},
    {"id": "D6", "persona": "P2", "kind": "MISSING_FIELD", "mutation": "MISSING_SEGMENT",
     "title": {"vi": "Thiếu trường: nhóm khách hàng trống", "en": "Missing field: customer segment empty"},
     "change": {"vi": "customer_profile = trống trong hồ sơ HLB", "en": "customer_profile empty in the HLB profile"}},
]


def _set_json(sb: Any, cid: str, col: str, fn) -> None:
    row = sb.db.one("SELECT %s FROM customers WHERE customer_id = ?" % col, (cid,))
    v = _j(row[col], {}) or {}
    fn(v)
    sb.db.run("UPDATE customers SET %s = ? WHERE customer_id = ?" % col, (json.dumps(v), cid))


def _pct(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))]


def simulate_gateway(n: int, window_s: float, workers: int, queue_max: Optional[int], budget_s: Optional[float],
                     engine_s: float, seed: int, buckets: int = 30) -> Dict[str, Any]:
    """Deterministic FIFO multi-server queue. queue_max=None / budget_s=None means 'no safeguard'."""
    rng = random.Random(seed)
    arrivals = sorted(rng.uniform(0, window_s) for _ in range(n))
    med, spread = OVERLOAD_ASSUMPTIONS["downstream_median_ms"] / 1000.0, OVERLOAD_ASSUMPTIONS["downstream_spread"]
    service = [engine_s + med * rng.lognormvariate(0, spread) for _ in range(n)]
    free = [0.0] * workers
    heapq.heapify(free)
    starts: List[float] = []  # start times of admitted requests (non-decreasing under FIFO)
    out = []  # (arrival, end, outcome, latency)
    for t, s in zip(arrivals, service):
        waiting = len(starts) - bisect.bisect_right(starts, t)
        if queue_max is not None and waiting >= queue_max:
            out.append((t, t + 0.05, "SHED", 0.05))
            continue
        start = max(t, free[0])
        wait = start - t
        if budget_s is not None and wait + med >= budget_s:    # deadline-aware: cannot finish in time -> answer now
            out.append((t, t + min(wait, budget_s), "TIMEOUT", min(wait, budget_s)))
            continue
        heapq.heappop(free)
        starts.append(start)
        if budget_s is not None and wait + s > budget_s:       # cancelled at the deadline, worker released then
            heapq.heappush(free, t + budget_s)
            out.append((t, t + budget_s, "TIMEOUT", budget_s))
        else:
            heapq.heappush(free, start + s)
            out.append((t, start + s, "DECIDED", wait + s))
    decided = [x[3] for x in out if x[2] == "DECIDED"]
    end = max([x[1] for x in out] or [0])
    span = max(window_s, end)
    step = span / buckets
    tl = []
    for i in range(buckets):
        a, b = i * step, (i + 1) * step
        tl.append({"t": round(b, 2), "arrived": sum(1 for x in out if a <= x[0] < b),
                   "decided": sum(1 for x in out if x[2] == "DECIDED" and a <= x[1] < b),
                   "fallback": sum(1 for x in out if x[2] != "DECIDED" and a <= x[1] < b),
                   "in_system": sum(1 for x in out if x[0] < b and x[1] >= b)})
    c = Counter(x[2] for x in out)
    return {"requests": n, "decided": c.get("DECIDED", 0), "timed_out": c.get("TIMEOUT", 0), "shed": c.get("SHED", 0),
            "fallback": c.get("TIMEOUT", 0) + c.get("SHED", 0),
            "latency_ms": {"p50": round(1000 * (_pct(decided, 50) or 0)), "p95": round(1000 * (_pct(decided, 95) or 0)),
                           "max": round(1000 * (max(decided) if decided else 0))},
            "all_latency_p95_ms": round(1000 * (_pct([x[3] for x in out], 95) or 0)),
            "waited_over_counter_s": sum(1 for x in out if x[3] > WAIT_AT_COUNTER_S),
            "peak_in_system": max((b["in_system"] for b in tl), default=0), "drained_after_s": round(end, 1),
            "offered_rps": round(n / window_s, 1), "capacity_rps": round(workers / (engine_s + med * 1.06), 1), "timeline": tl}


class RiskLabMixin:
    # -------------------------------------------------------------------------------------------- config
    def risk_round_config(self) -> Dict[str, Any]:
        lock = self._lock()
        return {"overload": {"presets": OVERLOAD_PRESETS, "bounds": OVERLOAD_BOUNDS, "assumptions": OVERLOAD_ASSUMPTIONS},
                "rule_cases": [{k: c[k] for k in ("id", "kind", "title", "what")} for c in RULE_CASES],
                "data_cases": [{k: c[k] for k in ("id", "kind", "title", "change", "persona")} for c in DATA_CASES],
                "qa_sample_pct": {"green": self.policy["officer_controls"]["green_qa_sample_pct"],
                                  "layer2": self.policy["officer_controls"]["layer2_qa_sample_pct"]},
                "live_policy": {"version": self.policy["policy_version"], "hash": self.policy_hash,
                                "locked_hash": lock.get("sha256"), "matches_lock": lock.get("sha256") == self.policy_hash}}

    @staticmethod
    def _lock() -> Dict[str, Any]:
        try:
            with open(os.path.join(ROOT, "server", "policy", "policy.lock.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # -------------------------------------------------------------------------------------------- R1
    def risk_overload(self, body: Dict[str, Any]) -> Dict[str, Any]:
        body = body or {}
        p: Dict[str, int] = {}
        errors = {}
        for k, (lo, hi) in OVERLOAD_BOUNDS.items():
            v = body.get(k)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                errors[k] = {"code": "OUT_OF_RANGE", "min": lo, "max": hi}
            else:
                p[k] = v
        if errors:
            raise ServiceError(422, "INVALID_OVERLOAD_INPUT", "Some load parameters are out of range.", {"fields": errors})
        # Real engine: one decision per persona (normal conditions) - compute time measured, outcomes reused.
        samples = []
        for persona in PERSONAS:
            r = self._sandbox_run(self._validate_live(persona_inputs(persona)), [], None)
            samples.append({"persona": persona["id"], "display_name": persona["display_name"], "band": r["assessment"]["band"],
                            "reason_code": r["assessment"]["reason_code"], "checkout_reason_code": r["checkout"]["reason_code"],
                            "next_action": r["next_action"], "engine_ms": r["assessment"]["engine_ms"] or 0})
        engine_ms = statistics.mean(s["engine_ms"] for s in samples)
        seed = hash((p["requests"], p["window_s"], p["workers"], p["queue_max"], p["budget_ms"])) & 0xFFFF
        protected = simulate_gateway(p["requests"], p["window_s"], p["workers"], p["queue_max"], p["budget_ms"] / 1000.0,
                                     engine_ms / 1000.0, seed)
        unprotected = simulate_gateway(p["requests"], p["window_s"], p["workers"], None, None, engine_ms / 1000.0, seed)
        mix = Counter()
        for i in range(protected["decided"]):
            mix[samples[i % len(samples)]["next_action"]] += 1
        return {"params": p, "engine": {"measured_ms_per_decision": round(engine_ms, 2), "samples": samples},
                "protected": protected, "unprotected": unprotected,
                "decided_outcomes": dict(mix),
                "fallback_state": {"credit_decisions_written": 0, "approvals_during_fallback": 0, "holds_placed": 0,
                                   "payment_options": ["PAY_NOW", "RETRY", "CONVERT_LATER"], "rule_ids": ["TX-03"],
                                   "reason_code": "R010-API"},
                "assumptions": OVERLOAD_ASSUMPTIONS}

    # -------------------------------------------------------------------------------------------- R2
    def _replay_rows(self) -> List[Dict[str, Any]]:
        with _ROWS_LOCK:
            if "rows" not in _ROWS:
                if ROOT not in sys.path:
                    sys.path.insert(0, ROOT)
                from scripts import dataset_audit as D  # stdlib xlsx reader shared with the audit scripts
                header, *body = D.read_xlsx(os.path.join(ROOT, "data", "reference", "hlbvn_dataset.xlsx"))["dataset"]
                _ROWS["rows"] = [dict(zip(header, r)) for r in body]
            return _ROWS["rows"]

    def _replay(self, policy: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = self._replay_rows()  # also puts the repository root on sys.path
        from scripts import segment_replay as SR  # same replay used for docs/test_logs/segment_replay.json
        E = {"readiness": RD, "capacity": CAP, "routing": RT, "terms": TM, "policy": policy, "v5": True, "v6": True}
        return [SR.replay_row(E, r) for r in rows]

    def risk_rule_case(self, body: Dict[str, Any]) -> Dict[str, Any]:
        case = next((c for c in RULE_CASES if c["id"] == (body or {}).get("case")), None)
        if not case:
            raise ServiceError(422, "UNKNOWN_CASE", "Unknown rule case.")
        if case["kind"] == "CONFIG":
            return self._config_case(case)
        return self._evidence_case(case)

    def _config_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        mutated = copy.deepcopy(self.policy)
        node: Any = mutated
        path = case["change"]["path"]
        for step in path[:-1]:
            node = next(x for x in node if all(x.get(k) == v for k, v in step.items())) if isinstance(step, dict) else node[step]
        before = node[path[-1]]
        node[path[-1]] = case["change"]["to"]
        lock = self._lock()
        errors = PI.validate(mutated)
        mhash = PI.policy_hash(mutated)
        startup = {"refused": False, "error": None}
        try:
            sb = self._sandbox(policy=mutated)
            sb.db.close()
        except ValueError as e:
            startup = {"refused": True, "error": str(e)}
        out = {"case": {k: case[k] for k in ("id", "kind", "title", "what")},
               "change": {"parameter": ".".join(s if isinstance(s, str) else s.get("rule_id") for s in path), "from": before,
                          "to": case["change"]["to"]},
               "controls": {
                   "startup_validation": {"errors": errors, "service_refuses_to_start": startup["refused"], "message": startup["error"]},
                   "version_lock": {"policy_version": mutated["policy_version"], "locked_hash": lock.get("sha256"), "candidate_hash": mhash,
                                    "release_refused": mhash != lock.get("sha256"),
                                    "how": "scripts/policy_lock.py refuses a changed hash under the same version; tests/test_v5_controls.py fails the build"},
                   "live_service": {"policy_hash": self.policy_hash, "still_locked": self.policy_hash == lock.get("sha256")},
               }}
        if not errors:
            if _ROWS.get("base_hash") != self.policy_hash:
                _ROWS["base"], _ROWS["base_hash"] = self._replay(self.policy), self.policy_hash
            base, cand = _ROWS["base"], self._replay(mutated)
            flips = Counter("%s→%s" % (b["band"], c["band"]) for b, c in zip(base, cand) if b["band"] != c["band"])
            higher = [(c.get("limit") or 0) - (b.get("limit") or 0) for b, c in zip(base, cand)
                      if b["band"] == "GREEN" and c["band"] == "GREEN" and (c.get("limit") or 0) > (b.get("limit") or 0)]
            seg = Counter(b["segment"] for b, c in zip(base, cand) if b["band"] != c["band"])
            off_b = sum(1 for b in base if b.get("route") in ("HUMAN_REVIEW", "FRAUD_REVIEW"))
            off_c = sum(1 for c in cand if c.get("route") in ("HUMAN_REVIEW", "FRAUD_REVIEW"))
            out["shadow_replay"] = {"rows": len(base), "decisions_changed": sum(flips.values()), "flips": dict(flips),
                                    "by_segment": dict(seg), "higher_limits": len(higher),
                                    "avg_limit_increase_vnd": int(statistics.mean(higher)) if higher else 0,
                                    "officer_referrals": {"locked": off_b, "candidate": off_c},
                                    "green": {"locked": sum(1 for b in base if b["band"] == "GREEN"),
                                              "candidate": sum(1 for c in cand if c["band"] == "GREEN")},
                                    "note": "Replay on the 10,000 dataset rows with the assumptions of scripts/segment_replay.py "
                                            "(sources verified, consent given, no Layer 2). Indicative only."}
        return out

    def _evidence_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        persona = next(p for p in PERSONAS if p["id"] == case["persona"])
        inp = self._validate_live(dict(persona_inputs(persona), **case.get("overrides", {})))
        mutate = None
        if case.get("mutation") == "L2_IDENTITY_MISMATCH":
            def mutate(sb, cid):
                _set_json(sb, cid, "alternative_data_json",
                          lambda x: x["open_banking"].update(subject_match=False, match_on=["full_name"]))
        first = self._sandbox_run(inp, [], None, mutate=mutate)
        res = {"case": {k: case[k] for k in ("id", "kind", "title", "what")}, "persona": {"id": persona["id"], "display_name": persona["display_name"]},
               "result": first}
        if case.get("branch"):
            res["branch"] = self._sandbox_run(inp, [], case["branch"], mutate=mutate)
        if case["id"] == "E2":
            res["components"] = [{"label": {"vi": "Chỉ cờ rủi ro", "en": "Risk flags only"},
                                  "result": self._sandbox_run(self._validate_live(persona_inputs(persona)), [], None)},
                                 {"label": {"vi": "Chỉ nợ vượt khả năng", "en": "Debt breach only"},
                                  "result": self._sandbox_run(self._validate_live(dict(persona_inputs(persona), dti_ratio=1.0,
                                                                                       unified_credit_score=650, on_time_bill_ratio=0.9)), [], None)}]
        return res

    # -------------------------------------------------------------------------------------------- R3
    def risk_data_case(self, body: Dict[str, Any]) -> Dict[str, Any]:
        case = next((c for c in DATA_CASES if c["id"] == (body or {}).get("case")), None)
        if not case:
            raise ServiceError(422, "UNKNOWN_CASE", "Unknown data case.")
        persona = next(p for p in PERSONAS if p["id"] == case["persona"])
        inp = self._validate_live(persona_inputs(persona))
        row = {"monthly_inflow_vnd": 10331332, "inflow_cv": 0.288, "casa_balance_vnd": 5556626, "regular_weekly_pattern": 1,
               "on_time_bill_ratio": 0.7022}  # HLB_POP_100001384 dataset values (P3)
        stale = (self.as_of.toordinal() - 200)
        from datetime import date as _date
        mutations = {
            "STALE_TX": lambda sb, cid: _set_json(sb, cid, "sources_json",
                                                   lambda s: s["hlb_transactions"].update(observed_at=_date.fromordinal(stale).isoformat())),
            "CONFLICT_TX": lambda sb, cid: _set_json(sb, cid, "applicant_json", lambda a: a.update(has_observable_tx=0, **row)),
            "INVALID_SCORE": lambda sb, cid: _set_json(sb, cid, "applicant_json", lambda a: a.update(unified_credit_score=920)),
            "MISSING_SEGMENT": lambda sb, cid: _set_json(sb, cid, "applicant_json", lambda a: a.update(customer_profile=None)),
        }
        mutate = mutations.get(case.get("mutation"))
        switches = case.get("switches", [])
        result = self._sandbox_run(inp, switches, None, mutate=mutate)
        if result["data_request"]["requested"]:
            result["branches"] = {k: self._sandbox_run(inp, switches, k, mutate=mutate) for k in ("share", "decline")}
        baseline = self._sandbox_run(inp, [], None)
        return {"case": {k: case[k] for k in ("id", "kind", "title", "change")},
                "persona": {"id": persona["id"], "display_name": persona["display_name"], "applicant_id": persona["applicant_id"]},
                "baseline": {"band": baseline["assessment"]["band"], "reason_code": baseline["assessment"]["reason_code"],
                             "approved_limit_vnd": baseline["limit"]["approved_limit_vnd"], "next_action": baseline["next_action"]},
                "result": result, "switches": switches}
