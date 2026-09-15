#!/bin/sh
set -eu

mkdir -p "$HOME/.kodi" "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"

Xvfb :99 -screen 0 1280x720x24 -nolisten tcp -ac \
    >"$HOME/.kodi/xvfb.log" 2>&1 &
xvfb_pid=$!
kodi_pid=""

cleanup() {
    if [ -n "$kodi_pid" ]; then
        kill -TERM "$kodi_pid" 2>/dev/null || true
        wait "$kodi_pid" 2>/dev/null || true
    fi
    kill -TERM "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

ready=0
for _ in $(seq 1 100); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.1
done
if [ "$ready" -ne 1 ]; then
    echo "Xvfb did not become ready" >&2
    exit 1
fi

dbus-run-session -- /usr/bin/kodi --standalone \
    >"$HOME/.kodi/kodi-stdout.log" 2>&1 &
kodi_pid=$!
set +e
wait "$kodi_pid"
status=$?
set -e
kodi_pid=""
exit "$status"
