"""Healthcheck: poll ollama + ComfyUI every 60s, kickstart on N consecutive fails.

`KeepAlive=true` in each service's plist already respawns crashes — this task
covers the other half: hangs and wedged states where the process is alive but
unresponsive (model load deadlock, MPS driver stall, etc).

Per-service failure counter at `/var/db/.{label}-fails`. After
`FAILURE_THRESHOLD` consecutive failures we issue `launchctl kickstart -k`
which respawns the daemon (kills + restarts). Counter resets on success or
after a forced respawn so the next cycle starts clean.

Reports unhealthy events to the macOS unified log via `logger -p user.warn`
so they show up in `log show --predicate 'process == "logger"' --last 1h` and
can be picked up by beszel/other monitors.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from group_data.all import COMFYUI, OLLAMA, PIPER, WHISPER
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.healthcheck"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
SCRIPT_PATH = "/usr/local/bin/mini-healthcheck.sh"
LOG_PATH = "/opt/homebrew/var/log/healthcheck.log"

INTERVAL_SECONDS = 60
FAILURE_THRESHOLD = 3
CURL_TIMEOUT = 5

# (launchd label, healthcheck URL). We probe the internal loopback port so the
# check exercises only the upstream service, not Caddy or pf.
# - ollama  /api/tags        — daemon-level readiness, no model load
# - comfyui /system_stats    — JSON OK once the workflow runtime is up
# - whisper /                — whisper-server's index page returns 200 on GET
# - piper   /voices          — POST-only / returns 405 to GET; /voices is a
#                              GET endpoint our custom wrapper exposes, so it
#                              doubles as the readiness probe
CHECKS = (
    ("com.eetu.ollama", f"http://127.0.0.1:{OLLAMA['internal_port']}/api/tags"),
    ("com.eetu.comfyui", f"http://127.0.0.1:{COMFYUI['internal_port']}/system_stats"),
    ("com.eetu.whisper", f"http://127.0.0.1:{WHISPER['internal_port']}/"),
    ("com.eetu.piper", f"http://127.0.0.1:{PIPER['internal_port']}/voices"),
)

_check_calls = "\n".join(f'check "{label}" "{url}"' for label, url in CHECKS)

_script = textwrap.dedent(f"""
#!/bin/sh
set -u

check() {{
  label="$1"
  url="$2"
  fails_file="/var/db/.${{label}}-fails"
  fails=$(cat "$fails_file" 2>/dev/null || echo 0)
  if curl -fsS --max-time {CURL_TIMEOUT} "$url" >/dev/null 2>&1; then
    if [ "$fails" -gt 0 ]; then
      echo 0 > "$fails_file"
    fi
    return 0
  fi
  fails=$((fails + 1))
  echo "$fails" > "$fails_file"
  if [ "$fails" -ge {FAILURE_THRESHOLD} ]; then
    logger -p user.warn \\
      "mini-healthcheck: $label unhealthy ($fails consecutive failures); kickstarting"
    launchctl kickstart -k "system/$label" 2>/dev/null || true
    echo 0 > "$fails_file"
  fi
}}

{_check_calls}
""").lstrip()

files.put(
    name="Write healthcheck script",
    src=io.BytesIO(_script.encode()),
    dest=SCRIPT_PATH,
    user="root",
    group="wheel",
    mode="755",
)

# StartInterval triggers every N seconds regardless of last run completion. At
# 60s intervals with a 5s curl timeout, we'd have to lose >55s/run to overlap
# — fine.
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
  <key>StartInterval</key>
  <integer>{INTERVAL_SECONDS}</integer>
  <key>StandardOutPath</key>
  <string>{LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>{LOG_PATH}</string>
</dict>
</plist>
"""

files.put(
    name="Write healthcheck plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

_static_hash = hashlib.sha256((_script + _plist).encode()).hexdigest()

server.shell(
    name="Bootstrap healthcheck + reload on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)
