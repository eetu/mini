"""Shared utilities for task modules."""

from collections.abc import Iterable


def kickstart_if_changed(
    label: str,
    static_hash: str,
    env_files: Iterable[str] = (),
    plist: str | None = None,
) -> str:
    """Shell command that reloads a launchd job when its config fingerprint changes.

    `static_hash` covers content known at plan time (plist + inline config strings).
    `env_files` are paths hashed at run time — useful for secrets written by
    tasks/secrets.py that rotate out-of-band. The combined stamp lives at
    `/var/db/.{label}-stamp`.

    `plist` defaults to `/Library/LaunchDaemons/{label}.plist`. The launchd
    target is `system/{label}`.

    On hash change we bootout + bootstrap rather than `kickstart -k`, because
    kickstart only respawns the cached program — it does not re-read the plist
    file. So a ProgramArguments / EnvironmentVariables change written by
    `files.put` would otherwise be ignored until the next reboot. bootout
    drops the loaded job, bootstrap re-reads the plist from disk.
    """
    stamp = f"/var/db/.{label}-stamp"
    plist = plist or f"/Library/LaunchDaemons/{label}.plist"
    env_files = tuple(env_files)
    reload_block = (
        f"launchctl bootout system/{label} >/dev/null 2>&1 || true\n"
        f"  launchctl enable system/{label}\n"
        f"  launchctl bootstrap system {plist}\n"
    )
    bootstrap_if_missing = (
        f"if ! launchctl print system/{label} >/dev/null 2>&1; then\n"
        f"  launchctl bootstrap system {plist}\n"
        f"  launchctl enable system/{label}\n"
        f"fi\n"
    )
    if not env_files:
        return (
            f'if [ "$(cat {stamp} 2>/dev/null)" != "{static_hash}" ]; then\n'
            f"  {reload_block}"
            f"  echo '{static_hash}' > {stamp}\n"
            f"else\n"
            f"  {bootstrap_if_missing}"
            f"fi"
        )
    files_arg = " ".join(env_files)
    return (
        f'CURRENT="{static_hash}"\n'
        f"for f in {files_arg}; do\n"
        f'  [ -f "$f" ] && CURRENT="$CURRENT:$(shasum -a 256 "$f" | cut -d\' \' -f1)"\n'
        f"done\n"
        f'CURRENT=$(printf "%s" "$CURRENT" | shasum -a 256 | cut -d\' \' -f1)\n'
        f'if [ "$(cat {stamp} 2>/dev/null)" != "$CURRENT" ]; then\n'
        f"  {reload_block}"
        f'  echo "$CURRENT" > {stamp}\n'
        f"else\n"
        f"  {bootstrap_if_missing}"
        f"fi"
    )
