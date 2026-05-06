# Mini IaC

Agentless infrastructure-as-code for a Mac mini M4 Pro (24 GB) running ollama as a LAN-facing OpenAI-compatible LLM endpoint, using **pyinfra** (Python, SSH-only, no agents). Sibling project to `../raspi`.

## Deploy

```fish
set -x BW_SESSION (bw unlock --raw)   # required when OLLAMA["require_api_key"] or BESZEL["enabled"] is True
uv run pyinfra inventory.py deploy.py
```

Idempotent — safe to re-run at any time.

## Validate before commit

```fish
uv run ruff check .
uv run ruff format .
```

No dry-run mode in pyinfra — linting + a careful read is the pre-commit check.

## Key files

| File | Purpose |
|---|---|
| `deploy.py` | Entry point — ordered list of `local.include()` task files |
| `inventory.py` | SSH target (Mini IP, user, key) — gitignored |
| `group_data/all.py` | Service config (ports, models, flags) — gitignored |
| `group_data/all.example.py` | Template for `all.py` — keep in sync when adding config |
| `vault.py` | Bitwarden CLI helpers — secrets fetched at deploy time |
| `tasks/` | One file per concern (bootstrap, ssh, firewall, …) |
| `files/` | Static config files copied to the Mini verbatim |

## Service / task patterns

### LaunchDaemon (Caddy, Ollama)

1. Render plist + any inline config (Caddyfile, wrapper script) as Python strings
2. `files.put` the plist into `/Library/LaunchDaemons/{label}.plist`
3. `tasks.util.kickstart_if_changed(label, hash, env_files=...)` — bootstraps on first run, kickstarts when the hashed inputs change
4. Stamp file at `/var/db/.{label}-stamp` records the last-applied hash

### Hash-based restart

The same pattern as raspi systemd, swapped for launchd:
- raspi: `systemctl restart svc` + `/etc/systemd/system/.{svc}-stamp`
- mini: `launchctl kickstart -k system/{label}` + `/var/db/.{label}-stamp`

`env_files` lets a service detect rotated secrets written by `tasks/secrets.py` without the service task itself reading the secret value.

## Architecture

```
LAN client  ─►  http://192.168.1.155:11434  ─►  Caddy  ─►  127.0.0.1:11435  ─►  Ollama
WG client   ─►  (same, via raspi WG → LAN routing)
ai.<domain> ─►  raspi Traefik (TLS termination)  ─►  http://192.168.1.155:11434  ─►  same chain
```

- **Caddy** owns the LAN-facing port (11434). Always installed; gates on `Authorization: Bearer` when `OLLAMA["require_api_key"]` is True.
- **Ollama** binds to `127.0.0.1:{internal_port}` — never reachable except through Caddy.
- **pf** restricts inbound 22/11434 to `lan_cidr` + `wg_subnet`.
- **Pi-side** (separate repo) handles `ai.<domain>` DNS + Traefik routing + TLS — no work on the Mini for that.
- **Beszel agent** (optional, `BESZEL["enabled"]`) — native LaunchDaemon dialing the raspi hub via WebSocket using `BESZEL["hub_url"]` + a TOKEN/KEY pair from BW. No inbound port; nothing for pf to allow.

## Secrets handling — AI assistants read this

**Do NOT read secret values into your context.** All live credentials are in `/etc/secrets/*` on the Mini (env files written by `tasks/secrets.py`) and in the Bitwarden `mini` folder. `group_data/all.py` itself contains only non-secret config — it is safe to read and to edit when mirroring additions made to `group_data/all.example.py`.

**Banned operations** (these dump plaintext into the conversation transcript):
- `ssh mini sudo cat /etc/secrets/...`
- `ssh mini sudo grep ... /etc/secrets/...`
- Reading raw values from `bw get item ...` (filenames, field *names*, and `bw status`/membership checks are fine — values are not)

**Allowed operations** (secret stays inside the shell, never echoed):
- `ssh mini sudo launchctl kickstart -k system/com.eetu.caddy` / `print system/com.eetu.caddy` / log file tails (provided the service doesn't log its own secrets)
- `ssh mini sudo ls -la /etc/secrets/` (filenames only, no contents)
- `ssh mini sudo stat /etc/secrets/ollama.env` (size/mode/mtime, no contents)
- `ssh mini sudo shasum -a 256 /etc/secrets/ollama.env` (hash for change detection)

Rule of thumb: it's fine to *use* a secret in a remote command, never to *transport* it into the assistant's context.

## SSH access

Public keys for the deploy/admin user are listed in `SSH["authorized_keys"]` in `group_data/all.py`. They're managed idempotently — added if missing, never removed by IaC. Private keys are stored in Bitwarden (SSH-key item type) for the user's personal use; the deploy never reads them.

## Bitwarden

Items live in a Bitwarden folder named `mini`. See `vault.py` docstring for the full item list. The `BW_SESSION` env var must be set before deploy when any service flag enables auth — pyinfra fetches secrets locally at deploy time and writes them to `/etc/secrets/` on the Mini (never committed to git).

### Rotating the Ollama API key

```fish
# generate a new token
openssl rand -hex 32
# paste it into the `api_key` hidden field on the BW item `mini/ollama`
uv run pyinfra inventory.py tasks/secrets.py tasks/caddy.py
```

`tasks/caddy.py` hashes `/etc/secrets/ollama.env` at run time, so a rotated secret triggers a kickstart without any plist change.

### Enabling / disabling auth

Flip `OLLAMA["require_api_key"]` in `group_data/all.py`, then redeploy. Going from True → False removes `/etc/secrets/ollama.env` and switches the Caddyfile to a transparent reverse proxy. False → True requires the BW item to exist with `api_key` populated, or `vault.ollama_api_key()` raises before any file is written.

### Enabling / disabling the Beszel agent

Flip `BESZEL["enabled"]` in `group_data/all.py`, then redeploy. False → True needs BW item `mini/beszel-agent` populated with hidden fields `token` (universal token) and `key` (hub ed25519 pubkey) — both copied from the running raspi (hub UI Add System dialog, or `/etc/secrets/beszel-agent.env` on the raspi). True → False boots the daemon out and removes the binary, wrapper, plist, stamp, and env file.

## Ports in use

| Port | Service |
|---|---|
| 22    | SSH (LAN + WG only) |
| 11434 | Caddy (gateway) |
| 11435 | Ollama (loopback only) |

## Memory budget (24 GB unified)

- macOS + apps: keep ~6 GB free for browser/IDE.
- Gemma4 26B q4_K_M: ~18 GB resident when loaded.
- `OLLAMA["keep_alive"] = "15m"` — model unloads between sessions; first request after idle takes ~5–15 s to reload from NVMe.
- Per-request `keep_alive` override wins: agents that hammer the model can pin it for the duration of their run with `"keep_alive": "1h"` in the JSON body.
