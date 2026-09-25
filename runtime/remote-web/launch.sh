#!/bin/sh
set -eu
umask 077
exec kodi-lab-admin serve
