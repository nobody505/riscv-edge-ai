#!/bin/bash
set -euo pipefail

fail=0
check() {
  if "$@"; then
    printf '[OK] %s\n' "$*"
  else
    printf '[FAIL] %s\n' "$*" >&2
    fail=1
  fi
}

check grep -q 'spacemit k1-x MUSE-Pi-Pro' /proc/device-tree/model
check test -s /etc/elder-assistant/elder.env
check test "$(stat -c %a /etc/elder-assistant/elder.env)" = 600
check test -s /etc/elder-assistant/onenet-mqtt-ca.pem
check test "$(stat -c %U:%G:%a /etc/elder-assistant/onenet-mqtt-ca.pem)" = root:elder-assistant:640
check test -x /home/space/spacevenv/bin/python3
check test -x /home/space/jdk_cam/workspace/consumer_final_new_gray
check test -x /usr/local/libexec/elder-assistant/radar_led
check test -x /usr/local/libexec/elder-assistant/ws2812
check test -x /usr/local/libexec/elder-assistant/fall_detect_adxl345.py
check test -x /usr/local/libexec/elder-assistant/k1_bootfix.py
check test -x /usr/local/libexec/elder-assistant/start-camera
check test -f /usr/local/lib/elder-assistant/hook_simple.so
check test -x /usr/local/sbin/elder-hwctl
check test "$(stat -c %U:%G /usr/local/libexec/elder-assistant/start-camera)" = root:root
check test "$(stat -c %U:%G /usr/local/lib/elder-assistant/hook_simple.so)" = root:root
check test "$(stat -c %a /run/elder-assistant)" = 3770
check test "$(stat -c %U:%G /run/elder-assistant)" = root:elder-assistant
check test "$(stat -c %U:%G:%a /run/elder-assistant/ml307a_at.lock)" = root:elder-assistant:660
check test -e /dev/i2c-3
check test -e /dev/i2c-4
check test -e /dev/ttyS0
check test -e /dev/tcm
check test "$(stat -c %G:%a /dev/tcm)" = elder-assistant:660
check test -e /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
# The glob and pipeline intentionally expand inside sh -c.
# shellcheck disable=SC2016
check sh -c 'find /dev/serial/by-id -maxdepth 1 -name "*ML307A*if02*" | grep -q .'
check sha256sum -c /home/space/.cache/sensevoice/SHA256SUMS

for unit in k1-network-init.service lbs-service.service k1-network-health.service \
  elder-care.target elder-radar.service elder-fall.service elder-voice.service \
  elder-incoming-sms.service; do
  check systemctl is-active --quiet "$unit"
done

check systemctl is-enabled --quiet elder-care.target
check systemctl is-enabled --quiet k1-network-health.service
check systemctl is-enabled --quiet lbs-service.service
check systemctl is-enabled --quiet k1-network-init.service
check sh -c 'systemctl is-enabled serial-getty@ttyS0.service 2>&1 | grep -q masked'
# shellcheck disable=SC2016
check sh -c '[ "$(pgrep -xc radar_led || true)" -eq 1 ]'
# shellcheck disable=SC2016
check sh -c '[ "$(pgrep -fc "[/]home/space/voice/voice_assistant_v4.py" || true)" -eq 1 ]'
# shellcheck disable=SC2016
check sh -c '[ "$(pgrep -fc "[/]usr/local/libexec/elder-assistant/fall_detect_adxl345.py" || true)" -eq 1 ]'
# shellcheck disable=SC2016
check sh -c '[ "$(pgrep -fc "[/]home/space/incoming_sms_to_tts.py" || true)" -eq 1 ]'

/home/space/spacevenv/bin/python3 - <<'PY'
import kaldi_native_fbank
import onnxruntime
import numpy
import paho.mqtt.client
import pyaudio
import pypinyin
import scipy
import serial
import soundfile
import yaml
print('[INFO] onnxruntime', onnxruntime.__version__)
print('[INFO] providers', onnxruntime.get_available_providers())
print('[INFO] numpy', numpy.__version__, 'scipy', scipy.__version__)
PY

systemctl --failed --no-pager || true
echo '[INFO] This verifier never sends SMS, starts navigation, or triggers a fall alarm.'
exit "$fail"
