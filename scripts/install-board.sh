#!/bin/bash
set -euo pipefail

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
target_user=space
target_home=/home/space
service_group=elder-assistant
config_dir=/etc/elder-assistant
libexec_dir=/usr/local/libexec/elder-assistant
lib_dir=/usr/local/lib/elder-assistant
config_file=
skip_packages=0
start_services=1

usage() {
  echo "usage: sudo $0 --config /path/to/elder.env [--skip-packages] [--no-start]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) config_file=${2:-}; shift 2 ;;
    --skip-packages) skip_packages=1; shift ;;
    --no-start) start_services=0; shift ;;
    *) usage; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -n "$config_file" ] && [ -f "$config_file" ] || { usage; exit 2; }
grep -q 'spacemit k1-x MUSE-Pi-Pro' /proc/device-tree/model || {
  echo "This installer targets the SpaceMIT K1 MUSE Pi Pro." >&2
  exit 1
}
python3 "$repo/scripts/validate-elder-env.py" "$config_file"
id "$target_user" >/dev/null 2>&1 || {
  echo "Expected Bianbu user '$target_user' does not exist." >&2
  exit 1
}

(cd "$repo/vendor" && sha256sum -c SHA256SUMS)
(cd "$repo/config" && sha256sum -c SHA256SUMS)
openssl x509 -in "$repo/config/onenet-mqtt-ca.pem" -checkend 2592000 -noout
for archive in "$repo"/vendor/*.tar.gz; do
  python3 "$repo/scripts/validate-tar-archive.py" "$archive"
done

if [ "$skip_packages" -eq 0 ]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential libopencv-dev libsndfile1 portaudio19-dev \
    python3 python3-venv python3-pip python3-paho-mqtt python3-serial \
    alsa-utils i2c-tools network-manager ca-certificates
fi

getent group "$service_group" >/dev/null 2>&1 || groupadd --system "$service_group"
usermod -a -G "audio,dialout,video,$service_group" "$target_user"

install -d -o "$target_user" -g "$target_user" \
  "$target_home/voice" "$target_home/jdk_cam/src" \
  "$target_home/jdk_cam/include" "$target_home/jdk_cam/workspace" \
  "$target_home/_tts_demo/examples/NLP" "$target_home/.cache/sensevoice"
install -d -o "$target_user" -g "$target_user" -m 0700 \
  "$target_home/.local/state/elder-incoming-sms"
install -d -o root -g "$service_group" -m 0750 "$config_dir"
install -d -o root -g root -m 0755 "$libexec_dir" "$lib_dir"
install -o root -g root -m 0600 "$config_file" "$config_dir/elder.env"
install -o root -g "$service_group" -m 0640 "$repo/config/onenet-mqtt-ca.pem" \
  "$config_dir/onenet-mqtt-ca.pem"
install -o root -g root -m 0644 "$repo/config/elder-assistant.tmpfiles" \
  /usr/lib/tmpfiles.d/elder-assistant.conf
systemd-tmpfiles --create /usr/lib/tmpfiles.d/elder-assistant.conf

if [ ! -x "$target_home/spacevenv/bin/python3" ]; then
  runuser -u "$target_user" -- \
    python3 -m venv --system-site-packages "$target_home/spacevenv"
fi
site_dir=$(runuser -u "$target_user" -- \
  "$target_home/spacevenv/bin/python3" -c 'import site; print(site.getsitepackages()[0])')
runuser -u "$target_user" -- \
  tar -xzf "$repo/vendor/k1-python-runtime-deps-20260726.tar.gz" -C "$site_dir"
runuser -u "$target_user" -- \
  tar -xzf "$repo/vendor/k1-python-pure-deps-20260726-r2.tar.gz" -C "$site_dir"
runuser -u "$target_user" -- \
  tar -xzf "$repo/vendor/k1-python-ort-20260726.tar.gz" -C "$site_dir"
runuser -u "$target_user" -- \
  tar -xzf "$repo/vendor/spacemit-ort.riscv64.2.0.1.tar.gz" -C "$target_home"
runuser -u "$target_user" -- \
  "$target_home/spacevenv/bin/python3" "$repo/scripts/test-runtime-imports.py"

runuser -u "$target_user" -- install -m 0644 \
  "$repo/voice/voice_assistant_v4.py" "$target_home/voice/voice_assistant_v4.py"
runuser -u "$target_user" -- install -m 0644 \
  "$repo/voice/nav_engine.py" "$target_home/nav_engine.py"
runuser -u "$target_user" -- install -m 0644 \
  "$repo/network/lbs_service.py" "$target_home/lbs_service.py"
runuser -u "$target_user" -- install -m 0644 \
  "$repo/voice/incoming_sms_to_tts.py" "$target_home/incoming_sms_to_tts.py"
install -o root -g root -m 0755 "$repo/hardware/fall_detect_adxl345.py" \
  "$libexec_dir/fall_detect_adxl345.py"
runuser -u "$target_user" -- \
  cp -a "$repo/voice/vendor/spacemit_asr" "$target_home/_tts_demo/examples/NLP/"
runuser -u "$target_user" -- \
  cp -a "$repo/voice/vendor/spacemit_audio" "$target_home/_tts_demo/examples/NLP/"

runuser -u "$target_user" -- "$repo/scripts/assemble-sensevoice.sh" \
  "$target_home/.cache/sensevoice" -

runuser -u "$target_user" -- install -m 0644 \
  "$repo/yolo-mipi/src/consumer_final_new_gray.cpp" \
  "$target_home/jdk_cam/src/consumer_final_new_gray.cpp"
runuser -u "$target_user" -- \
  cp -a "$repo/yolo-mipi/include/." "$target_home/jdk_cam/include/"
runuser -u "$target_user" -- install -m 0644 \
  "$repo/yolo-mipi/Makefile" "$target_home/jdk_cam/Makefile"
runuser -u "$target_user" -- install -m 0644 \
  "$repo/yolo-mipi/best_6out.xq.onnx" "$target_home/jdk_cam/best_6out.xq.onnx"
runuser -u "$target_user" -- install -m 0755 \
  "$repo/yolo-mipi/run_dual.sh" "$target_home/jdk_cam/run_dual.sh"
runuser -u "$target_user" -- make -C "$target_home/jdk_cam" \
  workspace/consumer_final_new_gray \
  SPACEMIT_ORT="$target_home/spacemit-ort.riscv64.2.0.1"
file "$target_home/jdk_cam/workspace/consumer_final_new_gray"
readelf -d "$target_home/jdk_cam/workspace/consumer_final_new_gray" | \
  grep -E 'onnxruntime|spacemit_ep|opencv'

install -o root -g root -m 0755 "$repo/yolo-mipi/start_dual.sh" \
  "$libexec_dir/start-camera"
install -o root -g root -m 0644 "$repo/config/dual_ov5647.json" \
  "$config_dir/dual_ov5647.json"
gcc -shared -fPIC -O2 "$repo/yolo-mipi/hook_simple.c" \
  -o "$lib_dir/hook_simple.so" -ldl -lpthread
chown root:root "$lib_dir/hook_simple.so"
chmod 0644 "$lib_dir/hook_simple.so"

gcc -O2 "$repo/hardware/radar_led.c" -o "$libexec_dir/radar_led" -lm
gcc -O2 "$repo/hardware/ws2812.c" -o "$libexec_dir/ws2812" -lm
gcc -O2 "$repo/hardware/hc_sr04.c" -o "$libexec_dir/hc_sr04" -lm
chown root:root "$libexec_dir/radar_led" "$libexec_dir/ws2812" \
  "$libexec_dir/hc_sr04"
chmod 0755 "$libexec_dir/radar_led" "$libexec_dir/ws2812" \
  "$libexec_dir/hc_sr04"

install -o root -g root -m 0755 "$repo/network/k1_bootfix.py" \
  "$libexec_dir/k1_bootfix.py"
install -o root -g root -m 0755 "$repo/network/k1-network-health.py" \
  /usr/local/sbin/k1-network-health.py
install -d -o root -g root -m 0755 /usr/local/share/doc/k1-network-health
install -o root -g root -m 0644 "$repo/docs/NETWORK_HEALTH.md" \
  /usr/local/share/doc/k1-network-health/README.md
install -o root -g root -m 0755 "$repo/scripts/elder-hwctl" \
  /usr/local/sbin/elder-hwctl
install -o root -g root -m 0440 "$repo/config/elder-assistant.sudoers" \
  /etc/sudoers.d/elder-assistant
visudo -cf /etc/sudoers.d/elder-assistant
install -o root -g root -m 0644 "$repo/config/99-asrpro-uart0.rules" \
  /etc/udev/rules.d/99-asrpro-uart0.rules
install -o root -g root -m 0644 "$repo/config/99-elder-hardware.rules" \
  /etc/udev/rules.d/99-elder-hardware.rules

for unit in elder-care.target elder-voice.service elder-fall.service \
  elder-radar.service elder-incoming-sms.service lbs-service.service \
  k1-network-init.service k1-network-health.service; do
  install -o root -g root -m 0644 "$repo/systemd/$unit" "/etc/systemd/system/$unit"
done

systemctl mask serial-getty@ttyS0.service
udevadm control --reload-rules
udevadm trigger --action=change /sys/class/tty/ttyS0 || true
udevadm trigger --action=change --sysname-match=tcm || true
systemctl daemon-reload
systemctl enable k1-network-init.service lbs-service.service \
  k1-network-health.service elder-care.target

runuser -u "$target_user" -- "$target_home/spacevenv/bin/python3" -m py_compile \
  "$target_home/voice/voice_assistant_v4.py" "$target_home/nav_engine.py" \
  "$target_home/incoming_sms_to_tts.py" "$target_home/lbs_service.py"
python3 -m py_compile "$libexec_dir/fall_detect_adxl345.py" \
  /usr/local/sbin/k1-network-health.py "$libexec_dir/k1_bootfix.py"
systemd-analyze verify /etc/systemd/system/elder-care.target \
  /etc/systemd/system/elder-voice.service /etc/systemd/system/elder-fall.service \
  /etc/systemd/system/elder-radar.service \
  /etc/systemd/system/elder-incoming-sms.service \
  /etc/systemd/system/lbs-service.service \
  /etc/systemd/system/k1-network-init.service \
  /etc/systemd/system/k1-network-health.service

if [ "$start_services" -eq 1 ]; then
  systemctl start k1-network-init.service
  systemctl start lbs-service.service k1-network-health.service elder-care.target
  "$repo/scripts/verify-board.sh"
else
  echo "Installed without starting services (--no-start)."
fi
