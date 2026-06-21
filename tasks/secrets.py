"""Write secrets from 1Password to /etc/secrets/ on the Mini.

`vault` fetches from the `mini` 1Password vault at deploy time (see vault.py).
Only runs when a service has its `require_*` flag enabled. Empty deploys (no
secrets configured) leave /etc/secrets/ as an empty 700 dir.
"""

import io
import shlex

from pyinfra.operations import files

import vault
from group_data.all import (
    BESZEL,
    COMFYUI,
    FINKEYB,
    MLFLOW,
    OLLAMA,
    PIPER,
    SCRIBE_PRESS,
    WHISPER,
)


def _put_secret(name, content, dest, mode="600", group="wheel"):
    files.put(
        name=f"Write secret: {name}",
        src=io.BytesIO(content.encode()),
        dest=dest,
        user="root",
        group=group,
        mode=mode,
    )


files.directory(
    name="Create /etc/secrets (700)",
    path="/etc/secrets",
    user="root",
    group="wheel",
    mode="700",
    present=True,
)

# --- Ollama API key (only when auth is enabled) ---

if OLLAMA.get("require_api_key"):
    _put_secret(
        "ollama.env",
        f"OLLAMA_API_KEY={vault.ollama_api_key()}\n",
        "/etc/secrets/ollama.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/ollama.env (auth disabled)",
        path="/etc/secrets/ollama.env",
        present=False,
    )

# --- ComfyUI API key (only when auth is enabled) ---

if COMFYUI.get("require_api_key"):
    _put_secret(
        "comfyui.env",
        f"COMFYUI_API_KEY={vault.comfyui_api_key()}\n",
        "/etc/secrets/comfyui.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/comfyui.env (auth disabled)",
        path="/etc/secrets/comfyui.env",
        present=False,
    )

# --- Whisper API key (only when auth is enabled) ---

if WHISPER.get("require_api_key"):
    _put_secret(
        "whisper.env",
        f"WHISPER_API_KEY={vault.whisper_api_key()}\n",
        "/etc/secrets/whisper.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/whisper.env (auth disabled)",
        path="/etc/secrets/whisper.env",
        present=False,
    )

# --- Piper API key (only when auth is enabled) ---

if PIPER.get("require_api_key"):
    _put_secret(
        "piper.env",
        f"PIPER_API_KEY={vault.piper_api_key()}\n",
        "/etc/secrets/piper.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/piper.env (auth disabled)",
        path="/etc/secrets/piper.env",
        present=False,
    )

# --- Scribe-press API key (only when auth is enabled) ---

if SCRIBE_PRESS.get("require_api_key"):
    _put_secret(
        "scribe-press.env",
        f"SCRIBE_PRESS_API_KEY={vault.scribe_press_api_key()}\n",
        "/etc/secrets/scribe-press.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/scribe-press.env (auth disabled)",
        path="/etc/secrets/scribe-press.env",
        present=False,
    )

# --- MLflow API key (only when auth is enabled) ---

if MLFLOW.get("require_api_key"):
    _put_secret(
        "mlflow.env",
        f"MLFLOW_API_KEY={vault.mlflow_api_key()}\n",
        "/etc/secrets/mlflow.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/mlflow.env (auth disabled)",
        path="/etc/secrets/mlflow.env",
        present=False,
    )

# --- finkeyb retrain box: GH PAT + deployed-API creds (only when enabled) ---

if FINKEYB.get("enabled"):
    _fk = vault.finkeyb_creds()
    # gh_token + api_token can contain shell-significant chars; the wrapper sources this file via
    # POSIX `.`, so quote the values. api_url is a plain URL.
    _put_secret(
        "finkeyb.env",
        f"GH_TOKEN={shlex.quote(_fk['gh_token'])}\n"
        f"FINKEYB_API_TOKEN={shlex.quote(_fk['api_token'])}\n"
        f"FINKEYB_API_URL={_fk['api_url']}\n",
        "/etc/secrets/finkeyb.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/finkeyb.env (finkeyb disabled)",
        path="/etc/secrets/finkeyb.env",
        present=False,
    )

# --- Beszel agent TOKEN + KEY (synced from 1Password on every deploy) ---

if BESZEL.get("enabled"):
    _bz = vault.beszel_agent_creds()
    # Shell-quote both values: KEY is `ssh-ed25519 AAAA...` with a space, which
    # the wrapper's POSIX `.` loader would otherwise parse as
    # `KEY=ssh-ed25519` plus a command `AAAA...`. systemd's EnvironmentFile
    # parses raw, so the raspi side gets away without quoting.
    _bz_token = shlex.quote(_bz["token"])
    _bz_key = shlex.quote(_bz["key"])
    _put_secret(
        "beszel-agent.env",
        f"TOKEN={_bz_token}\nKEY={_bz_key}\n",
        "/etc/secrets/beszel-agent.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/beszel-agent.env (beszel disabled)",
        path="/etc/secrets/beszel-agent.env",
        present=False,
    )
