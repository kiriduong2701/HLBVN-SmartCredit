"""State machines: limit-activation/increase application (credit decision),
checkout transaction (payment), and exposure hold.

They are deliberately independent: an APPROVED credit line can sit next to a
PAYMENT_FAILED checkout, and a checkout never waits on an application.
"""
from typing import Dict, Set


class InvalidTransition(Exception):
    def __init__(self, machine: str, current: str, target: str):
        super().__init__("%s: %s -> %s is not allowed" % (machine, current, target))
        self.machine, self.current, self.target = machine, current, target


# Limit activation / limit increase applications (credit decisions; never part of a checkout).
CREDIT_TRANSITIONS: Dict[str, Set[str]] = {
    "ASSESSMENT_PENDING": {"STP_ELIGIBLE", "MANUAL_REVIEW", "POLICY_STOP", "MORE_INFORMATION_REQUIRED", "DECLINED",
                           "CANCELLED"},
    "STP_ELIGIBLE": {"APPROVED", "MANUAL_REVIEW", "OFFER_PENDING"},
    # GREEN below the requested limit: the line opens only when the customer accepts the counter-offer (audit F-04).
    "OFFER_PENDING": {"APPROVED", "CANCELLED", "MORE_INFORMATION_REQUIRED", "POLICY_STOP"},
    "MORE_INFORMATION_REQUIRED": {"ASSESSMENT_PENDING", "MANUAL_REVIEW", "POLICY_STOP", "CANCELLED"},
    # Amber human review. The officer may approve (below the configured authority), reject, or request
    # consent-based alternative data from the customer (back to MORE_INFORMATION_REQUIRED).
    "MANUAL_REVIEW": {"APPROVED", "REJECTED", "MORE_INFORMATION_REQUIRED", "POLICY_STOP", "CANCELLED"},
    # A RED policy decline can be appealed once: it goes to human review.
    "DECLINED": {"MANUAL_REVIEW"},
    "APPROVED": set(), "REJECTED": set(), "POLICY_STOP": set(), "CANCELLED": set(),
}
CREDIT_TERMINAL = {"APPROVED", "REJECTED", "POLICY_STOP", "CANCELLED"}
CREDIT_OPEN = {"ASSESSMENT_PENDING", "STP_ELIGIBLE", "MORE_INFORMATION_REQUIRED", "MANUAL_REVIEW", "OFFER_PENDING"}
# Application kinds: LIMIT_ACTIVATION, LIMIT_INCREASE, LINE_REVIEW (automatic renewal before the 90-day expiry, or a QA
# follow-up) and LINE_REINSTATEMENT (officer decision after a cured default).
APPLICATION_KINDS = {"LIMIT_ACTIVATION", "LIMIT_INCREASE", "LINE_REVIEW", "LINE_REINSTATEMENT"}

# Checkout transaction. No state waits for a credit officer.
TX_TRANSITIONS: Dict[str, Set[str]] = {
    "QR_CREATED": {"PAYMENT_METHOD_SELECTED", "TERMS_PENDING_CONFIRMATION", "CANCELLED", "EXPIRED"},
    # Pay Now chosen; awaiting authentication + payment.
    "PAYMENT_METHOD_SELECTED": {"PAYMENT_METHOD_SELECTED", "TERMS_PENDING_CONFIRMATION", "DISBURSEMENT_PROCESSING",
                                "QR_CREATED", "CANCELLED", "EXPIRED"},
    # Pay Later GREEN: exposure hold placed; customer reviews, accepts the agreement and authenticates.
    "TERMS_PENDING_CONFIRMATION": {"TERMS_PENDING_CONFIRMATION", "DISBURSEMENT_PROCESSING", "PAYMENT_METHOD_SELECTED",
                                   "QR_CREATED", "CANCELLED", "EXPIRED"},
    "DISBURSEMENT_PROCESSING": {"PAYMENT_COMPLETED", "PAYMENT_FAILED"},
    "PAYMENT_FAILED": {"DISBURSEMENT_PROCESSING", "PAYMENT_METHOD_SELECTED", "QR_CREATED", "CANCELLED", "EXPIRED"},
    "PAYMENT_COMPLETED": set(), "CANCELLED": set(), "EXPIRED": set(),
}
TX_TERMINAL = {"PAYMENT_COMPLETED", "CANCELLED", "EXPIRED"}
TX_EXPIRABLE = {s for s, t in TX_TRANSITIONS.items() if "EXPIRED" in t}
TX_ACTIVE_BEFORE_PAYMENT = set(TX_TRANSITIONS) - TX_TERMINAL - {"DISBURSEMENT_PROCESSING"}

HOLD_TRANSITIONS: Dict[str, Set[str]] = {"HELD": {"RELEASED", "BOOKED"}, "RELEASED": set(), "BOOKED": set()}
LINE_STATUSES = {"ACTIVE", "FROZEN", "SUSPENDED", "CLOSED"}


def check_credit(current: str, target: str) -> None:
    if target not in CREDIT_TRANSITIONS.get(current, set()):
        raise InvalidTransition("credit", current, target)


def check_tx(current: str, target: str) -> None:
    if target not in TX_TRANSITIONS.get(current, set()):
        raise InvalidTransition("transaction", current, target)


def check_hold(current: str, target: str) -> None:
    if target not in HOLD_TRANSITIONS.get(current, set()):
        raise InvalidTransition("exposure hold", current, target)
