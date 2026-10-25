"""CLI: `hermes-bridge serve | init | check`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from . import __version__
from .app import Bridge
from .config import ConfigError, load
from .server import serve


def _init_secret(path: Path) -> bool:
    """Create PATH with a fresh 256-bit hex secret (mode 0600) unless it already exists."""
    if path.exists() and path.stat().st_size > 0:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(32) + "\n")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-bridge", description="Allowlisting management API for Hermes Agent.")
    ap.add_argument("--config", help="TOML config file (environment variables override it)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Run the bridge (default)")
    init = sub.add_parser("init", help="Create the key and dashboard-token files if missing (never prints them)")
    init.add_argument("--key-file", required=True)
    init.add_argument("--dashboard-token-file", required=True)
    sub.add_parser("check", help="Probe the Hermes dashboard API and print the compatibility report")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "init":
        for label, p in (("bridge key", args.key_file), ("dashboard token", args.dashboard_token_file)):
            made = _init_secret(Path(p))
            print(f"{label}: {'created' if made else 'kept existing'} {p}")
        return 0
    try:
        cfg = load(args.config)
        cfg.bridge_key(), cfg.dashboard_token()  # fail fast on unreadable/short secrets
    except (ConfigError, OSError) as exc:
        print(f"hermes-bridge: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG
    bridge = Bridge(cfg)
    if args.cmd == "check":
        result = bridge.compat(force=True)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    result = bridge.compat(force=True)
    if result["ok"]:
        logging.info("Hermes dashboard API OK (hermes %s)", result.get("hermes"))
    serve(bridge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
