"""Piper TTS endpoint.

Uses `piper-tts[http]` from PyPI (uv venv) for the Python API + bundled voice
downloader, but replaces upstream's buffered `piper.http_server` with our own
streaming Flask wrapper (`files/piper-server.py`). The wrapper emits chunked
WAV so first-byte latency stays low for long utterances. `/voices` shape is
preserved verbatim so existing clients (../chat) don't have to change.

Voices: PIPER["voices"] is a list of slugs (`<lang>-<voice>-<quality>`); each
is a pair of files under `/Users/Shared/piper-voices/`. The bundled
`piper.download_voices` CLI handles the download + checksum step. The first
slug in the list is the wrapper's `--default-model`; all listed slugs are
loadable at request time by including `"voice": "<slug>"` in the POST body.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from group_data.all import PIPER
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.piper"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/piper-run.sh"
INSTALL_PATH = "/Applications/piper"
VENV_PYTHON = f"{INSTALL_PATH}/.venv/bin/python"
SERVER_SCRIPT = f"{INSTALL_PATH}/piper-server.py"
VERSION_STAMP = f"{INSTALL_PATH}.version"
VOICES_PATH = "/Users/Shared/piper-voices"
LOG_PATH = "/opt/homebrew/var/log/piper.log"

VERSION = PIPER["version"]
INTERNAL_PORT = PIPER["internal_port"]
# Piper voice slugs: <lang>-<voice>-<quality>. The download CLI accepts these
# directly and pulls both .onnx + .onnx.json from rhasspy/piper-voices. First
# entry seeds the daemon (passed as `-m`); the rest are loadable at request
# time by clients via the JSON `"voice"` field, since they all live under
# the same --data-dir.
VOICE_SLUGS = list(PIPER["voices"])
if not VOICE_SLUGS:
    raise RuntimeError("PIPER['voices'] must list at least one voice slug")
DEFAULT_VOICE = VOICE_SLUGS[0]

# --- Voice directory (root-owned, survives venv rebuilds) ---
files.directory(
    name=f"Create {VOICES_PATH}",
    path=VOICES_PATH,
    user="root",
    group="wheel",
    mode="755",
    present=True,
)

# --- Install piper + venv ---
# Bumping VERSION wipes INSTALL_PATH/.venv (not the voices) and reinstalls.
# `piper-tts[http]` extra pulls FastAPI + uvicorn for the bundled server.
server.shell(
    name=f"Install piper-tts {VERSION} + .venv",
    commands=[
        textwrap.dedent(f"""
        STAMP={VERSION_STAMP}
        if [ "$(cat "$STAMP" 2>/dev/null)" = "{VERSION}" ] && [ -x "{VENV_PYTHON}" ]; then
          exit 0
        fi
        rm -rf {INSTALL_PATH}
        mkdir -p {INSTALL_PATH}
        /opt/homebrew/bin/uv venv --python 3.12 {INSTALL_PATH}/.venv
        /opt/homebrew/bin/uv pip install \\
          --python {VENV_PYTHON} \\
          'piper-tts[http]=={VERSION}'
        echo '{VERSION}' > "$STAMP"
        """).strip(),
    ],
)

# --- Pull every configured voice ---
# `piper.download_voices` skips already-present files, so each call is
# idempotent. Medium-quality voices are ~63 MB; low-quality variants are
# smaller. Listing each voice as its own pyinfra op gives clearer logs than
# folding them into a shell loop.
for _slug in VOICE_SLUGS:
    server.shell(
        name=f"Download piper voice {_slug}",
        commands=[
            textwrap.dedent(f"""
            if [ -f "{VOICES_PATH}/{_slug}.onnx" ] && \\
               [ -f "{VOICES_PATH}/{_slug}.onnx.json" ]; then
              exit 0
            fi
            {VENV_PYTHON} -m piper.download_voices \\
              --data-dir {VOICES_PATH} \\
              {_slug}
            """).strip(),
        ],
    )

# --- Streaming server script ---
# Custom Flask wrapper that emits chunked WAV instead of buffering. Source is
# version-controlled at files/piper-server.py and dropped onto the Mini next
# to the venv; the launchd plist invokes it via the venv's python.
files.put(
    name="Write piper streaming server",
    src="files/piper-server.py",
    dest=SERVER_SCRIPT,
    user="root",
    group="wheel",
    mode="755",
)

with open("files/piper-server.py", "rb") as _server_src_file:
    _server_src = _server_src_file.read()

# --- Wrapper script ---
# `--default-model` sets the voice loaded on startup. All voices under
# --data-dir are loadable at request time when the client passes
# `"voice": "<slug>"` in the JSON body.
_wrapper = textwrap.dedent(f"""
#!/bin/sh
set -e
exec {VENV_PYTHON} {SERVER_SCRIPT} \\
    --default-model {DEFAULT_VOICE} \\
    --data-dir {VOICES_PATH} \\
    --host 127.0.0.1 \\
    --port {INTERNAL_PORT}
""").lstrip()

files.put(
    name="Write piper wrapper",
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
    name="Write piper plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

# Hash plist + wrapper + server script + version + voice list so any voice
# add/remove, any wrapper tweak, or any server-script edit kicks the daemon.
_static_hash = hashlib.sha256(
    _plist.encode()
    + _wrapper.encode()
    + _server_src
    + (VERSION + str(INTERNAL_PORT) + ",".join(VOICE_SLUGS)).encode(),
).hexdigest()

server.shell(
    name="Bootstrap piper + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)
