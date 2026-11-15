import collections, hmac, html, logging, os, secrets, socket, threading, time, urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from rebate_rail.connectors.fake_stripe import FakeStripe
from rebate_rail.engine import Engine, MAX_RECHECKS
from rebate_rail.inbox import Inbox
from rebate_rail.intent_log import IntentLog
from rebate_rail.models import Actor
from rebate_rail.reconcile import reconcile
from rebate_rail.sample_data import build
from rebate_rail.vault import Vault

BANNER = "Demo operated by AI agents. Made-up data. Do not enter real personal data. Nothing is charged."
MAX_BODY, MAX_SESSIONS, SESSION_TTL = 65536, 256, 30 * 60
MAX_WORKERS, MAX_CLIENT_WORKERS, REQUEST_DEADLINE = 32, 4, 15
LOGIN_LIMIT, LOGIN_WINDOW, LOG_ROWS = 20, 60, 50
ALL_ROLES = frozenset(("operator", "billing-approver", "exception-owner"))
LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
LOG = logging.getLogger("rebate_rail.demo")
def esc(value): return html.escape(str(value), quote=True)
def page(title, body):
    style = "body{font:16px system-ui;max-width:1050px;margin:auto;padding:1rem}.banner{background:#fff3a8;padding:12px;border:2px solid #8a6d00}table{border-collapse:collapse;width:100%}td,th{padding:7px;border:1px solid #ccc}nav a{margin-right:1rem}.bad{color:#a00}.good{color:#075}button{margin:3px}"
    nav = "<nav><a href='/'>Home</a><a href='/inbox'>Inbox</a><a href='/log'>Log</a><a href='/reconcile'>Reconcile</a><a href='/login'>Sign in</a></nav>"
    return f"<!doctype html><html><head><meta charset=utf-8><title>{esc(title)}</title><style>{style}</style></head><body><div class=banner>{esc(BANNER)}</div>{nav}<h1>{esc(title)}</h1>{body}</body></html>".encode()
def form(action, label, fields="", csrf=""):
    token = f"<input type=hidden name=csrf value='{esc(csrf)}'>" if csrf else ""
    return f"<form method=post action='{esc(action)}'>{token}{fields}<button>{esc(label)}</button></form>"
def row(*values): return "<tr>" + "".join(f"<td>{esc(value)}</td>" for value in values) + "</tr>"
def table(headers, rows): return "<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>" + rows + "</table>"
def normalize_public_base_url(value):
    if not value: return None
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin")
    try: parsed.port
    except ValueError as error: raise ValueError("PUBLIC_BASE_URL has an invalid port") from error
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))

class App:
    def __init__(self): self.lock = threading.Lock(); self.reset()
    def reset(self):
        self.vault, self.log, self.inbox, self.connector = Vault(), IntentLog(), Inbox(), FakeStripe()
        vendor, accounts, aggregates, rule = build(self.vault)
        self.engine = Engine(vendor, accounts, aggregates, rule, self.connector, self.log, self.inbox, self.vault); self.last_batch = None
    def set_faults(self, fields):
        self.connector.create_faults = ["timeout_after_commit"] if "timeout_after_commit" in fields else []
        self.connector.lookup_failures = 2 if "lookup_unavailable" in fields else 0
    def home(self, csrf=""):
        rule = self.engine.rule
        body = "<p>This browser has an isolated sample vendor and sandbox.</p><ol><li>Run September earn</li><li>Review both inbox holds</li><li>Approve the batch</li><li>Inject faults and post</li><li>Resolve, unfreeze, and reconcile</li></ol>"
        body += f"<p>Rule: {esc(rule.min_calls)} calls, {esc(rule.rebate_bps / 100)}% rebate, ${esc(rule.cap_cents // 100)} cap, {esc(rule.expiry_days)} day expiry.</p>"
        body += form("/batches", "Run September earn", csrf=csrf) + form("/reset", "Reset my sandbox", csrf=csrf)
        if self.last_batch: body += f"<p><a href='/batches/{esc(self.last_batch.id)}'>Open latest batch</a></p>"
        rows = "".join(row(a.label, a.id, a.state.value) for a in self.engine.accounts.values())
        return page("Rebate Rail", body + table(("Account", "Opaque ID", "State"), rows))
    def batch(self, ident, csrf=""):
        batch = self.engine.batches[ident]; intents = (self.engine.intents[i] for i in batch.intent_ids)
        rows = "".join(row(i.account_id, f"${i.amount_cents / 100:.2f}", i.state.value, ", ".join(i.abuse_reasons) or "—") for i in intents)
        rows += "".join(row(account_id, "—", "SKIPPED", reason) for account_id, reason in batch.skips)
        body = f"<p>Status: {esc(batch.status)}</p>" + table(("Account", "Credit", "State", "Reasons"), rows)
        body += form(f"/batches/{ident}/approve", "Approve as billing approver", csrf=csrf)
        fields = "<label><input type=checkbox name=timeout_after_commit> timeout_after_commit</label> <label><input type=checkbox name=lookup_unavailable> lookup_unavailable</label>"
        body += form("/faults", "Replace pending faults", fields, csrf) + form(f"/batches/{ident}/post", "Post approved credits", csrf=csrf)
        return page("September batch", body + f"<p>Pricing hypothesis only. Preview: ${esc(f'{batch.fee_cents / 100:.2f}')}</p>")
    def inbox_page(self, csrf=""):
        rows = ""
        for item in self.inbox.items.values():
            actions = ""
            if item.status == "OPEN" and item.kind == "ABUSE_REVIEW":
                actions = form(f"/inbox/{item.id}/approve", "Approve", csrf=csrf) + form(f"/inbox/{item.id}/reject", "Reject", "<input name=note placeholder='optional note'>", csrf)
            elif item.status == "OPEN":
                actions = form(f"/inbox/{item.id}/recheck", "Re-check", csrf=csrf) if self.engine.intents[item.intent_id].rechecks < MAX_RECHECKS else "<p>Re-check limit reached. Reset your sandbox from Home to start again.</p>"
                actions += form(f"/inbox/{item.id}/void", "Void", csrf=csrf)
            rows += row(item.kind, item.account_id, item.status + " " + (item.resolution or ""))[:-5] + f"<td>{actions}</td></tr>"
        for account in self.engine.accounts.values():
            if account.state.value != "FROZEN" or not account.frozen_by_intent: continue
            item = next((x for x in self.inbox.items.values() if x.intent_id == account.frozen_by_intent and x.kind == "UNCERTAIN_GRANT"), None)
            if item: rows += row("ACCOUNT_FROZEN", account.id, "OPEN")[:-5] + "<td>" + form(f"/inbox/{item.id}/unfreeze", "Unfreeze", csrf=csrf) + "</td></tr>"
        return page("Exception-owner inbox", table(("Kind", "Account", "Status", "Actions"), rows))
    def log_page(self):
        entries = self.log.entries(); body = f"<p class=good>Hash chain verified: {esc(self.log.verify())}</p><p>Showing latest {min(len(entries), LOG_ROWS)} of {len(entries)} entries.</p>"
        return page("Append-only intent log", body + table(("#", "Event", "Intent", "Hash"), "".join(row(e.seq, e.event_type, e.intent_id, e.hash[:12]) for e in entries[-LOG_ROWS:])))
    def reconcile_page(self):
        report = reconcile(self.engine.intents, self.connector, self.engine.accounts)
        names = ("matched", "amount_mismatch", "missing_on_billing", "orphan_on_billing", "uncertain")
        return page("Reconciliation", "".join(f"<h2>{esc(name)} ({len(getattr(report, name))})</h2><p>{esc(', '.join(getattr(report, name)) or 'None')}</p>" for name in names))

class DemoServer(ThreadingHTTPServer):
    daemon_threads, block_on_close = True, False
    def __init__(self, address, handler, max_workers=MAX_WORKERS, request_deadline=REQUEST_DEADLINE, max_client_workers=MAX_CLIENT_WORKERS):
        if max_workers < 1 or max_client_workers < 1 or request_deadline <= 0: raise ValueError("request limits must be positive")
        self._request_slots = threading.BoundedSemaphore(max_workers); self._request_timers, self._client_workers = {}, {}; self._workers_lock = threading.Lock()
        self.max_client_workers, self.request_deadline = max_client_workers, request_deadline
        super().__init__(address, handler)
    @staticmethod
    def _expire_request(request):
        try: request.shutdown(socket.SHUT_RDWR)
        except OSError: pass
    def _release_request(self, request):
        with self._workers_lock:
            pending = self._request_timers.pop(request, None)
            if pending:
                timer, client = pending; timer.cancel(); self._client_workers[client] -= 1
                if not self._client_workers[client]: del self._client_workers[client]
                self._request_slots.release()
    def process_request(self, request, client_address):
        client = client_address[0]
        with self._workers_lock:
            accepted = self._client_workers.get(client, 0) < self.max_client_workers and self._request_slots.acquire(blocking=False)
            if accepted:
                timer = threading.Timer(self.request_deadline, self._expire_request, (request,)); timer.daemon = True
                self._client_workers[client] = self._client_workers.get(client, 0) + 1; self._request_timers[request] = (timer, client)
        if not accepted:
            try: request.setblocking(False); request.send(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError: pass
            self.shutdown_request(request); return
        try: timer.start(); super().process_request(request, client_address)
        except Exception: self._release_request(request); raise
    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self._release_request(request)
    def handle_error(self, request, client_address): return None
class TooManyRequests(Exception): pass

class Handler(BaseHTTPRequestHandler):
    server_version = "RebateRailDemo/2"
    def log_message(self, fmt, *args): return None
    def _session(self, create=True):
        cached = getattr(self, "_session_cache", None)
        if cached is not None: return cached
        jar = cookies.SimpleCookie(self.headers.get("Cookie", "")); sid = jar["rr_session"].value if "rr_session" in jar else None; now = time.monotonic()
        with self.server.sessions_lock:
            self.server.prune_sessions(now); session = self.server.sessions.get(sid)
            if session is not None: session["last_seen"] = now; result = (sid, session, False)
            elif not create: result = (None, None, False)
            else:
                if len(self.server.sessions) >= self.server.max_sessions: raise TooManyRequests
                sid = secrets.token_urlsafe(24)
                while sid in self.server.sessions: sid = secrets.token_urlsafe(24)
                session = {"csrf": secrets.token_urlsafe(24), "roles": set(ALL_ROLES), "app": App(), "last_seen": now, "login_attempts": collections.deque()}
                self.server.sessions[sid] = session; result = (sid, session, True)
        self._session_cache = result; return result
    def _rotate_session(self, sid, session):
        with self.server.sessions_lock:
            if self.server.sessions.get(sid) is not session: raise PermissionError
            replacement = secrets.token_urlsafe(24)
            while replacement in self.server.sessions: replacement = secrets.token_urlsafe(24)
            del self.server.sessions[sid]; session.update(csrf=secrets.token_urlsafe(24), last_seen=time.monotonic()); self.server.sessions[replacement] = session
        self._session_cache = (replacement, session, True); return replacement
    def _absolute_url(self, location): return self.server.public_base_url + location if self.server.public_base_url and location.startswith("/") else location
    def _absolute_html(self, body, content_type):
        if not self.server.public_base_url or not content_type.startswith("text/html"): return body
        prefix = self.server.public_base_url.encode()
        return body.replace(b"href='/", b"href='" + prefix + b"/").replace(b"action='/", b"action='" + prefix + b"/")
    def _send(self, status, body=b"", content_type="text/html; charset=utf-8", location=None, create_session=True):
        body = self._absolute_html(body, content_type); sid, _, fresh = self._session(create_session) if create_session else getattr(self, "_session_cache", (None, None, False)); self.send_response(status)
        headers = {"Content-Type": content_type, "Content-Length": str(len(body)), "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'", "Referrer-Policy": "no-referrer"}
        for key, value in headers.items(): self.send_header(key, value)
        if fresh and sid: self.send_header("Set-Cookie", "rr_session=" + sid + "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.server.hosted else ""))
        if location: self.send_header("Location", self._absolute_url(location))
        try:
            self.end_headers()
            if body: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): self.close_connection = True
    def _redirect(self, location): self._send(303, location=location)
    def _error(self, status, request_id):
        titles = {400: "Bad request", 403: "Forbidden", 404: "Not found", 413: "Request too large", 429: "Too many requests"}
        self._send(status, page(titles.get(status, "Server error"), f"<p>Request could not be completed. Reference: {esc(request_id)}</p>"), create_session=False)
    def _valid_host(self):
        expected = self.server.public_authority
        if expected is None: return True
        supplied = self.headers.get("Host", "").strip().lower()
        if not supplied or any(character in supplied for character in "\r\n/@"): return False
        try: parsed = urllib.parse.urlsplit("//" + supplied); supplied_port = parsed.port
        except ValueError: return False
        return parsed.hostname == expected[0] and supplied_port == expected[1]
    def _same_origin(self):
        origin = self.headers.get("Origin")
        return not origin or urllib.parse.urlsplit(origin).netloc.lower() == self.headers.get("Host", "").lower()
    def _read_fields(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY: raise OverflowError
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
    def _authorize_post(self, fields, role=None, login=False):
        sid, session, _ = self._session(create=not self.server.hosted)
        if session is None or not self._same_origin(): raise PermissionError
        supplied = self.headers.get("X-CSRF-Token") or (fields.get("csrf") or [""])[0]
        if (self.server.hosted or self.headers.get("Origin")) and not hmac.compare_digest(supplied, session["csrf"]): raise PermissionError
        if role and role not in session["roles"]: raise PermissionError
        return sid, session, Actor("web-session", frozenset(session["roles"]))
    def _allow_login_attempt(self, session):
        now = time.monotonic()
        with self.server.login_lock:
            attempts = session["login_attempts"]
            while attempts and now - attempts[0] >= self.server.login_window: attempts.popleft()
            if len(attempts) >= self.server.login_limit: return False
            attempts.append(now); return True
    def _failure(self, error, request_id, post=False):
        status, kind = (429, "rate_limited") if isinstance(error, TooManyRequests) else (403, "forbidden") if isinstance(error, PermissionError) else (400 if post else 404, "invalid" if post else "not_found") if isinstance(error, ValueError) else (404, "not_found") if isinstance(error, KeyError) else (500, "internal")
        LOG.log(logging.ERROR if status == 500 else logging.WARNING, "request_failed id=%s kind=%s", request_id, kind, exc_info=status == 500); self._error(status, request_id)
    def do_GET(self):
        request_id = secrets.token_hex(4)
        if not self._valid_host(): return self._error(400, request_id)
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz": return self._send(200, b"ok", "text/plain; charset=utf-8", create_session=False)
            if path not in ("/", "/login", "/inbox", "/log", "/reconcile") and not path.startswith("/batches/"): return self._error(404, request_id)
            _, session, _ = self._session(create=not path.startswith("/batches/"))
            if session is None: return self._error(404, request_id)
            if path == "/login":
                note = "<p>Your isolated demo sandbox already has all roles; no credential is required.</p>"
                if self.server.token_roles: note += form("/login", "Add configured role", "<input type=password name=token autocomplete=current-password>", session["csrf"])
                return self._send(200, page("Sign in", note))
            app = session["app"]
            with app.lock:
                if path == "/": body = app.home(session["csrf"])
                elif path == "/inbox": body = app.inbox_page(session["csrf"])
                elif path == "/log": body = app.log_page()
                elif path == "/reconcile": body = app.reconcile_page()
                else: body = app.batch(path.split("/")[2], session["csrf"])
            self._send(200, body)
        except Exception as error: self._failure(error, request_id)
    def do_POST(self):
        request_id = secrets.token_hex(4)
        if not self._valid_host(): return self._error(400, request_id)
        try: fields = self._read_fields()
        except OverflowError: return self._error(413, request_id)
        except (UnicodeDecodeError, ValueError): return self._error(400, request_id)
        try:
            path = urllib.parse.urlsplit(self.path).path; parts = path.strip("/").split("/")
            valid_item = len(parts) == 3 and ((parts[0] == "batches" and parts[2] in ("approve", "post")) or (parts[0] == "inbox" and parts[2] in ("approve", "reject", "recheck", "void", "unfreeze")))
            if path not in ("/login", "/reset", "/batches", "/faults") and not valid_item: raise KeyError
            if path == "/login":
                sid, session, _ = self._authorize_post(fields, login=True)
                if not self._allow_login_attempt(session): raise TooManyRequests
                token = (fields.get("token") or [""])[0]; roles = next((roles for candidate, roles in self.server.token_roles.items() if hmac.compare_digest(token, candidate)), None)
                if roles is None: raise PermissionError
                session["roles"].update(roles); self._rotate_session(sid, session); return self._redirect("/")
            role = "billing-approver" if path.endswith("/approve") and path.startswith("/batches/") else "exception-owner" if path.startswith("/inbox/") else None
            _, session, actor = self._authorize_post(fields, role); app = session["app"]
            with app.lock:
                if path == "/reset": app.reset(); target = "/"
                elif path == "/batches":
                    if app.last_batch is None: app.last_batch = app.engine.create_batch("2026-09")
                    target = f"/batches/{app.last_batch.id}"
                elif path == "/faults": app.set_faults(fields); target = f"/batches/{app.last_batch.id}" if app.last_batch else "/"
                elif parts[0] == "batches":
                    if parts[2] == "approve": app.engine.approve_batch(parts[1], actor)
                    else: app.engine.post_batch(parts[1])
                    target = f"/batches/{parts[1]}"
                else:
                    item_id, action = parts[1], parts[2]
                    if action in ("approve", "reject"):
                        app.engine.review_abuse(item_id, action == "approve", actor); note = (fields.get("note") or [""])[0][:200]
                        if note: app.inbox.items[item_id].resolution += ": " + note
                    else: app.engine.resolve_uncertain(item_id, action, actor)
                    target = "/inbox"
            self._redirect(target)
        except Exception as error: self._failure(error, request_id, post=True)

def make_server(address, hosted=None, token_roles=None, max_workers=MAX_WORKERS, request_deadline=REQUEST_DEADLINE, max_client_workers=MAX_CLIENT_WORKERS, public_base_url=None):
    public_base_url = normalize_public_base_url(public_base_url); server = DemoServer(address, Handler, max_workers, request_deadline, max_client_workers); server.public_base_url = public_base_url
    if public_base_url:
        parsed = urllib.parse.urlsplit(public_base_url); default_port = 443 if parsed.scheme == "https" else 80; port = parsed.port
        server.public_authority = (parsed.hostname, None if port == default_port else port)
    else: server.public_authority = None
    inferred_hosted = address[0] not in LOOPBACK_HOSTS; server.hosted = bool(public_base_url) or (inferred_hosted if hosted is None else hosted)
    server.sessions, server.token_roles = {}, token_roles or {}; server.sessions_lock, server.login_lock = threading.Lock(), threading.Lock()
    server.max_sessions, server.session_ttl = MAX_SESSIONS, SESSION_TTL; server.login_limit, server.login_window = LOGIN_LIMIT, LOGIN_WINDOW
    def prune_sessions(now):
        for sid in [sid for sid, session in server.sessions.items() if now - session["last_seen"] >= server.session_ttl]: del server.sessions[sid]
    server.prune_sessions = prune_sessions; return server

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("DEMO_HOST") or "127.0.0.1"; port = int(os.environ.get("PORT", "8080")); public_base_url = normalize_public_base_url(os.environ.get("PUBLIC_BASE_URL"))
    https_terminated = os.environ.get("DEMO_HTTPS_TERMINATED", "").lower() == "true"
    contract_ready = bool(os.environ.get("DEMO_HOST") and os.environ.get("PORT") and public_base_url and https_terminated)
    if https_terminated and public_base_url and not public_base_url.startswith("https://"): raise RuntimeError("PUBLIC_BASE_URL must use HTTPS when DEMO_HTTPS_TERMINATED=true")
    if host not in LOOPBACK_HOSTS and not contract_ready and os.environ.get("REBATE_RAIL_ALLOW_HOSTED") != "1": raise RuntimeError("non-loopback binding requires the hosting contract or REBATE_RAIL_ALLOW_HOSTED=1")
    configured = [os.environ.get("REBATE_RAIL_" + role + "_TOKEN", "") for role in ("OPERATOR", "APPROVER", "EXCEPTION")]
    if any(configured) and (any(not token for token in configured) or len(set(configured)) != 3): raise RuntimeError("role tokens must be either omitted or three distinct values")
    token_roles = dict(zip(configured, ({"operator"}, {"billing-approver"}, {"exception-owner"}))) if all(configured) else {}
    hosted = host not in LOOPBACK_HOSTS or bool(public_base_url) or https_terminated
    server = make_server((host, port), hosted=hosted, token_roles=token_roles, public_base_url=public_base_url)
    LOG.info("listening host=%s port=%s hosted=%s", host, server.server_address[1], hosted)
    try: server.serve_forever()
    finally: server.server_close()
if __name__ == "__main__": main()
