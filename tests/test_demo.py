import http.client
import http.cookiejar
import re
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch
import demo

class RunningServer:
    def __init__(self, hosted=False, token_roles=None, **options):
        self.server = demo.make_server(("127.0.0.1", 0), hosted, token_roles, **options)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

class Browser:
    def __init__(self, port, proxy_tls=False):
        self.port = port
        # Hosted integration tests emulate a TLS-terminating proxy over loopback.
        protocols = ("https", "http") if proxy_tls else ("https", "wss")
        self.jar = http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(secure_protocols=protocols))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
    def get(self, path):
        with self.opener.open(f"http://127.0.0.1:{self.port}{path}") as response:
            return response.read().decode()
    def post(self, path, fields=None):
        body = urllib.parse.urlencode(fields or {}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", body)
        with self.opener.open(request) as response:
            return urllib.parse.urlsplit(response.geturl()).path, response.read().decode()
    @property
    def sid(self):
        return next(cookie.value for cookie in self.jar if cookie.name == "rr_session")

class DemoTests(unittest.TestCase):
    def setUp(self):
        self.running = RunningServer()
        self.server = self.running.server
        self.browser = Browser(self.running.port)
    def tearDown(self):
        self.running.close()
    def app(self, browser=None):
        return self.server.sessions[(browser or self.browser).sid]["app"]

    def test_health_banner_limit_escape_and_flow(self):
        self.browser.get("/")
        self.browser.post("/reset")
        self.assertEqual(self.browser.get("/healthz"), "ok")
        for path in ("/", "/inbox", "/log", "/reconcile"):
            self.assertIn(demo.BANNER, self.browser.get(path))
        connection = http.client.HTTPConnection("127.0.0.1", self.running.port)
        connection.request("POST", "/reset", b"x" * 65537,
                           {"Content-Length": "65537", "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(connection.getresponse().status, 413)
        connection.close()
        batch_path, _ = self.browser.post("/batches")
        app = self.app()
        open_abuse = [x for x in app.inbox.items.values() if x.kind == "ABUSE_REVIEW"]
        self.browser.post(f"/inbox/{open_abuse[0].id}/reject", {"note": "<script>alert(1)</script>"})
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.browser.get("/inbox"))
        self.browser.post(f"/inbox/{open_abuse[1].id}/approve")
        self.browser.post(batch_path + "/approve")
        self.browser.post("/faults", {"timeout_after_commit": "on", "lookup_unavailable": "on"})
        self.browser.post(batch_path + "/post")
        uncertain = next(x for x in app.inbox.items.values() if x.kind == "UNCERTAIN_GRANT")
        self.browser.post(f"/inbox/{uncertain.id}/recheck")
        self.browser.post(f"/inbox/{uncertain.id}/unfreeze")
        self.assertIn("uncertain (0)", self.browser.get("/reconcile"))
        refs = [g.metadata["rr_ref"] for g in app.connector.grants.values()]
        self.assertEqual(len(refs), len(set(refs)))

    def test_access_requests_are_not_logged(self):
        with patch.object(demo.LOG, "info") as info:
            self.browser.get("/?private=query")
        info.assert_not_called()
        self.assertIsNone(demo.Handler.log_message(object(), "ignored"))

    def test_session_flood_refuses_newcomers_without_evicting_visitors(self):
        self.assertEqual(self.browser.get("/healthz"), "ok")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.browser.get("/missing?secret=value")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(len(self.server.sessions), 0)
        self.server.max_sessions = 2
        browsers = [Browser(self.running.port) for _ in range(2)]
        for browser in browsers:
            browser.get("/")
        sessions = dict(self.server.sessions)
        for _ in range(8):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                Browser(self.running.port).get("/")
            self.assertEqual(caught.exception.code, 429)
            self.assertIsNone(caught.exception.headers.get("Set-Cookie"))
            caught.exception.close()
        for browser in browsers:
            browser.post("/batches")
            self.assertIs(self.server.sessions[browser.sid], sessions[browser.sid])
        self.assertEqual(set(self.server.sessions), set(sessions))
        with self.server.sessions_lock:
            for session in self.server.sessions.values():
                session["last_seen"] -= self.server.session_ttl + 1
        Browser(self.running.port).get("/")
        self.assertEqual(len(self.server.sessions), 1)

    def test_repeated_batch_posts_are_idempotent_and_bounded(self):
        self.browser.get("/")
        first, _ = self.browser.post("/batches")
        app = self.app()
        counts = (len(app.engine.batches), len(app.engine.intents), len(app.log.entries()), len(app.inbox.items))
        for _ in range(20):
            path, _ = self.browser.post("/batches")
            self.assertEqual(path, first)
        self.assertEqual((len(app.engine.batches), len(app.engine.intents),
                          len(app.log.entries()), len(app.inbox.items)), counts)

    def test_hosted_demo_needs_no_credentials(self):
        hosted = RunningServer(hosted=True)
        self.addCleanup(hosted.close)
        browser = Browser(hosted.port, proxy_tls=True)
        home = browser.get("/")
        csrf = re.search(r"name=csrf value='([^']+)'", home).group(1)
        path, _ = browser.post("/batches", {"csrf": csrf})
        self.assertTrue(path.startswith("/batches/"))

    def test_login_adds_roles_and_rotates_session(self):
        hosted = RunningServer(hosted=True, token_roles={"approver": {"billing-approver"}})
        self.addCleanup(hosted.close)
        browser = Browser(hosted.port, proxy_tls=True)
        login = browser.get("/login")
        old_sid = browser.sid
        hosted.server.sessions[old_sid]["roles"] = {"operator"}
        csrf = re.search(r"name=csrf value='([^']+)'", login).group(1)
        browser.post("/login", {"csrf": csrf, "token": "approver"})
        self.assertNotEqual(browser.sid, old_sid)
        self.assertNotIn(old_sid, hosted.server.sessions)
        self.assertEqual(hosted.server.sessions[browser.sid]["roles"], {"operator", "billing-approver"})
        self.assertTrue(next(iter(browser.jar)).secure)
        self.assertEqual(len(hosted.server.sessions[browser.sid]["login_attempts"]), 1)

    def test_hosted_cookie_is_secure_and_local_cookie_works_over_http(self):
        self.browser.get("/")
        self.assertFalse(next(iter(self.browser.jar)).secure)
        hosted = RunningServer(hosted=True)
        self.addCleanup(hosted.close)
        connection = http.client.HTTPConnection("127.0.0.1", hosted.port)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse()
        cookie = response.getheader("Set-Cookie")
        response.read()
        for attribute in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)
        browser = Browser(hosted.port)
        browser.get("/")
        request = urllib.request.Request(f"http://127.0.0.1:{hosted.port}/")
        browser.jar.add_cookie_header(request)
        self.assertIsNone(request.get_header("Cookie"))

    def test_visitors_have_isolated_sandboxes(self):
        other = Browser(self.running.port)
        self.browser.get("/")
        other.get("/")
        self.assertIsNot(self.app(), self.app(other))
        self.browser.post("/batches")
        self.assertEqual(len(self.app().engine.batches), 1)
        self.assertEqual(len(self.app(other).engine.batches), 0)
        other_app = self.app(other)
        self.browser.post("/reset")
        self.assertIs(self.app(other), other_app)

    def test_worker_limit_and_total_request_deadline(self):
        limited = RunningServer(max_workers=1, request_deadline=.25)
        self.addCleanup(limited.close)
        slow = socket.create_connection(("127.0.0.1", limited.port), timeout=1)
        self.addCleanup(slow.close)
        slow.sendall(b"GET / HTTP/1.1\r\nHost: local")
        time.sleep(.05)
        rejected = socket.create_connection(("127.0.0.1", limited.port), timeout=1)
        self.addCleanup(rejected.close)
        rejected.sendall(b"GET /healthz HTTP/1.1\r\nHost: local\r\n\r\n")
        self.assertIn(b"503 Service Unavailable", rejected.recv(256))
        time.sleep(.3)
        self.assertEqual(slow.recv(1), b"")

    def test_client_limit_reserves_workers_for_other_peers_and_releases_slots(self):
        limited = RunningServer(max_workers=3, max_client_workers=1, request_deadline=2)
        self.addCleanup(limited.close)
        slow = socket.create_connection(("127.0.0.1", limited.port), timeout=1)
        self.addCleanup(slow.close)
        slow.sendall(b"GET / HTTP/1.1\r\nHost: local")
        deadline = time.monotonic() + 1
        while not limited.server._client_workers and time.monotonic() < deadline:
            time.sleep(.01)
        blocked = http.client.HTTPConnection("127.0.0.1", limited.port, timeout=1)
        self.addCleanup(blocked.close)
        blocked.request("GET", "/healthz", headers={"X-Forwarded-For": "different-client"})
        self.assertEqual(blocked.getresponse().status, 503)
        other = http.client.HTTPConnection("127.0.0.1", limited.port, timeout=1, source_address=("127.0.0.2", 0))
        self.addCleanup(other.close)
        other.request("GET", "/healthz")
        self.assertEqual(other.getresponse().read(), b"ok")
        slow.close()
        deadline = time.monotonic() + 1
        while limited.server._client_workers and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(limited.server._client_workers, {})
        self.assertEqual(limited.server._request_timers, {})
        self.assertEqual(Browser(limited.port).get("/healthz"), "ok")

    def test_login_token_attempts_are_isolated_and_survive_rotation(self):
        hosted = RunningServer(hosted=True, token_roles={"right": {"operator"}})
        self.addCleanup(hosted.close)
        hosted.server.login_limit = 2
        browser = Browser(hosted.port, proxy_tls=True)
        login = browser.get("/login")
        csrf = re.search(r"name=csrf value='([^']+)'", login).group(1)
        codes = []
        for _ in range(3):
            try:
                browser.post("/login", {"csrf": csrf, "token": "wrong"})
            except urllib.error.HTTPError as error:
                codes.append(error.code)
                error.close()
        self.assertEqual(codes, [403, 403, 429])
        other = Browser(hosted.port, proxy_tls=True)
        other.get("/login")
        for _ in range(2):
            csrf = hosted.server.sessions[other.sid]["csrf"]
            other.post("/login", {"csrf": csrf, "token": "right"})
        csrf = hosted.server.sessions[other.sid]["csrf"]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            other.post("/login", {"csrf": csrf, "token": "right"})
        self.assertEqual(caught.exception.code, 429)
        caught.exception.close()
        attempts = hosted.server.sessions[other.sid]["login_attempts"]
        with hosted.server.login_lock:
            for index in range(len(attempts)):
                attempts[index] -= hosted.server.login_window + 1
        other.post("/login", {"csrf": csrf, "token": "right"})

    def test_fault_recheck_attack_has_bounded_state_and_rendering(self):
        self.browser.get("/")
        path, _ = self.browser.post("/batches")
        app = self.app()
        for item in list(app.inbox.items.values()):
            self.browser.post(f"/inbox/{item.id}/approve")
        self.browser.post(path + "/approve")
        self.browser.post("/faults", {"timeout_after_commit": "on", "lookup_unavailable": "on"})
        self.browser.post(path + "/post")
        item = next(x for x in app.inbox.items.values() if x.kind == "UNCERTAIN_GRANT")
        for _ in range(demo.MAX_RECHECKS):
            self.browser.post("/faults", {"lookup_unavailable": "on"})
            self.browser.post(f"/inbox/{item.id}/recheck")
        counts = (len(app.log.entries()), len(app.connector.calls), len(app.inbox.items), app.vault.sent_count)
        for _ in range(20):
            self.browser.post("/faults", {"timeout_after_commit": "on", "lookup_unavailable": "on"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.browser.post(f"/inbox/{item.id}/recheck")
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        self.assertEqual((len(app.log.entries()), len(app.connector.calls), len(app.inbox.items), app.vault.sent_count), counts)
        self.assertEqual(app.connector.create_faults, ["timeout_after_commit"])
        self.assertEqual(app.connector.lookup_failures, 2)
        self.assertIn("Re-check limit reached", self.browser.get("/inbox"))
        with patch.object(demo, "LOG_ROWS", 5):
            body = self.browser.get("/log")
        self.assertEqual(body.count("<tr><td>"), 5)
        self.assertIn(f"Showing latest 5 of {counts[0]} entries", body)
        self.assertIn("Hash chain verified: True", body)
        self.browser.post("/faults")
        self.assertEqual(app.connector.create_faults, [])
        self.assertEqual(app.connector.lookup_failures, 0)
        self.browser.post("/reset")
        self.assertEqual(len(app.log.entries()), 0)

if __name__ == "__main__":
    unittest.main()
