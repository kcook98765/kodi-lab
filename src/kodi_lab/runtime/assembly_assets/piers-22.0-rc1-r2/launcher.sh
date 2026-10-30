#!/bin/sh
set -eu
export KODI_DATA=/home/kodi/.kodi
export KODI_HOME=/app/share/kodi
export PYTHONHOME=/opt/fdo
export PYTHONPATH=/app/lib/python3.14/site-packages:/opt/fdo/lib/python3.14/site-packages
export GCONV_PATH=/opt/fdo/lib/x86_64-linux-gnu/gconv
export LIBGL_DRIVERS_PATH=/opt/mesa/lib/dri
export __EGL_VENDOR_LIBRARY_FILENAMES=/opt/mesa/glvnd/egl_vendor.d/50_mesa.json
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export FONTCONFIG_PATH=/etc/fonts
libraries=/app/lib:/opt/mesa/lib:/opt/fdo/lib/x86_64-linux-gnu:/opt/fdo/lib/x86_64-linux-gnu/pulseaudio:/opt/fdo/lib
exec /opt/fdo/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 --library-path "$libraries" /app/lib/kodi/kodi.bin "$@"
