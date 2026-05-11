"""Hourly disk pressure alert via macOS unified log.

Model weights live on the same APFS volume as macOS; runaway logs or rogue HF
downloads can fill the disk silently on a headless box. This task installs a
launchd job that runs hourly, reads `df` for `/Users/Shared`, and emits a
`user.warn` syslog entry when free space drops below `DISK_ALERT_GB`.

We rely on the unified log + beszel-agent's disk metrics as the alerting
substrate — no webhook in this task. View via:
    log show --predicate 'eventMessage contains "mini-diskalert"' --last 1d

Threshold is configurable via `DISK_ALERT_GB` in `group_data/all.py`; default
is 20 GB which gives roughly 2x headroom for a Flux model download mid-deploy.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from group_data import all as _all
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.diskalert"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
SCRIPT_PATH = "/usr/local/bin/mini-diskalert.sh"
LOG_PATH = "/opt/homebrew/var/log/diskalert.log"

INTERVAL_SECONDS = 3600
THRESHOLD_GB = int(getattr(_all, "DISK_ALERT_GB", 20))
WATCH_PATH = "/Users/Shared"

# `df -k` reports in KiB. We convert to GB by /1024/1024 (binary) then compare
# against threshold. `awk` extracts available column from the data row.
_script = textwrap.dedent(f"""
#!/bin/sh
set -u

avail_kib=$(df -k "{WATCH_PATH}" | awk 'NR==2 {{print $4}}')
avail_gb=$((avail_kib / 1024 / 1024))

if [ "$avail_gb" -lt {THRESHOLD_GB} ]; then
  logger -p user.warn \\
    "mini-diskalert: {WATCH_PATH} has only ${{avail_gb}} GB free (threshold {THRESHOLD_GB} GB)"
fi
echo "$(date -u +%FT%TZ) avail=${{avail_gb}}GB threshold={THRESHOLD_GB}GB"
""").lstrip()

files.put(
    name="Write diskalert script",
    src=io.BytesIO(_script.encode()),
    dest=SCRIPT_PATH,
    user="root",
    group="wheel",
    mode="755",
)

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
    name="Write diskalert plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

_static_hash = hashlib.sha256((_script + _plist).encode()).hexdigest()

server.shell(
    name="Bootstrap diskalert + reload on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)
