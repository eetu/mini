"""MLflow tracking + model registry — the durable home for finkeyb's experiment lineage.

The finkeyb retrain loop (tasks/finkeyb.py) trains on the mini and logs every run here; the
laptop points MLFLOW_TRACKING_URI at this server to browse runs + the model registry. The
registry needs a DB backend (the file store has no registry), so we use sqlite with artifacts
on the same Shared volume — both survive a version bump (only the venv is rebuilt).

Binds 127.0.0.1:{internal_port}; Caddy fronts {port} and (when require_api_key) gates it on a
bearer token, same as every other upstream. The retrain job talks to the loopback port
directly, so it never needs the token.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from group_data.all import MLFLOW
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.mlflow"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/mlflow-run.sh"
INSTALL_PATH = "/Applications/finkeyb-mlflow"
VENV_PYTHON = f"{INSTALL_PATH}/.venv/bin/python"
MLFLOW_BIN = f"{INSTALL_PATH}/.venv/bin/mlflow"
VERSION_STAMP = f"{INSTALL_PATH}.version"
# Durable store: sqlite DB + artifact tree on the Shared volume (NOT under the venv, so a
# version bump that wipes INSTALL_PATH leaves the run history + registry intact).
DATA_PATH = "/Users/Shared/finkeyb/mlflow"
LOG_PATH = "/opt/homebrew/var/log/mlflow.log"

VERSION = MLFLOW["version"]
INTERNAL_PORT = MLFLOW["internal_port"]

# --- Durable data dir (survives version bumps) ---
files.directory(
    name=f"Create {DATA_PATH}",
    path=DATA_PATH,
    user="root",
    group="wheel",
    mode="755",
    present=True,
)

# --- MLflow venv (pinned, version-stamped, idempotent) ---
# Bumping MLFLOW["version"] wipes INSTALL_PATH and rebuilds the venv; the DB/artifacts in
# DATA_PATH are untouched. mlflow[extras] would drag in heavy deps we don't need for a
# sqlite+local-artifact server, so install the plain package at the version the finkeyb backend
# resolved (so the tracking server and the logging client agree).
server.shell(
    name=f"Install MLflow {VERSION} venv",
    commands=[
        textwrap.dedent(f"""
        STAMP={VERSION_STAMP}
        if [ "$(cat "$STAMP" 2>/dev/null)" != "{VERSION}" ]; then
          rm -rf {INSTALL_PATH}
          mkdir -p {INSTALL_PATH}
          /opt/homebrew/bin/uv venv --python 3.12 {INSTALL_PATH}/.venv
          /opt/homebrew/bin/uv pip install --python {VENV_PYTHON} "mlflow=={VERSION}"
          echo '{VERSION}' > "$STAMP"
        fi
        """).strip(),
    ],
)

# --- Wrapper script ---
# --host 127.0.0.1 keeps it loopback-only (Caddy is the only path in). The registry requires
# the DB backend; artifacts live beside the DB so the laptop's downloads resolve.
_wrapper = textwrap.dedent(f"""
#!/bin/sh
set -e
exec {MLFLOW_BIN} server \\
    --backend-store-uri "sqlite:///{DATA_PATH}/mlflow.db" \\
    --default-artifact-root "{DATA_PATH}/mlartifacts" \\
    --host 127.0.0.1 \\
    --port {INTERNAL_PORT}
""").lstrip()

files.put(
    name="Write mlflow wrapper",
    src=io.BytesIO(_wrapper.encode()),
    dest=WRAPPER_PATH,
    user="root",
    group="wheel",
    mode="755",
)

# --- LaunchDaemon plist ---
_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists/1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{WRAPPER_PATH}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/var/root</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>{LOG_PATH}</string>
</dict>
</plist>
"""

files.put(
    name="Write mlflow plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

_static_hash = hashlib.sha256(
    (_wrapper + _plist + VERSION + str(INTERNAL_PORT)).encode()
).hexdigest()

server.shell(
    name="Bootstrap mlflow + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)
