"""Apple Screen Sharing (VNC) toggle.

True  → bootstrap + enable the com.apple.screensharing LaunchDaemon so it
        runs now and at every boot. Auth uses the macOS user password.
False (or key missing) → bootout + disable so it stays off across reboots.

The pf anchor (tasks/firewall.py) already passes all LAN + WG inbound, so
port 5900 is automatically restricted to those networks. No extra rule.

First-time setup gotcha: macOS 14+ TCC requires the Screen Sharing toggle to
be flipped once via the UI (System Settings > General > Sharing) on the Mac
itself. launchctl alone cannot grant TCC consent. After that one manual
toggle, this task keeps the daemon enabled across reboots.

If a future deploy ever shows the connection error
"<host> ei saa jakaa näyttöä / cannot share screen — disable and re-enable",
do the toggle dance once on-device; nothing in the IaC needs to change.

Run alone to flip without a full deploy:
    uv run pyinfra inventory.py tasks/screensharing.py
"""

from pyinfra.operations import server

from group_data import all as _all

LABEL = "com.apple.screensharing"
PLIST = f"/System/Library/LaunchDaemons/{LABEL}.plist"

# Treat a missing key the same as False — defensive against future drops.
_enabled = bool(getattr(_all, "SCREEN_SHARING", False))

_KICKSTART = (
    "/System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart"
)

if _enabled:
    # Apple Remote Management (ARDAgent) and plain Screen Sharing are mutually
    # exclusive — macOS rejects connections with "disable one, re-enable in
    # Settings" when both are partially active. Tear ARD down first, then
    # bootout + bootstrap screensharingd so it picks up clean state.
    server.shell(
        name="Enable Apple Screen Sharing (clean Remote Management first)",
        commands=[
            f"""
            if [ -x {_KICKSTART} ]; then
              {_KICKSTART} -deactivate -configure -access -off >/dev/null 2>&1 || true
            fi
            if launchctl print system/{LABEL} >/dev/null 2>&1; then
              launchctl bootout system/{LABEL} || true
            fi
            launchctl enable system/{LABEL}
            launchctl bootstrap system {PLIST}
            """,
        ],
    )
else:
    server.shell(
        name="Disable Apple Screen Sharing",
        commands=[
            f"""
            if launchctl print system/{LABEL} >/dev/null 2>&1; then
              launchctl bootout system/{LABEL} || true
            fi
            launchctl disable system/{LABEL}
            """,
        ],
    )
