# hermes-bridge

A small, allowlisting management API for a self-hosted [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(by Nous Research). It lets a phone app (built for [Hermes Mobile](https://github.com/suryavamsi6/hermes-app))
manage profiles, MCP servers, routines, skills, toolsets, plugins, memory and personas **without exposing
the Hermes dashboard**.

- Standard library only (Python 3.11+). No runtime dependencies. Runs on any Linux: bare metal, Docker,
  Incus/LXC, a VPS, a Raspberry Pi.
- Forwards an **allowlist** of the dashboard's own REST API (documented by Hermes under *Web Dashboard →
  REST API*). Any path not in its route table is a 404 that never reaches Hermes.
- Never forwards the dashboard's terminal/PTY, file browser, secret reveal, raw `/api/env` reads, config
  writes, backups, credentials or OAuth routes.
- Checks on startup, and every 5 minutes, that every dashboard route it uses still answers in the shape it
  expects. If one doesn't, it says so instead of guessing.

Chat doesn't go through the bridge. Chat stays on Hermes's own API server (`/v1/…`, `/p/<profile>/…`).
**The bridge is optional**: Hermes Mobile chats fine without it.

## Why it exists

Hermes's API server (port 8642) is built for chat. Management (profiles, MCP, cron jobs, plugins, skills,
memory) lives in the **web dashboard API**, which is far too powerful to put on the internet: it includes a
terminal, a file browser and secret reveal. The bridge is a narrow door into it:

- it exposes only the routes a mobile client needs, each validated;
- it has its own key, separate from your Hermes chat keys;
- it adds three things the dashboard can't do in one call: a profile list with display names and
  "has a chat key" flags, **creating a profile together with its own chat key** (written into that
  profile's `.env`, returned once), and **rotating a profile's chat key**.

## How it fits together

```
phone ──HTTPS──▶ your proxy (Tailscale Funnel, Caddy, nginx, Cloudflare Tunnel…)
                   ├─ /        ──▶ Hermes API server   127.0.0.1:8642  (chat; per-profile API_SERVER_KEY)
                   └─ /bridge  ──▶ hermes-bridge       127.0.0.1:8765  (management; bridge key)
                                     └─ loopback ──▶ `hermes serve`   127.0.0.1:9120  (fixed session token)
```

The bridge needs a Hermes dashboard backend that accepts a **fixed session token**: a loopback-only
`hermes serve` started with `HERMES_DASHBOARD_SESSION_TOKEN` in its **process environment** (a line in
`~/.hermes/.env` is ignored for this variable). A dashboard bound to a LAN or public address uses browser
login instead, and the bridge refuses it with an explanation.

Two gotchas, both handled by the example units:

- **Browser login can switch on even on loopback.** If `dashboard.public_url` (or
  `HERMES_DASHBOARD_PUBLIC_URL`) names a non-loopback host, which is common when a browser dashboard sits
  behind a reverse proxy, Hermes gates *every* dashboard process behind browser login. The backend unit sets
  `HERMES_DASHBOARD_PUBLIC_URL=http://127.0.0.1:9120` for itself, which beats `config.yaml`. Keep your
  browser dashboard's public URL in `config.yaml`, **not** in `~/.hermes/.env` (Hermes loads `.env` over the
  process environment, so a value there would gate the private backend again).
- **Start order.** If you already run a browser dashboard, leave it alone and start a second, private backend
  with `--isolated`. Start it *after* your main dashboard: the first dashboard on a host owns Hermes's
  "host backend" record, and a headless one owning it makes a later `hermes dashboard` restart refuse to start.

## Quick start (any Linux, as the user that runs Hermes)

Run these where Hermes runs (the host, or inside its container), as the same user.

```bash
# 0. Get the code
git clone https://github.com/suryavamsi6/hermes-bridge.git && cd hermes-bridge
pipx install .                      # or: uv tool install .   or: pip install --user .

# 1. Secrets: a bridge key for clients, and a session token shared with the private Hermes backend.
#    Created with mode 0600, 256-bit random, never printed.
hermes-bridge init --key-file ~/.config/hermes-bridge/bridge-key \
                   --dashboard-token-file ~/.config/hermes-bridge/dashboard-token
printf 'HERMES_DASHBOARD_SESSION_TOKEN=%s\n' "$(cat ~/.config/hermes-bridge/dashboard-token)" \
  > ~/.config/hermes-bridge/backend.env && chmod 600 ~/.config/hermes-bridge/backend.env

# 2. Run the private backend and the bridge (systemd user units; see below for other ways).
mkdir -p ~/.config/systemd/user
cp examples/hermes-bridge-backend.service examples/hermes-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-bridge-backend hermes-bridge
loginctl enable-linger "$USER"      # keep user services running after you log out

# 3. Check it
hermes-bridge check                 # needs the same env as the service, or: --config examples/hermes-bridge.toml
curl http://127.0.0.1:8765/v1/health
curl -H "Authorization: Bearer $(cat ~/.config/hermes-bridge/bridge-key)" http://127.0.0.1:8765/v1/status
```

Then expose it (next section) and paste the bridge key into the app (**Settings → Connection → Hermes
bridge key**). Read it once with `cat ~/.config/hermes-bridge/bridge-key`; don't paste it into chats or issues.

The example units assume `hermes` and `hermes-bridge` are in `~/.local/bin`; adjust `ExecStart` if not
(`command -v hermes`).

## Running it other ways

**Plain Python, no install** (handy for a first try):

```bash
export HERMES_BRIDGE_KEY_FILE=~/.config/hermes-bridge/bridge-key
export HERMES_BRIDGE_DASHBOARD_TOKEN_FILE=~/.config/hermes-bridge/dashboard-token
# in one terminal: the private backend
env HERMES_DASHBOARD_SESSION_TOKEN="$(cat $HERMES_BRIDGE_DASHBOARD_TOKEN_FILE)" \
    HERMES_DASHBOARD_PUBLIC_URL=http://127.0.0.1:9120 \
    hermes serve --isolated --host 127.0.0.1 --port 9120
# in another: the bridge
PYTHONPATH=src python3 -m hermes_bridge serve
# or with uv:  uvx --from . hermes-bridge serve
```

**Docker.** The image is just Python plus this package. Run it in the same network namespace as the private
Hermes backend (for example `--network host`, or the Hermes container's namespace with
`--network container:<hermes>`), and mount the two secret files:

```bash
docker build -t hermes-bridge .
docker run -d --name hermes-bridge --network host --restart unless-stopped \
  -e HERMES_BRIDGE_LISTEN=127.0.0.1:8765 -e HERMES_BRIDGE_DASHBOARD_URL=http://127.0.0.1:9120 \
  -v ~/.config/hermes-bridge/bridge-key:/run/secrets/hermes_bridge_key:ro \
  -v ~/.config/hermes-bridge/dashboard-token:/run/secrets/hermes_dashboard_token:ro \
  hermes-bridge
```

The private `hermes serve` backend still runs wherever Hermes runs (inside the Hermes container if Hermes is
containerised), with the same session token.

**Incus / LXC containers.** Run both units inside the Hermes container as the Hermes user. Bind the bridge to
the container's address (`HERMES_BRIDGE_LISTEN=<container-ip>:8765`) and point the host's proxy at it; keep
`hermes serve` on `127.0.0.1` inside the container so it's never reachable from outside.

**NixOS** (example, adapt to your setup): a system service running as the Hermes user.

```nix
{ pkgs, ... }:
let
  hermes-bridge = pkgs.python3Packages.buildPythonApplication {
    pname = "hermes-bridge"; version = "0.1.0"; pyproject = true;
    src = pkgs.fetchFromGitHub { owner = "suryavamsi6"; repo = "hermes-bridge"; rev = "main"; hash = pkgs.lib.fakeHash; };
    build-system = [ pkgs.python3Packages.setuptools ];
  };
in {
  systemd.services.hermes-bridge = {
    description = "hermes-bridge";
    after = [ "network.target" "hermes-bridge-backend.service" ];
    wantedBy = [ "multi-user.target" ];
    environment = {
      HERMES_BRIDGE_LISTEN = "127.0.0.1:8765";
      HERMES_BRIDGE_DASHBOARD_URL = "http://127.0.0.1:9120";
      HERMES_BRIDGE_KEY_FILE = "/var/lib/hermes-bridge/bridge-key";
      HERMES_BRIDGE_DASHBOARD_TOKEN_FILE = "/var/lib/hermes-bridge/dashboard-token";
    };
    serviceConfig = {
      User = "hermes";                       # the user that owns ~/.hermes
      ExecStart = "${hermes-bridge}/bin/hermes-bridge serve";
      Restart = "on-failure";
      NoNewPrivileges = true; ProtectSystem = "strict"; PrivateTmp = true;
    };
  };
  # Add a matching hermes-bridge-backend service that runs
  #   hermes serve --isolated --host 127.0.0.1 --port 9120
  # with HERMES_DASHBOARD_SESSION_TOKEN (EnvironmentFile) and HERMES_DASHBOARD_PUBLIC_URL=http://127.0.0.1:9120,
  # ordered after your browser dashboard. Generate the two secret files once with `hermes-bridge init`.
}
```

Replace `fakeHash` with the hash Nix reports on the first build. If your secrets manager (agenix, sops-nix)
already provides files, point the two `*_FILE` variables at them.

## Exposing it safely

- **Keep the bridge and the backend on loopback** (or a private container network). Put TLS in a proxy.
- **Never expose the Hermes dashboard ports (9119, 9120) publicly**, not even behind basic auth. The private
  backend on 9120 has a terminal and file access; only the bridge should talk to it.
- **Tailscale Funnel** (public HTTPS, no domain or open ports). Funnel strips the mount path:
  ```bash
  sudo tailscale funnel --bg --https=443 http://127.0.0.1:8642                       # Hermes chat API
  sudo tailscale funnel --bg --https=443 --set-path /bridge http://127.0.0.1:8765    # the bridge
  ```
  The app then uses `https://<machine>.<tailnet>.ts.net` and finds the bridge at `/bridge`. Use
  `tailscale serve` instead to keep both tailnet-only.
- **Reverse proxy with TLS** on your own domain. Caddy:
  ```caddy
  hermes.your-host.example {
    handle_path /bridge/* {
      reverse_proxy 127.0.0.1:8765
    }
    reverse_proxy 127.0.0.1:8642
  }
  ```
  nginx: `location /bridge/ { proxy_pass http://127.0.0.1:8765/; }`. If your proxy forwards the prefix
  unstripped, set `HERMES_BRIDGE_BASE_PATH=/bridge` instead.
- The bridge key is the lock on a public URL: keep it secret, and rotate it by writing a new value to the key
  file (`openssl rand -hex 32 > ~/.config/hermes-bridge/bridge-key`). Both secret files are re-read on every
  request, so no restart is needed.

## Configuration

Environment variables, or keys in a TOML file passed with `--config` (environment variables win). See
[`examples/hermes-bridge.toml`](examples/hermes-bridge.toml).

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_BRIDGE_LISTEN` | `127.0.0.1:8765` | Address to listen on. Keep it private; put TLS in the proxy. |
| `HERMES_BRIDGE_DASHBOARD_URL` | `http://127.0.0.1:9120` | The loopback Hermes backend. |
| `HERMES_BRIDGE_DASHBOARD_TOKEN_FILE` | required | File holding the backend's session token (bare value or `KEY=value`, 16+ chars). |
| `HERMES_BRIDGE_KEY_FILE` | required | File holding the bridge key clients send as `Authorization: Bearer` (16+ chars). |
| `HERMES_BRIDGE_BASE_PATH` | none | Prefix to strip (e.g. `/bridge`) if the proxy doesn't. |
| `HERMES_BRIDGE_DASHBOARD_PROFILE` | `default` | Profile the backend runs as. Plugin toggles and installs only work for it (a Hermes limit). |
| `HERMES_BRIDGE_TIMEOUT` | `30` | Seconds per Hermes call. |

CLI: `hermes-bridge serve` (default), `hermes-bridge init --key-file … --dashboard-token-file …` (creates
missing secret files, never prints them), `hermes-bridge check` (prints the compatibility report; exit 1 if
incompatible).

## API

All endpoints except `/v1/health` need `Authorization: Bearer <bridge key>`. Errors are returned as
`{"error": {"code", "message"}}`. `<p>` is a Hermes profile id (`default`, `scout`, …).

| Method & path | Hermes route used | Notes |
|---|---|---|
| `GET /v1/health` | none | Liveness; no auth. |
| `GET /v1/status` | `/api/health`, `/api/status` + read probes | Compatibility report; 503 with a reason when incompatible. |
| `GET /v1/profiles` | `GET /api/profiles`, `GET /api/env?profile=` | `id, display_name, description, model, is_default, api_key_set`. Only the `is_set` flag of `API_SERVER_KEY` is used; no values. |
| `POST /v1/profiles` | `POST /api/profiles`, `PUT /api/env?profile=` | Body `{name, description?, clone_from?, provider?, model?, soul?}`. Returns 201 with `api_key` once. |
| `POST /v1/profiles/<p>/key` | `PUT /api/env?profile=` | New key, returned once. Refused for `default`, whose key belongs to the server config. |
| `POST /v1/gateway/restart` | `POST /api/gateway/restart` | Rarely needed: multiplexed gateways read profile keys per request. |
| `GET/POST /v1/profiles/<p>/mcp` | `GET/POST /api/mcp/servers` | The list shows `env_keys` (names only). |
| `DELETE /v1/profiles/<p>/mcp/<name>` | `DELETE /api/mcp/servers/<name>` | |
| `PUT /v1/profiles/<p>/mcp/<name>/enabled` | `PUT /api/mcp/servers/<name>/enabled` | `{enabled}` |
| `POST /v1/profiles/<p>/mcp/<name>/test` | `POST /api/mcp/servers/<name>/test` | |
| `GET /v1/profiles/<p>/mcp-catalog`, `POST …/mcp-catalog/install` | `/api/mcp/catalog`, `/api/mcp/catalog/install` | `{name, env?, enable?}` |
| `GET /v1/profiles/<p>/jobs` | `GET /api/cron/jobs` | |
| `GET /v1/profiles/<p>/jobs/<id>/runs` | `GET /api/cron/jobs/<id>/runs` | `?limit=` 1–100 |
| `POST /v1/profiles/<p>/jobs/<id>/{pause,resume,trigger}` | same under `/api/cron/jobs` | |
| `GET /v1/profiles/<p>/skills`, `GET …/skills/content?name=`, `PUT …/skills/enabled` | `/api/skills`, `/api/skills/content`, `/api/skills/toggle` | `{name, enabled}` |
| `GET /v1/profiles/<p>/toolsets`, `PUT …/toolsets/<name>/enabled` | `/api/tools/toolsets` | `{enabled}` |
| `GET /v1/profiles/<p>/plugins` | `/api/dashboard/plugins/hub`, `/api/tools/toolsets` | Each plugin's `tools` come from the toolset with the same name. `toggle_supported` is shown. |
| `POST /v1/profiles/<p>/plugins/<name>/{enable,disable}` | `/api/dashboard/agent-plugins/<name>/…` | Only for the backend's own profile. Otherwise 409 with the `hermes -p <p> plugins …` command to run. |
| `GET /v1/profiles/<p>/plugin-catalog` | `/api/dashboard/plugins/catalog` | Hermes's curated plugin marketplace with installed/update state. Display fields only (no repo URLs). `install_supported` is shown. |
| `POST /v1/profiles/<p>/plugin-catalog/install` | `/api/dashboard/agent-plugins/install` | `{name}`: a catalog name only, never a git URL. Only for the backend's own profile (409 otherwise). Up to 180 s, since Hermes clones the plugin. |
| `GET /v1/profiles/<p>/memory` | `GET /api/memory` | Active provider, providers, built-in file sizes. |
| `GET/PUT /v1/profiles/<p>/memory/providers/<name>/config` | same under `/api/memory/providers` | Hermes blanks secret fields. PUT takes `{values}`. |
| `GET/PUT /v1/profiles/<p>/soul` | `GET/PUT /api/profiles/<p>/soul` | The persona (SOUL.md). PUT takes `{content}`, text up to 48 KB. |
| `GET /v1/profiles/<p>/usage?days=30` | `GET /api/analytics/usage` | Token and cost totals, per model and per day (days 1-365). |

## Compatibility

The bridge was written against hermes-agent release 2026.9.24 (commit `4ea8d23c`). `hermes-bridge
check` and `GET /v1/status` probe every read route it uses. Write routes can't be probed without side
effects; a changed write route surfaces as that call's own error.

## Tests

```bash
python -m unittest discover -s tests -v   # real HTTP against a fake Hermes dashboard (tests/fake_dashboard.py)
```

[Hermes Mobile](https://github.com/suryavamsi6/hermes-app) also runs its app-side client against this package
end to end (`npm test` there, with this repo checked out next to it).

## Troubleshooting

- **`/v1/status` says browser login / gated.** The backend isn't in session-token mode: check
  `HERMES_DASHBOARD_SESSION_TOKEN` is in its *process* env (not `~/.hermes/.env`), it's bound to 127.0.0.1,
  and `HERMES_DASHBOARD_PUBLIC_URL` isn't set to a non-loopback URL in `~/.hermes/.env`.
- **401 from the bridge.** The app's bridge key doesn't match the key file (whitespace is trimmed).
- **404 for a route you expect.** It's not on the allowlist, or the proxy didn't strip `/bridge` (set `HERMES_BRIDGE_BASE_PATH`).
- **Plugin toggle returns 409.** Hermes only toggles plugins for the backend's own profile; the response
  includes the `hermes -p <profile> plugins …` command to run instead.
- **`hermes dashboard` refuses to start after installing this.** The private backend started first and owns
  the host-backend record. Stop it, start your dashboard, then start the backend again.

## License

MIT, same as [Hermes Agent](https://github.com/NousResearch/hermes-agent). See [LICENSE.md](LICENSE.md). Not affiliated with Nous Research.
