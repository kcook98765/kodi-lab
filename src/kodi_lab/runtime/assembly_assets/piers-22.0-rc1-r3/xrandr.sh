#!/bin/sh
set -eu
exec /opt/fdo/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 --library-path /app/lib:/opt/mesa/lib:/opt/fdo/lib/x86_64-linux-gnu:/opt/fdo/lib/x86_64-linux-gnu/pulseaudio:/opt/fdo/lib /app/lib/kodi/kodi-xrandr "$@"
