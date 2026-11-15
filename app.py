import copy
import csv
import datetime
import html
import io
import ipaddress
import json
import math
import os
import socket
import threading
import time
import urllib.parse
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from rebate_rail.intent_log import Entry, IntentLog
from rebate_rail.parse import decode_upload, parse_grants, parse_references, parse_usage
from rebate_rail.plan import DEFAULT_RULE, approve, mark, reconcile, resolve, review, validate_rule
from rebate_rail.workspace import RateLimited, Store

MAX_BODY = 4 * 1024 * 1024
NOTICE = 'Operated by AI agents. Early version: keep your own copy of anything important. Do not enter customer names, emails or other personal data, card or bank details, or API keys. Nothing is charged.'
MODEL = 'No AI model is used; all results come from fixed rules shown on the page.'
RETENTION = 'Workspaces are deleted after 90 days without use. Export your own copy before then.'
SHARES = {'max_top_fingerprint_share', 'max_error_rate'}
GRANT_FIELDS = ['customer', 'amount[type]', 'amount[monetary][value]', 'expires_at', 'metadata[rr_ref]', 'Idempotency-Key', 'amount[monetary][currency]', 'category', 'applicability_config[scope][price_type]']


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body, sample=False):
    style = 'body{font:15px system-ui;max-width:1200px;margin:auto;padding:1rem}header{background:#fff4bd;padding:12px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px}textarea{width:100%;height:9rem}input{max-width:100%}form{margin:.7rem 0}code{overflow-wrap:anywhere}'
    flag = '<p>Made-up sample</p>' if sample else ''
    return f'<!doctype html><html><head><meta charset=utf-8><title>{esc(title)}</title><style>{style}</style></head><body><header>{esc(NOTICE)} <a href="https://manykiln.com/privacy">Privacy</a><br>{esc(MODEL)}<br>{esc(RETENTION)}</header>{flag}<h1>{esc(title)}</h1>{body}</body></html>'.encode()


def form(action, label, fields='', upload=False):
    enctype = ' enctype="multipart/form-data"' if upload else ''
    return f'<form method=post action="{esc(action)}"{enctype}>{fields}<button>{esc(label)}</button></form>'


def table(headers, rows):
    return '<table><tr>' + ''.join('<th>' + esc(x) + '</th>' for x in headers) + '</tr>' + rows + '</table>'


def supplied(fields, name):
    return (fields.get(name) or [''])[0]


def rule_inputs(rule):
    result = ''
    for key, value in rule.items():
        name, label = key, key.replace('_', ' ')
        if key in SHARES:
            name, label, value = key + '_percent', label + ' (%)', value * 100
        elif key.endswith('multiplier'):
            label += ' (times)'
        value = str(int(value)) if float(value).is_integer() else str(value)
        result += f'<label>{esc(label)} <input name="{esc(name)}" value="{esc(value)}"></label><br>'
    return result


def rule_from_fields(fields):
    values = {key: supplied(fields, key) for key in DEFAULT_RULE}
    for key in SHARES:
        if key + '_percent' in fields:
            values[key] = float(supplied(fields, key + '_percent')) / 100
    return validate_rule(values)


def capture_time(value, now):
    if not value.strip():
        return None
    try:
        try:
            stamp = float(value)
        except ValueError:
            parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('include UTC offset')
            stamp = parsed.timestamp()
        if not math.isfinite(stamp) or not 0 <= stamp <= now:
            raise ValueError('invalid capture time')
        return stamp
    except (ValueError, OverflowError) as exc:
        raise ValueError('Capture time must be a past UTC time, such as 2026-10-01T12:00:00Z, or Unix seconds') from exc


def grant_parameters(item):
    return dict(zip(GRANT_FIELDS, [item['customer'], 'monetary', item['amount_cents'], item['expires_at'],
                    item['reference'], item['reference'], item.get('currency', 'usd'), 'promotional', 'metered']))


def fields_body(handler):
    if handler.headers.get('Transfer-Encoding'):
        raise ValueError('Transfer-Encoding is unsupported')
    length = int(handler.headers.get('Content-Length', '-1'))
    if length < 0:
        raise ValueError('Content-Length required')
    if length > MAX_BODY:
        raise OverflowError
    deadline, raw = time.monotonic() + min(handler.server.upload_timeout, handler.server.request_deadline), bytearray()
    while len(raw) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('upload deadline exceeded')
        handler.connection.settimeout(min(handler.server.idle_timeout, remaining))
        chunk = handler.rfile.read1(min(65536, length - len(raw)))
        if not chunk:
            raise ValueError('incomplete body')
        raw.extend(chunk)
    raw, content_type = bytes(raw), handler.headers.get('Content-Type', '')
    if content_type.startswith('multipart/form-data'):
        message = BytesParser(policy=default).parsebytes(('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + raw)
        if not message.is_multipart():
            raise ValueError('invalid multipart body')
        result = {}
        for index, part in enumerate(message.iter_parts()):
            if index >= 40 or part.is_multipart():
                raise ValueError('too many or nested form fields')
            name = part.get_param('name', header='content-disposition')
            if name and name not in result:
                result[name] = [decode_upload(part.get_payload(decode=True) or b'')]
        return result
    if not content_type.startswith('application/x-www-form-urlencoded'):
        raise ValueError('unsupported form encoding')
    return urllib.parse.parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=40, errors='strict')


def bulk_mark(run, references, value, now):
    candidate = copy.deepcopy(run)
    indexed = {item['reference']: item for item in candidate['intents'] if item['reference']}
    records = []
    for reference in references:
        item = indexed.get(reference)
        if item is None:
            raise ValueError('reference does not belong to this run')
        mark({'intents': [item], 'report': candidate.get('report')}, reference, value, now)
        records.append(('MARK_' + value.upper(), reference, {'account_id': item['account_id']}))
    run.clear()
    run.update(candidate)
    return records


def resolution_actions(run, item):
    if not item['reference']:
        return []
    verdict = (run.get('report') or {}).get('verdicts', {}).get(item['reference'])
    actions = []
    if verdict in ('matched', 'amount_mismatch', 'duplicate_on_billing') and not item.get('resolution'):
        actions.append(('posted', 'Mark posted'))
    if verdict == 'safe_to_resubmit_same_key' and not item.get('billing_seen') and not item.get('resolution'):
        actions.append(('void', 'Void'))
    if item.get('frozen') and (item.get('resolution') or verdict in ('matched', 'safe_to_resubmit_same_key')):
        actions.append(('unfreeze', 'Unfreeze'))
    return actions


def reconcile_import(run, grants, dropped, captured_at, known, *, uploaded_at=None, complete=True):
    report = reconcile(run, grants, captured_at, known, uploaded_at=uploaded_at,
                       complete=complete and not dropped)
    report['dropped'] = dropped
    return report


class CappedServer(ThreadingHTTPServer):
    daemon_threads, block_on_close, idle_timeout, upload_timeout = True, False, 30, 300
    request_deadline, max_client_workers = 15, 8

    def __init__(self, address, handler, store, public_base, trusted_proxies=()):
        self.store, self.public_base = store, public_base.rstrip('/')
        parsed = urllib.parse.urlsplit(self.public_base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('PUBLIC_BASE_URL must be an HTTP(S) origin')
        self.public_origin, self.public_host = self.public_base, parsed.hostname.lower()
        self.public_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        self.trusted_proxies = frozenset(str(ipaddress.ip_address(x)) for x in trusted_proxies)
        self.slots = threading.BoundedSemaphore(32)
        self._workers_lock = threading.Lock()
        self._clients, self._timers = {}, {}
        super().__init__(address, handler)

    @staticmethod
    def expire(request):
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def release(self, request):
        with self._workers_lock:
            pending = self._timers.pop(request, None)
            if pending:
                timer, peer = pending
                timer.cancel()
                self._clients[peer] -= 1
                if not self._clients[peer]:
                    del self._clients[peer]
                self.slots.release()

    def process_request(self, request, client_address):
        peer = client_address[0]
        with self._workers_lock:
            accepted = self._clients.get(peer, 0) < self.max_client_workers and self.slots.acquire(False)
            if accepted:
                timer = threading.Timer(self.request_deadline, self.expire, (request,))
                timer.daemon = True
                self._clients[peer] = self._clients.get(peer, 0) + 1
                self._timers[request] = (timer, peer)
        if not accepted:
            try:
                request.setblocking(False)
                request.send(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            request.settimeout(self.idle_timeout)
            timer.start()
            super().process_request(request, client_address)
        except Exception:
            self.release(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release(request)

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = 'rebatelane/3'

    def log_message(self, format, *args):
        pass

    def actor(self):
        peer = str(ipaddress.ip_address(self.client_address[0]))
        if peer in self.server.trusted_proxies:
            # The trusted proxy must overwrite this header with one address.
            value = self.headers.get('X-Real-IP', '')
            try:
                return str(ipaddress.ip_address(value))
            except ValueError as exc:
                raise ValueError('Trusted proxy must supply a single valid X-Real-IP') from exc
        return peer

    def _host_ok(self):
        value = self.headers.get('Host', '').strip().lower()
        if not value or any(char in value for char in '\r\n/@'):
            return False
        try:
            parsed = urllib.parse.urlsplit('//' + value)
            port = parsed.port or (443 if self.server.public_base.startswith('https:') else 80)
            return parsed.hostname == self.server.public_host and port == self.server.public_port
        except ValueError:
            return False

    def _origin_ok(self):
        origin = self.headers.get('Origin')
        return origin == self.server.public_origin if origin is not None else self.headers.get('Referer', '').startswith(self.server.public_origin + '/')

    def send_value(self, status, body=b'', content_type='text/html; charset=utf-8', location=None, filename=None):
        self.send_response(status)
        headers = {'Content-Type': content_type, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin', 'Content-Security-Policy': "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'"}
        for key, value in headers.items():
            self.send_header(key, value)
        if location:
            self.send_header('Location', location)
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def error(self, status, message):
        self.send_value(status, page('Request failed', '<p>' + esc(message) + '</p>'))

    def redirect(self, location):
        self.send_value(303, location=location)

    def dashboard(self, base, data, readonly=False):
        rule, runs = data['rule'], data['runs']
        body = '<p>Prepare one period of promotional USD credits. Usage spend and caps are in USD; exports in other currencies never match USD intents. Nothing is sent.</p>'
        if not readonly:
            body += '<p><strong>Bookmark this link: it is the only key and cannot be recovered.</strong></p>'
        body += '<h2>Earn rule</h2><p>Defaults are made-up example values. Credit = min(spend × rebate_bps / 10000, cap), rounded down to cents. Missing abuse signals are not evaluated. Missing account age holds the row for review when a minimum age is required. Missing billing customer IDs are skipped. An account freeze blocks downloads across every run.</p>'
        if readonly:
            body += '<pre>' + esc(json.dumps(rule, indent=2)) + '</pre>'
        else:
            body += form(base + '/rule', 'Save rule', rule_inputs(rule))
            body += '<p>At most 1 MB and 5,000 rows per file, one period per run, 24 runs per workspace. UTF-8, BOM-marked UTF-16 and Windows-1252 files are accepted.</p>'
            body += form(base + '/usage', 'Build batch', '<textarea name=paste placeholder="Paste usage CSV"></textarea><input type=file name=file>', True)
        rows = ''.join(f'<tr><td><a href="{esc(base + "/runs/" + run["id"])}">{esc(run["id"])}</a></td><td>{esc(run["period"])}</td><td>{esc(run["status"])}</td></tr>' for run in runs)
        body += '<h2>Runs</h2>' + table(('Run', 'Period', 'Status'), rows) + '<h2>Needs review</h2>'
        needs = [(run, item) for run in runs for item in run['intents'] if item['state'] == 'HELD_ABUSE' or item.get('frozen')]
        body += (''.join(f'<p><a href="{esc(base + "/runs/" + run["id"])}">{esc(item["reference"] or item["account_id"])}</a></p>' for run, item in needs) or '<p>None</p>')
        if not readonly:
            body += form(base + '/share', 'Create or rotate read-only link') + f'<p><a href="{esc(base + "/export")}">Export everything</a> · <a href="{esc(base + "/log")}">Verify audit log</a></p>' + form(base + '/delete', 'Delete this workspace now')
        return page('rebatelane workspace', body, data.get('sample', False))

    def run_page(self, base, run, sample=False, readonly=False):
        rows = ''
        for item in run['intents']:
            actions = ''
            hidden = f'<input type=hidden name=reference value="{esc(item["reference"])}">'
            if not readonly and item['state'] == 'HELD_ABUSE':
                for decision in ('approve', 'reject'):
                    actions += form(base + '/review', decision.title(), hidden + f'<input type=hidden name=decision value={decision}><input name=note maxlength=200 placeholder="Optional note">')
            if not readonly:
                for action, label in resolution_actions(run, item):
                    actions += form(base + '/resolve', label, hidden + f'<input type=hidden name=action value={action}>')
            values = (item['account_id'], f'{item["spend_cents"] / 100:.2f}', f'{item["amount_cents"] / 100:.2f}', f'{item["cap_cents"] / 100:.2f}', time.strftime('%Y-%m-%d', time.gmtime(item['expires_at'])), item['state'], item.get('mark', ''), item.get('freeze_reason', '') if item.get('frozen') else '', ', '.join(item['reasons']), item['reference'], item.get('review_note', ''))
            rows += '<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in values) + '<td>' + actions + '</td></tr>'
        body = f'<p>Status: {esc(run["status"])}. All amounts are USD. Account freezes in any run block downloads for that account.</p>' + table(('Account', 'Spend', 'Credit', 'Cap', 'Expiry UTC', 'State', 'Submission', 'Freeze', 'Reasons', 'Reference', 'Review note', 'Review'), rows)
        if run['dropped']:
            body += '<h2>Dropped rows</h2>' + self.dropped_table(run['dropped'])
        if not readonly:
            body += form(base + '/approve', 'Approve batch') + f'<p><a href="{esc(base + "/grants.csv")}">Download grants CSV</a> · <a href="{esc(base + "/grants.json")}">Download grants JSON</a></p>'
            body += '<p>Downloads contain Stripe form parameter names. Send Idempotency-Key as an HTTP header, and the other row fields as form parameters. This app sends nothing. Mark attempted submissions immediately.</p>'
            body += form(base + '/mark', 'Save submission marks', '<textarea name=references placeholder="References separated by commas or newlines"></textarea><input type=file name=file><select name=mark><option>submitted_ok</option><option>submitted_unknown</option><option>rejected</option></select>', True)
            body += '<p>Unknown outcomes freeze immediately. A retry requires a complete export captured after the submission, followed by explicit Unfreeze. Upload time is never used as capture time.</p><h2>Reconcile grants export</h2><p>Paste a grant array, CSV, or every Stripe list page in order. Multiple JSON page objects may be separated by whitespace, or supplied as an array of pages. The final page must have has_more:false. The combined input limit is 1 MB and 5,000 grants. Use the time the first page was captured. An incomplete, filtered, undated or unconfirmed export cannot authorize absent credits.</p>'
            fields = '<textarea name=paste></textarea><input type=file name=file><label>Export capture time, UTC <input name=captured_at placeholder="2026-10-01T12:00:00Z"></label><label><input type=checkbox name=complete value=yes>I included all pages and all relevant customers, without filters or omissions.</label>'
            body += form(base + '/grants', 'Reconcile', fields, True)
        if run.get('report'):
            body += '<h2>Reconciliation</h2>' + self.report_body(run['report'])
        return page(run['id'], body, sample)

    @staticmethod
    def dropped_table(dropped):
        return table(('Line or item', 'Reason'), ''.join(f'<tr><td>{esc(x["line"])}</td><td>{esc(x["reason"])}</td></tr>' for x in dropped))

    @staticmethod
    def report_body(report):
        rows = ''.join(f'<tr><td>{esc(ref)}</td><td>{esc(verdict)}</td></tr>' for ref, verdict in report['verdicts'].items())
        body = '<p>Capture time: ' + esc(report.get('captured_at')) + '. Complete evidence: ' + esc(report.get('complete', False)) + '</p>'
        body += table(('Reference', 'Retry verdict'), rows) + ''.join(f'<h3>{esc(name)} ({len(refs)})</h3><p>{esc(", ".join(refs) or "None")}</p>' for name, refs in report['buckets'].items())
        if not report.get('complete', False):
            body += '<p>This export does not establish completeness and capture time. Absent references are frozen; upload a corrected complete export before creating or retrying them.</p>'
        if report.get('dropped'):
            body += '<h3>Dropped grants</h3>' + Handler.dropped_table(report['dropped'])
        return body

    def audit_page(self, token):
        try:
            entries = self.server.store.export(token)['log']
            log = IntentLog()
            log._entries = [Entry(**entry) for entry in entries]
            verified = log.verify()
        except (ValueError, TypeError, AttributeError):
            entries, verified = [], False
        rows = ''.join(f'<tr><td>{esc(entry["seq"])}</td><td>{esc(entry["event_type"])}</td><td>{esc(entry["intent_id"])}</td><td>{esc(entry["hash"][:16])}</td></tr>' for entry in entries[-100:]) if verified else ''
        body = f'<p>Hash chain verified: {verified}</p>'
        if not verified:
            body += '<p>Verification failed. The audit history cannot be trusted. Preserve your own records and investigate before relying on this workspace.</p>'
        self.send_value(200, page('Audit log', body + table(('#', 'Event', 'Reference', 'Hash'), rows)))

    def do_GET(self):
        if not self._host_ok():
            return self.error(421, 'Misdirected request')
        path, store = urllib.parse.urlsplit(self.path).path, self.server.store
        try:
            if path == '/healthz':
                return self.send_value(200, b'ok', 'text/plain; charset=utf-8')
            if path == '/':
                return self.send_value(200, page('rebatelane', '<p>Paste usage, review suspicious accounts and prepare USD rebate credits. No credits are sent.</p>' + form('/w', 'Start a workspace') + form('/w/sample', 'Try with sample data')))
            parts = path.strip('/').split('/')
            if len(parts) < 2 or parts[0] not in ('w', 'r'):
                raise KeyError
            token, readonly = parts[1], parts[0] == 'r'
            base = '/' + parts[0] + '/' + token
            data = store.shared(token) if readonly else {'sample': store.meta(token).get('sample', False)}
            if len(parts) == 2:
                if not readonly:
                    data.update(rule=store.rule(token), runs=store.runs(token))
                return self.send_value(200, self.dashboard(base, data, readonly))
            if not readonly and len(parts) == 3 and parts[2] in ('export', 'log'):
                if parts[2] == 'log':
                    return self.audit_page(token)
                return self.send_value(200, json.dumps(store.export(token), indent=2).encode(), 'application/json', filename='rebatelane-export.json')
            if len(parts) not in (4, 5) or parts[2] != 'runs':
                raise KeyError
            run = next((run for run in data['runs'] if run['id'] == parts[3]), None) if readonly else store.run(token, parts[3])
            if run is None:
                raise KeyError
            base += '/runs/' + run['id']
            if len(parts) == 4:
                return self.send_value(200, self.run_page(base, run, data['sample'], readonly))
            if readonly:
                raise KeyError
            if parts[4] == 'reconcile':
                return self.send_value(200, page('Reconciliation', self.report_body(run.get('report') or {'buckets': {}, 'verdicts': {}}), data['sample']))
            if parts[4] not in ('grants.csv', 'grants.json'):
                raise KeyError
            values = [grant_parameters(x) for x in store.downloads(token, run['id'])]
            comment = 'Not sent by this app. Send Idempotency-Key as a header; all other fields are Stripe form parameters. Mark attempted submissions immediately; reconcile a complete export captured after submission before retrying.'
            if parts[4].endswith('json'):
                return self.send_value(200, json.dumps({'notice': comment, 'data': values}, indent=2).encode(), 'application/json', filename='grants.json')
            out = io.StringIO()
            out.write('# ' + comment + '\n')
            writer = csv.DictWriter(out, fieldnames=GRANT_FIELDS)
            writer.writeheader()
            writer.writerows(values)
            self.send_value(200, out.getvalue().encode(), 'text/csv', filename='grants.csv')
        except (KeyError, FileNotFoundError):
            self.error(404, 'Not found')
        except ValueError as exc:
            self.error(400, str(exc))
        except Exception:
            self.error(500, 'Request could not be completed')

    def do_POST(self):
        if not self._host_ok():
            return self.error(421, 'Misdirected request')
        if not self._origin_ok():
            return self.error(403, 'Same-origin submission required')
        try:
            fields = fields_body(self)
        except OverflowError:
            return self.error(413, 'Encoded request body exceeds 4 MB; each file must be at most 1 MB')
        except TimeoutError:
            self.close_connection = True
            return self.error(408, 'Upload timed out. No changes were saved. Requests have a 15-second total deadline; retry on a faster connection.')
        except Exception:
            return self.error(400, 'Invalid request body')
        path, store = urllib.parse.urlsplit(self.path).path, self.server.store
        try:
            if path in ('/w', '/w/sample'):
                sample = path.endswith('sample')
                token = store.create(sample, actor=self.actor(), actor_limit=10, global_limit=60)
                if sample:
                    rows, dropped = parse_usage('account_id,period,calls,spend,billing_customer_id,top_fingerprint_share,account_age_days\nacct_sample1,2026-09,200000,5000,cus_sample1,0.1,90\nacct_sample2,2026-09,200000,5000,cus_sample2,0.8,90\n')
                    store.create_run(token, rows, dropped)
                return self.redirect('/w/' + token)
            parts = path.strip('/').split('/')
            if len(parts) < 3 or parts[0] != 'w':
                raise KeyError
            token, action = parts[1], parts[-1]
            store.meta(token)
            base = '/w/' + token
            if len(parts) == 3:
                if action == 'delete':
                    store.delete(token)
                    return self.redirect('/?deleted=1')
                if action == 'share':
                    url = self.server.public_base + '/r/' + store.rotate_share(token)
                    return self.send_value(200, page('Read-only link', f'<p><a href="{esc(url)}">{esc(url)}</a></p>'))
                if action == 'rule':
                    store.write(token, 'rule.json', rule_from_fields(fields))
                    return self.redirect(base)
                if action == 'usage':
                    rows, dropped = parse_usage(supplied(fields, 'file') or supplied(fields, 'paste'))
                    run = store.create_run(token, rows, dropped)
                    return self.redirect(base + '/runs/' + run['id'])
                raise KeyError
            if len(parts) != 5 or parts[2] != 'runs':
                raise KeyError
            run_id, reference, dropped = parts[3], supplied(fields, 'reference'), []
            grants = parse_grants(supplied(fields, 'file') or supplied(fields, 'paste'), dropped) if action == 'grants' else None
            captured = capture_time(supplied(fields, 'captured_at'), store.clock()) if action == 'grants' else None
            refs = parse_references(supplied(fields, 'file') or supplied(fields, 'references') or reference) if action == 'mark' else None
            def change(run, db):
                if action == 'approve':
                    approve(run)
                    return [('BATCH_APPROVED', item['reference'], {'run_id': run_id}) for item in run['intents'] if item['state'] == 'APPROVED']
                if action == 'review':
                    item = review(run, reference, supplied(fields, 'decision'), supplied(fields, 'note'))
                    event = 'REVIEW_APPROVED' if item['state'] == 'PROPOSED' else 'REVIEW_REJECTED'
                elif action == 'mark':
                    return bulk_mark(run, refs, supplied(fields, 'mark'), store.clock())
                elif action == 'resolve':
                    item = resolve(run, reference, supplied(fields, 'action'))
                    event = 'RESOLVED_' + supplied(fields, 'action').upper()
                elif action == 'grants':
                    report = reconcile_import(run, grants, dropped, captured, store.known_references(db), uploaded_at=store.clock(), complete=supplied(fields, 'complete') == 'yes')
                    return [('RECONCILED', run_id, {'grant_count': len(grants), 'dropped_count': len(dropped), 'captured_at': captured, 'complete': report['complete']})]
                else:
                    raise KeyError
                return [(event, reference, {'account_id': item['account_id']})]
            store.mutate_run(token, run_id, change)
            return self.redirect(base + '/runs/' + run_id)
        except (KeyError, FileNotFoundError):
            self.error(404, 'Not found')
        except RateLimited as exc:
            self.error(429, str(exc))
        except RuntimeError as exc:
            self.error(507, str(exc))
        except ValueError as exc:
            self.error(400, str(exc))
        except Exception:
            self.error(500, 'Request could not be completed')


def make_server(address, data_dir, public_base_url, cap_mb=200, clock=time.time, trusted_proxies=()):
    server = CappedServer(address, Handler, Store(data_dir, cap_mb, clock), public_base_url, trusted_proxies)
    server._sweep_stop = threading.Event()
    def sweep_loop():
        while not server._sweep_stop.wait(3600):
            server.store.sweep()
    threading.Thread(target=sweep_loop, daemon=True).start()
    original_close = server.server_close
    def close():
        server._sweep_stop.set()
        with server._workers_lock:
            requests = list(server._timers)
        for request in requests:
            server.expire(request)
        original_close()
    server.server_close = close
    return server


def main():
    host, port = os.environ.get('APP_HOST', '127.0.0.1'), int(os.environ.get('PORT', '8080'))
    public = os.environ.get('PUBLIC_BASE_URL', 'https://app-rebatelane.manykiln.com')
    if os.environ.get('APP_HTTPS_TERMINATED', '').lower() == 'true' and not public.startswith('https://'):
        raise RuntimeError('PUBLIC_BASE_URL must use HTTPS')
    data = os.environ.get('DATA_DIR')
    if not data:
        raise RuntimeError('DATA_DIR is required')
    proxies = [x.strip() for x in os.environ.get('APP_TRUSTED_PROXY_IPS', '').split(',') if x.strip()]
    server = make_server((host, port), data, public, int(os.environ.get('APP_DATA_CAP_MB', '200')), trusted_proxies=proxies)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
