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
    # Caddy listens on `port` (LAN-facing); ollama listens on `internal_port` (127.0.0.1 only).
    "port": 11434,
    "internal_port": 11435,
    # When True, Caddy enforces `Authorization: Bearer $OLLAMA_API_KEY` on every request.
    # Token comes from the `ollama` Bitwarden item, field `api_key`.
    # When False, Caddy is a transparent reverse proxy — no auth, LAN trust only.
    "require_api_key": False,
    # Models to pull at deploy time. See https://ollama.com/library/gemma4/tags
    # for sizes — `gemma4:26b` (18 GB, q4_K_M default) is the recommended daily
    # driver on 24 GB unified memory. Tag bare size variants (`26b`, `e4b`); only
    # append a quantization suffix (`-q8_0`, `-bf16`, …) when overriding the default.
    "models": [
        "gemma4:26b",
        "gemma4:e4b",
    ],
    # How long ollama keeps a model resident in RAM after the last request.
    # 15m frees memory between coding sessions; bump if you find yourself waiting through reloads.
    # Per-request override: include `"keep_alive": "1h"` (or `0`) in the JSON body.
    "keep_alive": "15m",
    # Where ollama stores model blobs. Survives `brew uninstall ollama`.
    "models_path": "/Users/Shared/ollama-models",
}

CADDY = {
    "version": "2",  # tracks the Homebrew major; pin if you want exact reproducibility
}
