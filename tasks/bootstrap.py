"""Homebrew install + base CLI tools + login shell."""

from pyinfra import host
from pyinfra.operations import files, server

from group_data.all import BREW, SHELL

UPGRADE_STAMP = "/var/db/mini-last-brew-upgrade"
UPGRADE_MAX_AGE_HOURS = 24

# Operations run as root via _sudo=True. Brew refuses to run as root, so each
# `brew ...` invocation drops back to the SSH user via `sudo -u <user> -H`.
# `-H` resets HOME so brew's bootsnap cache lands under the user's Library/,
# not /var/root (which would EACCES).
_user = host.data.get("ssh_user")

# --- Homebrew ---
# Install Homebrew if missing. Apple Silicon prefix is /opt/homebrew.

server.shell(
    name="Install Homebrew if missing",
    commands=[
        f"""
        if [ ! -x /opt/homebrew/bin/brew ]; then
          sudo -u {_user} -H bash -lc \\
            'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        fi
        """,
    ],
)

# Brewfile: deploy then `brew bundle` (idempotent — only installs missing).
files.put(
    name="Deploy Brewfile",
    src="files/Brewfile",
    dest="/opt/homebrew/etc/mini.Brewfile",
    user="root",
    group="wheel",
    mode="644",
)

server.shell(
    name="brew bundle (install/upgrade base packages)",
    commands=[
        f"""
        sudo -u {_user} -H /opt/homebrew/bin/brew bundle \\
          --file=/opt/homebrew/etc/mini.Brewfile
        """,
    ],
)

if BREW.get("auto_update"):
    server.shell(
        name="brew update + upgrade (once per 24h)",
        commands=[
            f"""
            STAMP="{UPGRADE_STAMP}"
            MAX_AGE={UPGRADE_MAX_AGE_HOURS * 3600}
            if [ ! -f "$STAMP" ] || [ $(( $(date +%s) - $(stat -f %m "$STAMP") )) -gt $MAX_AGE ]; then
              sudo -u {_user} -H /opt/homebrew/bin/brew update
              sudo -u {_user} -H /opt/homebrew/bin/brew upgrade
              touch "$STAMP"
            fi
            """,
        ],
    )

# --- Login shell ---

if SHELL:
    server.shell(
        name=f"Add {SHELL} to /etc/shells",
        commands=[
            f"""
            if ! grep -qx '{SHELL}' /etc/shells 2>/dev/null; then
              echo '{SHELL}' >> /etc/shells
            fi
            """,
        ],
    )

    server.shell(
        name=f"Set login shell for {_user} to {SHELL}",
        commands=[
            f"""
            CURRENT=$(dscl . -read /Users/{_user} UserShell | awk '{{print $2}}')
            if [ "$CURRENT" != "{SHELL}" ]; then
              chsh -s {SHELL} {_user}
            fi
            """,
        ],
    )
