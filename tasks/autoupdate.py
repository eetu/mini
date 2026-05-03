"""Enable automatic macOS updates: schedule checks, download, install."""

from pyinfra.operations import server

# Apple writes these into /Library/Preferences/com.apple.SoftwareUpdate.plist.
# `defaults write` is idempotent; the conditional just avoids log spam.
SOFTWAREUPDATE_PREFS = {
    "AutomaticCheckEnabled": True,
    "AutomaticDownload": True,
    "AutomaticallyInstallMacOSUpdates": True,
    "ConfigDataInstall": True,  # XProtect / MRT / system data files
    "CriticalUpdateInstall": True,  # security updates
}

for _key, _want in SOFTWAREUPDATE_PREFS.items():
    _want_str = "1" if _want else "0"
    server.shell(
        name=f"softwareupdate pref: {_key}={_want}",
        commands=[
            f"""
            CURRENT=$(defaults read /Library/Preferences/com.apple.SoftwareUpdate {_key} 2>/dev/null || echo "missing")
            if [ "$CURRENT" != "{_want_str}" ]; then
              defaults write /Library/Preferences/com.apple.SoftwareUpdate {_key} -bool {"true" if _want else "false"}
            fi
            """,
        ],
    )

# Enable scheduled background checks (writes to per-user prefs as well).
server.shell(
    name="softwareupdate --schedule on",
    commands=[
        """
        STATE=$(softwareupdate --schedule 2>/dev/null | awk -F': ' '/Automatic check/ {print $2}')
        if [ "$STATE" != "on" ]; then
          softwareupdate --schedule on
        fi
        """,
    ],
)

# Enable automatic app updates from the Mac App Store.
server.shell(
    name="App Store: enable automatic updates",
    commands=[
        """
        CURRENT=$(defaults read /Library/Preferences/com.apple.commerce AutoUpdate 2>/dev/null || echo "missing")
        if [ "$CURRENT" != "1" ]; then
          defaults write /Library/Preferences/com.apple.commerce AutoUpdate -bool true
        fi
        """,
    ],
)
