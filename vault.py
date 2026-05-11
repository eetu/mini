"""
Bitwarden CLI helpers. Requires BW_SESSION env var to be set:
    set -x BW_SESSION (bw unlock --raw)

Item structure in the 'mini' folder:
  ollama         login (unused)  fields: api_key (hidden)
  comfyui        login (unused)  fields: api_key (hidden)
  whisper        login (unused)  fields: api_key (hidden)
  piper          login (unused)  fields: api_key (hidden)
  beszel-agent   login (unused)  fields: token (hidden), key (hidden)

The `ollama`, `comfyui`, `whisper`, and `piper` items are only required when
their respective `require_api_key` flag is True. Generate each token once
with `openssl rand -hex 32` and paste it as the `api_key` hidden field on
the Bitwarden item before deploying with the flag enabled.

The `beszel-agent` item is required whenever tasks/beszel.py is in deploy.py.
Copy `token` and `key` from the running raspi hub — either from the Add System
dialog in the hub UI, or from `/etc/secrets/beszel-agent.env` on the raspi.
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


def _api_key(service: str) -> str:
    """Return the bearer token from BW item `mini/<service>`, field `api_key`.

    Only call when the service's `require_api_key` flag is True so the deploy
    fails fast with a clear error before writing a half-configured Caddyfile.
    """
    key = _fields(service).get("api_key", "") or ""
    if not key:
        raise RuntimeError(
            f"Bitwarden item 'mini/{service}' missing hidden field 'api_key'.\n"
            "Generate one: openssl rand -hex 32\n"
            "Then add it to the BW item before re-running the deploy."
        )
    return key


def ollama_api_key() -> str:
    return _api_key("ollama")


def comfyui_api_key() -> str:
    return _api_key("comfyui")


def whisper_api_key() -> str:
    return _api_key("whisper")


def piper_api_key() -> str:
    return _api_key("piper")


def beszel_agent_creds() -> dict:
    """Return TOKEN + KEY for the beszel agent from the `beszel-agent` BW item.

    Raises if either field is missing. Both come from the raspi hub:
      token — universal-token from the hub (hub UI > Add System, or
              /etc/secrets/beszel-agent.env on the raspi).
      key   — hub ed25519 public key (same source).
    """
    f = _fields("beszel-agent")
    token = f.get("token", "") or ""
    key = f.get("key", "") or ""
    missing = [n for n, v in (("token", token), ("key", key)) if not v]
    if missing:
        raise RuntimeError(
            "Bitwarden item 'mini/beszel-agent' missing hidden field(s): "
            f"{', '.join(missing)}.\n"
            "Copy them from the raspi hub UI (Add System) or from\n"
            "/etc/secrets/beszel-agent.env on the raspi, then re-run the deploy."
        )
    return {"token": token, "key": key}
