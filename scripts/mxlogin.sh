#!/usr/bin/env sh
# Change the bridge's Matrix/Element account from this machine.
#
# The prompts and the login run ON THE BRIDGE HOST; this script only carries
# your keystrokes there over SSH. The homeserver records the IP of whoever
# calls /login, and that must never be this laptop. The password stays inside
# the SSH tunnel - never in argv, a file, or shell history.
#
#   ./scripts/mxlogin.sh root@vps.example.com     # remote (normal case)
#   ./scripts/mxlogin.sh                          # local docker (dev only)
#
# Defaults may come from the environment:
#   BRIDGE_SSH_HOST=root@vps.example.com
#   BRIDGE_REMOTE_PATH=/srv/matrix-telegram-bridge
set -eu

SERVER="${1:-${BRIDGE_SSH_HOST:-}}"
REMOTE_PATH="${BRIDGE_REMOTE_PATH:-/srv/matrix-telegram-bridge}"
CONFIG="${BRIDGE_CONFIG:-/config/config.yaml}"

INNER="docker compose run --rm --entrypoint python bridge -m bridge.mxlogin --config ${CONFIG}"

if [ -z "$SERVER" ]; then
    echo "no server given -> running against LOCAL docker" >&2
    echo "(the homeserver will see THIS machine's egress address)" >&2
    cd "$(dirname "$0")/.."
    exec $INNER
fi

# -t forces a remote TTY so getpass() can disable echo for the password.
echo "connecting to ${SERVER} ..." >&2
exec ssh -t "$SERVER" "cd '${REMOTE_PATH}' && ${INNER}"
