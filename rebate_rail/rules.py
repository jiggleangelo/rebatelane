import calendar
import datetime
from .models import AccountState, Proposal

def abuse_reasons(rule, aggregate):
    reasons = []
    if aggregate.top_fingerprint_share > rule.max_top_fingerprint_share:
        reasons.append("repetitive_loop")
    if aggregate.peak_calls_per_min > rule.max_peak_calls_per_min:
        reasons.append("burst")
    if aggregate.error_rate > rule.max_error_rate:
        reasons.append("retry_storm")
    if aggregate.prior_period_calls > 0 and aggregate.calls > rule.max_prior_period_multiplier * aggregate.prior_period_calls:
        reasons.append("spike")
    if aggregate.prior_period_calls == 0 and aggregate.calls >= rule.new_account_calls_multiplier * rule.min_calls:
        reasons.append("new_and_big")
    return reasons

def expiry(period, days):
    year, month = map(int, period.split("-"))
    end = datetime.datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59,
                            tzinfo=datetime.timezone.utc)
    return int((end + datetime.timedelta(days=days)).timestamp())

def evaluate(rule, aggregates, accounts):
    proposals = []
    for aggregate in aggregates:
        account = accounts[aggregate.account_id]
        reason = None
        if account.state is AccountState.FROZEN:
            reason = "frozen"
        elif aggregate.account_age_days < rule.min_account_age_days:
            reason = "too_new"
        elif aggregate.calls < rule.min_calls:
            reason = "below_threshold"
        amount = min(aggregate.spend_cents * rule.rebate_bps // 10000, rule.cap_cents)
        if not reason and amount == 0:
            reason = "zero_amount"
        proposals.append(Proposal(aggregate.account_id, aggregate.period, amount,
                                  expiry(aggregate.period, rule.expiry_days),
                                  [] if reason else abuse_reasons(rule, aggregate), reason))
    return proposals
