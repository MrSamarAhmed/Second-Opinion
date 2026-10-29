"""The public demo must run from a checkout with no captured Qloo data.

Copies only the shipping code (`second_opinion/` and `web/`) into a temporary
directory — no `data/`, no fixtures, no `.env` — starts the real server there,
and exercises the endpoints the frontend calls.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ("second_opinion", "web")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestCleanCheckoutServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in SHIPPED:
            shutil.copytree(
                ROOT / name,
                self.root / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        self.assertFalse((self.root / "data").exists())
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items() if k != "QLOO_API_KEY"}
        env.pop("SO_MODE", None)
        env.update({"HOST": "127.0.0.1", "PORT": str(self.port), "PYTHONPATH": str(self.root)})
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "second_opinion.server"],
            cwd=str(self.root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.fail(f"server exited early: {self.proc.stderr.read().decode()}")
            try:
                status, _ = _get(self.port, "/api/health")
                if status == 200:
                    return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        self.fail("server did not become healthy")

    def _stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        self.tmp.cleanup()

    def test_seed_list_works_without_captured_files(self):
        status, body = _get(self.port, "/api/seeds")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["catalog"], "static")
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(len(payload["seeds"]), 8)
        self.assertFalse(payload["environment"]["live_ready"])

    def test_live_audit_refuses_without_a_key_instead_of_using_fixtures(self):
        status, body = _get(self.port, "/api/audit?seed=Laufey&mode=live")
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("QLOO_API_KEY", payload["error"])

    def test_default_mode_is_live_without_a_key(self):
        status, body = _get(self.port, "/api/audit?seed=Laufey")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["mode"], "live")

    def test_index_is_served_without_captured_files(self):
        status, body = _get(self.port, "/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Second Opinion", text)
        self.assertNotIn("data/fixtures", text)

    def test_fixture_mode_without_data_refuses_without_leaking_a_path(self):
        # This checkout ships no `data/`, so every fixture request meets the
        # adapter's "no data" failure. It must stay a fixture failure — honest,
        # mode-labelled, and free of the absolute path the server runs from.
        status, body = _get(self.port, "/api/audit?seed=Laufey&mode=fixture")
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["mode"], "fixture")
        error = payload["error"]
        self.assertIn("fixture", error)
        for path in (str(self.root), str(ROOT), str(Path.home())):
            self.assertNotIn(path, error, error)

    def test_a_fixture_failure_costs_the_live_budget_nothing(self):
        # The live bucket is the only one that protects quota, so a local fixture
        # refusal must not nibble at it: the live audit afterwards still fails for
        # the real reason (no credential), never as a rate limit.
        _get(self.port, "/api/audit?seed=Laufey&mode=fixture")
        status, body = _get(self.port, "/api/audit?seed=Laufey&mode=live")
        payload = json.loads(body)
        self.assertEqual(status, 503)
        self.assertIn("QLOO_API_KEY", payload["error"])
        self.assertNotEqual(payload.get("code"), "rate_limited")


if __name__ == "__main__":
    unittest.main()
