import secrets

from .connectors.base import ConnectorRejected, ConnectorUnknown
from .models import AccountState, Actor, Batch, CreditIntent, IntentState
from .rules import evaluate

MAX_RECHECKS = 8


class Engine:
    def __init__(self, vendor, accounts, aggregates, rule, connector, log, inbox, vault):
        self.vendor = vendor
        self.accounts = accounts
        self.aggregates = aggregates
        self.rule = rule
        self.connector = connector
        self.log = log
        self.inbox = inbox
        self.vault = vault
        self.intents = {}
        self.batches = {}

    @staticmethod
    def _id(prefix):
        return prefix + secrets.token_hex(8)

    @staticmethod
    def _require_role(actor, role):
        roles = actor.roles if isinstance(actor, Actor) else frozenset((actor,))
        if role not in roles:
            raise PermissionError("required role is missing")

    def create_batch(self, period):
        batch = Batch(self._id("batch_"), period, [])
        aggregates = [item for item in self.aggregates if item.period == period]
        for proposal in evaluate(self.rule, aggregates, self.accounts):
            if proposal.skip_reason:
                batch.skips.append((proposal.account_id, proposal.skip_reason))
                continue
            state = IntentState.HELD_ABUSE if proposal.abuse_reasons else IntentState.PROPOSED
            intent = CreditIntent(self._id("ci_"), batch.id, proposal.account_id,
                                  period, proposal.amount_cents, proposal.expires_at,
                                  state, list(proposal.abuse_reasons))
            self.log.append(state.value, intent.id,
                            {"account_id": intent.account_id,
                             "amount_cents": intent.amount_cents,
                             "reasons": list(intent.abuse_reasons)})
            self.intents[intent.id] = intent
            batch.intent_ids.append(intent.id)
            if state is IntentState.HELD_ABUSE:
                self.inbox.open("ABUSE_REVIEW", intent.id, intent.account_id)
        self.batches[batch.id] = batch
        return batch

    def review_abuse(self, item_id, approve, actor):
        self._require_role(actor, self.vendor.exception_owner_role)
        item = self.inbox.items[item_id]
        if item.kind != "ABUSE_REVIEW" or item.status != "OPEN":
            raise ValueError("item is not an open abuse review")
        intent = self.intents[item.intent_id]
        event = "REVIEW_APPROVED" if approve else "REVIEW_REJECTED"
        new_state = IntentState.PROPOSED if approve else IntentState.REJECTED
        self.log.append(event, intent.id, {"account_id": intent.account_id})
        intent.state = new_state
        self.inbox.resolve(item_id, "approved" if approve else "rejected")

    def approve_batch(self, batch_id, actor):
        self._require_role(actor, self.vendor.billing_approver_role)
        batch = self.batches[batch_id]
        if batch.status != "DRAFT":
            raise ValueError("batch is not a draft")
        if any(self.intents[ident].state is IntentState.HELD_ABUSE for ident in batch.intent_ids):
            raise ValueError("review abuse holds first")
        intents = [self.intents[ident] for ident in batch.intent_ids
                   if self.intents[ident].state is IntentState.PROPOSED]
        self.log.append_many(("BATCH_APPROVED", intent.id, {"batch_id": batch_id}) for intent in intents)
        for intent in intents:
            intent.state = IntentState.APPROVED
        batch.status = "APPROVED"
        batch.approved_by_role = self.vendor.billing_approver_role

    def _create_grant(self, intent, account):
        return self.connector.create_credit_grant(
            account.billing_customer_id, intent.amount_cents, "usd",
            intent.expires_at, intent.id, intent.id)

    def _record_posted(self, intent, grant, event):
        self.log.append(event, intent.id, {"grant_id": grant.id})
        intent.grant_id = grant.id
        intent.state = IntentState.POSTED

    def _attempt_create(self, intent, account):
        attempt = intent.attempts + 1
        self.log.append("SUBMIT_ATTEMPT", intent.id, {"attempt": attempt})
        intent.attempts = attempt
        intent.state = IntentState.SUBMITTING
        try:
            grant = self._create_grant(intent, account)
        except ConnectorRejected:
            self.log.append("SUBMIT_ERROR", intent.id, {"code": "rejected"})
            intent.state = IntentState.FAILED
            return "rejected"
        except ConnectorUnknown:
            self.log.append("SUBMIT_ERROR", intent.id, {"code": "unknown"})
            return "unknown"
        self._record_posted(intent, grant, "SUBMIT_OK")
        return "posted"

    def _lookup_after_unknown(self, intent, account):
        for _ in range(2):
            try:
                found = self.connector.find_grant_by_reference(account.billing_customer_id, intent.id)
            except ConnectorUnknown:
                self.log.append("LOOKUP_ERROR", intent.id, {"code": "unknown"})
                continue
            if found is not None:
                self._record_posted(intent, found, "LOOKUP_FOUND")
                return "posted"
            self.log.append("LOOKUP_NOT_FOUND", intent.id, {})
            return "not_found"
        return "unknown"

    def _submit(self, intent):
        account = self.accounts[intent.account_id]
        if account.state is AccountState.FROZEN:
            if not intent.frozen_skip_recorded:
                self.log.append("SUBMIT_SKIPPED", intent.id, {"reason": "account_frozen"})
                intent.frozen_skip_recorded = True
                skip = (account.id, "account_frozen")
                if skip not in self.batches[intent.batch_id].skips:
                    self.batches[intent.batch_id].skips.append(skip)
            return
        result = self._attempt_create(intent, account)
        while result == "unknown":
            lookup = self._lookup_after_unknown(intent, account)
            if lookup == "posted":
                return
            if lookup != "not_found" or intent.attempts >= 2:
                break
            result = self._attempt_create(intent, account)
            if result != "unknown":
                return
        if result != "unknown":
            return
        self.log.append_many([
            ("MARKED_UNCERTAIN", intent.id, {"account_id": account.id}),
            ("ACCOUNT_FROZEN", intent.id, {"account_id": account.id})])
        intent.state = IntentState.UNCERTAIN
        account.state = AccountState.FROZEN
        account.frozen_reason = "uncertain_grant"
        account.frozen_by_intent = intent.id
        self.inbox.open("UNCERTAIN_GRANT", intent.id, account.id)
        self.vault.send(self.vendor.exception_owner_contact, f"Grant {intent.id} uncertain; account frozen")

    def _refresh_batch(self, batch_id):
        batch = self.batches[batch_id]
        completed = (IntentState.POSTED, IntentState.REJECTED, IntentState.FAILED, IntentState.VOIDED)
        batch.status = ("POSTED" if all(self.intents[i].state in completed for i in batch.intent_ids)
                        else "POSTED_PARTIAL")
        posted = sum(self.intents[i].amount_cents for i in batch.intent_ids
                     if self.intents[i].state is IntentState.POSTED)
        batch.fee_cents = max(posted * 20 // 100, 50000) if posted else 0

    def post_batch(self, batch_id):
        batch = self.batches[batch_id]
        if batch.status not in ("APPROVED", "POSTED_PARTIAL"):
            raise ValueError("batch is not approved")
        for ident in batch.intent_ids:
            intent = self.intents[ident]
            if intent.state is IntentState.APPROVED:
                self._submit(intent)
        self._refresh_batch(batch_id)

    def resolve_uncertain(self, item_id, action, actor):
        self._require_role(actor, self.vendor.exception_owner_role)
        if action not in ("recheck", "void", "unfreeze"):
            raise ValueError("unknown resolution action")
        item = self.inbox.items[item_id]
        if item.kind != "UNCERTAIN_GRANT":
            raise ValueError("item is not an uncertain grant")
        intent = self.intents[item.intent_id]
        account = self.accounts[item.account_id]
        if action == "unfreeze":
            if account.state is AccountState.ACTIVE:
                raise ValueError("account is already active")
            if account.frozen_by_intent != intent.id:
                raise ValueError("account was frozen by another intent")
            if any(candidate.account_id == account.id and candidate.state is IntentState.UNCERTAIN
                   for candidate in self.intents.values()):
                raise ValueError("resolve uncertain intents before unfreezing")
            self.log.append("ACCOUNT_UNFROZEN", intent.id, {"account_id": account.id})
            account.state = AccountState.ACTIVE
            account.frozen_reason = None
            account.frozen_by_intent = None
            return
        if item.status != "OPEN" or intent.state is not IntentState.UNCERTAIN:
            raise ValueError("item is not open and uncertain")
        if action == "recheck":
            if intent.rechecks >= MAX_RECHECKS:
                raise ValueError("recheck limit reached; reset sandbox to start again")
            intent.rechecks += 1
            # A later failed lookup invalidates any earlier not-found evidence.
            item.resolution = None
            try:
                found = self.connector.find_grant_by_reference(account.billing_customer_id, intent.id)
            except ConnectorUnknown:
                self.log.append("LOOKUP_ERROR", intent.id, {"code": "unknown"})
                return
            if found is not None:
                self._record_posted(intent, found, "RESOLVED_POSTED")
                self.inbox.resolve(item.id, "posted")
                self._refresh_batch(intent.batch_id)
            else:
                self.log.append("RESOLUTION_LOOKUP_NOT_FOUND", intent.id, {})
                item.resolution = "not_found"
            return
        if item.resolution != "not_found":
            raise ValueError("recheck must confirm no grant before voiding")
        self.log.append("RESOLVED_VOIDED", intent.id, {})
        intent.state = IntentState.VOIDED
        self.inbox.resolve(item.id, "voided")
        self._refresh_batch(intent.batch_id)
