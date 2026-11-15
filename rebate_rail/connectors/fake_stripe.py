import time
from .base import ConnectorRejected, ConnectorUnknown, Grant

class FakeStripe:
    def __init__(self, idempotency_window=10**9):
        self.grants = {}
        self.idempotency = {}
        self.idempotency_window = idempotency_window
        self.create_faults = []
        self.lookup_failures = 0
        self.calls = []
        self._next = 1

    def inject(self, fault, count=1):
        if fault == "lookup_unavailable":
            self.lookup_failures += count
        else:
            self.create_faults.extend([fault] * count)

    def create_credit_grant(self, customer_id, amount_cents, currency, expires_at,
                            reference, idempotency_key):
        self.calls.append(("create", reference))
        now = time.monotonic()
        cached = self.idempotency.get(idempotency_key)
        if cached and now - cached[0] <= self.idempotency_window:
            return self.grants[cached[1]]
        fault = self.create_faults.pop(0) if self.create_faults else None
        if fault == "reject":
            raise ConnectorRejected("rejected")
        if fault == "timeout_before_commit":
            raise ConnectorUnknown("timeout_before_commit")
        grant = Grant(f"grant_{self._next}", customer_id, amount_cents, currency,
                      expires_at, {"rr_ref": reference})
        self._next += 1
        self.grants[grant.id] = grant
        self.idempotency[idempotency_key] = (now, grant.id)
        if fault == "timeout_after_commit":
            raise ConnectorUnknown("timeout_after_commit")
        return grant

    def find_grant_by_reference(self, customer_id, reference):
        self.calls.append(("lookup", reference))
        if self.lookup_failures:
            self.lookup_failures -= 1
            raise ConnectorUnknown("lookup_unavailable")
        for grant in self.grants.values():
            if grant.customer_id == customer_id and grant.metadata.get("rr_ref") == reference:
                return grant
        return None

    def list_grants(self, customer_id):
        return [g for g in self.grants.values() if g.customer_id == customer_id]
