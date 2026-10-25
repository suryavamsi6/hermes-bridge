"""hermes-bridge against a fake Hermes dashboard over real HTTP (run: python -m unittest)."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_dashboard import TOKEN, FakeHermes  # noqa: E402
from hermes_bridge.__main__ import main as cli_main  # noqa: E402
from hermes_bridge.app import Bridge  # noqa: E402
from hermes_bridge.config import load  # noqa: E402
from hermes_bridge.server import make_handler  # noqa: E402

KEY = "bridge-key-" + "a" * 32


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHermes()
        url = self.fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "key").write_text(KEY + "\n")
        (d / "token").write_text(f"HERMES_DASHBOARD_SESSION_TOKEN={TOKEN}\n")
        self.cfg = load(environ={"HERMES_BRIDGE_DASHBOARD_URL": url, "HERMES_BRIDGE_KEY_FILE": str(d / "key"),
                                 "HERMES_BRIDGE_DASHBOARD_TOKEN_FILE": str(d / "token"),
                                 "HERMES_BRIDGE_BASE_PATH": "/bridge", "HERMES_BRIDGE_LISTEN": "127.0.0.1:0"})
        self.bridge = Bridge(self.cfg)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.bridge))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        if self.fake.httpd.socket.fileno() != -1:
            self.fake.stop()
        self.tmp.cleanup()

    def req(self, method, path, body=None, key=KEY):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # ---- auth & allowlist
    def test_health_is_public_everything_else_needs_the_key(self):
        self.assertEqual(self.req("GET", "/v1/health", key=None)[0], 200)
        self.assertEqual(self.req("GET", "/v1/profiles", key=None)[0], 401)
        self.assertEqual(self.req("GET", "/v1/profiles", key="wrong-" + "b" * 32)[0], 401)
        self.assertEqual(self.req("GET", "/v1/profiles")[0], 200)

    def test_base_path_prefix_is_optional(self):
        self.assertEqual(self.req("GET", "/bridge/v1/health")[0], 200)
        self.assertEqual(self.req("GET", "/bridge/v1/profiles")[0], 200)

    def test_never_forwards_dangerous_dashboard_routes(self):
        for method, path in [("GET", "/api/env"), ("POST", "/api/env/reveal"), ("GET", "/api/files/download"),
                             ("GET", "/v1/profiles/default/env"), ("GET", "/v1/../api/env"), ("GET", "/api/pty"),
                             ("POST", "/v1/profiles/default/plugins/../../api/env/reveal/enable"),
                             ("PUT", "/v1/profiles/default/mcp/../enabled")]:
            status, body = self.req(method, path, {"enabled": True} if method in ("POST", "PUT") else None)
            self.assertIn(status, (400, 404, 405), (method, path, status))
        touched = [c for c in self.fake.calls if "reveal" in c or "/files" in c or "/pty" in c or c.startswith("PUT /api/env")]
        self.assertEqual(touched, [])
        self.assertFalse(any(c.startswith("GET /api/env") and "profile=" not in c for c in self.fake.calls))

    # ---- compat
    def test_status_reports_hermes_and_checks(self):
        status, body = self.req("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["hermes"]["release_date"], "2026.9.24")
        self.assertEqual(len(body["checks"]), 7)

    def test_api_drift_fails_clearly(self):
        self.fake.broken.add("/api/memory")
        status, body = self.req("GET", "/v1/profiles")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "dashboard_incompatible")
        self.assertIn("/api/memory", body["error"]["message"])
        self.assertIn("4ea8d23c", body["error"]["message"])

    def test_gated_dashboard_is_refused_with_explanation(self):
        self.fake.auth_required = True
        status, body = self.req("GET", "/v1/status")
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "dashboard_gated")

    def test_wrong_dashboard_token_is_explained(self):
        Path(self.cfg.dashboard_token_file).write_text("not-the-token-xxxxxxxx\n")
        status, body = self.req("GET", "/v1/status")
        self.assertEqual((status, body["code"]), (503, "dashboard_token_rejected"))

    def test_unreachable_dashboard(self):
        self.fake.stop()
        status, body = self.req("GET", "/v1/profiles")
        self.assertEqual((status, body["error"]["code"]), (503, "dashboard_unreachable"))

    # ---- profiles & keys
    def test_list_profiles_has_display_names_and_key_flags_but_no_values(self):
        status, body = self.req("GET", "/v1/profiles")
        self.assertEqual(status, 200)
        by_id = {p["id"]: p for p in body["profiles"]}
        self.assertEqual(by_id["default"]["display_name"], "default")
        self.assertTrue(by_id["default"]["api_key_set"])
        self.assertFalse(by_id["scout"]["api_key_set"])
        raw = json.dumps(body)
        for secret in ("main-key", "sk-secret", "redacted_value", "…"):
            self.assertNotIn(secret, raw)

    def test_create_profile_provisions_a_key_once(self):
        status, body = self.req("POST", "/v1/profiles", {"name": "research", "description": "papers"})
        self.assertEqual(status, 201, body)
        key = body["api_key"]
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(self.fake.env["research"]["API_SERVER_KEY"], key)
        self.assertIn("POST /api/profiles", self.fake.calls)
        self.assertIn("PUT /api/env?profile=research", self.fake.calls)
        listed = {p["id"]: p for p in self.req("GET", "/v1/profiles")[1]["profiles"]}
        self.assertTrue(listed["research"]["api_key_set"])
        self.assertNotIn(key, json.dumps(listed))
        status, body = self.req("POST", "/v1/profiles", {"name": "research"})
        self.assertEqual(status, 400)
        self.assertIn("already exists", body["error"]["message"])

    def test_create_profile_validates_ids(self):
        for bad in ("Research", "default", "../x", "", "a" * 70):
            self.assertEqual(self.req("POST", "/v1/profiles", {"name": bad})[0], 400, bad)
        self.assertEqual(self.req("POST", "/v1/profiles", {"name": "ok", "model": 3})[0], 400)

    def test_rotate_key(self):
        self.assertEqual(self.req("POST", "/v1/profiles/default/key")[1]["error"]["code"], "default_key_managed")
        self.assertEqual(self.req("POST", "/v1/profiles/nope/key")[0], 404)
        s1, b1 = self.req("POST", "/v1/profiles/scout/key")
        s2, b2 = self.req("POST", "/v1/profiles/scout/key")
        self.assertEqual((s1, s2), (200, 200))
        self.assertNotEqual(b1["api_key"], b2["api_key"])
        self.assertEqual(self.fake.env["scout"]["API_SERVER_KEY"], b2["api_key"])

    def test_gateway_restart(self):
        self.assertEqual(self.req("POST", "/v1/gateway/restart")[0], 202)

    # ---- features
    def test_mcp_lists_env_names_only_and_scopes_by_profile(self):
        status, body = self.req("GET", "/v1/profiles/default/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(body["servers"][0]["env_keys"], ["GITHUB_TOKEN"])
        self.assertNotIn("env", body["servers"][0])
        self.assertEqual(self.req("POST", "/v1/profiles/scout/mcp", {"name": "time", "url": "https://t", "env": {"A": "b"}})[0], 201)
        self.assertEqual(self.fake.mcp["scout"][0]["name"], "time")
        self.assertEqual(self.req("PUT", "/v1/profiles/scout/mcp/time/enabled", {"enabled": "yes"})[0], 400)
        self.assertEqual(self.req("PUT", "/v1/profiles/scout/mcp/time/enabled", {"enabled": False})[0], 200)
        self.assertEqual(self.req("DELETE", "/v1/profiles/scout/mcp/time")[0], 200)
        self.assertEqual(self.fake.mcp["scout"], [])
        self.assertEqual(self.req("GET", "/v1/profiles/scout/mcp-catalog")[1]["entries"][0]["name"], "time")

    def test_jobs(self):
        self.assertEqual(self.req("GET", "/v1/profiles/default/jobs")[1]["jobs"][0]["id"], "j1")
        status, body = self.req("POST", "/v1/profiles/default/jobs/j1/trigger")
        self.assertEqual((status, body["action"], body["profile"]), (200, "trigger", "default"))
        self.assertEqual(self.req("POST", "/v1/profiles/default/jobs/j1/pause")[1]["action"], "pause")
        self.assertEqual(self.req("POST", "/v1/profiles/default/jobs/j1/delete")[0], 404)
        self.assertEqual(self.req("GET", "/v1/profiles/default/jobs/j1/runs?limit=5000")[1]["limit"], 20)

    def test_plugins_with_tools_and_toggle_rules(self):
        status, body = self.req("GET", "/v1/profiles/default/plugins")
        self.assertEqual(status, 200)
        self.assertEqual(body["plugins"][0]["tools"], ["fetch", "search"])
        self.assertTrue(body["toggle_supported"])
        self.assertFalse(self.req("GET", "/v1/profiles/scout/plugins")[1]["toggle_supported"])
        status, body = self.req("POST", "/v1/profiles/scout/plugins/web-tools/disable")
        self.assertEqual((status, body["error"]["code"]), (409, "plugin_toggle_unsupported"))
        self.assertIn("hermes -p scout plugins disable web-tools", body["error"]["message"])
        self.assertEqual(self.req("POST", "/v1/profiles/default/plugins/web-tools/disable")[0], 200)
        self.assertFalse(self.fake.plugin_enabled["web-tools"])

    def test_plugin_catalog_and_install(self):
        status, body = self.req("GET", "/v1/profiles/default/plugin-catalog")
        self.assertEqual(status, 200)
        e = body["entries"][0]
        self.assertEqual((e["name"], e["installed"], e["requires_env"], e["provides_tools"]),
                         ("spotify", False, ["SPOTIFY_CLIENT_ID"], ["play"]))
        self.assertNotIn("repo", e)  # only display fields go to the phone
        self.assertTrue(body["install_supported"])
        self.assertFalse(self.req("GET", "/v1/profiles/scout/plugin-catalog")[1]["install_supported"])
        # Catalog names only: a git URL never reaches the dashboard.
        self.assertEqual(self.req("POST", "/v1/profiles/default/plugin-catalog/install",
                                  {"name": "https://evil.example/repo.git"})[0], 400)
        status, body = self.req("POST", "/v1/profiles/scout/plugin-catalog/install", {"name": "spotify"})
        self.assertEqual((status, body["error"]["code"]), (409, "plugin_install_unsupported"))
        status, body = self.req("POST", "/v1/profiles/default/plugin-catalog/install", {"name": "spotify"})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.last_install, {"identifier": "", "catalog_name": "spotify", "enable": True, "force": False})
        self.assertTrue(self.req("GET", "/v1/profiles/default/plugin-catalog")[1]["entries"][0]["installed"])

    def test_skills_toolsets_memory(self):
        self.assertEqual(self.req("GET", "/v1/profiles/default/skills")[1]["skills"][0]["name"], "web")
        self.assertEqual(self.req("PUT", "/v1/profiles/default/skills/enabled", {"name": "web", "enabled": False})[0], 200)
        self.assertEqual(self.req("GET", "/v1/profiles/default/toolsets")[1]["toolsets"][0]["tools"], ["fetch", "search"])
        self.assertEqual(self.req("GET", "/v1/profiles/default/memory")[1]["active"], "builtin")
        cfg = self.req("GET", "/v1/profiles/default/memory/providers/honcho/config")[1]
        self.assertEqual(cfg["fields"][0]["value"], "")
        self.assertEqual(self.req("PUT", "/v1/profiles/default/memory/providers/honcho/config", {"values": {"a": "b"}})[0], 200)

    def test_soul_read_and_write(self):
        self.assertEqual(self.req("GET", "/v1/profiles/default/soul"), (200, {"content": "You are Hermes.", "exists": True}))
        self.assertEqual(self.req("PUT", "/v1/profiles/default/soul", {"content": "Be brief."})[0], 200)
        self.assertEqual(self.fake.souls["default"], "Be brief.")
        self.assertEqual(self.req("PUT", "/v1/profiles/default/soul", {"content": 3})[0], 400)
        self.assertEqual(self.req("PUT", "/v1/profiles/default/soul", {"content": "x" * (48 * 1024 + 1)})[0], 413)
        self.assertEqual(self.req("GET", "/v1/profiles/nobody/soul")[0], 404)

    def test_usage_totals(self):
        code, body = self.req("GET", "/v1/profiles/scout/usage?days=7")
        self.assertEqual(code, 200)
        self.assertEqual(body["totals"]["profile"], "scout")
        self.assertEqual(body["totals"]["days"], 7)
        self.assertNotIn("skills", body)
        self.assertEqual(self.req("GET", "/v1/profiles/default/usage?days=x")[0], 400)

    def test_unknown_profile_and_bad_ids(self):
        self.assertEqual(self.req("GET", "/v1/profiles/ghost/jobs")[0], 404)
        self.assertEqual(self.req("GET", "/v1/profiles/Ghost/jobs")[0], 400)


class ServerHardeningTest(unittest.TestCase):
    """The HTTP layer: a bad Content-Length is refused instead of reading until the client hangs up."""
    setUp = BridgeTest.setUp
    tearDown = BridgeTest.tearDown

    def test_negative_or_garbage_content_length_is_rejected(self):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        from hermes_bridge.server import make_handler
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.bridge))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for bad in ("-1", "abc"):
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                c.putrequest("POST", "/v1/profiles"); c.putheader("Content-Length", bad); c.endheaders()
                r = c.getresponse()
                self.assertEqual(r.status, 400, bad)
                r.read(); c.close()
        finally:
            httpd.shutdown(); httpd.server_close()


class InitTest(unittest.TestCase):
    def test_init_creates_0600_secrets_once_and_never_prints_them(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            k, t = Path(d, "sub", "key"), Path(d, "token")
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["init", "--key-file", str(k), "--dashboard-token-file", str(t)])
            first = k.read_text()
            self.assertEqual(oct(os.stat(k).st_mode & 0o777), "0o600")
            self.assertNotIn(first.strip(), out.getvalue())
            with redirect_stdout(io.StringIO()):
                cli_main(["init", "--key-file", str(k), "--dashboard-token-file", str(t)])
            self.assertEqual(k.read_text(), first)


if __name__ == "__main__":
    unittest.main()
