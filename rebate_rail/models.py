from dataclasses import dataclass, field
from enum import Enum
class AccountState(Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
class IntentState(Enum):
    PROPOSED = "PROPOSED"
    HELD_ABUSE = "HELD_ABUSE"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    POSTED = "POSTED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"
    VOIDED = "VOIDED"
@dataclass(frozen=True)
class Actor:
    id: str
    roles: frozenset[str]
@dataclass
class Vendor:
    id: str
    name: str
    billing_approver_role: str
    exception_owner_role: str
    exception_owner_contact: str
@dataclass
class Account:
    id: str
    billing_customer_id: str
    label: str
    state: AccountState = AccountState.ACTIVE
    frozen_reason: str | None = None
    frozen_by_intent: str | None = None
@dataclass
class UsageAggregate:
    account_id: str
    period: str
    calls: int
    spend_cents: int
    top_fingerprint_share: float
    peak_calls_per_min: int
    error_rate: float
    prior_period_calls: int
    account_age_days: int
@dataclass
class EarnRule:
    min_calls: int
    rebate_bps: int
    cap_cents: int
    expiry_days: int
    min_account_age_days: int
    max_top_fingerprint_share: float = .50
    max_peak_calls_per_min: int = 5000
    max_error_rate: float = .30
    max_prior_period_multiplier: float = 5
    new_account_calls_multiplier: float = 3
@dataclass
class Proposal:
    account_id: str
    period: str
    amount_cents: int
    expires_at: int
    abuse_reasons: list[str] = field(default_factory=list)
    skip_reason: str | None = None
@dataclass
class CreditIntent:
    id: str
    batch_id: str
    account_id: str
    period: str
    amount_cents: int
    expires_at: int
    state: IntentState
    abuse_reasons: list[str] = field(default_factory=list)
    grant_id: str | None = None
    attempts: int = 0
    rechecks: int = 0
    frozen_skip_recorded: bool = False
@dataclass
class Batch:
    id: str
    period: str
    intent_ids: list[str]
    status: str = "DRAFT"
    approved_by_role: str | None = None
    fee_cents: int = 0
    skips: list[tuple[str, str]] = field(default_factory=list)
