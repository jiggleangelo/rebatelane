import unittest
import urllib.parse
from unittest.mock import patch
from rebate_rail.connectors.base import ConnectorRejected, ConnectorUnknown
from rebate_rail.connectors.stripe_http import StripeHttpConnector

class StripeHttpTests(unittest.TestCase):
    def test_gate_ships_closed(self):
        with self.assertRaises(RuntimeError):
            StripeHttpConnector("rk", "US", lambda *args: None, {"REBATE_RAIL_LIVE": "1"})

    def test_form_header_and_lookup_pages(self):
        calls = []
        def transport(method, url, headers, body):
            calls.append((method, url, headers, body))
            if method == "POST":
                return 200, {"id":"g1","customer":"cus","amount":{"monetary":{"value":123}},"metadata":{"rr_ref":"ci"}}
            return 200, {"data":[{"id":"g1","customer":"cus","amount":{"monetary":{"value":123}},"metadata":{"rr_ref":"ci"}}],"has_more":False}
        with patch("rebate_rail.connectors.stripe_http.CLEARED_JURISDICTIONS", frozenset({"US"})):
            connector = StripeHttpConnector("rk", "US", transport, {"REBATE_RAIL_LIVE":"1"})
            connector.create_credit_grant("cus", 123, "usd", 99, "ci", "idem")
            self.assertEqual(calls[0][2]["Idempotency-Key"], "idem")
            fields = urllib.parse.parse_qs(calls[0][3].decode())
            self.assertEqual(fields["metadata[rr_ref]"], ["ci"])
            self.assertEqual(connector.find_grant_by_reference("cus", "ci").id, "g1")

    def test_error_mapping(self):
        with patch("rebate_rail.connectors.stripe_http.CLEARED_JURISDICTIONS", frozenset({"US"})):
            for status, error in ((400, ConnectorRejected), (409, ConnectorUnknown), (429, ConnectorUnknown), (500, ConnectorUnknown)):
                connector = StripeHttpConnector("rk", "US", lambda *args, s=status: (s, {}), {"REBATE_RAIL_LIVE":"1"})
                with self.assertRaises(error): connector.list_grants("cus")

if __name__ == "__main__": unittest.main()
