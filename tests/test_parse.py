import json
import unittest
from rebate_rail.parse import parse_grants, parse_usage


class ParseFixTests(unittest.TestCase):
    def test_currency_symbols_and_grouped_thousands(self):
        rows, dropped = parse_usage('account_id,period,calls,spend,billing_customer_id\na,2026-09,100000,"$1,234.56",cus_a\n')
        self.assertEqual(rows[0]['spend_cents'], 123456)
        self.assertEqual(dropped, [])

    def test_bad_thousands_grouping_is_dropped(self):
        with self.assertRaises(ValueError):
            parse_usage('account_id,period,calls,spend,billing_customer_id\na,2026-09,100000,"12,34.56",cus_a\n')

    def test_dashboard_csv_metadata_reference_is_optional_and_read(self):
        csv_text = 'id,customer,amount_cents,currency,expires_at,metadata\ng,cus_a,200,usd,0,"{""rr_ref"":""ci_a""}"\n'
        self.assertEqual(parse_grants(csv_text)[0]['rr_ref'], 'ci_a')
        no_metadata = 'id,customer,amount_cents,currency,expires_at\ng,cus_a,200,usd,0\n'
        self.assertEqual(parse_grants(no_metadata)[0]['rr_ref'], '')

    def test_json_still_discards_unapproved_fields(self):
        grant = {'id': 'g', 'customer': 'cus_a', 'amount_cents': 2, 'currency': 'usd', 'expires_at': 0,
                 'metadata': {'rr_ref': 'ci_a', 'private': 'discard'}, 'description': 'discard'}
        parsed = parse_grants(json.dumps([grant]))[0]
        self.assertEqual(set(parsed), {'id', 'customer', 'amount_cents', 'currency', 'expires_at', 'rr_ref'})


if __name__ == '__main__': unittest.main()
