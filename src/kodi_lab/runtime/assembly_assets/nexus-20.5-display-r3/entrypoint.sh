#!/bin/sh
set -eu

mkdir -p "$HOME/.kodi" "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"

/usr/bin/python3 /usr/local/bin/kodi-lab-xvfb-guard

Xvfb :99 -screen 0 640x360x24 -nolisten tcp -ac \
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
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        break
    fi
    xvfb_state=$(ps -o stat= -p "$xvfb_pid" 2>/dev/null || true)
    case "$xvfb_state" in
        ""|Z*) break ;;
    esac
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        if read -r lock_pid </tmp/.X99-lock \
            && [ "$lock_pid" = "$xvfb_pid" ] \
            && kill -0 "$xvfb_pid" 2>/dev/null; then
            ready=1
            break
        fi
    fi
    sleep 0.1
done
if [ "$ready" -ne 1 ]; then
    echo "Xvfb did not become ready" >&2
    exit 1
fi

LP_NUM_THREADS=1 dbus-run-session -- /usr/bin/kodi --standalone \
    >"$HOME/.kodi/kodi-stdout.log" 2>&1 &
kodi_pid=$!
set +e
wait "$kodi_pid"
status=$?
set -e
kodi_pid=""
exit "$status"
