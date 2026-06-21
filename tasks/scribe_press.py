"""Scribe-press worker — Audible AAX/AAXC decrypt + remux on the Mac mini.

Press is the ffmpeg-heavy half of the scribe pair. It downloads AAXC from
Audible's CDN to local SSD, runs ffmpeg with the per-book voucher (or the
account's activation_bytes for legacy AAX), streams the artifacts back to
scribe (Pi) for NAS write, and forgets the job.

Tracks a branch (default `main`), not a semver tag — scribe is too young
for proper releases. The stamp records the commit SHA that built the
binary so a redeploy only rebuilds when something actually changed on
the tracked branch. Bumping `SCRIBE_PRESS["branch"]` is the same as
pulling a new image tag for the raspi-side containers — once scribe
stabilizes this can flip to a tag.

  - fetch tarball at `archive/refs/heads/{branch}.tar.gz`
  - `cargo build --release -p scribe-press`
  - install binary to /usr/local/bin/scribe-press
  - LaunchDaemon (bind 127.0.0.1:{internal_port})
  - Caddy site at LAN-facing port with bearer auth

Bearer secret lives in 1Password (`mini/scribe-press` item, `api_key` field)
and gets written to /etc/secrets/scribe-press.env by tasks/secrets.py — the
same value must be pasted into the raspi `scribe` 1Password item's `press_token`
field so scribe knows what to send.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from group_data.all import SCRIBE_PRESS
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.scribe-press"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/scribe-press-run.sh"
BIN_PATH = "/usr/local/bin/scribe-press"
INSTALL_PATH = "/Applications/scribe-press"
VERSION_STAMP = f"{INSTALL_PATH}.sha"
TMP_DIR = "/Users/Shared/scribe-press-tmp"
LOG_PATH = "/opt/homebrew/var/log/scribe-press.log"

REPO = SCRIBE_PRESS.get("repo", "eetu/scribe")
BRANCH = SCRIBE_PRESS.get("branch", "main")
INTERNAL_PORT = SCRIBE_PRESS["internal_port"]
MAX_JOBS = SCRIBE_PRESS.get("max_jobs", 2)
TARBALL_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.tar.gz"
SHA_API = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"

# --- Scratch dir (large; lives on the mini SSD, never on NAS) ---
files.directory(
    name=f"Create {TMP_DIR}",
    path=TMP_DIR,
    user="root",
    group="wheel",
    mode="755",
    present=True,
)

# --- Build scribe-press from source (rebuild only when remote SHA changes) ---
server.shell(
    name=f"Build scribe-press @ {BRANCH}",
    commands=[
        textwrap.dedent(f"""
        set -e
        export PATH=/opt/homebrew/bin:$PATH
        STAMP={VERSION_STAMP}
        SHA=$(curl -fsSL "{SHA_API}" \\
              -H "Accept: application/vnd.github+json" \\
              | /opt/homebrew/bin/jq -r .sha)
        if [ -z "$SHA" ] || [ "$SHA" = "null" ]; then
          echo "scribe-press: failed to resolve {BRANCH} HEAD via GH API" >&2
          exit 1
        fi
        if [ "$(cat "$STAMP" 2>/dev/null)" = "$SHA" ] && [ -x "{BIN_PATH}" ]; then
          exit 0
        fi
        TMP=$(mktemp -d)
        curl -fsSL -o "$TMP/scribe.tar.gz" "{TARBALL_URL}"
        rm -rf {INSTALL_PATH}
        mkdir -p {INSTALL_PATH}
        tar -xzf "$TMP/scribe.tar.gz" -C {INSTALL_PATH} --strip-components=1
        rm -rf "$TMP"
        cd {INSTALL_PATH}
        /opt/homebrew/bin/cargo build --release -p scribe-press
        install -m 755 target/release/scribe-press {BIN_PATH}
        echo "$SHA" > "$STAMP"
        """).strip(),
    ],
)

# --- Wrapper that sources the bearer secret + tmp-dir env ---
_wrapper = textwrap.dedent(f"""
#!/bin/sh
set -e
if [ -f /etc/secrets/scribe-press.env ]; then
  set -a
  . /etc/secrets/scribe-press.env
  set +a
fi
export PRESS_BIND=127.0.0.1:{INTERNAL_PORT}
export PRESS_TMP_DIR={TMP_DIR}
export PRESS_MAX_JOBS={MAX_JOBS}
export FFMPEG_BIN=/opt/homebrew/bin/ffmpeg
exec {BIN_PATH}
""").lstrip()

files.put(
    name="Write scribe-press wrapper",
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
    name="Write scribe-press plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

# Hash plist + wrapper + branch + port so any IaC tweak kicks the daemon.
# Note: this hash deliberately *doesn't* include the remote SHA — the
# source-fetch step above is the canonical trigger when scribe-press itself
# changes upstream. kickstart_if_changed then catches binary mtime drift
# via its own stamp because the build step rewrote /usr/local/bin/scribe-press.
_static_hash = hashlib.sha256(
    _plist.encode() + _wrapper.encode() + (BRANCH + str(INTERNAL_PORT) + str(MAX_JOBS)).encode(),
).hexdigest()

server.shell(
    name="Bootstrap scribe-press + kickstart on change",
    commands=[
        kickstart_if_changed(
            LABEL,
            _static_hash,
            env_files=["/etc/secrets/scribe-press.env"],
        )
    ],
)
