# mini

Agentless IaC for a Mac mini M4 Pro running ollama as a LAN-facing OpenAI-compatible LLM endpoint.

## What it does

- Hardens SSH (key-only, no password), keeps Remote Login enabled.
- Manages `authorized_keys` for the admin user from a list in `group_data/all.py`.
- Configures pf to allow inbound SSH + Caddy from LAN + raspi WireGuard subnet only.
- Sets `pmset` to keep the box awake as a server, wake on LAN, restart after power loss.
- Enables automatic macOS + App Store updates.
- Installs Homebrew + base CLI tools (`fish`, `git`, `htop`, `ripgrep`, …) from `files/Brewfile`.
- Runs Caddy as a LAN-facing reverse proxy in front of Ollama (with optional `Authorization: Bearer` enforcement).
- Runs Ollama on `127.0.0.1:11435`, pulls configured models on first deploy.

## First-time setup

1. Enable Remote Login on the Mini: System Settings → General → Sharing → Remote Login. Recent macOS requires this to be a GUI toggle (CLI needs Full Disk Access).
2. Place your SSH public key on the Mini manually (`ssh-copy-id` from your laptop) so pyinfra can connect.
3. Copy `inventory.example.py` → `inventory.py` and `group_data/all.example.py` → `group_data/all.py`. Fill in your LAN IP, SSH user, and add the same public key to `SSH["authorized_keys"]` so it stays managed across reinstalls.
4. (Optional) If you want bearer-token auth in front of Ollama: create a Bitwarden folder `mini`, an item `ollama` with hidden field `api_key` (`openssl rand -hex 32`), and set `OLLAMA["require_api_key"] = True`.
5. `uv run pyinfra inventory.py deploy.py`

If you're sitting at the Mini for the first deploy, skip the SSH dance and use `inventory.local.py` (already gitignored) which uses pyinfra's `@local` connector — no SSH needed.

## Daily ops

```fish
# Full deploy (idempotent)
uv run pyinfra inventory.py deploy.py

# Just one task
uv run pyinfra inventory.py tasks/caddy.py

# Rotate the API key (after editing the BW item)
set -x BW_SESSION (bw unlock --raw)
uv run pyinfra inventory.py tasks/secrets.py tasks/caddy.py
```

## Client usage

```fish
# Direct LAN
curl http://192.168.1.155:11434/api/generate \
  -d '{"model": "gemma4:26b-q4_K_M", "prompt": "hello", "stream": false}'

# With auth enabled
curl http://192.168.1.155:11434/api/generate \
  -H "Authorization: Bearer $OLLAMA_API_KEY" \
  -d '{"model": "gemma4:26b-q4_K_M", "prompt": "hello", "stream": false}'

# OpenAI-compatible
set -x OPENAI_BASE_URL http://192.168.1.155:11434/v1
set -x OPENAI_API_KEY (bw get item ollama | jq -r '.fields[] | select(.name=="api_key").value')
```

See `CLAUDE.md` for the architecture overview, secrets handling, and patterns.
