"""Whisper.cpp speech-to-text endpoint.

The Homebrew formula `whisper-cpp` builds with `-DWHISPER_BUILD_SERVER=OFF`,
so brew ships only `whisper-cli`. We need the HTTP server (the
`examples/server` target) to expose `/inference` on the LAN, so this task
clones whisper.cpp at the pinned tag and builds `whisper-server` from source
via cmake. Metal acceleration auto-enables on Apple Silicon.

The build follows the Ollama.app pattern: download a release tarball, install
the resulting binary at `/usr/local/bin/whisper-server`, stamp the version,
and let a LaunchDaemon run it bound to the loopback `internal_port`. Caddy
gates the LAN-facing `port`.

Models from https://huggingface.co/ggerganov/whisper.cpp/tree/main — the
default `ggml-large-v3-turbo-q5_0.bin` is ~574 MB, hits ~50x realtime on the
M4 Pro Metal backend, and quantization loss is imperceptible for English.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from group_data.all import WHISPER
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.whisper"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/whisper-run.sh"
BIN_PATH = "/usr/local/bin/whisper-server"
INSTALL_PATH = "/Applications/whisper.cpp"
VERSION_STAMP = f"{INSTALL_PATH}.version"
MODELS_PATH = "/Users/Shared/whisper-models"
LOG_PATH = "/opt/homebrew/var/log/whisper.log"

VERSION = WHISPER["version"]
INTERNAL_PORT = WHISPER["internal_port"]
MODEL_FILENAME = WHISPER["model_filename"]
MODEL_PATH = f"{MODELS_PATH}/{MODEL_FILENAME}"
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_FILENAME}"
TARBALL_URL = f"https://github.com/ggml-org/whisper.cpp/archive/refs/tags/{VERSION}.tar.gz"

# --- Models directory ---
files.directory(
    name=f"Create {MODELS_PATH}",
    path=MODELS_PATH,
    user="root",
    group="wheel",
    mode="755",
    present=True,
)

# --- Build whisper-server from source ---
# We invert the brew formula's `-DWHISPER_BUILD_SERVER=OFF` and add
# `-DWHISPER_BUILD_EXAMPLES=ON` so the server target compiles. Metal +
# Accelerate flags auto-detect on Apple Silicon. The build tree lives under
# INSTALL_PATH/build/ — bumping VERSION wipes the whole install dir and
# rebuilds from scratch. Models live outside and survive version bumps.
server.shell(
    name=f"Build whisper-server {VERSION}",
    commands=[
        textwrap.dedent(f"""
        STAMP={VERSION_STAMP}
        if [ "$(cat "$STAMP" 2>/dev/null)" = "{VERSION}" ] && [ -x "{BIN_PATH}" ]; then
          exit 0
        fi
        TMP=$(mktemp -d)
        curl -fsSL -o "$TMP/whisper.tar.gz" "{TARBALL_URL}"
        rm -rf {INSTALL_PATH}
        mkdir -p {INSTALL_PATH}
        tar -xzf "$TMP/whisper.tar.gz" -C {INSTALL_PATH} --strip-components=1
        rm -rf "$TMP"
        /opt/homebrew/bin/cmake -S {INSTALL_PATH} -B {INSTALL_PATH}/build \\
          -DCMAKE_BUILD_TYPE=Release \\
          -DBUILD_SHARED_LIBS=OFF \\
          -DWHISPER_BUILD_SERVER=ON \\
          -DWHISPER_BUILD_EXAMPLES=ON \\
          -DWHISPER_BUILD_TESTS=OFF
        /opt/homebrew/bin/cmake --build {INSTALL_PATH}/build --config Release -j
        install -m 755 -o root -g wheel \\
          {INSTALL_PATH}/build/bin/whisper-server {BIN_PATH}
        echo '{VERSION}' > "$STAMP"
        """).strip(),
    ],
)

# --- Pull the model weight (idempotent) ---
server.shell(
    name=f"Download {MODEL_FILENAME}",
    commands=[
        textwrap.dedent(f"""
        DEST="{MODEL_PATH}"
        if [ ! -f "$DEST" ]; then
          mkdir -p "$(dirname "$DEST")"
          curl -fL --retry 3 --retry-delay 5 -o "$DEST.part" "{MODEL_URL}"
          mv "$DEST.part" "$DEST"
        fi
        """).strip(),
    ],
)

# --- Wrapper script ---
# `--print-progress false` keeps the log readable. `--inference-path` defaults
# to `/inference`; we keep that name and let the chat app rewrite if it wants
# an OpenAI-style path.
_wrapper = textwrap.dedent(f"""
#!/bin/sh
set -e
exec {BIN_PATH} \\
    --model {MODEL_PATH} \\
    --host 127.0.0.1 \\
    --port {INTERNAL_PORT}
""").lstrip()

files.put(
    name="Write whisper wrapper",
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
    name="Write whisper plist",
    src=io.BytesIO(_plist.encode()),
    dest=PLIST_PATH,
    user="root",
    group="wheel",
    mode="644",
)

# Hash plist + wrapper + version + port so any edit triggers a kickstart even
# when the plist text is byte-identical across version bumps.
_static_hash = hashlib.sha256(
    (_plist + _wrapper + VERSION + str(INTERNAL_PORT) + MODEL_FILENAME).encode(),
).hexdigest()

server.shell(
    name="Bootstrap whisper + kickstart on change",
    commands=[kickstart_if_changed(LABEL, _static_hash)],
)
