"""pf firewall: anchor file with our rules, hooked into /etc/pf.conf, loaded at boot."""

import hashlib
import io

from pyinfra.operations import files, server

from group_data.all import NETWORK

# pf anchor — rules live here so we never rewrite Apple's /etc/pf.conf body.
# Trust model: LAN + WireGuard are fully trusted ("LAN is safe space"). Any
# inbound from elsewhere is blocked. Outbound is unrestricted (default macOS
# posture). This keeps mDNS/Bonjour/AirDrop and any future LAN service working
# without per-port rules — the perimeter is the router/VPN, not pf.
_pf_anchor = f"""# Loaded by /etc/pf.conf — managed by mini IaC. Edit tasks/firewall.py.
lan = "{NETWORK["lan_cidr"]}"
wg  = "{NETWORK["wg_subnet"]}"

# Loopback fully open.
pass quick on lo0 all

# Track all outbound so reply packets match state and skip the inbound deny.
# Without this rule, outbound TCP/UDP creates no state and the SYN-ACK / DNS
# reply lands in `block in log all`, breaking outbound internet.
pass out quick all keep state

# Trusted networks: pass inbound from LAN + WireGuard.
pass in quick from $lan to any keep state
pass in quick from $wg  to any keep state

# Default deny for everything else hitting non-loopback interfaces.
block in log all
"""

_anchor_hash = hashlib.sha256(_pf_anchor.encode()).hexdigest()

files.put(
    name="Write pf anchor",
    src=io.BytesIO(_pf_anchor.encode()),
    dest="/etc/pf.anchors/com.eetu.mini",
    user="root",
    group="wheel",
    mode="644",
)

# Hook our anchor into /etc/pf.conf. Apple's default file already has a
# couple of anchor lines; we append ours once and leave it. Edit-in-place
# is safer than rewriting because Apple sometimes ships new defaults.
server.shell(
    name="Reference our anchor from /etc/pf.conf",
    commands=[
        """
        # Only the active (uncommented) anchor line counts. A commented version
        # left over from manual edits should NOT make us skip — clean it up
        # and re-append a fresh pair of lines.
        if ! grep -E '^anchor "com\\.eetu\\.mini"' /etc/pf.conf >/dev/null 2>&1; then
          sed -i.bak '/com\\.eetu\\.mini/d' /etc/pf.conf
          printf '\\nanchor "com.eetu.mini"\\nload anchor "com.eetu.mini" from "/etc/pf.anchors/com.eetu.mini"\\n' >> /etc/pf.conf
          rm -f /etc/pf.conf.bak
        fi
        """,
    ],
)

# LaunchDaemon to enable pf and load our config at every boot.
# Apple's com.apple.pfctl runs early but with a different ruleset on some
# OS versions — owning our own load step makes the behavior predictable.
_pf_plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists/1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.eetu.pf-load</string>
  <key>ProgramArguments</key>
  <array>
    <string>/sbin/pfctl</string>
    <string>-E</string>
    <string>-f</string>
    <string>/etc/pf.conf</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/pf-load.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/pf-load.log</string>
</dict>
</plist>
"""

_plist_hash = hashlib.sha256(_pf_plist.encode()).hexdigest()
_combined_hash = hashlib.sha256(f"{_anchor_hash}{_plist_hash}".encode()).hexdigest()

files.put(
    name="Write pf launchd plist",
    src=io.BytesIO(_pf_plist.encode()),
    dest="/Library/LaunchDaemons/com.eetu.pf-load.plist",
    user="root",
    group="wheel",
    mode="644",
)

# Bootstrap the launchd job once, then reload pf in place when rules change.
# We don't kickstart the loader (it's a one-shot); we run pfctl directly so
# the new ruleset is active without a reboot.
server.shell(
    name="Bootstrap pf-load + reload on change",
    commands=[
        f"""
        if ! launchctl print system/com.eetu.pf-load >/dev/null 2>&1; then
          launchctl bootstrap system /Library/LaunchDaemons/com.eetu.pf-load.plist
          launchctl enable system/com.eetu.pf-load
        fi
        STAMP=/var/db/.mini-pf-stamp
        if [ "$(cat "$STAMP" 2>/dev/null)" != "{_combined_hash}" ]; then
          # Validate syntax before activating — bad rules could lock us out.
          pfctl -nf /etc/pf.conf
          pfctl -E 2>/dev/null || true
          pfctl -f /etc/pf.conf
          echo '{_combined_hash}' > "$STAMP"
        fi
        """,
    ],
)
