"""Shared utilities for task modules."""

from collections.abc import Iterable


def kickstart_if_changed(
    label: str,
    static_hash: str,
    env_files: Iterable[str] = (),
    plist: str | None = None,
) -> str:
    """Shell command that kickstarts a launchd job when its config fingerprint changes.

    `static_hash` covers content known at plan time (plist + inline config strings).
    `env_files` are paths hashed at run time — useful for secrets written by
    tasks/secrets.py that rotate out-of-band. The combined stamp lives at
    `/var/db/.{label}-stamp`.

    `plist` defaults to `/Library/LaunchDaemons/{label}.plist`. The launchd
    target is `system/{label}` — kickstart -k bounces the running job in place.
    """
    stamp = f"/var/db/.{label}-stamp"
    plist = plist or f"/Library/LaunchDaemons/{label}.plist"
    env_files = tuple(env_files)
    bootstrap = (
        f"if ! launchctl print system/{label} >/dev/null 2>&1; then\n"
        f"  launchctl bootstrap system {plist}\n"
        f"  launchctl enable system/{label}\n"
        f"fi\n"
    )
    if not env_files:
        return (
            f"{bootstrap}"
            f'if [ "$(cat {stamp} 2>/dev/null)" != "{static_hash}" ]; then\n'
            f"  launchctl kickstart -k system/{label}\n"
            f"  echo '{static_hash}' > {stamp}\n"
            f"fi"
        )
    files_arg = " ".join(env_files)
    return (
        f"{bootstrap}"
        f'CURRENT="{static_hash}"\n'
        f"for f in {files_arg}; do\n"
        f'  [ -f "$f" ] && CURRENT="$CURRENT:$(shasum -a 256 "$f" | cut -d\' \' -f1)"\n'
        f"done\n"
        f'CURRENT=$(printf "%s" "$CURRENT" | shasum -a 256 | cut -d\' \' -f1)\n'
        f'if [ "$(cat {stamp} 2>/dev/null)" != "$CURRENT" ]; then\n'
        f"  launchctl kickstart -k system/{label}\n"
        f'  echo "$CURRENT" > {stamp}\n'
        f"fi"
    )
