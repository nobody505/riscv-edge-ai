#!/bin/bash
set -euo pipefail

cleanup() {
  sudo -n /usr/local/sbin/elder-hwctl camera-stop || true
}
trap cleanup EXIT INT TERM

sudo -n /usr/local/sbin/elder-hwctl tcm-perms
sudo -n /usr/local/sbin/elder-hwctl camera-start &
for _ in $(seq 1 75); do
  [ -e /dev/shm/pipe0_frame ] && [ -e /dev/shm/pipe1_frame ] && break
  sleep 0.2
done

cd /home/space/jdk_cam/workspace
./consumer_final_new_gray ../best_6out.xq.onnx
