import concurrent.futures, copy, http.client, json, os, re, socket, sqlite3, tempfile, threading, time, unittest, urllib.parse
from pathlib import Path
from unittest.mock import patch
import app
from rebate_rail.intent_log import IntentLog
from rebate_rail.parse import parse_grants, parse_references
from rebate_rail.plan import approve, build_run, downloadable, mark, reconcile, resolve
from rebate_rail.workspace import RateLimited, Store

class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.host = 'test.local'; self.server = app.make_server(('127.0.0.1', 0), self.temp.name, 'http://' + self.host)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join(2); self.temp.cleanup()
    def request(self, method, path, fields=None, host=None, origin=True, extra=None):
        body = urllib.parse.urlencode(fields or {}).encode() if method == 'POST' else None; headers = {'Host': host or self.host}
        if method == 'POST':
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            if origin: headers['Origin'] = 'http://' + self.host
        headers.update(extra or {}); connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5); connection.request(method, path, body, headers)
        response = connection.getresponse(); self.last_headers = dict(response.getheaders()); result = response.status, response.read(), response.getheader('Location'); connection.close(); return result
    def workspace(self):
        status, _, location = self.request('POST', '/w'); self.assertEqual(status, 303); return location
    def batch(self, count=1):
        path = self.workspace(); usage = 'account_id,period,calls,spend,billing_customer_id,account_age_days\n' + '\n'.join(f'a{i},2026-09,200000,100,cus_{i},30' for i in range(count))
        status, _, run_path = self.request('POST', path + '/usage', {'paste': usage}); self.assertEqual(status, 303); self.assertEqual(self.request('POST', run_path + '/approve')[0], 303)
        values = json.loads(self.request('GET', run_path + '/grants.json')[1])['data']; return path, run_path, [v['metadata[rr_ref]'] for v in values]
    def test_health_notice_host_origin_and_unknown(self):
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, b'ok')); path = self.workspace(); status, body, _ = self.request('GET', path)
        self.assertEqual(status, 200); self.assertIn(app.NOTICE.encode(), body); self.assertIn(b'only key', body); self.assertEqual(self.request('GET', '/', host='wrong.local')[0], 421); self.assertEqual(self.request('POST', '/w', origin=False)[0], 403)
        before = set(self.server.store.locks); self.assertEqual(self.request('GET', '/w/nope')[0], 404); self.assertEqual(set(self.server.store.locks), before)
    def test_form_referrer_policy_preserves_origin_and_rejects_foreign_posts(self):
        self.request('GET', '/'); self.assertEqual(self.last_headers['Referrer-Policy'], 'same-origin'); self.assertEqual(self.request('POST', '/w', origin=False, extra={'Referer': 'http://test.local/'})[0], 303)
        for origin in ('null', 'https://evil.invalid', 'http://test.local.evil.invalid'): self.assertEqual(self.request('POST', '/w', extra={'Origin': origin, 'Referer': 'http://test.local/'})[0], 403)
        self.assertEqual(self.request('POST', '/w', origin=False, extra={'Referer': 'http://test.local.evil.invalid/'})[0], 403); self.batch()
    def test_shared_proxy_peer_can_use_more_than_four_workers(self):
        peers = []
        try:
            for _ in range(6):
                peer = socket.create_connection(self.server.server_address, timeout=2); peers.append(peer); peer.sendall(b'GET / HTTP/1.1\r\nHost: test.local\r\n')
            self.assertEqual(self.request('GET', '/healthz')[:2], (200, b'ok'))
        finally:
            for peer in peers: peer.close()
    def test_global_worker_cap_still_rejects_and_recovers(self):
        for _ in range(32): self.assertTrue(self.server.slots.acquire(False))
        try: self.assertEqual(self.request('GET', '/healthz')[0], 503)
        finally:
            for _ in range(32): self.server.slots.release()
        self.assertEqual(self.request('GET', '/healthz')[0], 200)
    def test_upload_progress_and_clear_idle_timeout(self):
        self.assertEqual((self.server.idle_timeout, self.server.upload_timeout), (30, 300)); self.assertEqual(self.server.request_deadline, 15); self.server.idle_timeout, self.server.upload_timeout = .25, 2
        body = b'x=' + b'a' * 8; connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.putrequest('POST', '/w', skip_host=True)
            for key, value in {'Host': self.host, 'Origin': 'http://' + self.host, 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': str(len(body))}.items(): connection.putheader(key, value)
            connection.endheaders()
            for offset in range(0, len(body), 2): connection.send(body[offset:offset + 2]); time.sleep(.08)
            response = connection.getresponse(); self.assertEqual(response.status, 303); response.read()
        finally: connection.close()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request('POST', '/w', b'x=', {'Host': self.host, 'Origin': 'http://' + self.host, 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': '10'})
            response = connection.getresponse(); self.assertEqual(response.status, 408); self.assertIn(b'No changes were saved', response.read())
        finally: connection.close()
    def test_flow_persistence_export_and_delete(self):
        path = self.workspace(); usage = 'account_id,period,calls,spend,billing_customer_id,account_age_days\na,2026-09,200000,100.00,cus_a,30\nb,wrong,2,1,cus_b,30\n'
        status, _, run_path = self.request('POST', path + '/usage', {'paste': usage}); self.assertEqual(status, 303); _, body, _ = self.request('GET', run_path); self.assertIn(b'not_evaluated', body); self.assertIn(b'Dropped rows', body)
        self.assertEqual(self.request('POST', run_path + '/approve')[0], 303); status, csv_body, _ = self.request('GET', run_path + '/grants.csv'); self.assertEqual(status, 200)
        values = csv_body.decode().splitlines()[2].split(','); self.assertEqual(values[4], values[5]); token = path.split('/')[2]; self.assertNotIn(token.encode(), self.request('GET', path + '/export')[1])
        for root, dirs, files in os.walk(self.temp.name):
            self.assertNotIn(token, root)
            for name in files: self.assertNotIn(token, name); self.assertNotIn(token.encode(), Path(root, name).read_bytes())
        self.assertEqual(self.request('POST', path + '/delete')[0], 303); self.assertEqual(self.request('POST', path + '/rule', {})[0], 404)
    def test_resolution_controls_and_download_filter_are_wired(self):
        _, run_path, refs = self.batch(); self.assertNotIn(b'/resolve', self.request('GET', run_path)[1]); self.assertEqual(self.request('POST', run_path + '/mark', {'reference': refs[0], 'mark': 'submitted_unknown'})[0], 303)
        self.assertEqual(json.loads(self.request('GET', run_path + '/grants.json')[1])['data'], []); self.assertNotIn(b'/resolve', self.request('GET', run_path)[1]); self.assertEqual(self.request('POST', run_path + '/resolve', {'reference': refs[0], 'action': 'unfreeze'})[0], 400)
    def test_bulk_marks_are_atomic_audited_and_persisted(self):
        path, run_path, refs = self.batch(3); before = self.request('GET', path + '/export')[1]; self.assertEqual(self.request('POST', run_path + '/mark', {'references': refs[0] + '\nci_missing', 'mark': 'submitted_ok'})[0], 400)
        self.assertEqual(self.request('GET', path + '/export')[1], before); self.assertEqual(self.request('POST', run_path + '/mark', {'references': 'reference\n' + '\n'.join(refs), 'mark': 'submitted_unknown'})[0], 303)
        export = json.loads(self.request('GET', path + '/export')[1]); self.assertEqual(sum(e['event_type'] == 'MARK_SUBMITTED_UNKNOWN' for e in export['log']), 3); self.assertEqual(json.loads(self.request('GET', run_path + '/grants.json')[1])['data'], [])
    def test_partial_grants_report_drops_and_do_not_authorize_absence(self):
        _, run_path, refs = self.batch(2); grants = [{'id': 'g1', 'customer': 'cus_0', 'amount_cents': 200, 'currency': 'usd', 'rr_ref': refs[0]}, {'id': 'bad@email.invalid', 'amount_cents': 1}]
        self.assertEqual(self.request('POST', run_path + '/grants', {'paste': json.dumps(grants)})[0], 303); body = self.request('GET', run_path)[1]; self.assertIn(b'Dropped grants', body); self.assertIn(b'do_not_retry_freeze', body); self.assertNotIn(b'bad@email.invalid', body); self.assertEqual(json.loads(self.request('GET', run_path + '/grants.json')[1])['data'], [])
    def test_audit_verification_success_and_failure_are_explicit(self):
        path, _, _ = self.batch(); self.assertIn(b'Hash chain verified: True', self.request('GET', path + '/log')[1]); export = json.loads(self.request('GET', path + '/export')[1]); export['log'][0]['data']['tampered'] = True
        with patch.object(self.server.store, 'export', return_value=export):
            status, body, _ = self.request('GET', path + '/log'); self.assertEqual(status, 200); self.assertIn(b'Hash chain verified: False', body); self.assertIn(b'Verification failed', body)
        with patch.object(self.server.store, 'export', side_effect=ValueError('hash chain verification failed')): self.assertIn(b'Verification failed', self.request('GET', path + '/log')[1])
    def test_rule_display_uses_percentages_and_clean_multipliers(self):
        path = self.workspace(); fields = dict(re.findall(r'<input name="([^"]+)" value="([^"]+)"', self.request('GET', path)[1].decode()))
        self.assertEqual(fields['max_top_fingerprint_share_percent'], '50'); self.assertEqual(fields['max_error_rate_percent'], '30'); self.assertEqual(fields['max_prior_period_multiplier'], '5'); fields['max_error_rate_percent'] = '12.5'
        self.assertEqual(self.request('POST', path + '/rule', fields)[0], 303); self.assertEqual(self.server.store.rule(path.split('/')[2])['max_error_rate'], .125)
    def test_rule_validation_and_rate_limit_http_status(self):
        path = self.workspace(); self.assertEqual(self.request('POST', path + '/rule', dict(app.DEFAULT_RULE, cap_cents=-1))[0], 400)
        with patch.object(self.server.store, 'create', side_effect=RateLimited('rate limit')): self.assertEqual(self.request('POST', '/w')[0], 429)

class ReviewLogicTests(unittest.TestCase):
    def test_grant_diagnostics_json_csv_and_privacy(self):
        good = {'id': 'g', 'customer': 'cus_a', 'amount_cents': 200, 'currency': 'usd', 'expires_at': 0, 'rr_ref': 'other_system'}
        for value in ([good, dict(good, id='bad id'), dict(good, customer='private@example.invalid')], {'data': [dict(good, amount_cents='wrong'), good]}):
            dropped = []; self.assertEqual(parse_grants(json.dumps(value), dropped), [good]); self.assertTrue(dropped); self.assertNotIn('private@example.invalid', json.dumps(dropped))
        dropped = []; parsed = parse_grants('id,customer,amount_cents,currency,expires_at,rr_ref\ng,cus_a,200,usd,0,ci_a\nbad id,cus_a,1,usd,0,ci_b', dropped); self.assertEqual(len(parsed), 1); self.assertEqual(dropped[0]['line'], 3)
        with self.assertRaises(ValueError): parse_grants(json.dumps([dict(good, id='bad id')]))
        with self.assertRaises(ValueError): parse_grants(json.dumps([good] * 5001))
    def test_reference_lists_csv_caps_and_duplicate_handling(self):
        for text in ('ci_a,ci_b\nci_a', 'reference\nci_a\nci_b', 'customer,metadata[rr_ref],Idempotency-Key\ncus_a,ci_a,ci_a\ncus_b,ci_b,ci_b'): self.assertEqual(parse_references(text), ['ci_a', 'ci_b'])
        for text in ('', 'ci_a,', 'not_a_reference', 'reference\n"unterminated', 'metadata[rr_ref],Idempotency-Key\nci_a,ci_b', '\n'.join('ci_' + str(i) for i in range(5001))):
            with self.subTest(text=text[:40]), self.assertRaises(ValueError): parse_references(text)
    def test_bulk_5000_marks_and_failure_leave_no_partial_mutation(self):
        run = build_run(StorageRegressionTests.rows(5000), app.DEFAULT_RULE); approve(run); refs = [item['reference'] for item in run['intents']]; before = copy.deepcopy(run)
        with self.assertRaises(ValueError): app.bulk_mark(run, refs + ['ci_missing'], 'submitted_unknown', 100)
        self.assertEqual(run, before); records = app.bulk_mark(run, refs, 'submitted_unknown', 100); self.assertEqual(len(records), 5000); self.assertTrue(all(item['frozen'] for item in run['intents'])); self.assertEqual(downloadable(run), [])
    def test_resolution_actions_follow_evidence_and_resolution(self):
        run = build_run(StorageRegressionTests.rows(1), app.DEFAULT_RULE); approve(run); item = run['intents'][0]; ref = item['reference']; self.assertEqual(app.resolution_actions(run, item), [])
        mark(run, ref, 'submitted_unknown', 100); self.assertEqual(app.resolution_actions(run, item), []); reconcile(run, [], 101); self.assertEqual(dict(app.resolution_actions(run, item)), {'void': 'Void', 'unfreeze': 'Unfreeze'})
        resolve(run, ref, 'void'); self.assertEqual(app.resolution_actions(run, item), [('unfreeze', 'Unfreeze')]); resolve(run, ref, 'unfreeze'); self.assertEqual(app.resolution_actions(run, item), [])
    def test_dropped_grants_never_authorize_unknown_retry(self):
        run = build_run(StorageRegressionTests.rows(1), app.DEFAULT_RULE); approve(run); item = run['intents'][0]; ref = item['reference']; mark(run, ref, 'submitted_unknown', 100)
        report = app.reconcile_import(run, [], [{'line': 1, 'reason': 'invalid grant id'}], 101, set()); self.assertEqual(report['verdicts'][ref], 'do_not_retry_freeze'); self.assertEqual(app.resolution_actions(run, item), []); self.assertEqual(downloadable(run), [])
        app.reconcile_import(run, [], [], 102, set()); resolve(run, ref, 'unfreeze'); self.assertEqual(downloadable(run), [item])

class StorageRegressionTests(unittest.TestCase):
    def setUp(self): self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.store = Store(self.temp.name); self.token = self.store.create()
    @staticmethod
    def rows(count): return [{'account_id': 'a' + str(i), 'period': '2026-09', 'calls': 200000, 'spend_cents': 10000, 'billing_customer_id': 'cus_' + str(i), 'account_age_days': 30} for i in range(count)]
    @staticmethod
    def approval(run, db):
        approve(run); return [('BATCH_APPROVED', item['reference'], {}) for item in run['intents']]
    def test_5000_rows_use_linear_hashes_and_one_capacity_walk(self):
        with patch.object(self.store, 'total_size', wraps=self.store.total_size) as walks, patch.object(IntentLog, '_digest', wraps=IntentLog._digest) as hashes:
            run = self.store.create_run(self.token, self.rows(5000), []); self.assertEqual(hashes.call_count, 5000); self.assertEqual(walks.call_count, 1); hashes.reset_mock(); walks.reset_mock()
            self.store.mutate_run(self.token, run['id'], self.approval); self.assertEqual(hashes.call_count, 5000); self.assertEqual(walks.call_count, 1)
    def test_log_supports_large_run_approval_marks_and_later_run(self):
        run = self.store.create_run(self.token, self.rows(5000), []); self.store.mutate_run(self.token, run['id'], self.approval); ref = run['intents'][0]['reference']
        def change(value, db):
            mark(value, ref, 'submitted_unknown', 100); return [('MARK_SUBMITTED_UNKNOWN', ref, {})]
        self.store.mutate_run(self.token, run['id'], change); self.store.create_run(self.token, self.rows(1), []); self.assertEqual(len(self.store.export(self.token)['log']), 10002)
    def test_large_run_and_four_full_exports_fit(self):
        run = self.store.create_run(self.token, self.rows(5000), []); self.store.mutate_run(self.token, run['id'], self.approval)
        grants = [{'id': 'g' + str(i), 'customer': item['customer'], 'amount_cents': item['amount_cents'], 'currency': 'usd', 'expires_at': 0, 'rr_ref': item['reference']} for i, item in enumerate(run['intents'])]
        for stamp in range(1, 5):
            def change(value, db):
                reconcile(value, grants, stamp); return [('RECONCILED', value['id'], {'grant_count': len(grants)})]
            self.store.mutate_run(self.token, run['id'], change)
        saved = self.store.run(self.token, run['id']); self.assertEqual([len(x['grants']) for x in saved['grant_exports']], [5000] * 4); self.assertEqual(len(self.store.export(self.token)['log']), 10004)
    def test_concurrent_marks_and_run_limit_are_serialized(self):
        run = self.store.create_run(self.token, self.rows(2), []); self.store.mutate_run(self.token, run['id'], self.approval); barrier = threading.Barrier(2)
        def worker(item):
            barrier.wait()
            def change(value, db):
                mark(value, item['reference'], 'submitted_ok', 100); return [('MARK_SUBMITTED_OK', item['reference'], {})]
            self.store.mutate_run(self.token, run['id'], change)
        with concurrent.futures.ThreadPoolExecutor(2) as pool: list(pool.map(worker, run['intents']))
        self.assertEqual([x['mark'] for x in self.store.run(self.token, run['id'])['intents']], ['submitted_ok'] * 2)
        for _ in range(22): self.store.create_run(self.token, self.rows(1), [])
        barrier = threading.Barrier(2)
        def create(_):
            barrier.wait()
            try: self.store.create_run(self.token, self.rows(1), []); return True
            except ValueError: return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool: self.assertEqual(sorted(pool.map(create, range(2))), [False, True])
        self.assertEqual(len(self.store.runs(self.token)), 24)
    def test_creation_budget_survives_restart_and_delete(self):
        now = [10000]
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, clock=lambda: now[0])
            for _ in range(60): token = store.create(); store.delete(token)
            store = Store(root, clock=lambda: now[0])
            with self.assertRaises(RateLimited): store.create()
            now[0] += 3600; store.create()
    def test_failed_audit_or_capacity_rolls_back_run_and_log(self):
        run = self.store.create_run(self.token, self.rows(1), []); before = self.store.export(self.token); original = self.store._append
        def fail(db, records): original(db, records); raise OSError('simulated interruption')
        with patch.object(self.store, '_append', side_effect=fail), self.assertRaises(OSError): self.store.mutate_run(self.token, run['id'], self.approval)
        self.assertEqual(self.store.export(self.token), before); self.store.cap = 1
        with self.assertRaises(RuntimeError): self.store.mutate_run(self.token, run['id'], self.approval)
        self.assertEqual(self.store.export(self.token), before)
    def test_uncommitted_sqlite_write_is_recovered_and_chain_verifies(self):
        self.store.append_log(self.token, 'PROPOSED', 'ci_a', {}); _, path = self.store.resolve(self.token); db = sqlite3.connect(path / 'workspace.sqlite3'); db.execute('INSERT INTO audit VALUES (?,?)', (2, '{partial')); db.close()
        restarted = Store(self.temp.name); restarted.append_log(self.token, 'BATCH_APPROVED', 'ci_a', {}); self.assertEqual(len(restarted.export(self.token)['log']), 2)
    def test_legacy_partial_tail_migrates_without_poisoning_next_append(self):
        _, path = self.store.resolve(self.token); (path / 'workspace.sqlite3').unlink(); (path / 'runs').mkdir(); (path / 'meta.json').write_text(json.dumps({'created_at': 1, 'updated_at': self.store.clock(), 'written': True})); (path / 'rule.json').write_text(json.dumps(app.DEFAULT_RULE))
        log = IntentLog(path / 'log.jsonl'); log.append('PROPOSED', 'ci_a', {})
        with open(path / 'log.jsonl', 'ab') as out: out.write(b'{"partial":')
        self.store.append_log(self.token, 'BATCH_APPROVED', 'ci_a', {}); self.assertEqual(len(self.store.export(self.token)['log']), 2)

if __name__ == '__main__': unittest.main()
