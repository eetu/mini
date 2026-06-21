"""Beszel agent: native LaunchDaemon connecting outbound to the raspi hub.

Mac mini ships only the agent — the hub lives in the sibling raspi project.
WebSocket mode: agent dials the hub using a universal TOKEN + the hub's
ed25519 public KEY. No inbound port opens on the Mini.

Why native (not Podman): podman on macOS runs inside a Linux VM, so any
container would report VM metrics instead of host metrics — defeats the
point. Same reason no GPU-bound service should ever live in a Mac container.

Secret layout:
  /etc/secrets/beszel-agent.env — TOKEN + KEY, written by tasks/secrets.py
                                  from the 1Password item `mini/beszel-agent`.

Bootstrapping TOKEN + KEY: copy them out of the running raspi hub UI
(Add System dialog) or from `/etc/secrets/beszel-agent.env` on the raspi,
then paste into the 1Password item before deploying.

Toggle: BESZEL["enabled"]. Flipping False removes the LaunchDaemon, binary,
wrapper, and stamp on the next deploy.
"""

import hashlib
import io

from pyinfra.operations import files, server

from group_data.all import BESZEL
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.beszel-agent"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
BIN_PATH = "/usr/local/bin/beszel-agent"
WRAPPER_PATH = "/usr/local/bin/beszel-agent-run.sh"
DATA_DIR = "/var/lib/beszel-agent"
ENV_FILE = "/etc/secrets/beszel-agent.env"

if BESZEL.get("enabled"):
    VERSION = BESZEL["version"]
    _RELEASE_URL = (
        f"https://github.com/henrygd/beszel/releases/download/{VERSION}"
        "/beszel-agent_darwin_arm64.tar.gz"
    )

    files.directory(
        name=f"Create {DATA_DIR}",
        path=DATA_DIR,
        user="root",
        group="wheel",
        mode="700",
        present=True,
    )

    server.shell(
        name=f"Install beszel-agent {VERSION}",
        commands=[
            f"""
            STAMP={BIN_PATH}.version
            if [ "$(cat "$STAMP" 2>/dev/null)" != "{VERSION}" ]; then
              TMP=$(mktemp -d)
              curl -fsSL "{_RELEASE_URL}" | tar -xz -C "$TMP" beszel-agent
              install -m 755 -o root -g wheel "$TMP/beszel-agent" "{BIN_PATH}"
              rm -rf "$TMP"
              echo '{VERSION}' > "$STAMP"
            fi
            """,
        ],
    )

    # Wrapper sources the env file so TOKEN/KEY stay in /etc/secrets (600 root)
    # and never land in the world-readable plist.
    _wrapper = f"""#!/bin/sh
set -e
if [ -f {ENV_FILE} ]; then
  set -a
  . {ENV_FILE}
  set +a
fi
exec {BIN_PATH}
"""

    files.put(
        name="Write beszel-agent wrapper",
        src=io.BytesIO(_wrapper.encode()),
        dest=WRAPPER_PATH,
        user="root",
        group="wheel",
        mode="755",
    )

    # HUB_URL + DATA_DIR are non-secret config — fine in the plist.
    # DISABLE_SSH=true mirrors raspi: agent dials hub, no SSH listener.
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
    <key>HUB_URL</key>
    <string>{BESZEL["hub_url"]}</string>
    <key>DATA_DIR</key>
    <string>{DATA_DIR}</string>
    <key>DISABLE_SSH</key>
    <string>true</string>
    <key>HOME</key>
    <string>/var/root</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/opt/homebrew/var/log/beszel-agent.log</string>
  <key>StandardErrorPath</key>
  <string>/opt/homebrew/var/log/beszel-agent.log</string>
</dict>
</plist>
"""

    files.put(
        name="Write beszel-agent plist",
        src=io.BytesIO(_plist.encode()),
        dest=PLIST_PATH,
        user="root",
        group="wheel",
        mode="644",
    )

    # env file hashed at run time so a rotated TOKEN/KEY in 1Password triggers a
    # kickstart on the next deploy without changing plist/wrapper.
    _static_hash = hashlib.sha256((_wrapper + _plist).encode()).hexdigest()

    server.shell(
        name="Bootstrap beszel-agent + kickstart on change",
        commands=[kickstart_if_changed(LABEL, _static_hash, env_files=(ENV_FILE,))],
    )

else:
    # Disabled: bootout the daemon (if running) and remove its files. Idempotent
    # — re-enabling is a flag flip + redeploy.
    server.shell(
        name="Tear down beszel-agent (disabled)",
        commands=[
            f"""
            if launchctl print system/{LABEL} >/dev/null 2>&1; then
              launchctl bootout system/{LABEL} || true
            fi
            rm -f {PLIST_PATH} /var/db/.{LABEL}-stamp \
                  {BIN_PATH} {BIN_PATH}.version {WRAPPER_PATH}
            """,
        ],
    )
