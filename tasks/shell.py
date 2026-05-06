"""Fish shell integrations: zoxide init.

Run on its own to refresh shell wiring without re-running the full deploy:
    uv run pyinfra inventory.py tasks/shell.py

zoxide installs via Brewfile (tasks/bootstrap.py). This task only owns the
config.fish lines that source it. Old `z` plugin (e.g. jethrokuan/z installed
via Fisher) must be removed manually — Fisher state lives in user dotfiles
and isn't tracked here:

    fisher remove jethrokuan/z
"""

from pyinfra.context import host
from pyinfra.operations import files

from group_data.all import SHELL

_user = host.data.get("ssh_user")
_home = f"/Users/{_user}"

if _user and "fish" in SHELL:
    files.directory(
        name="Ensure fish config dir exists",
        path=f"{_home}/.config/fish",
        user=_user,
        present=True,
    )

    # Older line written by an earlier deploy — drop it so the guarded version
    # below is the only zoxide init that survives.
    files.line(
        name="Drop unguarded zoxide init",
        path=f"{_home}/.config/fish/config.fish",
        line="zoxide init fish | source",
        present=False,
    )

    # Guard with `command -q zoxide` so non-interactive ssh shells (where
    # /opt/homebrew/bin may not be on PATH yet) don't choke at startup.
    files.line(
        name="Initialize zoxide in fish (guarded)",
        path=f"{_home}/.config/fish/config.fish",
        line="command -q zoxide; and zoxide init fish | source",
        present=True,
    )
