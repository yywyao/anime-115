#!/bin/sh
set -eu
umask 077
cd /opt/alist
if [ ! -f data/.mikan115-initialized ]; then
  test -n "${ALIST_PASSWORD:-}" || { echo 'ALIST_PASSWORD is required' >&2; exit 1; }
  ./alist admin set "$ALIST_PASSWORD" >/dev/null 2>&1
  touch data/.mikan115-initialized
fi
exec ./alist server --no-prefix
