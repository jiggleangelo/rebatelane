import unittest
from demo import App
from rebate_rail.engine import MAX_RECHECKS
from rebate_rail.models import AccountState, IntentState
from rebate_rail.rules import abuse_reasons

class HardeningTests(unittest.TestCase):
    def approved(self):
        app = App()
        batch = app.engine.create_batch("2026-09")
        for item in list(app.inbox.items.values()):
            app.engine.review_abuse(item.id, False, "exception-owner")
        app.engine.approve_batch(batch.id, "billing-approver")
        intent = next(i for i in app.engine.intents.values() if i.state is IntentState.APPROVED)
        return app, batch, intent

    def test_all_abuse_thresholds_are_configurable_with_boundaries(self):
        app = App()
        rule, aggregate = app.engine.rule, app.engine.aggregates[0]
        rule.max_top_fingerprint_share = .10
        rule.max_peak_calls_per_min = 800
        rule.max_error_rate = .01
        rule.max_prior_period_multiplier = 1
        self.assertEqual(abuse_reasons(rule, aggregate), ["repetitive_loop", "burst", "retry_storm", "spike"])
        aggregate.top_fingerprint_share = .10
        aggregate.peak_calls_per_min = 800
        aggregate.error_rate = .01
        aggregate.calls = aggregate.prior_period_calls
        self.assertEqual(abuse_reasons(rule, aggregate), [])
        aggregate.prior_period_calls = 0
        rule.new_account_calls_multiplier = 1
        aggregate.calls = rule.min_calls - 1
        self.assertEqual(abuse_reasons(rule, aggregate), [])
        aggregate.calls += 1
        self.assertEqual(abuse_reasons(rule, aggregate), ["new_and_big"])

    def test_frozen_skip_is_recorded_once_and_never_submitted(self):
        app, batch, intent = self.approved()
        account = app.engine.accounts[intent.account_id]
        account.state = AccountState.FROZEN
        for _ in range(20):
            app.engine.post_batch(batch.id)
        entries = [e for e in app.log.entries(intent.id) if e.event_type == "SUBMIT_SKIPPED"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].data, {"reason": "account_frozen"})
        self.assertEqual(batch.skips.count((account.id, "account_frozen")), 1)
        self.assertFalse(any(ref == intent.id for _, ref in app.connector.calls))
        self.assertEqual(intent.state, IntentState.APPROVED)
        self.assertEqual(batch.status, "POSTED_PARTIAL")
        account.state = AccountState.ACTIVE
        app.engine.post_batch(batch.id)
        self.assertEqual(intent.state, IntentState.POSTED)
        self.assertEqual(batch.status, "POSTED")

    def test_failed_intent_completes_batch_without_retry(self):
        app, batch, intent = self.approved()
        app.connector.inject("reject", 4)
        app.engine.post_batch(batch.id)
        self.assertEqual(intent.state, IntentState.FAILED)
        self.assertEqual(batch.status, "POSTED")
        self.assertEqual(batch.fee_cents, 0)
        calls = list(app.connector.calls)
        with self.assertRaises(ValueError):
            app.engine.post_batch(batch.id)
        self.assertEqual(app.connector.calls, calls)
        self.assertEqual(app.log.replay_state(intent.id), "FAILED")

    def test_void_and_found_resolution_refresh_batch_status_and_fees(self):
        for fault, expected in (("timeout_before_commit", IntentState.VOIDED), ("timeout_after_commit", IntentState.POSTED)):
            with self.subTest(fault=fault):
                app, batch, intent = self.approved()
                app.connector.inject(fault)
                app.connector.inject("reject", 3)
                app.connector.inject("lookup_unavailable", 2)
                app.engine.post_batch(batch.id)
                self.assertEqual(batch.status, "POSTED_PARTIAL")
                self.assertEqual(batch.fee_cents, 0)
                item = next(x for x in app.inbox.items.values() if x.kind == "UNCERTAIN_GRANT")
                app.engine.resolve_uncertain(item.id, "recheck", "exception-owner")
                if expected is IntentState.VOIDED:
                    self.assertEqual(batch.status, "POSTED_PARTIAL")
                    app.engine.resolve_uncertain(item.id, "void", "exception-owner")
                self.assertEqual(intent.state, expected)
                self.assertEqual(batch.status, "POSTED")
                self.assertEqual(batch.fee_cents, 0 if expected is IntentState.VOIDED else 50000)
                self.assertEqual(app.log.replay_state(intent.id), expected.value)

    def test_successful_not_found_rechecks_are_bounded_too(self):
        app, batch, intent = self.approved()
        app.connector.inject("timeout_before_commit")
        app.connector.inject("lookup_unavailable", 2)
        app.engine.post_batch(batch.id)
        item = next(x for x in app.inbox.items.values() if x.kind == "UNCERTAIN_GRANT")
        for _ in range(MAX_RECHECKS):
            app.engine.resolve_uncertain(item.id, "recheck", "exception-owner")
        counts = (len(app.log.entries()), len(app.connector.calls))
        with self.assertRaises(ValueError):
            app.engine.resolve_uncertain(item.id, "recheck", "exception-owner")
        self.assertEqual((len(app.log.entries()), len(app.connector.calls)), counts)
        app.engine.resolve_uncertain(item.id, "void", "exception-owner")
        self.assertEqual(intent.state, IntentState.VOIDED)

    def test_failed_recheck_invalidates_earlier_not_found_evidence(self):
        app, batch, intent = self.approved()
        app.connector.inject("timeout_before_commit")
        app.connector.inject("lookup_unavailable", 2)
        app.engine.post_batch(batch.id)
        item = next(x for x in app.inbox.items.values() if x.kind == "UNCERTAIN_GRANT")
        app.engine.resolve_uncertain(item.id, "recheck", "exception-owner")
        app.connector.inject("lookup_unavailable")
        app.engine.resolve_uncertain(item.id, "recheck", "exception-owner")
        with self.assertRaises(ValueError):
            app.engine.resolve_uncertain(item.id, "void", "exception-owner")
        self.assertEqual(intent.state, IntentState.UNCERTAIN)
