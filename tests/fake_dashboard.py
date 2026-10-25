"""A fake Hermes dashboard backend (loopback, token mode) mirroring hermes-agent 4ea8d23c:
routes, ?profile= scoping, X-Hermes-Session-Token auth, and payload shapes from
hermes_cli/web_routers/*.py and web/src/lib/api.ts."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

TOKEN = "dash-token-0123456789abcdef"
PUBLIC = {"/api/health", "/api/status"}


class FakeHermes:
    def __init__(self):
        self.calls: list[str] = []
        self.auth_required = False
        self.broken: set[str] = set()  # paths that 404 (simulate API drift)
        self.profiles = {"default": {"display_name": None}, "jobwing": {"display_name": None}}
        self.env = {"default": {"API_SERVER_KEY": "main-key-xxxxxxxxxxxxxxxx", "OPENAI_API_KEY": "sk-secret-value"},
                    "jobwing": {}}
        self.mcp = {"default": [{"name": "github", "transport": "http", "url": "https://x", "command": None, "args": [],
                                 "env": {"GITHUB_TOKEN": "***"}, "auth": "header", "enabled": True, "tools": ["search"]}],
                    "jobwing": []}
        # Shaped like hermes-agent cron/jobs.py create_job + _normalize_job_record (schedule is an object).
        self.jobs = {"default": [
            {"id": "j1", "name": "Morning brief", "prompt": "Summarise my calendar, weather and unread mail for today.",
             "skills": [], "schedule": {"kind": "cron", "expr": "0 8 * * *", "display": "0 8 * * *"},
             "schedule_display": "0 8 * * *", "repeat": {"times": None, "completed": 12}, "enabled": True,
             "state": "scheduled", "paused_at": None, "next_run_at": "2026-10-26T08:00:00+05:30",
             "last_run_at": "2026-10-25T08:00:04+05:30", "last_status": "ok", "deliver": "origin",
             "origin": {"platform": "api", "chat_id": "bot-chat"}, "latest_execution": {"status": "ok", "duration_s": 41.2}},
            {"id": "j2", "name": "Disk check", "prompt": "", "skills": ["homelab-health"], "skill": "homelab-health",
             "schedule": {"kind": "interval", "minutes": 360, "display": "every 360m"}, "schedule_display": "every 360m",
             "enabled": False, "state": "paused", "paused_at": "2026-10-20T10:00:00+05:30", "next_run_at": None,
             "last_run_at": "2026-10-20T06:00:00+05:30", "last_status": "error", "deliver": "local"}],
                     "jobwing": []}
        self.plugin_enabled = {"web-tools": True}

    # -- server plumbing
    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self, method):
                parts = urlsplit(self.path)
                q = dict(parse_qsl(parts.query))
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.calls.append(f"{method} {parts.path}" + (f"?{parts.query}" if parts.query else ""))
                status, payload = fake.route(method, parts.path, q, body, self.headers.get("X-Hermes-Session-Token"))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = lambda s: s._go("GET")  # noqa: E731
            do_POST = lambda s: s._go("POST")  # noqa: E731
            do_PUT = lambda s: s._go("PUT")  # noqa: E731
            do_DELETE = lambda s: s._go("DELETE")  # noqa: E731

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()  # refuse new connections (a real "dashboard is down")

    # -- routes
    def route(self, method, path, q, body, token):
        if path in self.broken:
            return 404, {"detail": "Not Found"}
        if path.startswith("/api/") and path not in PUBLIC and token != TOKEN:
            return 401, {"detail": "Unauthorized"}
        prof = (body or {}).get("profile") or q.get("profile") or "default"
        if path.startswith("/api/") and prof not in self.profiles and prof != "all" and path not in PUBLIC:
            return 404, {"detail": f"Profile '{prof}' not found"}
        if (method, path) == ("GET", "/api/health"):
            return 200, {"ok": True, "version": "0.0.0", "displayVersion": "dev", "commit": "4ea8d23c",
                         "auth_required": self.auth_required}
        if (method, path) == ("GET", "/api/status"):
            return 200, {"version": "0.0.0", "release_date": "2026.9.24", "gateway_running": True}
        if (method, path) == ("GET", "/api/profiles"):
            return 200, {"profiles": [{"name": n, "path": f"/h/{n}", "is_default": n == "default", "model": "m",
                                       "provider": "p", "has_env": True, "skill_count": 3, "gateway_running": True,
                                       "description": f"{n} bot", "description_auto": False,
                                       **({"display_name": v["display_name"]} if v["display_name"] else {})}
                                      for n, v in self.profiles.items()]}
        if (method, path) == ("POST", "/api/profiles"):
            name = body["name"]
            if name in self.profiles:
                return 400, {"detail": f"Profile '{name}' already exists"}
            self.profiles[name] = {"display_name": None}
            self.env[name], self.mcp[name], self.jobs[name] = {}, [], []
            return 200, {"ok": True, "name": name, "path": f"/h/{name}", "model_set": False, "model_error": "",
                         "mcp_written": 0, "skills_disabled": 0}
        if (method, path) == ("GET", "/api/env"):
            return 200, {k: {"is_set": bool(v), "redacted_value": v[:4] + "…", "description": ""}
                         for k, v in self.env[prof].items()}
        if (method, path) == ("PUT", "/api/env"):
            self.env[prof][body["key"]] = body["value"]
            return 200, {"ok": True}
        if (method, path) == ("POST", "/api/gateway/restart"):
            return 200, {"ok": True, "pid": 42, "name": "gateway-restart"}
        if (method, path) == ("GET", "/api/mcp/servers"):
            return 200, {"servers": self.mcp[prof]}
        if (method, path) == ("POST", "/api/mcp/servers"):
            # Stored like upstream _mcp_server_summary: the bearer token goes to .env, never into the row.
            row = {"name": body["name"], "transport": "http" if body.get("url") else "stdio", "url": body.get("url"),
                   "command": body.get("command"), "args": body.get("args") or [], "env": body.get("env") or {},
                   "auth": "header" if body.get("bearer_token") else body.get("auth"), "enabled": True, "tools": None,
                   "source": "config", "plugin": None}
            if body.get("bearer_token"):
                self.env[prof][f"MCP_{body['name'].upper()}_API_KEY"] = body["bearer_token"]
            self.mcp[prof].append(row)
            return 200, {"ok": True, "name": body["name"]}
        if m := re.fullmatch(r"/api/mcp/servers/([^/]+)", path):
            self.mcp[prof] = [s for s in self.mcp[prof] if s["name"] != m[1]]
            return 200, {"ok": True}
        if m := re.fullmatch(r"/api/mcp/servers/([^/]+)/enabled", path):
            return 200, {"ok": True, "name": m[1], "enabled": body["enabled"]}
        if (method, path) == ("GET", "/api/mcp/catalog"):
            return 200, {"entries": [{"name": "time", "installed": False}], "diagnostics": []}
        if (method, path) == ("GET", "/api/cron/jobs"):
            return 200, self.jobs[prof] if prof != "all" else sum(self.jobs.values(), [])
        if m := re.fullmatch(r"/api/cron/jobs/([^/]+)/(pause|resume|trigger)", path):
            return 200, {"id": m[1], "action": m[2], "profile": prof}
        if m := re.fullmatch(r"/api/cron/jobs/([^/]+)/runs", path):
            return 200, {"runs": [], "limit": int(q.get("limit", 20))}
        if (method, path) == ("GET", "/api/skills"):
            return 200, [{"name": "web", "description": "d", "category": "c", "enabled": True}]
        if (method, path) == ("PUT", "/api/skills/toggle"):
            return 200, {"ok": True, "name": body["name"], "enabled": body["enabled"]}
        if (method, path) == ("GET", "/api/tools/toolsets"):
            return 200, [{"name": "web-tools", "label": "Web", "description": "", "platform": "cli",
                          "platform_label": "CLI", "enabled": True, "configured": True, "tools": ["fetch", "search"]}]
        if (method, path) == ("GET", "/api/dashboard/plugins/hub"):
            return 200, {"plugins": [{"name": n, "version": "1", "description": "", "source": "user",
                                      "runtime_status": "enabled" if on else "disabled", "auth_required": False}
                                     for n, on in self.plugin_enabled.items()],
                         "orphan_dashboard_plugins": [], "providers": {"memory_provider": "builtin"}}
        if (method, path) == ("GET", "/api/dashboard/plugins/catalog"):
            return 200, {"entries": [
                {"name": "spotify", "title": "Spotify", "description": "Control playback", "maintainer": "nous",
                 "tier": "official", "category": "media", "version": "1.2.0", "repo": "https://github.com/x/y",
                 "sha": "a" * 40, "installed": "spotify" in self.plugin_enabled, "update_available": False,
                 "runtime_status": None, "capability_summary": "1 tool",
                 "capabilities": {"provides_tools": ["play"], "requires_env": ["SPOTIFY_CLIENT_ID"]}}],
                "removed": [], "generated_at": "2026-10-25T00:00:00Z"}
        if (method, path) == ("POST", "/api/dashboard/agent-plugins/install"):
            self.last_install = body
            if body.get("catalog_name") != "spotify":
                return 400, {"detail": "Unknown catalog entry"}
            self.plugin_enabled["spotify"] = True
            return 200, {"ok": True, "name": "spotify", "after_install_path": "/srv/x"}
        if m := re.fullmatch(r"/api/dashboard/agent-plugins/(.+)/(enable|disable)", path):
            self.plugin_enabled[m[1]] = m[2] == "enable"
            return 200, {"ok": True}
        if (method, path) == ("GET", "/api/memory"):
            return 200, {"active": "builtin", "providers": [], "builtin_files": {"memory": 10, "user": 5}}
        if m := re.fullmatch(r"/api/memory/providers/([^/]+)/config", path):
            return 200, ({"name": m[1], "fields": [{"key": "api_key", "kind": "secret", "value": "", "is_set": True}]}
                         if method == "GET" else {"ok": True})
        return 404, {"detail": "Not Found"}
