"""Enable Remote Login (sshd), deploy hardened sshd_config, manage authorized_keys."""

import hashlib

from pyinfra import host
from pyinfra.operations import files, server

from group_data.all import SSH

with open("files/sshd_config", "rb") as _f:
    _sshd_hash = hashlib.sha256(_f.read()).hexdigest()

# --- Verify Remote Login is on ---
# Recent macOS (Sequoia+) requires Full Disk Access to toggle Remote Login from
# the CLI, so we can't enable it programmatically. Soft-warn if it's off; the
# rest of the task still writes the hardened config so it's ready when the user
# flips the switch in System Settings → General → Sharing → Remote Login.

server.shell(
    name="Verify Remote Login is on (warn if not)",
    commands=[
        """
        STATE=$(systemsetup -getremotelogin 2>/dev/null | awk -F': ' '{print $2}')
        if [ "$STATE" != "On" ]; then
          echo ""
          echo "  ! Remote Login is OFF — enable it manually:"
          echo "    System Settings → General → Sharing → Remote Login"
          echo "  sshd_config has still been written and will apply once enabled."
          echo ""
        fi
        """,
    ],
)

# --- sshd_config ---

files.put(
    name="Configure sshd",
    src="files/sshd_config",
    dest="/etc/ssh/sshd_config",
    user="root",
    group="wheel",
    mode="644",
)

server.shell(
    name="Reload sshd if config changed",
    commands=[
        f"""
        STAMP=/etc/ssh/.mini-stamp
        if [ "$(cat "$STAMP" 2>/dev/null)" != "{_sshd_hash}" ]; then
          # No-op when Remote Login is off; harmless once enabled.
          launchctl kickstart -k system/com.openssh.sshd 2>/dev/null || true
          echo '{_sshd_hash}' > "$STAMP"
        fi
        """,
    ],
)

# --- authorized_keys ---
# Idempotent line-by-line management: each configured public key is added if
# missing, never removed. Lets you keep ad-hoc keys (e.g. a temporary build
# agent) alongside the canonical IaC-managed set. To remove a managed key:
# delete it from SSH["authorized_keys"] and clean up the line by hand once.

_ssh_user = host.data.get("ssh_user")
_home = f"/Users/{_ssh_user}"
_auth_keys = f"{_home}/.ssh/authorized_keys"

server.shell(
    name=f"Ensure {_home}/.ssh exists",
    commands=[
        f"""
        install -d -m 700 -o {_ssh_user} -g staff {_home}/.ssh
        if [ ! -f {_auth_keys} ]; then
          install -m 600 -o {_ssh_user} -g staff /dev/null {_auth_keys}
        fi
        """,
    ],
)

for _key in SSH.get("authorized_keys", []):
    files.line(
        name=f"Add authorized_key: {_key.split()[1][-12:]}",
        path=_auth_keys,
        line=_key,
        present=True,
    )
