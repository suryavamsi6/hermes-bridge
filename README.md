# hermes-bridge

A small management API for a self-hosted [Hermes Agent](https://github.com/NousResearch/hermes-agent),
built so mobile apps can manage profiles, MCP servers, routines, skills, toolsets, plugins and memory
settings without exposing the Hermes dashboard itself.

It sits next to Hermes and forwards an **allowlist** of the dashboard's own REST API, which Hermes
documents under *Web Dashboard → REST API*. It adds three things the dashboard doesn't do in one step:

- A profile list with display names, plus whether each profile has a chat key.
- **Create a profile with a chat key.** The bridge creates the profile, writes a fresh `API_SERVER_KEY`
  into that profile's `.env`, and returns the key once.
- **Rotate a profile's chat key.** It writes a new key and returns it once.

Chat itself stays on Hermes's API server (`/v1/...`, `/p/<profile>/...`). The bridge has no chat endpoints.

- No runtime dependencies (Python standard library, 3.11+). Runs on any Linux.
- Never forwards the dashboard's terminal/PTY, file browser, secret reveal, raw `/api/env` reads, config
  writes, backups, credentials or OAuth routes. Any path not in its route table is a 404 that never
  reaches Hermes.
- Checks on startup, and every 5 minutes, that every dashboard route it uses still answers in the
  shape it expects. If one doesn't, it fails with a clear message instead of guessing.

## How it fits together

```
phone ──HTTPS──▶ your proxy (Tailscale Funnel, Caddy, …)
                   ├─ /        ──▶ Hermes API server :8642   (chat; per-profile API_SERVER_KEY)
                   └─ /bridge  ──▶ hermes-bridge :8765        (management; bridge key)
                                     └─ loopback ──▶ `hermes serve` :9120 (fixed session token)
```

The bridge needs a Hermes dashboard backend that accepts a **fixed session token**. That means a
loopback-only `hermes serve` (or `hermes dashboard`) started with `HERMES_DASHBOARD_SESSION_TOKEN` in
its *process* environment. A line in `~/.hermes/.env` is ignored for this variable. A dashboard bound
to a LAN or public address uses browser login instead, and the bridge refuses it with an explanation.

Browser login also switches on for a loopback backend when `dashboard.public_url` (or
`HERMES_DASHBOARD_PUBLIC_URL`) names a non-loopback host, which is common when a browser dashboard sits
behind a reverse proxy. The example unit sets `HERMES_DASHBOARD_PUBLIC_URL=http://127.0.0.1:9120` for the
private backend only, which takes precedence over `config.yaml`. Keep the browser dashboard's address in
`config.yaml`, not in `~/.hermes/.env`: Hermes loads `.env` over the process environment, so a value
there would gate the private backend again.

If you already run a dashboard for your browser, leave it alone and start a second, private backend
with `--isolated`, as shown below. Start the bridge's backend *after* your main dashboard. Hermes
lets the first server on a host own the "host backend" record. A headless backend that owns it would
make a later `hermes dashboard` restart refuse to start.

## Install

```bash
pipx install hermes-bridge             # once published; until then: pipx install /path/to/hermes-bridge
# or: uvx --from /path/to/hermes-bridge hermes-bridge --help
# or: docker build -t hermes-bridge .
```

## Set up (generic Linux, as the user that runs Hermes)

```bash
# 1. Secrets: a bridge key for clients and a session token shared with the Hermes backend.
#    Created once with mode 0600 and never printed.
hermes-bridge init --key-file ~/.config/hermes-bridge/bridge-key \
                   --dashboard-token-file ~/.config/hermes-bridge/dashboard-token
printf 'HERMES_DASHBOARD_SESSION_TOKEN=%s\n' "$(cat ~/.config/hermes-bridge/dashboard-token)" \
  > ~/.config/hermes-bridge/backend.env && chmod 600 ~/.config/hermes-bridge/backend.env

# 2. The private Hermes backend and the bridge, as systemd user services (examples/ has both units).
cp examples/hermes-bridge-backend.service examples/hermes-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-bridge-backend hermes-bridge

# 3. Check it.
hermes-bridge check     # with the same env vars as the service, or --config examples/hermes-bridge.toml
```

Expose it with your proxy. For Tailscale Funnel, which strips the mount path:

```bash
tailscale funnel --bg --https=443 --set-path /bridge http://127.0.0.1:8765
```

The bridge also accepts the prefix unstripped when `base_path = "/bridge"` is set.

### Docker

Run the container in the same network namespace as the Hermes backend (for example
`--network host`), and mount the two secret files:

```bash
docker run -d --name hermes-bridge --network host \
  -e HERMES_BRIDGE_LISTEN=127.0.0.1:8765 -e HERMES_BRIDGE_DASHBOARD_URL=http://127.0.0.1:9120 \
  -v ~/.config/hermes-bridge/bridge-key:/run/secrets/hermes_bridge_key:ro \
  -v ~/.config/hermes-bridge/dashboard-token:/run/secrets/hermes_dashboard_token:ro \
  hermes-bridge
```

## Configuration

These are environment variables, or keys in a TOML file passed with `--config`. Environment
variables win. See `examples/hermes-bridge.toml`.

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_BRIDGE_LISTEN` | `127.0.0.1:8765` | Address to listen on. Keep it private; put TLS in the proxy. |
| `HERMES_BRIDGE_DASHBOARD_URL` | `http://127.0.0.1:9120` | The loopback Hermes backend. |
| `HERMES_BRIDGE_DASHBOARD_TOKEN_FILE` | required | File holding the backend's session token (bare value or `KEY=value`). |
| `HERMES_BRIDGE_KEY_FILE` | required | File holding the bridge key clients send as `Authorization: Bearer`. |
| `HERMES_BRIDGE_BASE_PATH` | none | Prefix to strip, if the proxy doesn't. |
| `HERMES_BRIDGE_DASHBOARD_PROFILE` | `default` | Profile the backend runs as. Plugin toggles only work for it (a Hermes limit). |
| `HERMES_BRIDGE_TIMEOUT` | `30` | Seconds per Hermes call. |

Both secret files are re-read on every request, so rotating a key only means editing its file.

## API

All endpoints except `/v1/health` need `Authorization: Bearer <bridge key>`. Errors are returned as
`{"error": {"code", "message"}}`. `<p>` is a Hermes profile id (`default`, `jobwing`, …).

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

## Development

```bash
python -m unittest discover -s tests -v   # a fake Hermes dashboard over real HTTP
```

## License

MIT, same as [Hermes Agent](https://github.com/NousResearch/hermes-agent) itself. See `LICENSE.md`.
