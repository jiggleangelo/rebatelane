import math, secrets, time
from rebate_rail.models import Account, EarnRule, UsageAggregate
from rebate_rail.rules import evaluate

SIGNALS = ('top_fingerprint_share', 'peak_calls_per_min', 'error_rate', 'prior_period_calls', 'account_age_days')
DEFAULT_RULE = {'min_calls': 100000, 'rebate_bps': 200, 'cap_cents': 20000, 'expiry_days': 90,
                'min_account_age_days': 30, 'max_top_fingerprint_share': .5,
                'max_peak_calls_per_min': 5000, 'max_error_rate': .3,
                'max_prior_period_multiplier': 5, 'new_account_calls_multiplier': 3}

def validate_rule(value):
    limits = {'min_calls': (0, 10**15), 'rebate_bps': (0, 10000), 'cap_cents': (0, 10**14),
              'expiry_days': (1, 3650), 'min_account_age_days': (0, 365000),
              'max_top_fingerprint_share': (0, 1), 'max_peak_calls_per_min': (0, 10**15),
              'max_error_rate': (0, 1), 'max_prior_period_multiplier': (0, 10**6), 'new_account_calls_multiplier': (0, 10**6)}
    fractional = {'max_top_fingerprint_share', 'max_error_rate', 'max_prior_period_multiplier', 'new_account_calls_multiplier'}
    result = {}
    for key, (low, high) in limits.items():
        try: number = float(value[key])
        except (KeyError, TypeError, ValueError, OverflowError) as exc: raise ValueError(f'invalid rule field: {key}') from exc
        if not math.isfinite(number) or not low <= number <= high or (key not in fractional and not number.is_integer()):
            raise ValueError(f'{key} must be between {low} and {high}' + ('' if key in fractional else ' and an integer'))
        result[key] = number if key in fractional else int(number)
    return result

def rule_object(value): return EarnRule(**validate_rule(value))

def build_run(rows, rule, dropped=None, now=None):
    rule = validate_rule(rule)
    if not rows or len(rows) > 5000 or len({x['period'] for x in rows}) != 1: raise ValueError('provide 1–5,000 rows for one period')
    accounts, aggregates, missing = {}, [], {}
    for row in rows:
        ident = row['account_id']
        if ident in accounts: raise ValueError('account_id must be unique within a run')
        accounts[ident] = Account(ident, row.get('billing_customer_id', ''), ident)
        missing[ident] = [name for name in SIGNALS if row.get(name) is None]
        aggregates.append(UsageAggregate(ident, row['period'], row['calls'], row['spend_cents'],
            row.get('top_fingerprint_share') or 0, row.get('peak_calls_per_min') or 0,
            row.get('error_rate') or 0, row.get('prior_period_calls') or 0,
            row.get('account_age_days') if row.get('account_age_days') is not None else rule['min_account_age_days']))
    proposals = evaluate(rule_object(rule), aggregates, accounts)
    by_id, intents = {row['account_id']: row for row in rows}, []
    for proposal in proposals:
        source = by_id[proposal.account_id]; abuse = list(proposal.abuse_reasons)
        if source.get('prior_period_calls') is None: abuse = [reason for reason in abuse if reason not in ('spike', 'new_and_big')]
        if source.get('account_age_days') is None and rule['min_account_age_days'] > 0: abuse.append('missing_account_age')
        skip = proposal.skip_reason if source.get('billing_customer_id') else 'missing_billing_customer_id'
        reasons = ([skip] if skip else abuse) + ['not_evaluated:' + name for name in missing[proposal.account_id]]
        state = 'SKIPPED' if skip else ('HELD_ABUSE' if abuse else 'PROPOSED')
        intents.append({'reference': '' if skip else 'ci_' + secrets.token_hex(8),
            'account_id': proposal.account_id, 'customer': source.get('billing_customer_id', ''),
            'currency': 'usd', 'period': proposal.period, 'spend_cents': source['spend_cents'],
            'amount_cents': proposal.amount_cents, 'cap_cents': rule['cap_cents'],
            'expires_at': proposal.expires_at, 'state': state, 'reasons': reasons,
            'review_note': '', 'mark': '', 'mark_time': None, 'frozen': False, 'freeze_reason': '', 'resolution': ''})
    return {'id': 'run_' + secrets.token_hex(8), 'period': rows[0]['period'],
            'created_at': int(time.time() if now is None else now), 'status': 'DRAFT',
            'intents': intents, 'dropped': list(dropped or []), 'grant_exports': [], 'report': None}

def review(run, reference, decision, note=''):
    intent = next((item for item in run['intents'] if item['reference'] == reference), None)
    if not intent or intent['state'] != 'HELD_ABUSE' or decision not in ('approve', 'reject'): raise ValueError('row is not awaiting review')
    intent['state'] = 'PROPOSED' if decision == 'approve' else 'REJECTED'
    intent['review_note'] = note[:200]
    return intent

def approve(run):
    if run['status'] != 'DRAFT' or any(item['state'] == 'HELD_ABUSE' for item in run['intents']): raise ValueError('review all held rows first')
    for item in run['intents']:
        if item['state'] == 'PROPOSED': item['state'] = 'APPROVED'
    run['status'] = 'APPROVED'

def mark(run, reference, value, now=None):
    if value not in ('submitted_ok', 'submitted_unknown', 'rejected'): raise ValueError('invalid submission mark')
    intent = next((item for item in run['intents'] if item['reference'] == reference and item['state'] == 'APPROVED'), None)
    if not intent or intent.get('resolution'): raise ValueError('unknown or resolved approved reference')
    if intent.get('mark') == 'submitted_ok' or intent.get('billing_seen'): raise ValueError('this credit already has billing evidence; resolve it instead')
    if intent.get('mark') == 'submitted_unknown' and value != 'submitted_ok':
        if (run.get('report') or {}).get('verdicts', {}).get(reference) != 'safe_to_resubmit_same_key':
            raise ValueError('reconcile the unknown submission before changing its mark')
    intent['mark'], intent['mark_time'] = value, time.time() if now is None else now
    intent['frozen'] = value == 'submitted_unknown'
    intent['freeze_reason'] = 'unknown_submission' if intent['frozen'] else ''
    if run.get('report'): run['report']['verdicts'].pop(reference, None)
    return intent

def reconcile(run, grants, captured_at=None, known_references=None, *, uploaded_at=None, complete=True):
    uploaded_at = time.time() if uploaded_at is None else uploaded_at
    if captured_at is not None:
        if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)) or not math.isfinite(captured_at) or not 0 <= captured_at <= uploaded_at:
            raise ValueError('capture time must be a finite past UTC timestamp')
    complete = bool(complete and getattr(grants, 'complete', True) and captured_at is not None)
    by_ref = {}
    for grant in grants:
        if grant['rr_ref']: by_ref.setdefault(grant['rr_ref'], []).append(grant)
    intents = {item['reference']: item for item in run['intents'] if item['reference']}
    known = set(known_references or ()) | set(intents)
    names = ('matched', 'amount_mismatch', 'missing_on_billing', 'orphan_on_billing', 'safe_to_create',
             'do_not_retry_freeze', 'duplicate_on_billing', 'safe_to_resubmit_same_key')
    buckets, verdicts = {name: [] for name in names}, {}
    for ref, intent in intents.items():
        found = by_ref.get(ref, [])
        if found: intent['billing_seen'] = True
        if len(found) > 1: verdict = 'duplicate_on_billing'
        elif found:
            grant = found[0]
            same = (grant['amount_cents'] == intent['amount_cents'] and grant['customer'] == intent['customer']
                    and grant['currency'].lower() == intent.get('currency', 'usd'))
            verdict = 'matched' if same else 'amount_mismatch'
        elif not complete:
            verdict = 'do_not_retry_freeze'
            # Do not overwrite a stronger, independently unresolved freeze.
            if not intent.get('frozen') or intent.get('freeze_reason') == 'incomplete_export':
                intent['frozen'], intent['freeze_reason'] = True, 'incomplete_export'
        elif intent['mark'] == 'submitted_ok' or intent.get('billing_seen'): verdict = 'missing_on_billing'
        elif intent['mark'] == 'submitted_unknown':
            marked = intent.get('mark_time')
            verdict = 'safe_to_resubmit_same_key' if marked is not None and captured_at >= marked else 'do_not_retry_freeze'
        elif intent['state'] != 'APPROVED' or intent.get('resolution') == 'voided' or intent['mark'] == 'rejected': verdict = 'do_not_retry_freeze'
        else: verdict = 'safe_to_create'
        verdicts[ref] = verdict; buckets[verdict].append(ref)
        if verdict in ('amount_mismatch', 'duplicate_on_billing'): intent['frozen'], intent['freeze_reason'] = True, 'billing_conflict'
        elif complete and verdict in ('safe_to_create', 'matched') and intent.get('freeze_reason') == 'incomplete_export':
            intent['frozen'], intent['freeze_reason'] = False, ''
    customers = {item['customer'] for item in intents.values()}
    for grant in grants:
        ref = grant['rr_ref']
        if ref.startswith('ci_') and ref not in known and grant['customer'] in customers: buckets['orphan_on_billing'].append(grant['id'])
    report = {'uploaded_at': uploaded_at, 'captured_at': captured_at, 'complete': complete, 'buckets': buckets, 'verdicts': verdicts}
    run['grant_exports'].append({'uploaded_at': uploaded_at, 'captured_at': captured_at, 'complete': complete, 'grants': list(grants)})
    run['grant_exports'] = run['grant_exports'][-4:]; run['report'] = report
    return report

def downloadable(run, workspace_runs=()):
    frozen = {item['account_id'] for source in [run, *workspace_runs] for item in source['intents'] if item.get('frozen')}
    report, result = run.get('report') or {}, []
    for item in run['intents']:
        if (item['state'] != 'APPROVED' or not item.get('customer') or item['amount_cents'] <= 0 or
                item['account_id'] in frozen or item.get('resolution') or item.get('billing_seen') or
                item.get('mark') in ('submitted_ok', 'rejected')): continue
        verdict = report.get('verdicts', {}).get(item['reference'])
        if item.get('mark') == 'submitted_unknown':
            captured = report.get('captured_at')
            if not report.get('complete') or captured is None or captured < (item.get('mark_time') or 0) or verdict != 'safe_to_resubmit_same_key': continue
        elif verdict not in (None, 'safe_to_create'): continue
        result.append(item)
    return result

def resolve(run, reference, action):
    intent = next((item for item in run['intents'] if item['reference'] == reference), None)
    report = run.get('report') or {}; verdict = report.get('verdicts', {}).get(reference)
    if not intent or action not in ('posted', 'void', 'unfreeze'): raise ValueError('invalid resolution')
    if action == 'posted':
        if verdict not in ('matched', 'amount_mismatch', 'duplicate_on_billing'): raise ValueError('latest export does not contain this reference')
        intent['mark'], intent['resolution'] = 'submitted_ok', 'posted'
    elif action == 'void':
        if verdict != 'safe_to_resubmit_same_key' or intent.get('billing_seen') or not report.get('complete'): raise ValueError('a complete newer not-found export is required')
        intent['resolution'] = 'voided'
    else:
        if not intent.get('frozen'): raise ValueError('account is already unfrozen')
        if not intent.get('resolution') and verdict not in ('matched', 'safe_to_resubmit_same_key', 'safe_to_create'):
            raise ValueError('resolve uncertain intents before unfreezing')
        intent['frozen'], intent['freeze_reason'] = False, ''
    return intent
