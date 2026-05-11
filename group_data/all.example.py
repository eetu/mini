# Copy this file to all.py and fill in your values.
# all.py is gitignored — never commit it.

NETWORK = {
    "lan_cidr": "192.168.x.0/24",  # your LAN subnet
    "lan_ip": "192.168.x.y",  # static IP reserved for the Mini (router-pinned)
    "wg_subnet": "10.8.0.0/24",  # raspi WireGuard subnet — clients reaching us via VPN
}

SHELL = "/opt/homebrew/bin/fish"  # /bin/zsh, /bin/bash, /opt/homebrew/bin/fish

SSH = {
    # Public keys appended to ~ssh_user/.ssh/authorized_keys at deploy time.
    # Public keys are not secret — list them directly. Store the matching
    # private keys wherever you keep them (Bitwarden SSH-key items, password
    # manager, etc.). The deploy never reads private keys.
    "authorized_keys": [
        # "ssh-ed25519 AAAAC3... your-comment",
    ],
}

# Base CLI tools installed via Homebrew. Edit `files/Brewfile` for the canonical list.
BREW = {
    "auto_update": True,  # run `brew update && brew upgrade` once per 24h on deploy
}

OLLAMA = {
    # Pin the Ollama.app version downloaded from GitHub releases. The official
    # .app bundles parts of the image-gen runtime that the Homebrew formula
    # omits; we install via tasks/ollama.py from
    # https://github.com/ollama/ollama/releases/<version>/Ollama-darwin.zip
    # and point the LaunchDaemon at the embedded server binary.
    "version": "v0.22.0",
    # Caddy listens on `port` (LAN-facing); ollama listens on `internal_port` (127.0.0.1 only).
    "port": 11434,
    "internal_port": 11435,
    # When True, Caddy enforces `Authorization: Bearer $OLLAMA_API_KEY` on every request.
    # Token comes from the `ollama` Bitwarden item, field `api_key`.
    # When False, Caddy is a transparent reverse proxy — no auth, LAN trust only.
    "require_api_key": False,
    # Models pulled at deploy time. See https://ollama.com/library/gemma4/tags
    # for sizes — `gemma4:26b` (18 GB, q4_K_M default) is the recommended daily
    # driver on 24 GB unified memory. Tag bare size variants (`26b`, `e4b`); only
    # append a quantization suffix (`-q8_0`, `-bf16`, …) when overriding the default.
    #
    # When `prune_unlisted` is True this list is the SOURCE OF TRUTH — any model
    # present on the box but not in this list is removed on the next deploy.
    # Drop a name here to delete it. Manual `ollama pull` of test models will
    # also be wiped — pin them here if you want them to survive.
    "models": [
        "gemma4:26b",
        "gemma4:e4b",
        # Embedding model for RAG / semantic search via /api/embeddings.
        # 137M params, ~270 MB on disk, negligible RAM compared to the chat
        # models. Add to keep RAG pipelines on the same endpoint as chat.
        "nomic-embed-text",
    ],
    # Strict declarative mode for the model set. False (default) leaves
    # ad-hoc-pulled models in place. True turns the deploy into a reconciler
    # that removes anything not listed above.
    "prune_unlisted": False,
    # How long ollama keeps a model resident in RAM after the last request.
    # 15m frees memory between coding sessions; bump if you find yourself waiting through reloads.
    # Per-request override: include `"keep_alive": "1h"` (or `0`) in the JSON body.
    "keep_alive": "15m",
    # Model to warm into RAM at every Mini boot. Fires once after the daemon
    # comes up so the first interactive request doesn't pay the ~5–15 s cold
    # load. Set to None to disable. Must be one of `models` above.
    "warmup_model": "gemma4:26b",
    # Where ollama stores model blobs. Survives `brew uninstall ollama`.
    "models_path": "/Users/Shared/ollama-models",
}

# Disk space alert threshold (GB). When free space on the volume containing
# /Users/Shared drops below this, tasks/diskalert.py emits a `user.warn`
# entry to the macOS unified log every hour. 20 GB leaves ~2x headroom for a
# Flux model download mid-deploy. Visible via:
#   log show --predicate 'eventMessage contains "mini-diskalert"' --last 1d
DISK_ALERT_GB = 20

CADDY = {
    "version": "2",  # tracks the Homebrew major; pin if you want exact reproducibility
}

# ComfyUI — opinionated img2img stack built around Flux.1 Kontext [dev] FP8.
# tasks/comfyui.py installs ComfyUI from the GitHub source tarball for
# COMFYUI["version"] into /Applications/ComfyUI/, builds a `.venv` via `uv`
# from requirements.txt, and pulls a fixed set of weights for Flux Kontext
# img2img: the FP8 diffusion model, T5-XXL + CLIP-L text encoders, and the
# Flux VAE. Model URLs and target subdirs live in tasks/comfyui.py — not here
# — because the stack is intentionally not user-configurable. The
# LaunchDaemon runs `.venv/bin/python main.py` bound to 127.0.0.1, and Caddy
# gates the LAN-facing port the same way it does ollama.
#
# Memory: Flux Kontext FP8 needs ~12 GB resident; loading alongside Gemma 26B
# (~18 GB) will OOM. The calling app is expected to coordinate eviction —
# send `"keep_alive": 0` to ollama before invoking ComfyUI, or rely on a short
# OLLAMA["keep_alive"]. ComfyUI keeps the checkpoint in RAM until process exit.
COMFYUI = {
    # Pin a ComfyUI git tag — the deploy downloads
    # https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/<version>.tar.gz
    # and extracts to /Applications/ComfyUI. Bumping this wipes the existing
    # install directory (incl. .venv) and reinstalls. Models live outside
    # the install dir and survive version bumps.
    "version": "v0.20.1",
    # Caddy listens on `port` (LAN-facing); ComfyUI listens on `internal_port` (127.0.0.1 only).
    "port": 8188,
    "internal_port": 8189,
    # When True, Caddy enforces `Authorization: Bearer $COMFYUI_API_KEY` on every request.
    # Token comes from the `comfyui` Bitwarden item, field `api_key`.
    # When False, Caddy is a transparent reverse proxy — no auth, LAN trust only.
    "require_api_key": False,
}

# Beszel agent — outbound monitoring agent that dials the raspi hub.
# When enabled, requires the BW item `mini/beszel-agent` with hidden fields
# `token` (universal token from the hub) and `key` (hub ed25519 public key).
# Both copy from the running raspi: hub UI > Add System, or
# /etc/secrets/beszel-agent.env on the raspi.
BESZEL = {
    "enabled": False,
    # Pin the agent binary to the same major as the raspi hub. Mismatched
    # major versions can break the WS protocol. Releases:
    # https://github.com/henrygd/beszel/releases
    "version": "v0.18.7",
    # Where the agent dials the hub. The raspi hub binds to 127.0.0.1 only,
    # so this must be a network-reachable address. Pick one:
    #   - Through the raspi Traefik route (TLS): "https://metrics.<your-domain>"
    #   - Direct to raspi LAN IP (only if you expose the hub on the LAN, which
    #     the raspi project does NOT do by default).
    "hub_url": "https://metrics.example.com",
}

# Apple Screen Sharing (VNC on 5900). True bootstraps + enables the
# com.apple.screensharing LaunchDaemon. False (or omitted) tears it down and
# disables it across reboots. pf's LAN+WG perimeter already gates 5900 — no
# extra firewall rule needed. Connect via Finder > Cmd+K > vnc://<lan_ip>.
SCREEN_SHARING = False
