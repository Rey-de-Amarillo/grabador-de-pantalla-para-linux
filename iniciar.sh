#!/bin/sh
set -eu
if [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
    echo 'El grabador necesita una sesión X11.' >&2
    exit 1
fi
TASK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/grabador" && pwd)
exec /usr/bin/python3 "$TASK_DIR/app.py" "$@"
