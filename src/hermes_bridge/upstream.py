"""Client for the Hermes dashboard backend (`hermes serve`/`hermes dashboard`) on localhost.

Authenticates with the backend's fixed session token (HERMES_DASHBOARD_SESSION_TOKEN on the
Hermes side), sent as ``X-Hermes-Session-Token`` exactly like the dashboard's own web UI.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

SESSION_HEADER = "X-Hermes-Session-Token"


@dataclass
class UpstreamResponse:
    status: int
    body: Any  # parsed JSON, or {"detail": text} when the body wasn't JSON


class UpstreamUnreachable(Exception):
    pass


class Dashboard:
    def __init__(self, base_url: str, token_fn, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._token_fn = token_fn  # read per request, so a rotated token file needs no restart
        self.timeout = timeout

    def request(self, method: str, path: str, *, query: Optional[dict] = None, body: Any = None,
                auth: bool = True, timeout: Optional[float] = None) -> UpstreamResponse:
        q = {k: v for k, v in (query or {}).items() if v is not None}
        url = self.base_url + path + (("?" + urllib.parse.urlencode(q)) if q else "")
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers[SESSION_HEADER] = self._token_fn()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return UpstreamResponse(resp.status, _parse(resp.read()))
        except urllib.error.HTTPError as exc:
            return UpstreamResponse(exc.code, _parse(exc.read()))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise UpstreamUnreachable(f"Hermes dashboard backend at {self.base_url} is unreachable: {reason}") from None


def _parse(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw.decode("utf-8", "replace")[:500]}


def quote(segment: str, safe: str = "") -> str:
    return urllib.parse.quote(segment, safe=safe)
