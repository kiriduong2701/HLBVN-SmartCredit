"""Smart Credit application service.

Composed from focused mixins. Two journeys:
  * ActivationMixin - Limit Activation (all credit assessment, off the counter);
  * AcquisitionMixin - Layer 1 automatic data + Layer 2 consent-based alternative data (simulated adapters);
  * CheckoutMixin   - Instant Checkout (spend an approved line; never waits for a human);
  * LiveDemoMixin   - presenter-only: one hypothetical customer typed in live during the pitch (same engine, no shortcuts);
  * ScenarioLabMixin - presenter-only: the five read-only reference personas and the sandboxed Live Scenario Simulator;
  * RiskLabMixin    - presenter-only: the three risk-round demonstrations (overload, rule/evidence errors, data anomalies).
All state changes are validated against server/domain/states.py, run inside a
single SQLite transaction, and write append-only audit events.
"""
from .acquisition import AcquisitionMixin
from .activation import ActivationMixin
from .auth import AuthMixin
from .base import ACCOUNT, PAY_LATER, BaseService, ServiceError  # noqa: F401  (re-exported)
from .checkout import CheckoutMixin
from .live_demo import LiveDemoMixin
from .officer import OfficerMixin
from .risk_lab import RiskLabMixin
from .scenario_lab import ScenarioLabMixin
from .servicing import ServicingMixin


class SmartCreditService(ActivationMixin, AcquisitionMixin, CheckoutMixin, OfficerMixin, ServicingMixin, LiveDemoMixin,
                         ScenarioLabMixin, RiskLabMixin, AuthMixin,
                         BaseService):
    pass
