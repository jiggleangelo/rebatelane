import os
import urllib.error
import urllib.parse
from .base import ConnectorRejected, ConnectorUnknown, Grant

CLEARED_JURISDICTIONS = frozenset()

class StripeHttpConnector:
    __slots__ = ("transport", "_request", "_enabled")
    def __init__(self, restricted_key, jurisdiction, transport, environ=None):
        env = os.environ if environ is None else environ
        self._enabled = env.get("REBATE_RAIL_LIVE") == "1" and jurisdiction in CLEARED_JURISDICTIONS and bool(restricted_key)
        if not self._enabled: raise RuntimeError("live Stripe connector is disabled")
        self.transport = transport
        # Credentials stay in the closure, outside ordinary object inspection.
        def request(method, url, headers, body):
            safe_headers = dict(headers)
            safe_headers["Authorization"] = "Bearer " + restricted_key
            return transport(method, url, safe_headers, body)
        self._request = request
    def __repr__(self): return "StripeHttpConnector(restricted_key=<redacted>)"
    def _call(self, method, path, fields=None, idem=None):
        if not self._enabled: raise RuntimeError("live Stripe connector is disabled")
        body = urllib.parse.urlencode(fields or {}).encode() if fields is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if idem: headers["Idempotency-Key"] = idem
        try: status, data = self._request(method, "https://api.stripe.com" + path, headers, body)
        except (TimeoutError, urllib.error.URLError, OSError) as exc: raise ConnectorUnknown("transport unavailable") from exc
        if status in (408, 409, 424, 425, 429) or status >= 500: raise ConnectorUnknown("ambiguous billing response")
        if 400 <= status < 500: raise ConnectorRejected("billing request rejected")
        if not isinstance(data, dict): raise ConnectorUnknown("malformed billing response")
        return data
    @staticmethod
    def _grant(data, fallback_currency="usd"):
        try:
            monetary = data["amount"]["monetary"]
            return Grant(str(data["id"]), str(data["customer"]), int(monetary["value"]), str(monetary.get("currency", fallback_currency)), int(data.get("expires_at", 0)), dict(data.get("metadata", {})))
        except (KeyError, TypeError, ValueError) as exc: raise ConnectorUnknown("malformed billing response") from exc
    def create_credit_grant(self, customer_id, amount_cents, currency, expires_at, reference, idempotency_key):
        fields = {"customer": customer_id, "category": "promotional", "amount[type]": "monetary", "amount[monetary][value]": amount_cents, "amount[monetary][currency]": currency, "applicability_config[scope][price_type]": "metered", "expires_at": expires_at, "metadata[rr_ref]": reference}
        return self._grant(self._call("POST", "/v1/billing/credit_grants", fields, idempotency_key), currency)
    def list_grants(self, customer_id):
        path = "/v1/billing/credit_grants?" + urllib.parse.urlencode({"customer": customer_id})
        result = []
        while path:
            page = self._call("GET", path)
            try:
                data = page.get("data", [])
                if not isinstance(data, list): raise TypeError
                result.extend(self._grant(item) for item in data)
                path = "/v1/billing/credit_grants?" + urllib.parse.urlencode({"customer": customer_id, "starting_after": data[-1]["id"]}) if page.get("has_more") and data else None
            except (KeyError, TypeError, ValueError) as exc: raise ConnectorUnknown("malformed billing response") from exc
        return result
    def find_grant_by_reference(self, customer_id, reference):
        query = "metadata['rr_ref']:'%s' AND customer:'%s'" % (str(reference).replace("'", "\\'"), str(customer_id).replace("'", "\\'"))
        page = self._call("GET", "/v1/billing/credit_grants/search?" + urllib.parse.urlencode({"query": query, "limit": 2}))
        try: grants = [self._grant(item) for item in page.get("data", [])]
        except (TypeError, ValueError) as exc: raise ConnectorUnknown("malformed billing response") from exc
        return next((grant for grant in grants if grant.customer_id == customer_id and grant.metadata.get("rr_ref") == reference), None)
