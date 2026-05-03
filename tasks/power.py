"""pmset: keep the Mini awake as a server, wake on LAN, restart after power loss."""

from pyinfra.operations import server

# `pmset -a` applies to all power sources (mini is plugged in but -a is the
# safe form). Each setting is checked individually so re-runs are no-ops.
#
#   sleep 0          — never auto-sleep the system
#   disablesleep 1   — block sleep entirely (lid/idle/api requests)
#   womp 1           — wake on LAN (magic packet)
#   autorestart 1    — power on automatically after AC loss
#   powernap 0       — disable Power Nap (no benefit for a headless server)
#   displaysleep 10  — let the display sleep; we don't use one
PMSET_VALUES = {
    "sleep": "0",
    "disablesleep": "1",
    "womp": "1",
    "autorestart": "1",
    "powernap": "0",
    "displaysleep": "10",
}

for _key, _want in PMSET_VALUES.items():
    server.shell(
        name=f"pmset -a {_key} {_want}",
        commands=[
            f"""
            CURRENT=$(pmset -g custom 2>/dev/null | awk '/^ *{_key} / {{print $2; exit}}')
            if [ "$CURRENT" != "{_want}" ]; then
              pmset -a {_key} {_want}
            fi
            """,
        ],
    )
