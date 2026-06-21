"""finkeyb retrain box — the Mac-driven half of the deploy loop, on a schedule.

Nightly (FINKEYB["retrain_hour"]) the mini runs `finkeyb retrain --push`: pull new swipes from
the deployed testbed API into the DVC-tracked corpus, and if enough new data has accrued, train
a candidate on MPS, gate it against the champion, and on pass commit the new model.pt + push —
which triggers the build-image workflow → ghcr:main → the hetzner host's daily auto-update. The
mini never needs to be reachable from GitHub; it only reaches out (API pull, git push, dvc push).

Trains on MPS, so it's a LaunchDaemon (like comfyui) scheduled off ollama/comfyui peak. Logs runs
to the local MLflow server (tasks/mlflow.py) over loopback.

Prerequisites (the deploy fails fast / no-ops without them):
  - 1Password item `mini/finkeyb` with hidden fields: gh_token (a fine-grained PAT, contents:write),
    api_token (the FINKEYB_API_TOKEN shared secret); and field api_url (https://finkeyb...).
  - The GitHub repo (FINKEYB["repo"]) must exist and be pushable with that PAT.
  - DVC corpus transport: the mini uses a LOCAL remote here. The laptop's seed corpus must be
    made available to it once (copy the bytes, or switch both to a shared ssh remote) — after
    that the mini accumulates the corpus itself from the API pulls.
"""

import hashlib
import io
import textwrap

from pyinfra.operations import files, server

from group_data.all import FINKEYB, MLFLOW
from tasks.util import kickstart_if_changed

LABEL = "com.eetu.finkeyb-retrain"
PLIST_PATH = f"/Library/LaunchDaemons/{LABEL}.plist"
WRAPPER_PATH = "/usr/local/bin/finkeyb-retrain.sh"
# All finkeyb state under one parent (Shared = durable). The repo gets its OWN subdir — it must
# NOT be /Users/Shared/finkeyb itself, since tasks/mlflow.py creates .../finkeyb/mlflow there and
# `git clone` refuses a non-empty target.
INSTALL_PATH = "/Users/Shared/finkeyb/repo"  # repo clone (corpus + .dvc live inside it)
DVC_REMOTE = "/Users/Shared/finkeyb/dvc"  # local DVC remote (corpus bytes)
SECRETS_ENV = "/etc/secrets/finkeyb.env"
LOG_PATH = "/opt/homebrew/var/log/finkeyb-retrain.log"
UV = "/opt/homebrew/bin/uv"

ENABLED = FINKEYB.get("enabled", False)


def _disable() -> None:
    """Tear down the daemon when FINKEYB['enabled'] is False."""
    server.shell(
        name="Boot out finkeyb-retrain (disabled)",
        commands=[
            f"launchctl bootout system/{LABEL} >/dev/null 2>&1 || true\n"
            f"rm -f {PLIST_PATH} {WRAPPER_PATH} /var/db/.{LABEL}-stamp"
        ],
    )


if not ENABLED:
    _disable()
else:
    REPO = FINKEYB["repo"]
    BRANCH = FINKEYB.get("branch", "main")
    MIN_NEW = int(FINKEYB.get("min_new_swipes", 20))
    HOUR = int(FINKEYB.get("retrain_hour", 4))
    MLFLOW_URI = f"http://127.0.0.1:{MLFLOW['internal_port']}"

    files.directory(
        name=f"Create {DVC_REMOTE}",
        path=DVC_REMOTE,
        user="root",
        group="wheel",
        mode="755",
        present=True,
    )

    # --- Clone/update the repo + venv + DVC local remote ---
    # Auth WITHOUT leaking the PAT: the origin URL is tokenless, and a credential helper supplies
    # the token from $GH_TOKEN at git-runtime. So the token never lands in argv (ps), in
    # .git/config, or in this deploy context — only in /etc/secrets/finkeyb.env (600) and the
    # helper's env. The helper string stores `$GH_TOKEN` literally (single-quoted), resolved each
    # time git runs it. GIT_TERMINAL_PROMPT=0 + clearing inherited helpers avoids the macOS
    # osxkeychain helper blocking under headless sudo (the hang we hit). uv sync installs the
    # backend (torch for MPS + dvc) — slow on first run.
    _cred = r"""'!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'"""
    server.shell(
        name="Clone/update finkeyb + sync venv + dvc local remote",
        commands=[
            textwrap.dedent(f"""
            set -e
            . {SECRETS_ENV}
            export GH_TOKEN GIT_TERMINAL_PROMPT=0
            CRED={_cred}
            URL="https://github.com/{REPO}.git"
            if [ ! -d {INSTALL_PATH}/.git ]; then
              # Fresh clone. Clear any leftover from a failed/partial run so clone won't refuse a
              # non-empty target (safe: no .git => no working tree; corpus bytes live in the DVC remote).
              rm -rf {INSTALL_PATH}
              git -c credential.helper= -c credential.helper="$CRED" clone --branch {BRANCH} "$URL" {INSTALL_PATH}
            else
              git -C {INSTALL_PATH} remote set-url origin "$URL"
            fi
            # Persist tokenless remote + env-based helper so the wrapper's pull/push authenticate.
            # An empty value FIRST resets the inherited helper list (git is additive, so the global
            # osxkeychain helper would otherwise still be invoked on every fetch/pull/push and hang
            # under headless sudo); then add ours. Applies to all git ops in this repo, incl. the
            # retrain CLI's push.
            git -C {INSTALL_PATH} config --replace-all credential.helper ""
            git -C {INSTALL_PATH} config --add credential.helper "$CRED"
            git -C {INSTALL_PATH} config user.email "finkeyb-bot@invinite.tech"
            git -C {INSTALL_PATH} config user.name "finkeyb retrain (mini)"
            git -C {INSTALL_PATH} fetch --quiet origin {BRANCH}
            git -C {INSTALL_PATH} checkout --quiet {BRANCH}
            git -C {INSTALL_PATH} reset --hard origin/{BRANCH}
            cd {INSTALL_PATH}/backend
            {UV} sync --frozen
            # Machine-local DVC remote (written to .dvc/config.local, gitignored — not the laptop's).
            {UV} run dvc remote add --local --force minilocal {DVC_REMOTE}
            {UV} run dvc remote default --local minilocal
            {UV} run dvc pull || true
            """).strip(),
        ],
    )

    # --- Wrapper: the actual scheduled run ---
    _wrapper = textwrap.dedent(f"""
    #!/bin/sh
    set -e
    . {SECRETS_ENV}
    # Export GH_TOKEN so the repo's credential helper can authenticate git pull/push (the token
    # is NOT in the remote URL); GIT_TERMINAL_PROMPT=0 so auth fails fast instead of hanging.
    export GH_TOKEN GIT_TERMINAL_PROMPT=0
    export FINKEYB_API_URL FINKEYB_API_TOKEN
    export MLFLOW_TRACKING_URI="{MLFLOW_URI}"
    cd {INSTALL_PATH}
    git pull --ff-only || true
    cd {INSTALL_PATH}/backend
    {UV} sync --frozen
    # retrain: pull swipes -> threshold (min-new) -> train (MPS) -> gate -> on pass, commit+push
    # model.pt + corpus pointer (triggers build-image) and dvc push the corpus bytes.
    exec {UV} run finkeyb retrain --push --min-new {MIN_NEW}
    """).lstrip()

    files.put(
        name="Write finkeyb-retrain wrapper",
        src=io.BytesIO(_wrapper.encode()),
        dest=WRAPPER_PATH,
        user="root",
        group="wheel",
        mode="755",
    )

    # --- LaunchDaemon: nightly, not KeepAlive (a periodic job, not a service) ---
    # RunAtLoad false so a deploy doesn't kick off a (GPU-contending) train; it fires at HOUR:00.
    _plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists/1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{WRAPPER_PATH}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/var/root</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{HOUR}</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>{LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>{LOG_PATH}</string>
</dict>
</plist>
"""

    files.put(
        name="Write finkeyb-retrain plist",
        src=io.BytesIO(_plist.encode()),
        dest=PLIST_PATH,
        user="root",
        group="wheel",
        mode="644",
    )

    _static_hash = hashlib.sha256(
        (_wrapper + _plist + REPO + BRANCH + str(HOUR) + str(MIN_NEW)).encode()
    ).hexdigest()

    server.shell(
        name="Bootstrap finkeyb-retrain + reload on change",
        commands=[kickstart_if_changed(LABEL, _static_hash, env_files=(SECRETS_ENV,))],
    )
