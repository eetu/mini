"""Caddy as the LAN-facing gateway in front of ollama.

Always installed — provides a stable indirection layer regardless of whether
auth is enforced. When OLLAMA["require_api_key"] is True, Caddy gates every
request on `Authorization: Bearer $OLLAMA_API_KEY`. Otherwise it's a
transparent reverse proxy and the LAN+pf perimeter is the only trust boundary.
"""

import hashlib
import io

from pyinfra.operations import files, server

from group_data.all import OLLAMA
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.caddy"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/caddy-run.sh"
CADDYFILE_PATH = "/etc/caddy/Caddyfile"

# --- Caddyfile ---

if OLLAMA.get("require_api_key"):
    # Bearer-gated: the @authed matcher only matches when the request header
    # is exactly `Bearer <token>`. Anything else falls through to the 401.
    _caddyfile = f"""{{
    admin off
    auto_https off
}}

:{OLLAMA["port"]} {{
    @authed header Authorization "Bearer {{env.OLLAMA_API_KEY}}"
    handle @authed {{
        reverse_proxy 127.0.0.1:{OLLAMA["internal_port"]}
    }}
    respond "unauthorized" 401
    log {{
        output file /opt/homebrew/var/log/caddy/access.log
    }}
}}
"""
else:
    _caddyfile = f"""{{
    admin off
    auto_https off
}}

:{OLLAMA["port"]} {{
    reverse_proxy 127.0.0.1:{OLLAMA["internal_port"]}
    log {{
        output file /opt/homebrew/var/log/caddy/access.log
    }}
}}
"""

# --- Wrapper script ---
# Sources /etc/secrets/ollama.env when present so {env.OLLAMA_API_KEY} in the
# Caddyfile resolves at startup. Rotating the secret only requires
# re-running tasks/secrets.py + tasks/caddy.py — the env-file hash trips
# kickstart_if_changed below.
_wrapper = """#!/bin/sh
set -e
if [ -f /etc/secrets/ollama.env ]; then
  . /etc/secrets/ollama.env
  export OLLAMA_API_KEY
fi
exec /opt/homebrew/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
"""

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
  <string>/opt/homebrew/var/log/caddy/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/opt/homebrew/var/log/caddy/stderr.log</string>
</dict>
</plist>
"""

# --- Directories ---

for _path, _mode in (
    ("/etc/caddy", "755"),
    ("/opt/homebrew/var/log/caddy", "755"),
):
    files.directory(
        name=f"Create {_path}",
        path=_path,
        user="root",
        group="wheel",
        mode=_mode,
        present=True,
    )

# --- Files ---

files.put(
    name="Write Caddyfile",
    src=io.BytesIO(_caddyfile.encode()),
    dest=CADDYFILE_PATH,
    user="root",
    group="wheel",
    mode="644",
)

files.put(
    name="Write caddy wrapper",
    src=io.BytesIO(_wrapper.encode()),
    dest=WRAPPER_PATH,
    user="root",
    group="wheel",
    mode="755",
)

files.put(
    name="Write caddy plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

# --- Bootstrap + kickstart on change ---

_static_hash = hashlib.sha256(
    (_caddyfile + _wrapper + _plist).encode(),
).hexdigest()

_env_files = ("/etc/secrets/ollama.env",) if OLLAMA.get("require_api_key") else ()

server.shell(
    name="Bootstrap caddy + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash, env_files=_env_files)],
)
