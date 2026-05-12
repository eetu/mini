# mini

Agentless IaC for a Mac mini M4 Pro (24 GB) running a small constellation of
local AI services as LAN-facing endpoints: chat LLMs (Ollama), img2img
(ComfyUI / Flux Kontext), speech-to-text (Whisper.cpp), and text-to-speech
(Piper). Sibling to `../raspi`.

## What it does

- Hardens SSH (key-only, no password); manages `authorized_keys` from `SSH["authorized_keys"]`.
- pf firewall: trust LAN + raspi WireGuard subnet, default-deny elsewhere.
- pmset: never sleep, wake on LAN, auto-restart after AC loss.
- Automatic macOS + App Store updates, daily background check.
- Homebrew + base CLI tools (`fish`, `git`, `htop`, `ripgrep`, `uv`, `cmake`, …) from `files/Brewfile`.
- Fish shell + zoxide wiring.
- Caddy: LAN-facing gateway in front of every upstream, iterating over a
  `SERVICES` tuple. Optional bearer-token auth per service. Access log
  auto-rotates via `roll_size`/`roll_keep`. Adds `X-Forwarded-For` upstream.
- Ollama: official `Ollama.app` binary (not Homebrew), bound to `127.0.0.1:11435`.
  Pulls + (optionally) prunes models declared in `OLLAMA["models"]`. Optional
  boot-time model warmup via `OLLAMA["warmup_model"]`.
- ComfyUI: pinned source tarball + uv venv + PyTorch nightly, Flux.1 Kontext
  GGUF stack for img2img on MPS. Bound to `127.0.0.1:8189`. Workflow JSONs
  under `files/comfyui-workflows/` sync to the install on each deploy.
- Whisper.cpp: built from source via cmake (the Homebrew formula ships with
  the HTTP server disabled). Loopback on `127.0.0.1:8191`, Metal-accelerated,
  default `ggml-large-v3-turbo-q5_0.bin` (~574 MB).
- Piper TTS: `piper-tts[http]` in a uv venv, fronted by a small custom Flask
  wrapper (`files/piper-server.py`) that emits chunked WAV — or Ogg/Opus
  (~11x smaller) via `?format=opus` (ffmpeg in the Brewfile). Loopback on
  `127.0.0.1:8193`. All slugs in `PIPER["voices"]` are downloaded and
  loadable; clients pick at request time. 40+ languages upstream.
- Beszel agent (optional): outbound WebSocket to the raspi monitoring hub.
- Apple Screen Sharing (optional): toggled via `SCREEN_SHARING`.
- Storage: Time Machine + Spotlight excludes for every `/Users/Shared/*-models|voices` dir.
- Log rotation: daily copytruncate of launchd-captured logs (10 MiB threshold).
- Healthcheck: 60-second poll of each upstream's loopback port. Three
  consecutive failures kicks the offending daemon via `launchctl kickstart -k`.
- Disk pressure alert: hourly `df` on `/Users/Shared`, emits a `user.warn`
  syslog entry below `DISK_ALERT_GB`.

## Ports

| Port | Service                |
|------|------------------------|
| 22   | SSH (LAN + WG only)    |
| 5900 | Apple Screen Sharing (optional) |
| 8188 | ComfyUI (via Caddy)    |
| 8189 | ComfyUI (loopback)     |
| 8190 | Whisper.cpp (via Caddy) |
| 8191 | Whisper.cpp (loopback) |
| 8192 | Piper TTS (via Caddy)  |
| 8193 | Piper TTS (loopback)   |
| 11434 | Ollama (via Caddy)    |
| 11435 | Ollama (loopback)     |

## First-time setup

1. Enable Remote Login on the Mini: System Settings → General → Sharing → Remote Login.
   Recent macOS requires this to be a GUI toggle (CLI needs Full Disk Access).
2. Place your SSH public key on the Mini manually (`ssh-copy-id`) so pyinfra can connect.
3. Copy `inventory.example.py` → `inventory.py` and `group_data/all.example.py` → `group_data/all.py`.
   Fill in LAN IP, SSH user, and add the same public key to `SSH["authorized_keys"]`.
4. (Optional) For bearer-token auth: create a Bitwarden folder `mini`, an item
   per service (`ollama`, `comfyui`, `whisper`, `piper`) with a hidden field
   `api_key` (`openssl rand -hex 32`), then flip the matching
   `require_api_key` flag to True.
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

# Bump ComfyUI / Whisper / Piper to a new release
# edit COMFYUI["version"] (or WHISPER["version"], PIPER["version"]) in
# group_data/all.py, then:
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

### Whisper STT

```fish
# Multipart POST a WAV. Returns JSON with the transcribed text.
# Language is auto-detected; pass `-F language=fi` to pin.
# Non-WAV input is NOT enabled — clients re-encode (`ffmpeg -i in.m4a out.wav`)
# before posting. Enabling on-server requires --convert + ffmpeg in the
# Brewfile.
curl -X POST -F file=@audio.wav \
  -F response_format=json \
  http://192.168.x.y:8190/inference
```

### Piper TTS

```fish
# WAV (default — chunked, ~352 kbps for 22050 Hz mono)
curl -X POST -H "Content-Type: application/json" \
  -d '{"text": "hello world", "voice": "en_US-amy-medium"}' \
  -o out.wav http://192.168.x.y:8192/

# Ogg/Opus (~11x smaller; clean Browser MSE playback)
curl -X POST -H "Content-Type: application/json" \
  -d '{"text": "hello world"}' \
  -o out.ogg "http://192.168.x.y:8192/?format=opus"

# Finnish
curl -X POST -H "Content-Type: application/json" \
  -d '{"text": "hei maailma", "voice": "fi_FI-harri-medium"}' \
  -o out.wav http://192.168.x.y:8192/

# List loaded voices
curl http://192.168.x.y:8192/voices
```

## Aborting in-flight requests

Two flavours depending on the upstream:

### Ollama — close the connection

No `/cancel` endpoint. Dropping the TCP connection mid-stream is the abort
signal: Caddy propagates the cancellation to Ollama, the writer hits a
broken pipe on the next token, generation stops, and the model context is
released (the `OLLAMA_KEEP_ALIVE` timer starts fresh). Applies to
`/api/generate`, `/api/chat`, `/api/embeddings`, and `/v1/chat/completions`.

```js
// Browser / Node
const ctrl = new AbortController();
fetch("http://192.168.x.y:11434/api/chat", {
  method: "POST",
  body: JSON.stringify({...}),
  signal: ctrl.signal,
});
ctrl.abort();  // closes the connection -> ollama aborts
```

```python
# Python httpx — exit the streaming context to abort
with httpx.Client() as c, c.stream("POST", url, json=body) as r:
    for line in r.iter_lines():
        if should_stop:
            break  # connection closes here
```

```fish
# curl — Ctrl-C closes the connection, ollama logs "context canceled"
curl -N --max-time 5 http://192.168.x.y:11434/api/generate -d '{...}'
```

### ComfyUI — `POST /interrupt`

ComfyUI keeps a single in-flight prompt + a queue of pending ones. The
running prompt is cancelled by an explicit endpoint, not by closing the
HTTP connection (the original POST returned immediately with the prompt ID;
the actual work happens asynchronously on the queue).

```fish
# Cancel the currently running prompt
curl -X POST http://192.168.x.y:8188/interrupt

# Clear all pending prompts (does NOT stop the one currently running)
curl -X POST http://192.168.x.y:8188/queue -d '{"clear": true}'

# Delete a specific queued prompt by ID
curl -X POST http://192.168.x.y:8188/queue \
  -d '{"delete": ["<prompt_id>"]}'
```

The running sampler step finishes its current iteration before halting —
no mid-step kill. WebSocket subscribers (`/ws`) receive
`execution_interrupted` so UI can react.

### Whisper + Piper — request/response, no abort needed

Both are short turn-around (sub-second to a few seconds). Drop the
connection if you want to discard the response; the server completes its
current work but the bytes go nowhere.

See `CLAUDE.md` for architecture, secrets handling, memory budget, and per-task patterns.
