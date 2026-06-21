"""
Secret access for the deploy via the 1Password CLI.

A thin ``Backend`` (3 primitives: read_field / read_login / item_exists) sits
under a stable set of public helpers (``ollama_api_key()``,
``beszel_agent_creds()``, …) that the task files call.

Auth: the 1Password CLI through the desktop-app integration. The first `op`
call in a deploy triggers Touch ID; the unlock is cached (~10 min) for the
rest. No session env var. (Enable: 1Password → Settings → Developer →
"Integrate with 1Password CLI". `op signin` if not using the desktop app.)

The vault name is the `mini` 1Password vault; override with the OP_VAULT env var.

Item / field map (one vault, items named as below):
  ollama         login (unused)  field: api_key (hidden)
  comfyui        login (unused)  field: api_key (hidden)
  whisper        login (unused)  field: api_key (hidden)
  piper          login (unused)  field: api_key (hidden)
  scribe-press   login (unused)  field: api_key (hidden) — must match the raspi
                                 `scribe` item's `press_token` field
  beszel-agent   login (unused)  fields: token (hidden), key (hidden)

The `ollama`, `comfyui`, `whisper`, `piper`, and `scribe-press` items are only
required when their respective `require_api_key` flag is True. Generate each
token once with `openssl rand -hex 32` and paste it as the `api_key` hidden
field on the item before deploying with the flag enabled.

The `beszel-agent` item is required whenever tasks/beszel.py is in deploy.py.
Copy `token` and `key` from the running raspi hub — either from the Add System
dialog in the hub UI, or from `/etc/secrets/beszel-agent.env` on the raspi.
"""

import json
import os
import subprocess
from typing import Protocol


class Backend(Protocol):
    """Vendor-neutral secret store. ``read_field`` returns "" for a missing
    field so the fail-fast wrappers below can raise a uniform error."""

    def read_field(self, item: str, field: str) -> str: ...
    def read_login(self, item: str) -> dict: ...
    def item_exists(self, item: str) -> bool: ...


# --------------------------------------------------------------------------- #
# 1Password backend (default)
# --------------------------------------------------------------------------- #


class OpBackend:
    """1Password CLI. Reads parse `op item get --format json` (robust to
    sectioned fields, unlike `op read op://…`)."""

    def __init__(self, vault: str):
        self.vault = vault
        self._cache: dict[str, dict] = {}

    def _op(self, *args: str) -> str:
        return subprocess.run(
            ["op", *args, "--vault", self.vault],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _item(self, name: str) -> dict:
        if name not in self._cache:
            self._cache[name] = json.loads(self._op("item", "get", name, "--format", "json"))
        return self._cache[name]

    def _field_map(self, name: str) -> dict:
        """label -> value for flat fields; for sectioned fields the key is the
        reconstructed `section.label` (recovers names 1Password split on a dot)."""
        out: dict[str, str] = {}
        for f in self._item(name).get("fields", []):
            val = f.get("value")
            if val is None:
                continue
            label = f.get("label", "")
            section = (f.get("section") or {}).get("label")
            out[f"{section}.{label}" if section else label] = val
        return out

    def read_field(self, item: str, field: str) -> str:
        return self._field_map(item).get(field, "") or ""

    def read_login(self, item: str) -> dict:
        login = {"username": "", "password": ""}
        for f in self._item(item).get("fields", []):
            if f.get("purpose") == "USERNAME":
                login["username"] = f.get("value") or ""
            elif f.get("purpose") == "PASSWORD":
                login["password"] = f.get("value") or ""
        return login

    def item_exists(self, item: str) -> bool:
        try:
            self._item(item)
            return True
        except subprocess.CalledProcessError:
            return False


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #

_VAULT = os.environ.get("OP_VAULT", "mini")
_b: Backend = OpBackend(_VAULT)


# --------------------------------------------------------------------------- #
# Public helpers — vendor-neutral; task files call these
# --------------------------------------------------------------------------- #


def _api_key(service: str) -> str:
    """Return the bearer token from item `<service>`, field `api_key`.

    Only call when the service's `require_api_key` flag is True so the deploy
    fails fast with a clear error before writing a half-configured Caddyfile.
    """
    key = _b.read_field(service, "api_key")
    if not key:
        raise RuntimeError(
            f"Secret item '{_VAULT}/{service}' missing field 'api_key'.\n"
            "Generate one: openssl rand -hex 32\n"
            "Then add it to the item before re-running the deploy."
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


def scribe_press_api_key() -> str:
    """Bearer that gates inbound clients hitting scribe-press over Caddy.
    Same value must be pasted into the raspi `scribe` item under
    `press_token` so the Pi-side backend can authenticate."""
    return _api_key("scribe-press")


def beszel_agent_creds() -> dict:
    """Return TOKEN + KEY for the beszel agent from the `beszel-agent` item.

    Raises if either field is missing. Both come from the raspi hub:
      token — universal-token from the hub (hub UI > Add System, or
              /etc/secrets/beszel-agent.env on the raspi).
      key   — hub ed25519 public key (same source).
    """
    token = _b.read_field("beszel-agent", "token")
    key = _b.read_field("beszel-agent", "key")
    missing = [n for n, v in (("token", token), ("key", key)) if not v]
    if missing:
        raise RuntimeError(
            f"Secret item '{_VAULT}/beszel-agent' missing field(s): "
            f"{', '.join(missing)}.\n"
            "Copy them from the raspi hub UI (Add System) or from\n"
            "/etc/secrets/beszel-agent.env on the raspi, then re-run the deploy."
        )
    return {"token": token, "key": key}
