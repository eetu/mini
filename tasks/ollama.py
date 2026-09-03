"""Ollama: official .app from GitHub releases, run headless via LaunchDaemon.

We do NOT use the Homebrew ollama formula — its build omits parts of the
image-gen runtime (see ollama/ollama#15882). Instead we download
Ollama-darwin.zip from the GitHub release for OLLAMA["version"], unpack
to /Applications/Ollama.app, and point the LaunchDaemon at the embedded
server binary at Contents/Resources/ollama. The Cocoa wrapper app never
runs — only its bundled CLI/server binary does — so Sparkle auto-update
never fires and the box stays pinned to the version we declared.
"""

import hashlib
import io

from pyinfra import host
from pyinfra.operations import files, server

from group_data.all import OLLAMA
from tasks.util import kickstart_if_changed

CLI_SYMLINK = "/usr/local/bin/ollama"

LABEL = "com.eetu.ollama"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
APP_PATH = "/Applications/Ollama.app"
BIN_PATH = f"{APP_PATH}/Contents/Resources/ollama"
VERSION_STAMP = f"{APP_PATH}.version"
VERSION = OLLAMA["version"]
RELEASE_URL = f"https://github.com/ollama/ollama/releases/download/{VERSION}/Ollama-darwin.zip"
MODELS_PATH = OLLAMA["models_path"]
INTERNAL_HOST = f"127.0.0.1:{OLLAMA['internal_port']}"

# --- Drop the brew formula if a previous deploy installed it ---
# Brew refuses to run as root, so drop to the SSH user. Idempotent: the
# `list` check exits non-zero when the formula isn't installed.
_user = host.data.get("ssh_user")
server.shell(
    name="Uninstall Homebrew ollama if present (replaced by Ollama.app)",
    commands=[
        f"""
        if /opt/homebrew/bin/brew list --formula ollama >/dev/null 2>&1; then
          sudo -u {_user} -H /opt/homebrew/bin/brew uninstall --ignore-dependencies ollama
        fi
        """,
    ],
)

# --- Models directory ---
# Owned by root; ollama (running as root via LaunchDaemon) reads/writes here.
# Lives under /Users/Shared so it survives any future tooling change.
files.directory(
    name=f"Create {MODELS_PATH}",
    path=MODELS_PATH,
    user="root",
    group="wheel",
    mode="755",
    present=True,
)

# --- Ollama.app install (pinned, version-stamped, idempotent) ---
# Strip the quarantine xattr so the LaunchDaemon doesn't get blocked by
# Gatekeeper when launching the embedded binary directly.
server.shell(
    name=f"Install Ollama.app {VERSION}",
    commands=[
        f"""
        STAMP={VERSION_STAMP}
        if [ "$(cat "$STAMP" 2>/dev/null)" != "{VERSION}" ]; then
          TMP=$(mktemp -d)
          curl -fsSL -o "$TMP/Ollama-darwin.zip" "{RELEASE_URL}"
          rm -rf {APP_PATH}
          unzip -q "$TMP/Ollama-darwin.zip" -d /Applications
          xattr -dr com.apple.quarantine {APP_PATH} 2>/dev/null || true
          rm -rf "$TMP"
          echo '{VERSION}' > "$STAMP"
        fi
        """,
    ],
)

# --- CLI symlink on PATH ---
# /usr/local/bin is on the default macOS PATH (/etc/paths). The Ollama.app GUI
# normally creates this symlink the first time you open it; since we never
# launch the GUI, do it ourselves so `ollama` works for interactive sessions.
files.link(
    name=f"Symlink {CLI_SYMLINK} -> embedded binary",
    path=CLI_SYMLINK,
    target=BIN_PATH,
    user="root",
    group="wheel",
    present=True,
)

# --- LaunchDaemon plist ---
# OLLAMA_HOST: bind localhost only — Caddy is the only thing that reaches us.
# OLLAMA_KEEP_ALIVE: how long a model stays resident after the last request.
# OLLAMA_MODELS: where blobs live.
_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists/1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{BIN_PATH}</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key>
    <string>{INTERNAL_HOST}</string>
    <key>OLLAMA_KEEP_ALIVE</key>
    <string>{OLLAMA["keep_alive"]}</string>
    <key>OLLAMA_MODELS</key>
    <string>{MODELS_PATH}</string>
    <key>HOME</key>
    <string>/var/root</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/opt/homebrew/var/log/ollama.log</string>
  <key>StandardErrorPath</key>
  <string>/opt/homebrew/var/log/ollama.log</string>
</dict>
</plist>
"""

files.put(
    name="Write ollama plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

# Hash plist + version so a version bump alone triggers a kickstart even when
# the plist text is byte-identical (it usually is across versions).
_plist_hash = hashlib.sha256((_plist + VERSION).encode()).hexdigest()

server.shell(
    name="Bootstrap ollama + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _plist_hash)],
)

# --- Wait for ollama to be ready, then pull models ---
# Tag normalization: `ollama pull <name>` without a tag stores the model as
# `<name>:latest`, but the config may list the bare name. Expand to canonical
# `<name>:<tag>` form before comparing against `ollama list`, so
# `nomic-embed-text` in config matches the `nomic-embed-text:latest` row.
# Without this, the prune walk would re-delete every bare-name model right
# after pulling it.
_models = OLLAMA["models"]
_prune = bool(OLLAMA.get("prune_unlisted"))


def _tagged(model):
    return model if ":" in model else f"{model}:latest"


server.shell(
    name="Wait for ollama to accept requests",
    commands=[
        f"""
        # Up to 30s for the daemon to come up after a kickstart.
        for i in $(seq 1 30); do
          if curl -fsS http://{INTERNAL_HOST}/api/tags >/dev/null 2>&1; then exit 0; fi
          sleep 1
        done
        echo "ollama not answering on {INTERNAL_HOST}" >&2
        exit 1
        """,
    ],
)

# Pulls are deliberately one operation per model rather than a single
# reconcile loop. `ollama pull` writes its progress bar to a tty pyinfra
# doesn't have, so nothing surfaces until the command exits — a fresh
# 18 GB model is ~5 min of total silence, and a changed list of two big
# models reads as a hung deploy. Per-model operations name what's
# downloading, and an interrupted deploy keeps every model that already
# finished instead of restarting the whole set.
#
# `ollama list` is the idempotence check (a local HTTP call, ~10 ms): present
# means skip. No stamp file — an all-or-nothing stamp written after the last
# pull is exactly what made an interrupt lose the bookkeeping for pulls that
# had in fact succeeded.
for _model in _models:
    server.shell(
        name=f"Pull ollama model {_model}",
        commands=[
            f"""
            set -e
            export OLLAMA_HOST={INTERNAL_HOST}
            if {BIN_PATH} list 2>/dev/null | awk 'NR>1 {{print $1}}' \\
                 | grep -qx '{_tagged(_model)}'; then
              exit 0
            fi
            {BIN_PATH} pull '{_model}'
            """,
        ],
    )

# Strict mode — only emitted when `prune_unlisted` is True. Walks
# `ollama list` and deletes anything not in the desired set. Runs after the
# pull operations above, so the desired set is fully present by now.
if _prune:
    _desired = " " + " ".join(_tagged(m) for m in _models) + " "
    server.shell(
        name="Prune ollama models not in config",
        commands=[
            f"""
            set -e
            export OLLAMA_HOST={INTERNAL_HOST}
            DESIRED="{_desired}"
            for m in $({BIN_PATH} list 2>/dev/null | awk 'NR>1 {{print $1}}'); do
              case "$DESIRED" in
                *" $m "*) ;;
                *) {BIN_PATH} rm "$m" ;;
              esac
            done
            """,
        ],
    )

# Drop the stamp left by the older single-operation reconciler.
server.shell(
    name="Remove stale ollama models stamp",
    commands=["rm -f /var/db/.mini-ollama-models-stamp"],
)

# --- Boot-time model warmup ---
# Loads the primary chat model into RAM after every Mini boot so the first
# interactive request doesn't pay the cold-load latency (~5–15 s for a 26B
# q4_K_M model on this hardware). One-shot launchd job: RunAtLoad fires once
# at boot, no KeepAlive (we don't want it to thrash on every transient
# failure). Polls /api/tags until the daemon answers, then issues a tiny
# /api/generate with `keep_alive` matching OLLAMA["keep_alive"] so the warm
# state survives long enough to matter without pinning RAM forever.
_warmup_model = OLLAMA.get("warmup_model")
_warmup_label = "com.eetu.ollama-warmup"
_warmup_plist_path = f"/Library/LaunchDaemons/{_warmup_label}.plist"
_warmup_script_path = "/usr/local/bin/mini-ollama-warmup.sh"
_warmup_log_path = "/opt/homebrew/var/log/ollama-warmup.log"

if _warmup_model:
    _warmup_script = f"""#!/bin/sh
set -u
HOST="{INTERNAL_HOST}"
MODEL="{_warmup_model}"
KEEP_ALIVE="{OLLAMA["keep_alive"]}"

# Wait up to 5 minutes for ollama to accept requests after the system boots.
# `KeepAlive=true` on the ollama daemon plus pmset auto-restart means we'll
# almost always race the daemon during early boot; long timeout is fine.
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "http://$HOST/api/tags" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

# Issue a 1-token generate so the model loads. `keep_alive` overrides the
# daemon default for this request so a 0s default wouldn't immediately evict.
curl -fsS --max-time 120 \\
  -H 'Content-Type: application/json' \\
  -d "{{\\"model\\": \\"$MODEL\\", \\"prompt\\": \\"hi\\", \\"stream\\": false, \\"keep_alive\\": \\"$KEEP_ALIVE\\"}}" \\
  "http://$HOST/api/generate" >/dev/null
echo "warmed $MODEL"
"""

    files.put(
        name="Write ollama-warmup script",
        src=io.BytesIO(_warmup_script.encode()),
        dest=_warmup_script_path,
        user="root",
        group="wheel",
        mode="755",
    )

    # One-shot at load. No KeepAlive (don't respawn — if the script fails,
    # ollama will load on first user request anyway). No StartInterval.
    _warmup_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists/1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_warmup_label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{_warmup_script_path}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{_warmup_log_path}</string>
  <key>StandardErrorPath</key>
  <string>{_warmup_log_path}</string>
</dict>
</plist>
"""

    files.put(
        name="Write ollama-warmup plist",
        src=io.BytesIO(_warmup_plist.encode()),
        dest=_warmup_plist_path,
        user="root",
        group="wheel",
        mode="644",
    )

    _warmup_hash = hashlib.sha256((_warmup_script + _warmup_plist).encode()).hexdigest()

    server.shell(
        name="Bootstrap ollama-warmup + reload on change",
        commands=[kickstart_if_changed(_warmup_label, _warmup_hash)],
    )
else:
    # Disabled: bootout and clean up so the box doesn't carry stale plist/script.
    server.shell(
        name="Tear down ollama-warmup (disabled)",
        commands=[
            f"""
            if launchctl print system/{_warmup_label} >/dev/null 2>&1; then
              launchctl bootout system/{_warmup_label} || true
            fi
            rm -f {_warmup_plist_path} /var/db/.{_warmup_label}-stamp {_warmup_script_path}
            """,
        ],
    )
