"""Daily log rotation for launchd-captured service logs.

macOS launchd opens `StandardOutPath` / `StandardErrorPath` with `O_APPEND`, so
truncating the file in place is safe — the daemon's next write appends from
offset 0 of the now-empty file. No `SIGHUP` / kickstart needed, which matters
because kickstarting ollama unloads a multi-GB model.

Rotation strategy is copy-then-truncate:
  1. `gzip -c log > log.1.gz`
  2. shift older `.N.gz` files up
  3. truncate `log` in place

Caddy's `access.log` is rotated by Caddy itself (Caddyfile `roll_size` /
`roll_keep`) — only its stdout/stderr land here. The job runs once at load
plus daily via `StartCalendarInterval`.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from tasks.util import kickstart_if_changed

LABEL = "com.eetu.log-rotate"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
SCRIPT_PATH = "/usr/local/bin/mini-log-rotate.sh"
LOG_PATH = "/opt/homebrew/var/log/log-rotate.log"

# (log path, retained .N.gz count). Threshold for rotation is hard-coded
# below — any file over 10 MiB rolls; smaller files are left alone.
# Caddy's access.log self-rotates inside Caddy via roll_size/roll_keep, so
# it's intentionally absent here — only the daemon's stdout/stderr go through
# this rotator. Any service that writes via launchd's StandardOut/ErrorPath
# needs an entry, otherwise the file grows unbounded.
ROTATIONS = (
    ("/opt/homebrew/var/log/ollama.log", 7),
    ("/opt/homebrew/var/log/ollama-warmup.log", 3),
    ("/opt/homebrew/var/log/comfyui.log", 7),
    ("/opt/homebrew/var/log/whisper.log", 7),
    ("/opt/homebrew/var/log/piper.log", 7),
    ("/opt/homebrew/var/log/beszel-agent.log", 7),
    ("/opt/homebrew/var/log/healthcheck.log", 3),
    ("/opt/homebrew/var/log/diskalert.log", 3),
    ("/opt/homebrew/var/log/log-rotate.log", 3),
    ("/opt/homebrew/var/log/caddy/stdout.log", 3),
    ("/opt/homebrew/var/log/caddy/stderr.log", 3),
)
MAX_SIZE_BYTES = 10 * 1024 * 1024

_rotate_calls = "\n".join(f'rotate "{path}" {keep}' for path, keep in ROTATIONS)

_script = textwrap.dedent(f"""
#!/bin/sh
set -e

rotate() {{
  path="$1"
  keep="$2"
  [ -f "$path" ] || return 0
  size=$(stat -f %z "$path" 2>/dev/null || echo 0)
  [ "$size" -lt {MAX_SIZE_BYTES} ] && return 0
  i=$((keep - 1))
  while [ "$i" -ge 1 ]; do
    if [ -f "$path.$i.gz" ]; then
      mv "$path.$i.gz" "$path.$((i + 1)).gz"
    fi
    i=$((i - 1))
  done
  gzip -c "$path" > "$path.1.gz"
  : > "$path"
  echo "rotated $path ($size bytes)"
}}

{_rotate_calls}
""").lstrip()

files.put(
    name="Write log-rotate script",
    src=io.BytesIO(_script.encode()),
    dest=SCRIPT_PATH,
    user="root",
    group="wheel",
    mode="755",
)

# StartCalendarInterval at 04:10 daily. RunAtLoad triggers an immediate pass on
# every deploy so a freshly-bumped MAX_SIZE_BYTES applies without waiting for
# 4 AM. `AbandonProcessGroup` lets the script's subshells outlive launchd's
# initial reap window — not strictly needed for this short job but harmless.
_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists/1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{SCRIPT_PATH}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>4</integer>
    <key>Minute</key>
    <integer>10</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>{LOG_PATH}</string>
</dict>
</plist>
"""

files.put(
    name="Write log-rotate plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

_static_hash = hashlib.sha256((_script + _plist).encode()).hexdigest()

server.shell(
    name="Bootstrap log-rotate + reload on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)
