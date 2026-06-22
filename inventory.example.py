# Copy this file to inventory.py and fill in your values.
#
# SSH auth: pin one key from the SSH agent (e.g. 1Password) per host by its
# comment, so paramiko presents only that key. paramiko has no `IdentitiesOnly`
# equivalent — left alone it offers every agent key one by one, which can trip
# sshd's MaxAuthTries before reaching the right one. Pinning keeps the private
# key in the agent (nothing on the filesystem).

import paramiko


def _agent_key(comment):
    """Return the agent key whose comment matches, or None.

    None on a locked/absent agent or a missing key, so the deploy falls back to
    normal agent behaviour rather than failing at inventory-load time.
    """
    try:
        return next(
            (k for k in paramiko.Agent().get_keys() if getattr(k, "comment", "") == comment),
            None,
        )
    except Exception:
        return None


def _auth(comment):
    key = _agent_key(comment)
    if key:
        return {
            "ssh_paramiko_connect_kwargs": {
                "pkey": key,
                "allow_agent": False,
                "look_for_keys": False,
            }
        }
    return {"ssh_allow_agent": True}


hosts = [
    (
        "192.168.x.y",  # Mini's LAN IP (router-pinned)
        {
            "ssh_user": "your_username",
            # The agent-key comment for this host (see `ssh-add -l`). Prefer an
            # on-disk key file? Swap `**_auth(...)` for "ssh_key": "~/.ssh/your_key".
            "_sudo": True,
            **_auth("your-agent-key-comment"),
        },
    ),
]
