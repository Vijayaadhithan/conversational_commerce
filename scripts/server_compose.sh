#!/bin/sh
# The server records the pulled dependency image digests in deploy/images.env.
set -eu
cd "$(dirname "$0")/.."
if [ ! -f deploy/images.env ]; then
    echo "Missing deploy/images.env; pin the server dependency images first." >&2
    exit 1
fi
exec docker compose --env-file deploy/images.env -f compose.server.yml "$@"
