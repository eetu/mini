"""Caddy as the LAN-facing gateway in front of ollama and ComfyUI.

Always installed — provides a stable indirection layer regardless of whether
auth is enforced. Each upstream gets its own `:port` site block. When the
service's `require_api_key` flag is True, Caddy gates every request on
`Authorization: Bearer $<SERVICE>_API_KEY`. Otherwise the block is a
transparent reverse proxy and the LAN+pf perimeter is the only trust boundary.
"""

import hashlib
import io

from pyinfra.operations import files, server

from group_data.all import COMFYUI, OLLAMA
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.caddy"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/caddy-run.sh"
CADDYFILE_PATH = "/etc/caddy/Caddyfile"

# --- Caddyfile site blocks ---
#
# Ollama applies an anti-DNS-rebinding check: when bound to 127.0.0.1:PORT it
# only accepts requests whose Host header matches that exact upstream. Caddy
# must rewrite Host on the way through, otherwise external hostnames
# (e.g. ai.anarkisti.com via raspi Traefik) get a 403 from ollama. ComfyUI
# doesn't share that quirk but the same rewrite is harmless.
#
# flush_interval -1 disables response body buffering so streaming responses
# (ollama SSE chat tokens, ComfyUI websocket-fallback progress) reach the
# client as the upstream emits them. Without it, Caddy buffers chunks and
# long jobs can hit upstream/downstream idle timeouts before the first byte
# makes it through.


def _site_block(port, upstream_port, env_var, require_api_key):
    upstream = f"127.0.0.1:{upstream_port}"
    if require_api_key:
        return f""":{port} {{
    @authed header Authorization "Bearer {{env.{env_var}}}"
    handle @authed {{
        reverse_proxy {upstream} {{
            header_up Host {{upstream_hostport}}
            flush_interval -1
        }}
    }}
    respond "unauthorized" 401
    log {{
        output file /opt/homebrew/var/log/caddy/access.log
    }}
}}
"""
    return f""":{port} {{
    reverse_proxy {upstream} {{
        header_up Host {{upstream_hostport}}
        flush_interval -1
    }}
    log {{
        output file /opt/homebrew/var/log/caddy/access.log
    }}
}}
"""


_caddyfile = (
    "{\n    admin off\n    auto_https off\n}\n\n"
    + _site_block(
        OLLAMA["port"],
        OLLAMA["internal_port"],
        "OLLAMA_API_KEY",
        OLLAMA.get("require_api_key", False),
    )
    + "\n"
    + _site_block(
        COMFYUI["port"],
        COMFYUI["internal_port"],
        "COMFYUI_API_KEY",
        COMFYUI.get("require_api_key", False),
    )
)

# --- Wrapper script ---
# Sources both /etc/secrets/<service>.env files when present so
# {env.<SERVICE>_API_KEY} placeholders in the Caddyfile resolve at startup.
# Rotating either secret only requires re-running tasks/secrets.py +
# tasks/caddy.py — the env-file hashes trip kickstart_if_changed below.
_wrapper = """#!/bin/sh
set -e
for f in /etc/secrets/ollama.env /etc/secrets/comfyui.env; do
  if [ -f "$f" ]; then
    set -a
    . "$f"
    set +a
  fi
done
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

_env_files = tuple(
    path
    for path, present in (
        ("/etc/secrets/ollama.env", OLLAMA.get("require_api_key")),
        ("/etc/secrets/comfyui.env", COMFYUI.get("require_api_key")),
    )
    if present
)

server.shell(
    name="Bootstrap caddy + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash, env_files=_env_files)],
)
