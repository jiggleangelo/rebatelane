import concurrent.futures, csv, http.client, importlib, io, json, socket, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
import app
from rebate_rail.parse import parse_grants, parse_usage
from rebate_rail.plan import DEFAULT_RULE, approve, build_run, downloadable, mark, reconcile, resolve
from rebate_rail.workspace import Store

test_app = importlib.import_module(f'{__package__}.test_app' if __package__ else 'test_app')
def rows(period='2026-09'): return [{'account_id': 'a', 'period': period, 'calls': 200000, 'spend_cents': 10000, 'billing_customer_id': 'cus_a', 'account_age_days': 90}]
def approved():
    run = build_run(rows(), DEFAULT_RULE); approve(run); return run

class EvidenceTests(unittest.TestCase):
    def test_has_more_freezes_every_absent_reference(self):
        for marked in (False, True):
            run = approved(); item = run['intents'][0]
            if marked: mark(run, item['reference'], 'submitted_unknown', 100)
            grants = parse_grants('{"data":[],"has_more":true}'); report = reconcile(run, grants, 101, uploaded_at=102)
            self.assertFalse(report['complete']); self.assertEqual(report['verdicts'][item['reference']], 'do_not_retry_freeze'); self.assertTrue(item['frozen']); self.assertEqual(downloadable(run), [])
            with self.assertRaises(ValueError): resolve(run, item['reference'], 'unfreeze')
    def test_upload_time_cannot_make_old_capture_safe(self):
        run = approved(); item = run['intents'][0]; mark(run, item['reference'], 'submitted_unknown', 100.5); report = reconcile(run, [], 100, uploaded_at=10000)
        self.assertEqual(report['verdicts'][item['reference']], 'do_not_retry_freeze'); self.assertEqual(downloadable(run), []); reconcile(run, [], 101, uploaded_at=10000); resolve(run, item['reference'], 'unfreeze')
        self.assertEqual(downloadable(run), [item]); self.assertEqual(run['grant_exports'][-1]['captured_at'], 101)
    def test_missing_or_invalid_capture_time_never_authorizes_retry(self):
        run = approved(); item = run['intents'][0]; mark(run, item['reference'], 'submitted_unknown', 100); reconcile(run, [], uploaded_at=1000); self.assertEqual(downloadable(run), [])
        for value in ('nan', 'inf', '-1', '1001', '2026-01-01T00:00:00'):
            with self.subTest(value=value), self.assertRaises(ValueError): app.capture_time(value, 1000)
        self.assertEqual(app.capture_time('1970-01-01T00:01:41Z', 1000), 101)
    def test_dropped_row_freeze_clears_with_corrected_export(self):
        run = approved(); item = run['intents'][0]; app.reconcile_import(run, [], [{'line': 2, 'reason': 'invalid grant id'}], 100, set()); self.assertEqual(item['freeze_reason'], 'incomplete_export'); self.assertEqual(downloadable(run), [])
        app.reconcile_import(run, [], [], 101, set()); self.assertFalse(item['frozen']); self.assertEqual(downloadable(run), [item]); mark(run, item['reference'], 'submitted_unknown', 102)
        app.reconcile_import(run, [], [{'line': 2, 'reason': 'invalid grant id'}], 103, set()); app.reconcile_import(run, [], [], 104, set()); self.assertEqual(item['freeze_reason'], 'unknown_submission'); self.assertEqual(downloadable(run), [])
    def test_multiple_pages_support_more_than_one_hundred_grants(self):
        values = [{'id': 'g' + str(i), 'customer': 'cus_a', 'amount_cents': 1, 'description': 'discard'} for i in range(101)]; pages = [{'data': values[:100], 'has_more': True}, {'data': values[100:], 'has_more': False}]
        for text in (json.dumps(pages), '\n'.join(json.dumps(page) for page in pages)):
            grants = parse_grants(text); self.assertEqual(len(grants), 101); self.assertTrue(grants.complete); self.assertEqual(grants.pages, 2); self.assertNotIn('description', grants[0])
        pages[-1]['has_more'] = True; self.assertFalse(parse_grants(json.dumps(pages)).complete)
        with self.assertRaises(ValueError): parse_grants(json.dumps({'data': [], 'has_more': 'false'}))
        with self.assertRaises(ValueError): parse_grants(json.dumps([{'data': values * 25, 'has_more': True}, {'data': values * 25, 'has_more': False}]))
    def test_missing_age_holds_until_review(self):
        parsed, _ = parse_usage('account_id,period,calls,spend,billing_customer_id\na,2026-09,200000,100,cus_a'); run = build_run(parsed, DEFAULT_RULE); self.assertEqual(run['intents'][0]['state'], 'HELD_ABUSE'); self.assertIn('missing_account_age', run['intents'][0]['reasons'])
        with self.assertRaises(ValueError): approve(run)
        self.assertEqual(downloadable(run), []); parsed[0]['account_age_days'] = 30; self.assertEqual(build_run(parsed, DEFAULT_RULE)['intents'][0]['state'], 'PROPOSED'); parsed[0]['account_age_days'] = 29; self.assertIn('too_new', build_run(parsed, DEFAULT_RULE)['intents'][0]['reasons'])

class StorageBudgetTests(unittest.TestCase):
    def test_global_budget_counts_other_workspaces_and_temporary_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); first, second = store.create(), store.create(); run = store.create_run(first, rows(), []); before = store.export(first); _, path = store.resolve(first)
            snapshot_size = (path / 'workspace.sqlite3').stat().st_size; used = store.total_size(store.root)
            # The cap is one byte short of holding existing data plus a replacement snapshot.
            store.cap = used + snapshot_size - 1
            with self.assertRaises(RuntimeError): store.mutate_run(first, run['id'], lambda value, db: (approve(value), [])[1])
            self.assertEqual(store.export(first), before); self.assertLessEqual(store.total_size(store.root), store.cap); store.delete(second); store.mutate_run(first, run['id'], lambda value, db: (approve(value), [])[1])
            self.assertEqual(store.run(first, run['id'])['status'], 'APPROVED'); self.assertEqual(list(Path(root).glob('**/.pending-*')), [])
    def test_global_capacity_is_serialized_between_store_instances(self):
        with tempfile.TemporaryDirectory() as root:
            first = Store(root); a, b = first.create(), first.create(); second = Store(root); before = first.total_size(first.root); first.cap = second.cap = before + 30000; barrier = threading.Barrier(2); payload = {'padding': 'x' * 20000}
            def write(pair):
                store, token = pair; barrier.wait()
                try: store.append_log(token, 'TEST', 'ci_a', payload); return True
                except RuntimeError: return False
            with concurrent.futures.ThreadPoolExecutor(2) as pool: results = list(pool.map(write, [(first, a), (second, b)]))
            self.assertLessEqual(sum(results), 1); self.assertLessEqual(first.total_size(first.root), first.cap); self.assertEqual(len(first.export(a)['log']) + len(second.export(b)['log']), sum(results))
    def test_freeze_blocks_other_runs_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); token = store.create(); runs = [store.create_run(token, rows(period), []) for period in ('2026-08', '2026-09')]
            for run in runs: store.mutate_run(token, run['id'], lambda value, db: (approve(value), [])[1])
            ref = runs[0]['intents'][0]['reference']; store.mutate_run(token, runs[0]['id'], lambda value, db: (mark(value, ref, 'submitted_unknown', 100), [])[1]); restarted = Store(root); self.assertEqual(restarted.downloads(token, runs[1]['id']), [])
            def clear(value, db):
                reconcile(value, [], 101); resolve(value, ref, 'unfreeze'); return []
            restarted.mutate_run(token, runs[0]['id'], clear); self.assertEqual(len(restarted.downloads(token, runs[1]['id'])), 1)
    def test_retention_tracks_reads_and_sweep_deletes_idle_workspace(self):
        now = [1000]
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, clock=lambda: now[0]); token = store.create(); now[0] += 89 * 86400; store.meta(token); now[0] += 2 * 86400; store.sweep(); store.resolve(token); now[0] += 89 * 86400; store.sweep()
            with self.assertRaises(KeyError): store.resolve(token)

class HTTPReviewFixTests(test_app.AppTests):
    def test_creation_actor_is_wired_and_headers_cannot_spoof_it(self):
        for _ in range(10): self.workspace()
        self.assertEqual(self.request('POST', '/w', extra={'X-Forwarded-For': '192.0.2.5', 'X-Real-IP': '192.0.2.5'})[0], 429); connection = http.client.HTTPConnection(*self.server.server_address, source_address=('127.0.0.2', 0), timeout=3)
        try:
            connection.request('POST', '/w', b'', {'Host': self.host, 'Origin': 'http://' + self.host, 'Content-Type': 'application/x-www-form-urlencoded'}); response = connection.getresponse(); self.assertEqual(response.status, 303); response.read()
        finally: connection.close()
    def test_trusted_proxy_identity_requires_explicit_configuration(self):
        self.server.trusted_proxies = frozenset({'127.0.0.1'}); self.assertEqual(self.request('POST', '/w')[0], 400); self.assertEqual(self.request('POST', '/w', extra={'X-Real-IP': '192.0.2.1, 192.0.2.2'})[0], 400)
        with patch.object(self.server.store, 'create', wraps=self.server.store.create) as create:
            self.assertEqual(self.request('POST', '/w', extra={'X-Real-IP': '192.0.2.1'})[0], 303); self.assertEqual(create.call_args.kwargs['actor'], '192.0.2.1'); self.assertEqual(create.call_args.kwargs['global_limit'], 60)
    def test_slow_headers_have_absolute_deadline_and_peer_reservation(self):
        self.server.request_deadline = .35; self.server.max_client_workers = 1; slow = socket.create_connection(self.server.server_address, timeout=2)
        try:
            slow.sendall(b'GET / HTTP/1.1\r\nHost: test.local\r\nX-Slow: '); deadline = time.monotonic() + 1
            while not self.server._clients and time.monotonic() < deadline: time.sleep(.005)
            self.assertEqual(self.request('GET', '/healthz')[0], 503); other = http.client.HTTPConnection(*self.server.server_address, timeout=2, source_address=('127.0.0.2', 0))
            try:
                other.request('GET', '/healthz', headers={'Host': self.host}); response = other.getresponse(); self.assertEqual((response.status, response.read()), (200, b'ok'))
            finally: other.close()
            for _ in range(6):
                time.sleep(.08)
                try: slow.sendall(b'x')
                except OSError: break
            self.assertEqual(slow.recv(1), b'')
        finally: slow.close()
        deadline = time.monotonic() + 1
        while self.server._clients and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(self.server._clients, {}); self.assertEqual(self.request('GET', '/healthz')[0], 200)
    def test_downloads_use_stripe_form_parameters(self):
        _, run_path, refs = self.batch(); parameters = json.loads(self.request('GET', run_path + '/grants.json')[1])['data'][0]
        self.assertEqual(parameters['amount[type]'], 'monetary'); self.assertEqual(parameters['amount[monetary][value]'], 200); self.assertEqual(parameters['amount[monetary][currency]'], 'usd'); self.assertEqual(parameters['applicability_config[scope][price_type]'], 'metered'); self.assertEqual(parameters['category'], 'promotional')
        self.assertEqual(parameters['metadata[rr_ref]'], refs[0]); self.assertEqual(parameters['Idempotency-Key'], refs[0]); self.assertNotIn('amount_cents', parameters)
        csv_text = self.request('GET', run_path + '/grants.csv')[1].decode().split('\n', 1)[1]; csv_row = next(csv.DictReader(io.StringIO(csv_text))); self.assertEqual(csv_row, {key: str(value) for key, value in parameters.items()})
    def test_http_old_capture_and_incomplete_page_are_frozen(self):
        _, run_path, refs = self.batch()
        with patch.object(self.server.store, 'clock', return_value=100): self.assertEqual(self.request('POST', run_path + '/mark', {'reference': refs[0], 'mark': 'submitted_unknown'})[0], 303)
        with patch.object(self.server.store, 'clock', return_value=200):
            for fields in ({'paste': '[]', 'captured_at': '99', 'complete': 'yes'}, {'paste': '{"data":[],"has_more":true}', 'captured_at': '101', 'complete': 'yes'}, {'paste': '[]', 'captured_at': '101'}, {'paste': '[]', 'complete': 'yes'}):
                self.assertEqual(self.request('POST', run_path + '/grants', fields)[0], 303); self.assertEqual(self.request('POST', run_path + '/resolve', {'reference': refs[0], 'action': 'unfreeze'})[0], 400); self.assertEqual(json.loads(self.request('GET', run_path + '/grants.json')[1])['data'], [])
            self.assertEqual(self.request('POST', run_path + '/grants', {'paste': '[]', 'captured_at': '201', 'complete': 'yes'})[0], 400); self.assertEqual(self.request('POST', run_path + '/grants', {'paste': '[]', 'captured_at': '101', 'complete': 'yes'})[0], 303)
            self.assertEqual(self.request('POST', run_path + '/resolve', {'reference': refs[0], 'action': 'unfreeze'})[0], 303); self.assertEqual(len(json.loads(self.request('GET', run_path + '/grants.json')[1])['data']), 1)
    def test_http_cross_run_freeze_is_enforced(self):
        path, first, refs = self.batch(); usage = 'account_id,period,calls,spend,billing_customer_id,account_age_days\na0,2026-10,200000,100,cus_0,30'; status, _, second = self.request('POST', path + '/usage', {'paste': usage})
        self.assertEqual(status, 303); self.assertEqual(self.request('POST', second + '/approve')[0], 303); self.assertEqual(self.request('POST', first + '/mark', {'reference': refs[0], 'mark': 'submitted_unknown'})[0], 303); self.assertEqual(json.loads(self.request('GET', second + '/grants.json')[1])['data'], [])
    def test_review_notes_are_escaped_on_owner_and_shared_pages(self):
        path = self.workspace(); usage = 'account_id,period,calls,spend,billing_customer_id\na,2026-09,200000,100,cus_a'; _, _, run_path = self.request('POST', path + '/usage', {'paste': usage}); token, run_id = path.split('/')[-1], run_path.split('/')[-1]
        ref = self.server.store.run(token, run_id)['intents'][0]['reference']; note = '<script>alert("x")</script>'; self.assertEqual(self.request('POST', run_path + '/review', {'reference': ref, 'decision': 'approve', 'note': note})[0], 303); share = self.server.store.rotate_share(token)
        for url in (run_path, '/r/' + share + '/runs/' + run_id):
            body = self.request('GET', url)[1]; self.assertIn(app.esc(note).encode(), body); self.assertNotIn(note.encode(), body)
    def test_retention_notice_is_on_all_user_pages(self):
        path, run_path, _ = self.batch()
        for url in ('/', path, run_path, path + '/log'): self.assertIn(app.RETENTION.encode(), self.request('GET', url)[1])

if __name__ == '__main__': unittest.main()
