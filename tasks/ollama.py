"""Ollama: launchd service bound to 127.0.0.1, model pulls, behind Caddy."""

import hashlib
import io

from pyinfra.operations import files, server

from group_data.all import OLLAMA
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.ollama"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
MODELS_PATH = OLLAMA["models_path"]
INTERNAL_HOST = f"127.0.0.1:{OLLAMA['internal_port']}"

# --- Models directory ---
# Owned by root; ollama (running as root via LaunchDaemon) reads/writes here.
# Lives under /Users/Shared so it survives `brew uninstall ollama`.
files.directory(
    name=f"Create {MODELS_PATH}",
    path=MODELS_PATH,
    user="root",
    group="wheel",
    mode="755",
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
    <string>/opt/homebrew/bin/ollama</string>
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

_plist_hash = hashlib.sha256(_plist.encode()).hexdigest()

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
_models_hash = hashlib.sha256(_models_joined.encode()).hexdigest()

server.shell(
    name="Pull ollama models",
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
          if ! /opt/homebrew/bin/ollama list 2>/dev/null | awk '{{print $1}}' | grep -qx "$model"; then
            /opt/homebrew/bin/ollama pull "$model"
          fi
        done
        echo '{_models_hash}' > "$STAMP"
        """,
    ],
)
