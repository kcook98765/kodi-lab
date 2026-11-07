#!/bin/sh
set -eu
# No command, path, image or shutdown policy is supplied by the caller.
exec /usr/bin/python3 /usr/local/lib/kodi-lab-supervisor.py
