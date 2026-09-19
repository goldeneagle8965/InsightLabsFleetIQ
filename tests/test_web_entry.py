"""Deployment entry-point checks: CLI/env handling and per-visitor sessions.

`scripts/run_web.py` is what a judge, a projector or a container actually runs. These tests
cover the two claims that matter for a live demo:

* the server honours HOST/PORT/MAX_SESSIONS/SESSION_TTL_S from the environment, so the same
  image works locally and on a platform that injects $PORT;
* two visitors get two independent fleets, so nobody clicks pause and freezes someone else's
  screen.

Everything binds to 127.0.0.1 on an ephemeral port and shuts down in tearDown.

Run: python -m unittest tests.test_web_entry
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_web  # noqa: E402
from simulation.dashboard import Dashboard, serve_sessions  # noqa: E402


class TestCliAndEnv(unittest.TestCase):
    def test_defaults_are_reachable_and_portable(self) -> None:
        for key in ("HOST", "PORT", "MAX_SESSIONS", "SESSION_TTL_S"):
            os.environ.pop(key, None)
        args = run_web.parse_args([])
        self.assertEqual("0.0.0.0", args.host, "default bind must be reachable from another device")
        self.assertEqual(8765, args.port)
        self.assertFalse(args.single)

    def test_environment_overrides_win(self) -> None:
        saved = {k: os.environ.get(k) for k in ("HOST", "PORT", "MAX_SESSIONS", "SESSION_TTL_S")}
        os.environ.update({"HOST": "127.0.0.1", "PORT": "9099", "MAX_SESSIONS": "7", "SESSION_TTL_S": "120"})
        try:
            args = run_web.parse_args([])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual("127.0.0.1", args.host)
        self.assertEqual(9099, args.port)
        self.assertEqual(7, args.visitors, "MAX_SESSIONS must cap concurrent fleets")
        self.assertEqual(120.0, args.ttl)

    def test_command_line_beats_environment(self) -> None:
        saved = os.environ.get("PORT")
        os.environ["PORT"] = "9099"
        try:
            args = run_web.parse_args(["--port", "8123", "--single", "--visitors", "2"])
        finally:
            if saved is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = saved
        self.assertEqual(8123, args.port)
        self.assertTrue(args.single)
        self.assertEqual(2, args.visitors)

    def test_port_probe_detects_a_busy_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            busy = srv.getsockname()[1]
            self.assertFalse(run_web.human_port_free("127.0.0.1", busy), "busy port reported as free")
        self.assertTrue(run_web.human_port_free("127.0.0.1", busy), "freed port still reported busy")

    def test_lan_ip_is_usable(self) -> None:
        ip = run_web.lan_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(ip, "lan_ip returned an empty string")


class _Visitor:
    """One browser: keeps its own cookie jar and talks plain http.client."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.sid: str | None = None

    def conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes, dict]:
        c = self.conn()
        headers = {"Content-Type": "application/json"}
        if self.sid:
            headers["Cookie"] = f"sid={self.sid}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            c.request(method, path, body=payload, headers=headers)
            resp = c.getresponse()
            blob = resp.read()
            info = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
        finally:
            c.close()
        cookie = info.get("set-cookie", "")
        if "sid=" in cookie:
            self.sid = cookie.split("sid=", 1)[1].split(";", 1)[0].strip()
        return status, blob, info

    def state(self) -> dict:
        status, blob, _ = self.request("GET", "/state")
        assert status == 200, f"GET /state returned {status}"
        return json.loads(blob.decode("utf-8"))


class TestLiveServer(unittest.TestCase):
    def setUp(self) -> None:
        # limit=4 exercises the session cap without opening 32 fleets in a test run.
        self.httpd = serve_sessions(lambda sid: Dashboard(label=sid[:6]), host="127.0.0.1", port=0,
                                    ttl_s=120.0, limit=4)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                       daemon=True, name="test-http")
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def test_health_and_landing_page(self) -> None:
        v = _Visitor(self.port)
        status, blob, info = v.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(json.loads(blob.decode("utf-8"))["ok"])
        self.assertEqual("nosniff", info.get("x-content-type-options"))

        status, blob, info = v.request("GET", "/")
        self.assertEqual(200, status)
        self.assertIn("text/html", info.get("content-type", ""))
        page = blob.decode("utf-8")
        self.assertIn('lang="en"', page.lower())
        self.assertGreater(len(page), 2000, "landing page looks truncated")

    def test_static_assets_are_served_with_correct_types(self) -> None:
        v = _Visitor(self.port)
        for path, needle, min_bytes in (
            ("/static/app.js", "function", 15000),
            ("/static/style.css", "{", 1000),
        ):
            with self.subTest(path=path):
                status, blob, info = v.request("GET", path)
                self.assertEqual(200, status, f"{path} returned {status}")
                self.assertGreater(len(blob), min_bytes, f"{path} is suspiciously small")
                self.assertIn(needle, blob.decode("utf-8"))
                if path.endswith(".js"):
                    self.assertIn("javascript", info.get("content-type", ""))

    def test_unknown_path_404s_without_raising(self) -> None:
        v = _Visitor(self.port)
        status, _, _ = v.request("GET", "/definitely-not-a-route")
        self.assertEqual(404, status)

    def test_two_visitors_get_independent_fleets(self) -> None:
        a, b = _Visitor(self.port), _Visitor(self.port)

        self.assertEqual(200, a.request("GET", "/state")[0])
        self.assertEqual(200, b.request("GET", "/state")[0])
        self.assertIsNotNone(a.sid, "server did not issue a session cookie")
        self.assertIsNotNone(b.sid, "server did not issue a session cookie")
        self.assertNotEqual(a.sid, b.sid, "both visitors were given the same session")
        self.assertNotEqual(a.state()["server"]["session"], b.state()["server"]["session"])

        status, blob, _ = a.request("POST", "/action", {"action": "pause"})
        self.assertEqual(200, status)
        self.assertTrue(json.loads(blob.decode("utf-8"))["ok"])
        self.assertTrue(a.state()["paused"], "pause did not apply to the visitor who clicked it")
        self.assertFalse(b.state()["paused"], "one visitor's pause froze another visitor's fleet")

        a.request("POST", "/action", {"action": "resume"})
        self.assertFalse(a.state()["paused"])

    def test_actions_are_rejected_when_malformed(self) -> None:
        v = _Visitor(self.port)
        status, blob, _ = v.request("POST", "/action", {"action": "add_task", "pick": [1, 1], "drop": [9, 7]})
        self.assertEqual(200, status, "a rejected action must not 500 the server")
        payload = json.loads(blob.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload, "a rejected action must say why")


if __name__ == "__main__":
    unittest.main(verbosity=2)
