"""Route table and handlers. Everything the bridge can reach upstream is listed in ROUTES below;
any other path is a 404 that never touches the dashboard (no generic proxying).

Never forwarded: terminal/PTY, files, /api/env reads (only API_SERVER_KEY's is_set flag is
read internally), /api/env/reveal, config writes, backups, credentials, webhooks, OAuth, chat.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Callable, Optional

from . import TESTED_HERMES, __version__
from .config import Config, ConfigError
from .upstream import Dashboard, UpstreamResponse, UpstreamUnreachable, quote

log = logging.getLogger("hermes_bridge")

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # hermes_constants.PROFILE_ID_RE
NAME_RE = r"[^/]{1,200}"
MAX_BODY = 64 * 1024
PROBE_TIMEOUT = 5.0  # compat probes: a hung dashboard must not stall every request for the full timeout


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


Result = tuple[int, Any]

# Read-only probes that prove the dashboard speaks the API this bridge was written against.
# (path, expected top-level shape: "list" or the key that must be present)
COMPAT_PROBES: list[tuple[str, str]] = [
    ("/api/profiles", "profiles"),
    ("/api/mcp/servers", "servers"),
    ("/api/cron/jobs", "list"),
    ("/api/skills", "list"),
    ("/api/tools/toolsets", "list"),
    ("/api/dashboard/plugins/hub", "plugins"),
    ("/api/memory", "providers"),
]


class Bridge:
    def __init__(self, cfg: Config, dashboard: Optional[Dashboard] = None):
        self.cfg = cfg
        self.dash = dashboard or Dashboard(cfg.dashboard_url, cfg.dashboard_token, cfg.timeout)
        self._compat: Optional[tuple[float, dict]] = None
        self._compat_lock = threading.Lock()
        self.routes: list[tuple[str, re.Pattern, Callable[..., Result], bool, bool]] = []
        r = self._route
        r("GET", r"/v1/health", self.health, auth=False)
        r("GET", r"/v1/status", self.status, gate=False)  # reports the compat result itself
        r("GET", r"/v1/profiles", self.list_profiles)
        r("POST", r"/v1/profiles", self.create_profile)
        r("POST", rf"/v1/profiles/(?P<p>{NAME_RE})/key", self.rotate_key)
        r("POST", r"/v1/gateway/restart", self.restart_gateway)
        P = rf"/v1/profiles/(?P<p>{NAME_RE})"
        r("GET", P + r"/mcp", self.mcp_list)
        r("POST", P + r"/mcp", self.mcp_add)
        r("DELETE", P + rf"/mcp/(?P<name>{NAME_RE})", self.mcp_remove)
        r("PUT", P + rf"/mcp/(?P<name>{NAME_RE})/enabled", self.mcp_enabled)
        r("POST", P + rf"/mcp/(?P<name>{NAME_RE})/test", self.mcp_test)
        r("GET", P + r"/mcp-catalog", self.mcp_catalog)
        r("POST", P + r"/mcp-catalog/install", self.mcp_catalog_install)
        r("GET", P + r"/jobs", self.jobs_list)
        r("GET", P + rf"/jobs/(?P<job>{NAME_RE})/runs", self.job_runs)
        r("POST", P + rf"/jobs/(?P<job>{NAME_RE})/(?P<action>pause|resume|trigger)", self.job_action)
        r("GET", P + r"/skills", self.skills_list)
        r("GET", P + r"/skills/content", self.skill_content)
        r("PUT", P + r"/skills/enabled", self.skill_toggle)
        r("GET", P + r"/toolsets", self.toolsets_list)
        r("PUT", P + rf"/toolsets/(?P<name>{NAME_RE})/enabled", self.toolset_toggle)
        r("GET", P + r"/plugins", self.plugins_list)
        r("POST", P + r"/plugins/(?P<name>.{1,200})/(?P<action>enable|disable)", self.plugin_toggle)
        r("GET", P + r"/plugin-catalog", self.plugins_catalog)
        r("POST", P + r"/plugin-catalog/install", self.plugin_catalog_install)
        r("GET", P + r"/memory", self.memory_status)
        r("GET", P + rf"/memory/providers/(?P<name>{NAME_RE})/config", self.memory_config_get)
        r("PUT", P + rf"/memory/providers/(?P<name>{NAME_RE})/config", self.memory_config_put)
        r("GET", P + r"/soul", self.soul_get)
        r("PUT", P + r"/soul", self.soul_put)

    def _route(self, method: str, pattern: str, fn: Callable[..., Result], auth: bool = True, gate: bool = True) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), fn, auth, gate and auth))

    # ------------------------------------------------------------------ dispatch
    def handle(self, method: str, path: str, query: dict[str, str], headers: dict[str, str],
               body: bytes) -> Result:
        try:
            return self._handle(method, path, query, headers, body)
        except HttpError as e:
            return e.status, {"error": {"code": e.code, "message": e.message, **e.extra}}
        except UpstreamUnreachable as e:
            return 502, {"error": {"code": "dashboard_unreachable", "message": str(e)}}
        except ConfigError as e:
            log.error("configuration error: %s", e)
            return 500, {"error": {"code": "bridge_misconfigured", "message": str(e)}}

    def _handle(self, method, path, query, headers, body) -> Result:
        if self.cfg.base_path and (path == self.cfg.base_path or path.startswith(self.cfg.base_path + "/")):
            path = path[len(self.cfg.base_path):] or "/"
        allowed = False
        for m, pattern, fn, auth, gate in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            allowed = True
            if m != method:
                continue
            if auth:
                self._authorize(headers)
            if gate:
                self._ensure_compatible()
            params = {k: _unquote(v) for k, v in match.groupdict().items()}
            for v in params.values():
                if any(seg in (".", "..") for seg in v.split("/")) or any(ord(c) < 32 for c in v):
                    raise HttpError(400, "invalid_path", "Path parameters may not contain '.', '..' or control characters.")
            if "p" in params and not PROFILE_ID_RE.match(params["p"]):
                raise HttpError(400, "invalid_profile", "Profile ids are lowercase letters, digits, '-' and '_'.")
            return fn(query=query, body=_json_body(body) if method in ("POST", "PUT") else None, **params)
        if allowed:
            raise HttpError(405, "method_not_allowed", f"{method} is not allowed on {path}")
        raise HttpError(404, "not_found", "No such bridge endpoint.")

    def _authorize(self, headers: dict[str, str]) -> None:
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token.encode(), self.cfg.bridge_key().encode()):
            time.sleep(0.25)  # blunt online guessing a little; the key itself is 256 bits
            raise HttpError(401, "unauthorized", "Missing or wrong bridge key.")

    # ------------------------------------------------------------------ compat
    def compat(self, force: bool = False) -> dict:
        with self._compat_lock:
            now = time.monotonic()
            if self._compat and not force:
                at, result = self._compat
                if now - at < (300 if result["ok"] else 10):
                    return result
            result = self._probe()
            self._compat = (now, result)
            if not result["ok"]:
                log.error("Hermes dashboard API check failed: %s", result["message"])
            return result

    def _probe(self) -> dict:
        try:
            health = self.dash.request("GET", "/api/health", auth=False, timeout=PROBE_TIMEOUT)
        except UpstreamUnreachable as e:
            return {"ok": False, "code": "dashboard_unreachable", "message": str(e), "checks": []}
        if health.status != 200 or not isinstance(health.body, dict) or health.body.get("ok") is not True:
            return _incompatible("/api/health", health, "is this a Hermes dashboard backend?")
        if health.body.get("auth_required"):
            return {"ok": False, "code": "dashboard_gated", "checks": [], "message":
                    "The Hermes backend the bridge talks to is in browser-login mode, so the bridge's session "
                    "token is refused. Hermes turns that mode on when the backend is bound to a non-loopback "
                    "address, or when dashboard.public_url / HERMES_DASHBOARD_PUBLIC_URL names a non-loopback "
                    "host (even for a backend on 127.0.0.1). Fix: run the bridge's backend as `hermes serve "
                    "--host 127.0.0.1` with HERMES_DASHBOARD_SESSION_TOKEN and "
                    "HERMES_DASHBOARD_PUBLIC_URL=http://127.0.0.1:<port> in its process environment, and keep "
                    "HERMES_DASHBOARD_PUBLIC_URL out of ~/.hermes/.env (that file overrides the process "
                    "environment; put the LAN address in config.yaml dashboard.public_url instead)."}
        status = self.dash.request("GET", "/api/status", auth=False, timeout=PROBE_TIMEOUT)
        hermes = {"version": health.body.get("version"), "commit": health.body.get("commit"),
                  "release_date": status.body.get("release_date") if isinstance(status.body, dict) else None}
        checks = []
        for path, shape in COMPAT_PROBES:
            resp = self.dash.request("GET", path, timeout=PROBE_TIMEOUT)
            if resp.status in (401, 403):
                return {"ok": False, "code": "dashboard_token_rejected", "hermes": hermes, "checks": checks,
                        "message": "The dashboard backend rejected the bridge's session token. The token file must "
                                   "hold the same value the backend was started with (HERMES_DASHBOARD_SESSION_TOKEN)."}
            good = resp.status == 200 and (isinstance(resp.body, list) if shape == "list"
                                           else isinstance(resp.body, dict) and shape in resp.body)
            checks.append({"path": path, "ok": good, "status": resp.status})
            if not good:
                return {**_incompatible(path, resp, "expected " + ("a list" if shape == "list" else f"a '{shape}' field")),
                        "hermes": hermes, "checks": checks}
        older = (hermes["release_date"] or "") < TESTED_HERMES["release_date"] if hermes["release_date"] else False
        return {"ok": True, "hermes": hermes, "tested_against": TESTED_HERMES, "checks": checks,
                "warning": ("This Hermes is older than the release the bridge was tested against." if older else None)}

    def _ensure_compatible(self) -> None:
        result = self.compat()
        if not result["ok"]:
            raise HttpError(503, result.get("code", "dashboard_incompatible"), result["message"])

    # ------------------------------------------------------------------ helpers
    def _call(self, method: str, path: str, *, ok=(200, 201), **kw) -> Any:
        resp = self.dash.request(method, path, **kw)
        if resp.status in ok:
            return resp.body
        raise _upstream_error(resp)

    # ------------------------------------------------------------------ endpoints
    def health(self, **_) -> Result:
        return 200, {"ok": True, "service": "hermes-bridge", "version": __version__}

    def status(self, **_) -> Result:
        result = self.compat(force=True)
        return (200 if result["ok"] else 503), {"bridge": {"version": __version__}, **result}

    def list_profiles(self, **_) -> Result:
        body = self._call("GET", "/api/profiles")
        out = []
        for p in body.get("profiles", []):
            name = p.get("name")
            if not name:
                continue
            out.append({
                "id": name, "display_name": p.get("display_name") or name,
                "description": p.get("description") or "", "is_default": bool(p.get("is_default")),
                "model": p.get("model"), "provider": p.get("provider"),
                "gateway_running": bool(p.get("gateway_running")), "skill_count": p.get("skill_count"),
                "api_key_set": self._api_key_set(name)})
        return 200, {"profiles": out}

    def _api_key_set(self, profile: str) -> Optional[bool]:
        """Whether the profile's .env has API_SERVER_KEY. Reads /api/env internally and keeps
        only the boolean; values (even redacted previews) never leave this function."""
        resp = self.dash.request("GET", "/api/env", query={"profile": profile})
        if resp.status != 200 or not isinstance(resp.body, dict):
            return None
        row = resp.body.get("API_SERVER_KEY")
        return bool(row.get("is_set")) if isinstance(row, dict) else False

    def create_profile(self, body: dict, **_) -> Result:
        name = str(body.get("name") or "").strip()
        if not PROFILE_ID_RE.match(name) or name == "default":
            raise HttpError(400, "invalid_profile", "Pick a lowercase id: letters, digits, '-' or '_' (not 'default').")
        payload = _pick(body, {"description": str, "clone_from": str, "provider": str, "model": str, "soul": str})
        payload["name"] = name
        created = self._call("POST", "/api/profiles", body=payload)
        key = secrets.token_hex(32)
        resp = self.dash.request("PUT", "/api/env", query={"profile": name},
                                 body={"key": "API_SERVER_KEY", "value": key, "profile": name})
        if resp.status != 200:
            err = _upstream_error(resp)
            raise HttpError(502, "key_provision_failed",
                            f"Profile '{name}' was created, but writing its API key failed: {err.message}. "
                            "Retry with POST /v1/profiles/{name}/key.", profile_created=True)
        return 201, {"profile": {"id": name, "display_name": name, "description": payload.get("description", ""),
                                 "model_set": created.get("model_set"), "model_error": created.get("model_error")},
                     "api_key": key,
                     "note": "Shown once. Store it in the app; the bridge cannot show it again."}

    def rotate_key(self, p: str, **_) -> Result:
        if p == "default":
            raise HttpError(409, "default_key_managed",
                            "The default profile's API key is the server's main API_SERVER_KEY; change it on the server.")
        self._require_profile(p)
        key = secrets.token_hex(32)
        self._call("PUT", "/api/env", query={"profile": p}, body={"key": "API_SERVER_KEY", "value": key, "profile": p})
        return 200, {"profile": p, "api_key": key,
                     "note": "Shown once. Any other client using the old key for this profile must be updated."}

    def _require_profile(self, p: str) -> None:
        names = {x.get("name") for x in self._call("GET", "/api/profiles").get("profiles", [])}
        if p not in names:
            raise HttpError(404, "unknown_profile", f"No Hermes profile named '{p}'.")

    def restart_gateway(self, **_) -> Result:
        return 202, self._call("POST", "/api/gateway/restart")

    # MCP
    def mcp_list(self, p: str, **_) -> Result:
        body = self._call("GET", "/api/mcp/servers", query={"profile": p})
        servers = []
        for s in body.get("servers", []):
            s = dict(s)
            env = s.pop("env", None) or {}
            s["env_keys"] = sorted(env) if isinstance(env, dict) else []  # names only, never values
            servers.append(s)
        return 200, {"servers": servers}

    def mcp_add(self, p: str, body: dict, **_) -> Result:
        payload = _pick(body, {"name": str, "url": str, "command": str, "args": list, "env": dict,
                               "auth": str, "bearer_token": str})
        if not payload.get("name"):
            raise HttpError(400, "invalid_body", "name is required")
        payload["profile"] = p
        res = self._call("POST", "/api/mcp/servers", query={"profile": p}, body=payload)
        if isinstance(res, dict):
            res.pop("env", None)
        return 201, res

    def mcp_remove(self, p: str, name: str, **_) -> Result:
        return 200, self._call("DELETE", f"/api/mcp/servers/{quote(name)}", query={"profile": p})

    def mcp_enabled(self, p: str, name: str, body: dict, **_) -> Result:
        enabled = _bool(body, "enabled")
        return 200, self._call("PUT", f"/api/mcp/servers/{quote(name)}/enabled", query={"profile": p},
                               body={"enabled": enabled, "profile": p})

    def mcp_test(self, p: str, name: str, **_) -> Result:
        return 200, self._call("POST", f"/api/mcp/servers/{quote(name)}/test", query={"profile": p})

    def mcp_catalog(self, p: str, **_) -> Result:
        return 200, self._call("GET", "/api/mcp/catalog", query={"profile": p})

    def mcp_catalog_install(self, p: str, body: dict, **_) -> Result:
        payload = _pick(body, {"name": str, "env": dict, "enable": bool})
        if not payload.get("name"):
            raise HttpError(400, "invalid_body", "name is required")
        payload["profile"] = p
        return 200, self._call("POST", "/api/mcp/catalog/install", query={"profile": p}, body=payload)

    # Cron
    def jobs_list(self, p: str, **_) -> Result:
        return 200, {"jobs": self._call("GET", "/api/cron/jobs", query={"profile": p})}

    def job_runs(self, p: str, job: str, query: dict, **_) -> Result:
        limit = query.get("limit", "20")
        limit = limit if limit.isdigit() and 0 < int(limit) <= 100 else "20"
        return 200, self._call("GET", f"/api/cron/jobs/{quote(job)}/runs", query={"profile": p, "limit": limit})

    def job_action(self, p: str, job: str, action: str, **_) -> Result:
        return 200, self._call("POST", f"/api/cron/jobs/{quote(job)}/{action}", query={"profile": p})

    # Skills / toolsets
    def skills_list(self, p: str, **_) -> Result:
        return 200, {"skills": self._call("GET", "/api/skills", query={"profile": p})}

    def skill_content(self, p: str, query: dict, **_) -> Result:
        name = query.get("name", "")
        if not name:
            raise HttpError(400, "invalid_query", "name is required")
        return 200, self._call("GET", "/api/skills/content", query={"name": name, "profile": p})

    def skill_toggle(self, p: str, body: dict, **_) -> Result:
        name = str(body.get("name") or "")
        if not name:
            raise HttpError(400, "invalid_body", "name is required")
        return 200, self._call("PUT", "/api/skills/toggle", query={"profile": p},
                               body={"name": name, "enabled": _bool(body, "enabled"), "profile": p})

    def toolsets_list(self, p: str, **_) -> Result:
        return 200, {"toolsets": self._call("GET", "/api/tools/toolsets", query={"profile": p})}

    def toolset_toggle(self, p: str, name: str, body: dict, **_) -> Result:
        return 200, self._call("PUT", f"/api/tools/toolsets/{quote(name)}", query={"profile": p},
                               body={"enabled": _bool(body, "enabled"), "profile": p})

    # Plugins
    def plugins_list(self, p: str, **_) -> Result:
        hub = self._call("GET", "/api/dashboard/plugins/hub", query={"profile": p})
        toolsets = self._call("GET", "/api/tools/toolsets", query={"profile": p})
        tools_by_name = {t.get("name"): t.get("tools") or [] for t in toolsets if isinstance(t, dict)}
        plugins = []
        for row in hub.get("plugins", []):
            name = row.get("name", "")
            plugins.append({
                "name": name, "version": row.get("version"), "description": row.get("description") or "",
                "source": row.get("source"), "runtime_status": row.get("runtime_status"),
                "enabled": row.get("runtime_status") == "enabled", "auth_required": bool(row.get("auth_required")),
                # Hermes exposes tools per toolset, not per plugin; a toolset named like the plugin is its tools.
                "tools": tools_by_name.get(name) or tools_by_name.get(name.rsplit("/", 1)[-1]) or []})
        return 200, {"plugins": plugins, "toggle_supported": p == self.cfg.dashboard_profile,
                     "providers": hub.get("providers") or {}}

    def plugin_toggle(self, p: str, name: str, action: str, **_) -> Result:
        if p != self.cfg.dashboard_profile:
            raise HttpError(409, "plugin_toggle_unsupported",
                            f"Hermes's dashboard API turns plugins on and off only for the profile it runs as "
                            f"('{self.cfg.dashboard_profile}'). On the server run: hermes -p {p} plugins {action} {name}")
        return 200, self._call("POST", f"/api/dashboard/agent-plugins/{quote(name, safe='/')}/{action}")

    # Plugin marketplace (Hermes's curated catalog). Installs go by catalog name only, so the bridge
    # never forwards an arbitrary git URL; Hermes resolves the repo and its pinned commit itself.
    _CATALOG_FIELDS = ("name", "title", "description", "maintainer", "tier", "category", "version", "docs_url",
                       "image", "installed", "update_available", "runtime_status", "capability_summary")

    def plugins_catalog(self, p: str, **_) -> Result:
        body = self._call("GET", "/api/dashboard/plugins/catalog")
        entries = []
        for e in body.get("entries", []) if isinstance(body, dict) else []:
            if not isinstance(e, dict):
                continue
            row = {k: e.get(k) for k in self._CATALOG_FIELDS}
            caps = e.get("capabilities") if isinstance(e.get("capabilities"), dict) else {}
            row["requires_env"] = list(caps.get("requires_env") or [])
            row["provides_tools"] = list(caps.get("provides_tools") or [])
            entries.append(row)
        return 200, {"entries": entries, "install_supported": p == self.cfg.dashboard_profile}

    def plugin_catalog_install(self, p: str, body: dict, **_) -> Result:
        name = str(body.get("name") or "")
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", name):
            raise HttpError(400, "invalid_body", "name must be a catalog name ([a-z0-9_-], up to 64 characters)")
        if p != self.cfg.dashboard_profile:
            raise HttpError(409, "plugin_install_unsupported",
                            f"Hermes's dashboard API installs plugins only for the profile it runs as "
                            f"('{self.cfg.dashboard_profile}'). On the server run: hermes -p {p} plugins install {name}")
        # Install clones the plugin's repo, so allow it far longer than an ordinary call.
        return 200, self._call("POST", "/api/dashboard/agent-plugins/install", timeout=max(self.cfg.timeout, 180.0),
                               body={"identifier": "", "catalog_name": name, "enable": True, "force": False})

    # Memory
    def memory_status(self, p: str, **_) -> Result:
        return 200, self._call("GET", "/api/memory", query={"profile": p})

    def memory_config_get(self, p: str, name: str, **_) -> Result:
        return 200, self._call("GET", f"/api/memory/providers/{quote(name)}/config", query={"profile": p})

    def memory_config_put(self, p: str, name: str, body: dict, **_) -> Result:
        values = body.get("values")
        if not isinstance(values, dict):
            raise HttpError(400, "invalid_body", "values must be an object")
        return 200, self._call("PUT", f"/api/memory/providers/{quote(name)}/config", query={"profile": p},
                               body={"values": values})

    # The profile's persona (SOUL.md) via the dashboard's /api/profiles/{name}/soul. Writes are
    # size-capped and must be text; the dashboard writes atomically.
    def soul_get(self, p: str, **_) -> Result:
        self._require_profile(p)
        body = self._call("GET", f"/api/profiles/{quote(p)}/soul")
        return 200, {"content": str(body.get("content") or ""), "exists": bool(body.get("exists"))}

    def soul_put(self, p: str, body: dict, **_) -> Result:
        content = body.get("content")
        if not isinstance(content, str):
            raise HttpError(400, "invalid_body", "content must be a string")
        if len(content.encode("utf-8")) > SOUL_MAX_BYTES:
            raise HttpError(413, "too_large", f"SOUL.md is limited to {SOUL_MAX_BYTES // 1024} KB.")
        self._require_profile(p)
        self._call("PUT", f"/api/profiles/{quote(p)}/soul", body={"content": content})
        return 200, {"ok": True}


SOUL_MAX_BYTES = 48 * 1024  # under MAX_BODY, leaving room for JSON escaping


# ---------------------------------------------------------------------- small helpers
def _incompatible(path: str, resp: UpstreamResponse, why: str) -> dict:
    return {"ok": False, "code": "dashboard_incompatible", "checks": [],
            "message": f"Hermes dashboard API mismatch at {path} (HTTP {resp.status}; {why}). hermes-bridge "
                       f"{__version__} was tested against hermes-agent {TESTED_HERMES['release_date']} "
                       f"({TESTED_HERMES['commit']}); update the bridge or Hermes."}


def _upstream_error(resp: UpstreamResponse) -> HttpError:
    detail = resp.body.get("detail") if isinstance(resp.body, dict) else None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    message = str(detail or (resp.body.get("error") if isinstance(resp.body, dict) else "") or f"HTTP {resp.status}")
    if resp.status in (401, 403):
        return HttpError(502, "dashboard_token_rejected", "The Hermes dashboard rejected the bridge's session token.")
    status = resp.status if 400 <= resp.status < 500 else 502
    return HttpError(status, f"hermes_{resp.status}", message)


def _json_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HttpError(400, "invalid_json", "Body must be JSON.") from None
    if not isinstance(body, dict):
        raise HttpError(400, "invalid_json", "Body must be a JSON object.")
    return body


def _pick(body: dict, fields: dict[str, type]) -> dict:
    out = {}
    for key, typ in fields.items():
        if key in body and body[key] is not None:
            if not isinstance(body[key], typ) or (typ is int and isinstance(body[key], bool)):
                raise HttpError(400, "invalid_body", f"{key} must be {typ.__name__}")
            out[key] = body[key]
    return out


def _bool(body: dict, key: str) -> bool:
    if not isinstance(body.get(key), bool):
        raise HttpError(400, "invalid_body", f"{key} must be true or false")
    return body[key]


def _unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)
