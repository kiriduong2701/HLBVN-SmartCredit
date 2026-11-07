"""Authoritative repayment calculation.

EVERY amount shown to a customer or officer (checkout offers, confirmation
screen, loan schedule, conversion offer, officer view) is produced by
``build_terms`` / ``tenure_offers``. The frontend never computes money.

Pricing model (configured in demo_policy.json -> repayment_terms; all values are
DEMO_ASSUMPTIONs, not HLBVN product terms):
  * interest is FLAT on the original principal: principal x rate x tenure/12;
  * each instalment is split into a principal part and an interest part so the
    credit line can be measured on outstanding PRINCIPAL (not on interest/fees);
  * the equivalent reducing-balance rate (monthly IRR x 12) and the effective
    annual rate ((1+IRR)^12 - 1) are disclosed next to the flat rate.
Pure functions only.
"""
import calendar
from datetime import date
from typing import Any, Dict, List


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _irr_monthly(principal: int, payments: List[int]) -> float:
    """Monthly rate r such that principal = sum(p_i / (1+r)^i). Bisection; 0 when no interest."""
    if sum(payments) <= principal:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        pv = sum(p / (1 + mid) ** (i + 1) for i, p in enumerate(payments))
        if pv > principal:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _split(total: int, n: int, rounding: int) -> List[int]:
    """Equal parts rounded down to `rounding`; the remainder goes on the last part."""
    base = (total // n) // rounding * rounding if rounding > 1 else total // n
    parts = [base] * (n - 1)
    parts.append(total - base * (n - 1))
    return parts


def build_terms(principal_vnd: int, tenure_months: int, start: date, policy: Dict[str, Any]) -> Dict[str, Any]:
    cfg = policy["repayment_terms"]
    if tenure_months not in cfg["offered_tenures"]:
        raise ValueError("Tenure %s is not offered" % tenure_months)
    if not isinstance(principal_vnd, int) or principal_vnd <= 0:
        raise ValueError("Principal must be a positive whole VND amount")
    rate = cfg["annual_interest_rate_pct"]
    fee = int(cfg.get("upfront_fee_vnd") or 0)
    rounding = int(cfg.get("rounding_vnd") or 1)
    total_interest = int(round(principal_vnd * rate / 100.0 * tenure_months / 12.0))
    total = principal_vnd + total_interest + fee
    amounts = _split(total, tenure_months, rounding)
    principal_parts = _split(principal_vnd, tenure_months, 1)
    instalments = []
    for i in range(tenure_months):
        instalments.append({
            "number": i + 1,
            "due_date": _add_months(start, cfg["first_due_after_months"] + i).isoformat(),
            "amount_vnd": amounts[i],
            "principal_vnd": principal_parts[i],
            "interest_fee_vnd": amounts[i] - principal_parts[i],
        })
    r = _irr_monthly(principal_vnd, amounts)
    od = policy["loan_servicing"]["overdue_interest"]
    return {
        "label": cfg["label"],
        "source": cfg["source"],
        "principal_vnd": principal_vnd,
        "tenure_months": tenure_months,
        "annual_interest_rate_pct": rate,
        "rate_basis": cfg.get("rate_basis", "FLAT_ON_ORIGINAL_PRINCIPAL"),
        "total_interest_vnd": total_interest,
        "upfront_fee_vnd": fee,
        "total_cost_of_credit_vnd": total_interest + fee,
        "total_repayable_vnd": total,
        "monthly_instalment_vnd": amounts[0],
        "last_instalment_vnd": amounts[-1],
        "equivalent_reducing_rate_pct": round(r * 12 * 100, 1),
        "effective_annual_rate_pct": round(((1 + r) ** 12 - 1) * 100, 1),
        # Disclosed in the agreement before signing: no flat late fee (Circular 39 Art. 14); overdue interest within the
        # Art. 13.4 caps instead.
        "overdue_principal_rate_pct": round(rate * od["overdue_principal_rate_multiplier"], 2),
        "late_interest_rate_pct": od["late_interest_rate_pct_per_year"],
        "late_fee_vnd": 0,
        "instalments": instalments,
        "first_due_date": instalments[0]["due_date"],
        "last_due_date": instalments[-1]["due_date"],
        "calculated_by": "server.engine.terms.build_terms",
    }


def instalment_extremes(principal_vnd: int, tenure_months: int, policy: Dict[str, Any]) -> Dict[str, int]:
    """First and last instalment of the schedule build_terms would produce (same rounding), without the schedule or the
    IRR disclosure. Used for affordability searches and the dataset replay; customer-facing offers use build_terms."""
    cfg = policy["repayment_terms"]
    total = principal_vnd + int(round(principal_vnd * cfg["annual_interest_rate_pct"] / 100.0 * tenure_months / 12.0)) \
        + int(cfg.get("upfront_fee_vnd") or 0)
    amounts = _split(total, tenure_months, int(cfg.get("rounding_vnd") or 1))
    return {"first_vnd": amounts[0], "last_vnd": amounts[-1], "largest_vnd": max(amounts)}


def tenure_offers(principal_vnd: int, start: date, policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One comparable summary per offered tenure (no schedule), for side-by-side disclosure."""
    out = []
    for t in policy["repayment_terms"]["offered_tenures"]:
        full = build_terms(principal_vnd, t, start, policy)
        out.append({k: full[k] for k in ("tenure_months", "monthly_instalment_vnd", "last_instalment_vnd",
                                         "total_interest_vnd", "total_cost_of_credit_vnd", "total_repayable_vnd",
                                         "annual_interest_rate_pct", "equivalent_reducing_rate_pct",
                                         "effective_annual_rate_pct", "first_due_date", "last_due_date")})
    return out
