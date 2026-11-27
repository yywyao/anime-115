#!/bin/sh
set -eu

# Derive the same per-deployment key as mikan115.config.Config without writing it
# to .env or a persistent Sonarr config directory. The secret is not logged.
if [ -z "${MIKAN_SONARR_SECRET:-}" ]; then
    echo 'Sonarr profile requires the private deployment configuration.' >&2
    exit 1
fi
SONARR__AUTH__APIKEY="$(printf 'mikan115-sonarr-v1:%s' "$MIKAN_SONARR_SECRET" | sha256sum | awk '{print $1}')"
export SONARR__AUTH__APIKEY
unset MIKAN_SONARR_SECRET
exec /init
