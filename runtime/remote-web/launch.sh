#!/bin/sh
set -eu
umask 077

key_file="$KODI_LAB_DATA_ROOT/master-key"
if [ ! -d "$KODI_LAB_DATA_ROOT" ] || [ -L "$KODI_LAB_DATA_ROOT" ] \
    || [ "$(stat -c %a "$KODI_LAB_DATA_ROOT")" != 700 ] \
    || [ "$(stat -c %u "$KODI_LAB_DATA_ROOT")" != "$(id -u)" ] \
    || [ ! -f "$key_file" ] || [ -L "$key_file" ] \
    || [ "$(stat -c %a "$key_file")" != 600 ] \
    || [ "$(stat -c %u "$key_file")" != "$(id -u)" ]; then
    echo 'Private state root or master-key file is missing or unsafe; initialize state explicitly.' >&2
    exit 1
fi

IFS= read -r KODI_LAB_EXTERNAL_TARGET_MASTER_KEY < "$key_file"
export KODI_LAB_EXTERNAL_TARGET_MASTER_KEY
exec kodi-lab-web
