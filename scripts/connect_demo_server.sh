#!/bin/sh
# Keep this terminal open while the local Querix frontend uses the remote API.
set -eu
exec ssh -N -T \
  -o ControlMaster=no -o ControlPath=none \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:8000:127.0.0.1:8000 \
  "${1:-root@201.18.196.150}"
