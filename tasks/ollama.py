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
# `ollama pull` is idempotent — it skips already-present blobs — but we still
# want to skip the network round-trip when nothing's missing. Stamp file lists
# the models that succeeded last run; only models not in the stamp are pulled.

_models = OLLAMA["models"]
_models_joined = " ".join(_models)
_prune = bool(OLLAMA.get("prune_unlisted"))
# Hash covers list + prune flag so toggling the flag re-runs the reconciler
# even when the list itself is unchanged.
_models_hash = hashlib.sha256(f"{_models_joined}|prune={_prune}".encode()).hexdigest()

# Strict-mode prune block — only emitted when `prune_unlisted` is True.
# Walks `ollama list` and deletes anything not in the desired set. Models in
# the list but not yet on disk are pulled by the loop above, so by the time
# we reach this we know the desired set is fully present.
_prune_block = (
    f"""
        DESIRED=" {_models_joined} "
        for m in $({BIN_PATH} list 2>/dev/null | awk 'NR>1 {{print $1}}'); do
          case "$DESIRED" in
            *" $m "*) ;;
            *) {BIN_PATH} rm "$m" ;;
          esac
        done
    """
    if _prune
    else ""
)

server.shell(
    name="Reconcile ollama models" + (" (prune unlisted)" if _prune else ""),
    commands=[
        f"""
        set -e
        export OLLAMA_HOST={INTERNAL_HOST}
        STAMP=/var/db/.mini-ollama-models-stamp
        if [ "$(cat "$STAMP" 2>/dev/null)" = "{_models_hash}" ]; then
          exit 0
        fi
        # Wait up to 30s for the daemon to come up after kickstart.
        for i in $(seq 1 30); do
          if curl -fsS http://{INTERNAL_HOST}/api/tags >/dev/null 2>&1; then break; fi
          sleep 1
        done
        for model in {_models_joined}; do
          if ! {BIN_PATH} list 2>/dev/null | awk '{{print $1}}' | grep -qx "$model"; then
            {BIN_PATH} pull "$model"
          fi
        done
        {_prune_block}
        echo '{_models_hash}' > "$STAMP"
        """,
    ],
)
