import unittest
from rebate_rail.connectors.base import ConnectorRejected
from rebate_rail.connectors.fake_stripe import FakeStripe
from rebate_rail.engine import Engine
from rebate_rail.inbox import Inbox
from rebate_rail.intent_log import IntentLog
from rebate_rail.models import AccountState, IntentState
from rebate_rail.reconcile import reconcile
from rebate_rail.rules import abuse_reasons, evaluate
from rebate_rail.sample_data import build
from rebate_rail.vault import Vault

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.vault, self.log, self.inbox, self.fake = Vault(), IntentLog(), Inbox(), FakeStripe()
        vendor, accounts, aggregates, rule = build(self.vault)
        self.engine = Engine(vendor, accounts, aggregates, rule, self.fake, self.log, self.inbox, self.vault)

    def test_log_chain_tamper_and_api(self):
        self.log.append("PROPOSED", "ci_a", {"amount_cents": 10})
        self.assertTrue(self.log.verify())
        self.assertFalse(hasattr(self.log, "update")); self.assertFalse(hasattr(self.log, "delete"))
        self.log._entries[0].data["amount_cents"] = 11
        self.assertFalse(self.log.verify())

    def test_rules_and_boundaries(self):
        vendor, accounts, aggregates, rule = build(Vault())
        results = evaluate(rule, aggregates, accounts)
        self.assertEqual(sum(not p.skip_reason for p in results), 6)
        aggregate = aggregates[0]
        aggregate.top_fingerprint_share = .50; aggregate.peak_calls_per_min = 5000; aggregate.error_rate = .30
        aggregate.calls = 5 * aggregate.prior_period_calls
        self.assertEqual(abuse_reasons(rule, aggregate), [])
        aggregate.top_fingerprint_share = .50001
        self.assertIn("repetitive_loop", abuse_reasons(rule, aggregate))
        accounts[aggregate.account_id].state = AccountState.FROZEN
        self.assertEqual(evaluate(rule, [aggregate], accounts)[0].skip_reason, "frozen")

    def _approved_intent(self):
        batch = self.engine.create_batch("2026-09")
        for item in list(self.inbox.items.values()):
            self.engine.review_abuse(item.id, False, "exception-owner")
        self.engine.approve_batch(batch.id, "billing-approver")
        return batch, next(self.engine.intents[i] for i in batch.intent_ids if self.engine.intents[i].state is IntentState.APPROVED)

    def test_roles_and_holds(self):
        batch = self.engine.create_batch("2026-09")
        with self.assertRaises(PermissionError): self.engine.approve_batch(batch.id, "wrong")
        with self.assertRaises(ValueError): self.engine.approve_batch(batch.id, "billing-approver")

    def test_timeout_after_commit_no_duplicate(self):
        batch, intent = self._approved_intent()
        self.fake.inject("timeout_after_commit")
        self.engine.post_batch(batch.id)
        grants = [g for g in self.fake.grants.values() if g.metadata["rr_ref"] == intent.id]
        self.assertEqual(len(grants), 1)
        self.assertEqual(intent.state, IntentState.POSTED)
        self.assertEqual(self.log.replay_state(intent.id), intent.state.value)

    def test_unknown_freezes_and_resolves(self):
        batch, intent = self._approved_intent()
        self.fake.inject("timeout_after_commit"); self.fake.inject("lookup_unavailable", 2)
        self.engine.post_batch(batch.id)
        self.assertEqual(intent.state, IntentState.UNCERTAIN)
        self.assertEqual(self.engine.accounts[intent.account_id].state, AccountState.FROZEN)
        item = next(i for i in self.inbox.items.values() if i.kind == "UNCERTAIN_GRANT")
        with self.assertRaises(ValueError): self.engine.resolve_uncertain(item.id, "void", "exception-owner")
        self.engine.resolve_uncertain(item.id, "recheck", "exception-owner")
        self.assertEqual(intent.state, IntentState.POSTED)
        self.engine.resolve_uncertain(item.id, "unfreeze", "exception-owner")
        self.assertEqual(self.engine.accounts[intent.account_id].state, AccountState.ACTIVE)

    def test_fault_combinations_never_duplicate(self):
        faults = [None, "timeout_after_commit", "timeout_before_commit", "reject"]
        for first in faults:
            for second in faults:
                self.setUp(); batch, intent = self._approved_intent()
                for fault in (first, second):
                    if fault: self.fake.inject(fault)
                self.engine.post_batch(batch.id)
                count = sum(g.metadata["rr_ref"] == intent.id for g in self.fake.grants.values())
                self.assertLessEqual(count, 1)

    def test_reconcile_and_privacy(self):
        batch, intent = self._approved_intent(); self.engine.post_batch(batch.id)
        report = reconcile(self.engine.intents, self.fake, self.engine.accounts)
        self.assertIn(intent.id, report.matched)
        serialized = repr([entry.__dict__ for entry in self.log.entries()])
        self.assertNotIn("@", serialized); self.assertNotIn("vlt_", serialized)

    def test_vault_is_write_only(self):
        vault = Vault(); token = vault.put("x@example.invalid")
        self.assertTrue(token.startswith("vlt_")); self.assertFalse(hasattr(vault, "get"))
        vault.send(token, "hello"); self.assertEqual(vault.sent_count, 1)

if __name__ == "__main__": unittest.main()
