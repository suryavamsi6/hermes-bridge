"""Settings from environment variables, optionally overlaid on a TOML file (env wins)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ENV = {
    "listen": "HERMES_BRIDGE_LISTEN",
    "dashboard_url": "HERMES_BRIDGE_DASHBOARD_URL",
    "dashboard_token_file": "HERMES_BRIDGE_DASHBOARD_TOKEN_FILE",
    "key_file": "HERMES_BRIDGE_KEY_FILE",
    "base_path": "HERMES_BRIDGE_BASE_PATH",
    "dashboard_profile": "HERMES_BRIDGE_DASHBOARD_PROFILE",
    "timeout": "HERMES_BRIDGE_TIMEOUT",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    dashboard_url: str
    dashboard_token_file: Path
    key_file: Path
    base_path: str = ""          # e.g. "/bridge" when a proxy forwards the prefix unstripped
    dashboard_profile: str = "default"  # the profile the dashboard backend was launched as
    timeout: float = 30.0

    def dashboard_token(self) -> str:
        return _read_secret(self.dashboard_token_file, "dashboard token")

    def bridge_key(self) -> str:
        return _read_secret(self.key_file, "bridge key")


def _read_secret(path: Path, what: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read the {what} file {path}: {exc.strerror}") from None
    # Accept either a bare value or a single KEY=value line (env-file style).
    if "=" in value and "\n" not in value:
        value = value.split("=", 1)[1].strip()
    if len(value) < 16:
        raise ConfigError(f"the {what} in {path} is missing or shorter than 16 characters")
    return value


def load(config_file: str | None = None, environ: dict | None = None) -> Config:
    env = os.environ if environ is None else environ
    raw: dict = {}
    if config_file:
        with open(config_file, "rb") as fh:
            raw = tomllib.load(fh)
    for key, var in ENV.items():
        if env.get(var):
            raw[key] = env[var]
    listen = str(raw.get("listen", "127.0.0.1:8765"))
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ConfigError(f"listen must be HOST:PORT, got {listen!r}")
    for required in ("dashboard_token_file", "key_file"):
        if not raw.get(required):
            raise ConfigError(f"{required} is required (env {ENV[required]})")
    base = str(raw.get("base_path", "")).rstrip("/")
    if base and not base.startswith("/"):
        raise ConfigError("base_path must start with '/'")
    return Config(
        host=host.strip("[]"), port=int(port),
        dashboard_url=str(raw.get("dashboard_url", "http://127.0.0.1:9120")).rstrip("/"),
        dashboard_token_file=Path(raw["dashboard_token_file"]), key_file=Path(raw["key_file"]),
        base_path=base, dashboard_profile=str(raw.get("dashboard_profile", "default")),
        timeout=float(raw.get("timeout", 30)))
