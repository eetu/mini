# Mini IaC

Agentless infrastructure-as-code for a Mac mini M4 Pro (24 GB) running a small constellation of local AI services as LAN-facing endpoints: Ollama (chat LLM + embeddings), ComfyUI (Flux Kontext img2img), Whisper.cpp (speech-to-text), and Piper (text-to-speech). Uses **pyinfra** (Python, SSH-only, no agents). Sibling project to `../raspi`.

## Deploy

```fish
set -x BW_SESSION (bw unlock --raw)   # required when any service's require_api_key flag is True, or when BESZEL["enabled"] is True
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

### LaunchDaemon (Caddy, Ollama, ComfyUI, Whisper, Piper, healthcheck, …)

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
LAN client  ─►  http://192.168.1.155:8188   ─►  Caddy  ─►  127.0.0.1:8189   ─►  ComfyUI
LAN client  ─►  http://192.168.1.155:8190   ─►  Caddy  ─►  127.0.0.1:8191   ─►  Whisper.cpp
LAN client  ─►  http://192.168.1.155:8192   ─►  Caddy  ─►  127.0.0.1:8193   ─►  Piper
WG client   ─►  (same, via raspi WG → LAN routing)
ai.<domain> ─►  raspi Traefik (TLS termination)  ─►  http://192.168.1.155:11434  ─►  same chain
```

- **Caddy** owns every LAN-facing port via one daemon. `tasks/caddy.py` iterates over a `SERVICES` tuple — adding a service is a one-line append. Each site block gates on `Authorization: Bearer` independently when its `require_api_key` flag is True. Adds `X-Forwarded-For` upstream so service logs record the originating LAN client. Access log auto-rotates via `roll_size`/`roll_keep`.
- **Ollama** binds to `127.0.0.1:{OLLAMA["internal_port"]}` — never reachable except through Caddy. Installed from the official `Ollama.app` zip (not Homebrew — that build omits parts of the image-gen runtime). Optional boot-time warmup via `OLLAMA["warmup_model"]` so the first request after reboot doesn't pay the cold-load.
- **ComfyUI** binds to `127.0.0.1:{COMFYUI["internal_port"]}`. Opinionated stack: Flux.1 Kontext [dev] GGUF for img2img on MPS (FP8 unsupported on the MPS backend), weights pulled from HuggingFace by `tasks/comfyui.py`. PyTorch nightly overrides the stable torch from `requirements.txt`. Memory: ~14 GB resident; coexisting with Gemma 26B (~18 GB) overflows 24 GB — calling app evicts ollama (`"keep_alive": 0`) before invoking ComfyUI. Workflow JSONs under `files/comfyui-workflows/` sync to the install dir.
- **Whisper.cpp** binds to `127.0.0.1:{WHISPER["internal_port"]}`. Built from source via cmake (the Homebrew formula ships with `-DWHISPER_BUILD_SERVER=OFF`, so brew is not an option). Metal-accelerated; default model `ggml-large-v3-turbo-q5_0.bin` (~574 MB) is ~50x realtime on M4 Pro. POST WAV to `/inference` for transcription. Other formats supported only when whisper-server runs with `--convert` + ffmpeg on the host (currently disabled — clients re-encode to WAV first).
- **Piper** binds to `127.0.0.1:{PIPER["internal_port"]}`. Pure-Python via `piper-tts[http]` in a uv venv, fronted by a small custom Flask wrapper at `files/piper-server.py` that emits chunked-WAV (default) or chunked-Ogg/Opus (`?format=opus`, ~11x bandwidth reduction, ffmpeg from the Brewfile). `PIPER["voices"]` is a list of slugs (`<lang>-<voice>-<quality>`); all live in the same `/Users/Shared/piper-voices/` dir, daemon serves all of them, and clients pick at request time via `"voice": "<slug>"` in the POST body. `/voices` shape preserved verbatim from upstream so existing clients (../chat) don't have to change. 40+ languages supported upstream.
- **pf** trusts LAN + WG fully (`pass in quick from $lan/$wg keep state`); no per-port rules needed for new services.
- **Pi-side** (separate repo) handles `ai.<domain>` DNS + Traefik routing + TLS — no work on the Mini for that.
- **Beszel agent** (optional, `BESZEL["enabled"]`) — native LaunchDaemon dialing the raspi hub via WebSocket using `BESZEL["hub_url"]` + a TOKEN/KEY pair from BW. No inbound port; nothing for pf to allow.
- **Healthcheck** (`tasks/healthcheck.py`) — 60s poll of each upstream's loopback port. Three consecutive failures kicks the daemon via `launchctl kickstart -k` and emits a `user.warn` syslog entry. Catches hangs that `KeepAlive=true` doesn't.
- **Disk alert** (`tasks/diskalert.py`) — hourly `df` on `/Users/Shared`. Below `DISK_ALERT_GB`, emits `user.warn` to the unified log.
- **Log rotation** (`tasks/logrotate.py`) — daily copytruncate of launchd-captured logs at 10 MiB threshold. Caddy's access log self-rotates via the Caddyfile `roll_size`/`roll_keep` directives.

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

### Rotating a service API key

```fish
# generate a new token
openssl rand -hex 32
# paste it into the `api_key` hidden field on the matching BW item:
#   mini/ollama, mini/comfyui, mini/whisper, or mini/piper
uv run pyinfra inventory.py tasks/secrets.py tasks/caddy.py
```

`tasks/caddy.py` hashes every `/etc/secrets/<service>.env` at run time, so a rotated secret triggers a kickstart without any plist change.

### Enabling / disabling auth

Flip the matching `require_api_key` flag on `OLLAMA`, `COMFYUI`, `WHISPER`, or `PIPER` in `group_data/all.py`, then redeploy. Going from True → False removes the matching `/etc/secrets/<service>.env` and switches that site block to a transparent reverse proxy. False → True requires the BW item to exist with `api_key` populated, or the matching `vault.<service>_api_key()` getter raises before any file is written.

### Bumping ComfyUI

```fish
# edit COMFYUI["version"] in group_data/all.py to a new git tag from
# https://github.com/comfyanonymous/ComfyUI/tags
uv run pyinfra inventory.py tasks/comfyui.py
```

Wipes `/Applications/ComfyUI/` (incl. `.venv`), re-extracts the source tarball, and rebuilds the venv from the bundled `requirements.txt`. Weights under `/Users/Shared/comfyui-models/` are untouched. Custom nodes are NOT preserved across version bumps — the stack is intentionally vanilla.

### Bumping Whisper.cpp

```fish
# edit WHISPER["version"] in group_data/all.py to a new tag from
# https://github.com/ggml-org/whisper.cpp/tags
uv run pyinfra inventory.py tasks/whisper.py
```

Wipes `/Applications/whisper.cpp/`, re-downloads the source tarball, and rebuilds `whisper-server` via cmake. Model files under `/Users/Shared/whisper-models/` survive. Bumping `WHISPER["model_filename"]` triggers a fresh model download from `https://huggingface.co/ggerganov/whisper.cpp/tree/main` on the next deploy.

### Bumping Piper / adding voices

```fish
# edit PIPER["version"] to a new piper-tts release from
# https://pypi.org/project/piper-tts/, or append slugs to PIPER["voices"]:
#   "voices": ["en_US-amy-medium", "fi_FI-harri-medium", "de_DE-thorsten-high"]
uv run pyinfra inventory.py tasks/piper.py
```

Bumping `PIPER["version"]` wipes `/Applications/piper/.venv` and rebuilds it. Voice files under `/Users/Shared/piper-voices/` survive. Adding a slug to `voices` pulls it on the next deploy and exposes it at request time via the JSON `"voice"` field — no daemon restart cost beyond the kickstart triggered by the list change. The first slug seeds the daemon's `-m` default. Voice catalogue: https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md.

### Enabling / disabling the Beszel agent

Flip `BESZEL["enabled"]` in `group_data/all.py`, then redeploy. False → True needs BW item `mini/beszel-agent` populated with hidden fields `token` (universal token) and `key` (hub ed25519 pubkey) — both copied from the running raspi (hub UI Add System dialog, or `/etc/secrets/beszel-agent.env` on the raspi). True → False boots the daemon out and removes the binary, wrapper, plist, stamp, and env file.

## Ports in use

| Port  | Service |
|---|---|
| 22    | SSH (LAN + WG only) |
| 5900  | Apple Screen Sharing (when `SCREEN_SHARING = True`) |
| 8188  | Caddy → ComfyUI (LAN-facing) |
| 8189  | ComfyUI (loopback only) |
| 8190  | Caddy → Whisper.cpp (LAN-facing) |
| 8191  | Whisper.cpp (loopback only) |
| 8192  | Caddy → Piper (LAN-facing) |
| 8193  | Piper (loopback only) |
| 11434 | Caddy → Ollama (LAN-facing) |
| 11435 | Ollama (loopback only) |

## Memory budget (24 GB unified)

- macOS + apps: keep ~6 GB free for browser/IDE.
- Gemma4 26B q4_K_M: ~18 GB resident when loaded.
- Flux Kontext Q6_K (+ T5 Q5_K_M + CLIP-L + VAE + LoRA): ~14 GB resident when loaded.
- Whisper.cpp large-v3-turbo-q8_0: ~1.0 GB resident continuously. The model loads at daemon start (whisper-server's `--model` flag is eager, not lazy) and stays mapped for the daemon's lifetime; idle RSS stays ~1 GB rather than dropping. Bouncing the daemon (`launchctl kickstart -k system/com.eetu.whisper`) frees it. Earlier q5_0 quant was ~600 MB but mis-detected Finnish too often — q8_0 buys language-ID accuracy at the memory cost.
- Piper voice + onnx runtime: ~150 MB resident per voice + ~200 MB Python overhead. Always loaded while the daemon is up.
- 18 + 14 = 32 GB > 24 GB — Ollama and ComfyUI **cannot both hold a model resident**. The calling app (`../chat`) coordinates eviction by issuing `"keep_alive": 0` to ollama before invoking ComfyUI, then a normal request reloads the LLM after the image job. ComfyUI itself keeps the checkpoint resident until process exit; bouncing the daemon (`launchctl kickstart -k system/com.eetu.comfyui`) frees its RAM.
- Whisper + Piper coexist with Gemma (18 + 1 + 0.4 + ~3 macOS base ≈ 22.4 GB) but the headroom is now ~1.5 GB rather than the ~5 GB before the q8 swap. Mixed Gemma + Whisper + Flux is firmly out — the calling app still has to evict Gemma before invoking Flux. Watch `vm.swapusage` if you start running embedding + chat + STT bursts back-to-back.
- `OLLAMA["keep_alive"] = "15m"` — model unloads between sessions; first request after idle takes ~5–15 s to reload from NVMe. `OLLAMA["warmup_model"]` (if set) loads the named model into RAM once at every boot so the first interactive request after a reboot doesn't pay the cold-load.
- Per-request `keep_alive` override wins: agents that hammer the model can pin it for the duration of their run with `"keep_alive": "1h"` in the JSON body.
