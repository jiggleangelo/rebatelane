import tempfile, unittest
from unittest.mock import patch
from rebate_rail.plan import DEFAULT_RULE, approve, build_run, downloadable, mark, reconcile, review
from rebate_rail.workspace import RateLimited, Store

def rows(count=1): return [{'account_id': 'a' + str(i), 'period': '2026-09', 'calls': 200000, 'spend_cents': 10000, 'billing_customer_id': 'cus_' + str(i), 'account_age_days': 30} for i in range(count)]

class PlanFixTests(unittest.TestCase):
    def test_preapproval_reconcile_does_not_permanently_freeze(self):
        run = build_run(rows(), DEFAULT_RULE); ref = run['intents'][0]['reference']; reconcile(run, [], 10)
        self.assertEqual(run['report']['verdicts'][ref], 'do_not_retry_freeze'); self.assertFalse(run['intents'][0]['frozen']); approve(run); reconcile(run, [], 11)
        self.assertEqual(run['report']['verdicts'][ref], 'safe_to_create'); self.assertEqual(downloadable(run), run['intents'])
    def test_corrected_export_clears_only_incomplete_export_freeze(self):
        run = build_run(rows(), DEFAULT_RULE); approve(run); item = run['intents'][0]; item['frozen'], item['freeze_reason'] = True, 'incomplete_export'; reconcile(run, [], 20); self.assertFalse(item['frozen'])
        item['frozen'], item['freeze_reason'] = True, 'unknown_submission'; reconcile(run, [], 21); self.assertTrue(item['frozen'])
    def test_same_second_unknown_reconciliation_is_usable(self):
        run = build_run(rows(), DEFAULT_RULE); approve(run); item = run['intents'][0]; mark(run, item['reference'], 'submitted_unknown', 100); report = reconcile(run, [], 100)
        self.assertEqual(report['verdicts'][item['reference']], 'safe_to_resubmit_same_key')
    def test_review_note_is_preserved_for_display_and_export(self):
        suspicious = rows(); suspicious[0]['top_fingerprint_share'] = .9; run = build_run(suspicious, DEFAULT_RULE); review(run, run['intents'][0]['reference'], 'approve', 'checked by owner'); self.assertEqual(run['intents'][0]['review_note'], 'checked by owner')
    def test_creation_limit_is_per_actor(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, clock=lambda: 100)
            for _ in range(60): store.delete(store.create(actor='attacker'))
            with self.assertRaises(RateLimited): store.create(actor='attacker')
            store.create(actor='different-client')
    def test_capacity_check_does_not_use_rglob(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); token = store.create()
            with patch.object(type(store.root), 'rglob', side_effect=AssertionError('rglob')): store.create_run(token, rows(), [])

if __name__ == '__main__': unittest.main()
