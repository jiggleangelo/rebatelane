from dataclasses import dataclass, field
from .models import IntentState

@dataclass
class Report:
    matched: list = field(default_factory=list)
    amount_mismatch: list = field(default_factory=list)
    missing_on_billing: list = field(default_factory=list)
    orphan_on_billing: list = field(default_factory=list)
    uncertain: list = field(default_factory=list)


def reconcile(intents, connector, accounts):
    report = Report()
    grants = []
    for account in accounts.values():
        grants.extend(connector.list_grants(account.billing_customer_id))
    by_ref = {g.metadata.get("rr_ref"): g for g in grants}
    for intent in intents.values():
        grant = by_ref.get(intent.id)
        if intent.state is IntentState.UNCERTAIN:
            report.uncertain.append(intent.id)
        elif intent.state is IntentState.POSTED and not grant:
            report.missing_on_billing.append(intent.id)
        elif intent.state is IntentState.POSTED and grant.amount_cents != intent.amount_cents:
            report.amount_mismatch.append(intent.id)
        elif intent.state is IntentState.POSTED:
            report.matched.append(intent.id)
    for grant in grants:
        ref = grant.metadata.get("rr_ref")
        if ref not in intents or intents[ref].state is not IntentState.POSTED:
            report.orphan_on_billing.append(grant.id)
    return report
