"""Exclude model directories from Time Machine + Spotlight.

Combined ~30 GB of weights provide zero value in a TM backup (re-downloadable
from HuggingFace / ollama.com) and Spotlight indexing them just burns NVMe.

Spotlight: APFS indexing is volume-level — `mdutil -i off <subdir>` errors with
"unknown indexing state" because the indexing flag isn't a directory attribute.
The documented per-directory opt-out is a marker file at
`<dir>/.metadata_never_index`; mds skips the directory + its descendants when
present.

Time Machine: `tmutil addexclusion -p` writes a sticky exclusion that survives
the directory being deleted/recreated.

Runs after tasks/ollama.py + tasks/comfyui.py so the directories already exist
— the `[ -d ]` guard keeps this safe regardless of include order.
"""

from pyinfra.operations import server

EXCLUDED_PATHS = (
    "/Users/Shared/ollama-models",
    "/Users/Shared/comfyui-models",
    "/Users/Shared/whisper-models",
    "/Users/Shared/piper-voices",
)

for _path in EXCLUDED_PATHS:
    server.shell(
        name=f"Exclude {_path} from Time Machine + Spotlight",
        commands=[
            f"""
            if [ ! -d "{_path}" ]; then
              exit 0
            fi
            if ! tmutil isexcluded "{_path}" 2>/dev/null | grep -q '\\[Excluded\\]'; then
              tmutil addexclusion -p "{_path}"
            fi
            if [ ! -f "{_path}/.metadata_never_index" ]; then
              touch "{_path}/.metadata_never_index"
            fi
            """,
        ],
    )
