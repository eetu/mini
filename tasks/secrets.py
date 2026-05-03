"""Write secrets from Bitwarden to /etc/secrets/ on the Mini.

Only runs when a service has its `require_*` flag enabled. Empty deploys
(no secrets configured) leave /etc/secrets/ as an empty 700 dir.
"""

import io

from pyinfra.operations import files

import vault as bw
from group_data.all import OLLAMA


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
        f"OLLAMA_API_KEY={bw.ollama_api_key()}\n",
        "/etc/secrets/ollama.env",
    )
else:
    files.file(
        name="Remove /etc/secrets/ollama.env (auth disabled)",
        path="/etc/secrets/ollama.env",
        present=False,
    )
