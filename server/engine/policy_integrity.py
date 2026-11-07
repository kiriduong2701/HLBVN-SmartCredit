"""Policy integrity: a content hash recorded on every decision, and configuration checks that keep demo
parameters inside legal caps and inside the delegated-authority hierarchy.

The hash detects a threshold edited without a version bump: server/policy/policy.lock.json pins
{policy_version: sha256}; tests fail when the content changes but the version does not (audit section 4).
Pure functions.
"""
import hashlib
import json
from typing import Any, Dict, List


def policy_hash(policy: Dict[str, Any]) -> str:
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate(policy: Dict[str, Any]) -> List[str]:
    """Return configuration errors (empty list = valid)."""
    errors: List[str] = []
    a = policy["affordability"]
    oc = policy["officer_controls"]
    od = policy["loan_servicing"]["overdue_interest"]
    if not a["min_limit_vnd"] <= a["stp_max_limit_vnd"]:
        errors.append("stp_max_limit_vnd must be at least the minimum limit")
    authority = oc["approval_authority_max_vnd"] - (1 if oc["approval_authority_bound"] == "EXCLUSIVE" else 0)
    if a["stp_max_limit_vnd"] > authority:
        errors.append("automatic-approval ceiling must not exceed the officer approval authority (audit F-05)")
    if od["overdue_principal_rate_multiplier"] > od["overdue_principal_legal_cap_multiplier"]:
        errors.append("overdue principal interest above the Circular 39 Art. 13.4 cap (150% of the in-term rate)")
    if od["late_interest_rate_pct_per_year"] > od["late_interest_legal_cap_pct_per_year"]:
        errors.append("late interest on unpaid interest above the Circular 39 Art. 13.4 cap (10%/year)")
    if "late_fee_vnd" in policy["loan_servicing"]:
        errors.append("flat late fee is not a permitted lending fee (Circular 39 Art. 14)")
    if not 0 < policy["affordability"]["conservative_income"]["percentile"] < 50:
        errors.append("conservative income percentile must be below the median")
    codes = policy["reason_codes"]["codes"]
    for c in ("R001", "R001-CO", "R010-CAPX", "R010-FRD", "R010-API", "R010-SPLIT", "R020-CAP", "HS-01", "HS-05"):
        if c not in codes:
            errors.append("reason code %s missing from reason_codes" % c)
    return errors
