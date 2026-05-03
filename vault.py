"""
Bitwarden CLI helpers. Requires BW_SESSION env var to be set:
    set -x BW_SESSION (bw unlock --raw)

Item structure in the 'mini' folder:
  ollama  login  (unused)  fields: api_key (hidden, generated locally and stored in BW)

The `ollama` item is only required when OLLAMA["require_api_key"] is True.
Generate the token once with `openssl rand -hex 32` and paste it as the `api_key`
hidden field on the Bitwarden item before deploying with the flag enabled.
"""

import functools
import json
import subprocess


def _bw(*args):
    result = subprocess.run(
        ["bw"] + list(args),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@functools.cache
def _folder_id():
    folders = json.loads(_bw("list", "folders"))
    match = next((f for f in folders if f["name"] == "mini"), None)
    if not match:
        raise RuntimeError(
            "Bitwarden folder 'mini' not found.\n"
            'Run: bw create folder (echo \'{"name":"mini"}\' | bw encode)'
        )
    return match["id"]


@functools.cache
def _get_item(name):
    items = json.loads(_bw("list", "items", "--search", name, "--folderid", _folder_id()))
    matches = [i for i in items if i["name"] == name]
    if not matches:
        raise RuntimeError(f"Bitwarden item 'mini/{name}' not found")
    return matches[0]


def _fields(item_name) -> dict:
    item = _get_item(item_name)
    return {f["name"]: f["value"] for f in (item.get("fields") or [])}


def ollama_api_key() -> str:
    """Return the Ollama bearer token from the `ollama` BW item.

    Raises if the item or field is missing — only call when
    OLLAMA["require_api_key"] is True so the deploy fails fast with a clear
    error before writing a half-configured Caddyfile.
    """
    key = _fields("ollama").get("api_key", "") or ""
    if not key:
        raise RuntimeError(
            "Bitwarden item 'mini/ollama' missing hidden field 'api_key'.\n"
            "Generate one: openssl rand -hex 32\n"
            "Then add it to the BW item before re-running the deploy."
        )
    return key
