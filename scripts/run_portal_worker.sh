#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -localhost -rfbport 5900 -nopw \
    >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 \
    >/tmp/novnc.log 2>&1 &

exec python -m contador_ai.ccma_worker "$@"
