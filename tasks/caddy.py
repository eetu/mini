"""Caddy as the LAN-facing gateway in front of every Mini upstream.

Always installed — provides a stable indirection layer regardless of whether
auth is enforced. Each entry in `SERVICES` mints one `:port` site block
fronting a 127.0.0.1 upstream. When the service's `require_api_key` flag is
True, Caddy gates every request on `Authorization: Bearer $<SERVICE>_API_KEY`.
Otherwise the block is a transparent reverse proxy and the LAN+pf perimeter
is the only trust boundary.
"""

import hashlib
import io

from pyinfra.operations import files, server

from group_data.all import COMFYUI, OLLAMA, PIPER, WHISPER
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.caddy"

# (name, config-dict, bearer-env-var-name). Each entry mints one `:port` site
# block fronting a 127.0.0.1 upstream. Append a tuple here to route a new
# service; the rest of the file iterates the list. The bearer env-var name
# must match what tasks/secrets.py writes into /etc/secrets/<name>.env.
SERVICES = (
    ("ollama", OLLAMA, "OLLAMA_API_KEY"),
    ("comfyui", COMFYUI, "COMFYUI_API_KEY"),
    ("whisper", WHISPER, "WHISPER_API_KEY"),
    ("piper", PIPER, "PIPER_API_KEY"),
)
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
#
# X-Forwarded-For is added so upstream logs (ollama.log, comfyui.log) record
# the originating LAN client, not 127.0.0.1. Caddy's own access log already
# has the client IP via `remote_ip`.
#
# Access log uses Caddy's built-in roller (roll_size + roll_keep) so it never
# needs newsyslog / external rotation. Stdout/stderr from the wrapper go to
# launchd-captured files which tasks/logrotate.py handles separately.


_PROXY_DIRECTIVES = """\
header_up Host {upstream_hostport}
header_up X-Forwarded-For {client_ip}
flush_interval -1"""

_LOG_DIRECTIVE = """\
output file /opt/homebrew/var/log/caddy/access.log {
        roll_size 10MiB
        roll_keep 7
        roll_keep_for 720h
    }"""


def _site_block(port, upstream_port, env_var, require_api_key):
    upstream = f"127.0.0.1:{upstream_port}"
    if require_api_key:
        return f""":{port} {{
    @authed header Authorization "Bearer {{env.{env_var}}}"
    handle @authed {{
        reverse_proxy {upstream} {{
            {_PROXY_DIRECTIVES.replace(chr(10), chr(10) + "            ")}
        }}
    }}
    respond "unauthorized" 401
    log {{
        {_LOG_DIRECTIVE}
    }}
}}
"""
    return f""":{port} {{
    reverse_proxy {upstream} {{
        {_PROXY_DIRECTIVES.replace(chr(10), chr(10) + "        ")}
    }}
    log {{
        {_LOG_DIRECTIVE}
    }}
}}
"""


_caddyfile = "{\n    admin off\n    auto_https off\n}\n\n" + "\n".join(
    _site_block(
        cfg["port"],
        cfg["internal_port"],
        env_var,
        cfg.get("require_api_key", False),
    )
    for _, cfg, env_var in SERVICES
)

# --- Wrapper script ---
# Sources every /etc/secrets/<service>.env when present so
# {env.<SERVICE>_API_KEY} placeholders in the Caddyfile resolve at startup.
# Rotating any secret only requires re-running tasks/secrets.py + tasks/caddy.py
# — the env-file hashes trip kickstart_if_changed below.
_env_paths = " ".join(f"/etc/secrets/{name}.env" for name, _, _ in SERVICES)
_wrapper = f"""#!/bin/sh
set -e
for f in {_env_paths}; do
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
    f"/etc/secrets/{name}.env" for name, cfg, _ in SERVICES if cfg.get("require_api_key")
)

server.shell(
    name="Bootstrap caddy + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash, env_files=_env_files)],
)
