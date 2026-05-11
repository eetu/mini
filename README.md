# mini

Agentless IaC for a Mac mini M4 Pro (24 GB) running ollama (chat LLM) and
ComfyUI (Flux Kontext img2img) as LAN-facing endpoints. Sibling to `../raspi`.

## What it does

- Hardens SSH (key-only, no password); manages `authorized_keys` from `SSH["authorized_keys"]`.
- pf firewall: trust LAN + raspi WireGuard subnet, default-deny elsewhere.
- pmset: never sleep, wake on LAN, auto-restart after AC loss.
- Automatic macOS + App Store updates, daily background check.
- Homebrew + base CLI tools (`fish`, `git`, `htop`, `ripgrep`, `uv`, …) from `files/Brewfile`.
- Fish shell + zoxide wiring.
- Caddy: LAN-facing gateway in front of ollama (`:11434`) and ComfyUI (`:8188`).
  Optional bearer-token auth per service. Access log auto-rotates via
  `roll_size`/`roll_keep`.
- Ollama: official `Ollama.app` binary (not Homebrew), bound to `127.0.0.1:11435`.
  Pulls + (optionally) prunes models declared in `OLLAMA["models"]`.
- ComfyUI: pinned source tarball + uv venv + PyTorch nightly, Flux.1 Kontext
  GGUF stack for img2img on MPS. Bound to `127.0.0.1:8189`.
- Beszel agent (optional): outbound WebSocket to the raspi monitoring hub.
- Apple Screen Sharing (optional): toggled via `SCREEN_SHARING`.
- Storage: Time Machine + Spotlight excludes for `/Users/Shared/*-models`.
- Log rotation: daily copytruncate of launchd-captured logs (10 MiB threshold).

## First-time setup

1. Enable Remote Login on the Mini: System Settings → General → Sharing → Remote Login.
   Recent macOS requires this to be a GUI toggle (CLI needs Full Disk Access).
2. Place your SSH public key on the Mini manually (`ssh-copy-id`) so pyinfra can connect.
3. Copy `inventory.example.py` → `inventory.py` and `group_data/all.example.py` → `group_data/all.py`.
   Fill in LAN IP, SSH user, and add the same public key to `SSH["authorized_keys"]`.
4. (Optional) For bearer-token auth: create a Bitwarden folder `mini`, items
   `ollama` and/or `comfyui` with hidden field `api_key` (`openssl rand -hex 32`),
   then flip `OLLAMA["require_api_key"]` / `COMFYUI["require_api_key"]` to True.
5. (Optional) For Beszel monitoring: BW item `mini/beszel-agent` with hidden
   fields `token` + `key` (copied from the raspi hub), then `BESZEL["enabled"] = True`.
6. `set -x BW_SESSION (bw unlock --raw)` when any of the above needs BW.
7. `uv run pyinfra inventory.py deploy.py`

Sitting at the Mini for the first deploy? Skip SSH and use `inventory.local.py`
(gitignored) with pyinfra's `@local` connector.

## Daily ops

```fish
# Full deploy (idempotent)
uv run pyinfra inventory.py deploy.py

# One task in isolation
uv run pyinfra inventory.py tasks/caddy.py

# Rotate an API key (after editing the BW item)
set -x BW_SESSION (bw unlock --raw)
uv run pyinfra inventory.py tasks/secrets.py tasks/caddy.py

# Bump ComfyUI to a new git tag
# edit COMFYUI["version"] in group_data/all.py, then:
uv run pyinfra inventory.py tasks/comfyui.py
```

## Validate before commit

```fish
uv run ruff check .
uv run ruff format .
```

No pyinfra dry-run mode — linting + careful read is the pre-commit check.

## Client usage

### Ollama chat

```fish
curl http://192.168.x.y:11434/api/generate \
  -d '{"model": "gemma4:26b", "prompt": "hello", "stream": false}'

# With auth
curl http://192.168.x.y:11434/api/generate \
  -H "Authorization: Bearer $OLLAMA_API_KEY" \
  -d '{"model": "gemma4:26b", "prompt": "hello"}'

# OpenAI-compatible
set -x OPENAI_BASE_URL http://192.168.x.y:11434/v1
set -x OPENAI_API_KEY (bw get item ollama | jq -r '.fields[] | select(.name=="api_key").value')
```

### Embeddings

```fish
curl http://192.168.x.y:11434/api/embeddings \
  -d '{"model": "nomic-embed-text", "prompt": "hello world"}'
```

### ComfyUI img2img

```fish
# Eject Ollama's resident model first — combined memory > 24 GB
curl http://192.168.x.y:11434/api/generate \
  -d '{"model": "gemma4:26b", "prompt": "", "keep_alive": 0}'

# Submit a workflow JSON to ComfyUI
curl -X POST http://192.168.x.y:8188/prompt \
  -H "Content-Type: application/json" \
  -d @workflow.json
```

See `CLAUDE.md` for architecture, secrets handling, memory budget, and per-task patterns.
