from dataclasses import dataclass, field
from typing import Protocol

@dataclass
class Grant:
    id: str
    customer_id: str
    amount_cents: int
    currency: str
    expires_at: int
    metadata: dict = field(default_factory=dict)

class ConnectorRejected(Exception):
    pass

class ConnectorUnknown(Exception):
    pass

class BillingConnector(Protocol):
    def create_credit_grant(self, customer_id, amount_cents, currency, expires_at,
                            reference, idempotency_key): ...
    def find_grant_by_reference(self, customer_id, reference): ...
    def list_grants(self, customer_id): ...
