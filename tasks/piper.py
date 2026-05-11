"""Piper TTS endpoint.

Piper ships its own HTTP server (`python -m piper.http_server`) in the
`piper-tts[http]` extra, so this task is the cleanest of the Python-venv
stack: uv venv, `pip install piper-tts[http]==VERSION`, download the voice
files, run the module via LaunchDaemon. No custom wrapper service.

The HTTP API is `POST /` with `{"text": "..."}` returning a WAV body. Not
OpenAI-compatible — calling apps need a thin adapter if they expect
`/v1/audio/speech`. We trade compatibility for one less moving part on the
Mini.

Voices: PIPER["voices"] is a list of slugs (`<lang>-<voice>-<quality>`); each
is a pair of files under `/Users/Shared/piper-voices/`. The bundled
`piper.download_voices` CLI handles the download + checksum step. The first
slug in the list is passed as `-m` to `piper.http_server` to seed the daemon;
all listed slugs are loadable at request time by including
`"voice": "<slug>"` in the POST body, since `--data-dir` makes them all
visible. Re-runs are cheap — the CLI skips already-present files.
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

# --- Wrapper script ---
# `-m` only sets the default voice. All voices under --data-dir are loadable
# at request time when the client passes `"voice": "<slug>"` in the JSON body.
_wrapper = textwrap.dedent(f"""
#!/bin/sh
set -e
exec {VENV_PYTHON} -m piper.http_server \\
    --model {DEFAULT_VOICE} \\
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

# Hash plist + wrapper + version + voice list so any voice add/remove + any
# edit kicks the daemon.
_static_hash = hashlib.sha256(
    (_plist + _wrapper + VERSION + str(INTERNAL_PORT) + ",".join(VOICE_SLUGS)).encode(),
).hexdigest()

server.shell(
    name="Bootstrap piper + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)
