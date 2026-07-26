#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
cam_test=$(command -v cam-test) || {
  echo "cam-test is not installed" >&2
  exit 1
}

LD_PRELOAD=/usr/local/lib/elder-assistant/hook_simple.so \
  exec "$cam_test" /etc/elder-assistant/dual_ov5647.json
