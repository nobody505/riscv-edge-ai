#!/usr/bin/env python3
"""Initialize ML307A serial/RNDIS interfaces without shell command execution."""

import glob
import os
import subprocess
import time


def run(args, timeout=10):
    """Run a fixed argv command and return stdout; never invoke a shell."""
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def call(args):
    try:
        return subprocess.run(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def write_sysfs(path, value):
    """Write a constant or kernel-derived identifier to an explicit sysfs node."""
    try:
        with open(path, "w", encoding="ascii") as handle:
            handle.write(value)
        return True
    except OSError:
        return False


def find_at_port():
    candidates = sorted(glob.glob("/dev/serial/by-id/*ML307A*if02*"))
    for device in candidates:
        try:
            import serial

            with serial.Serial(
                device, 115200, timeout=0.5, write_timeout=1
            ) as port:
                port.write(b"AT\r\n")
                time.sleep(0.4)
                response = port.read(200).decode(errors="replace")
            if "OK" in response:
                return device
        except (OSError, ValueError):
            continue
    return None


print("[BOOTFIX] K1 network initialization", flush=True)

print("[BOOTFIX] 1/4 waiting for ML307A...", flush=True)
for _ in range(30):
    if "2ecc" in run(["/usr/bin/lsusb"]):
        print("[BOOTFIX] USB ready", flush=True)
        break
    time.sleep(2)

call(["/usr/sbin/modprobe", "option"])
call(["/usr/sbin/modprobe", "rndis_host"])
write_sysfs(
    "/sys/bus/usb-serial/drivers/option1/new_id", "2ecc 3012\n"
)
time.sleep(3)

# Release only CDC Data/Wireless interfaces from option and bind CDC Data to
# rndis_host. Interface identifiers come from sysfs, not user input.
for interface in sorted(glob.glob("/sys/bus/usb/drivers/option/2-1*:1.*")):
    interface_name = os.path.basename(interface)
    try:
        with open(
            os.path.join(interface, "bInterfaceClass"), encoding="ascii"
        ) as handle:
            interface_class = handle.read().strip().lower()
    except OSError:
        continue
    if interface_class not in {"0a", "e0"}:
        continue
    write_sysfs("/sys/bus/usb/drivers/option/unbind", interface_name)
    if interface_class == "0a":
        time.sleep(1)
        write_sysfs("/sys/bus/usb/drivers/rndis_host/bind", interface_name)

print("[BOOTFIX] 2/4 dialing...", flush=True)
at_port = find_at_port()
if at_port:
    print("[BOOTFIX] AT port found", flush=True)
    try:
        import serial

        with serial.Serial(
            at_port, 115200, timeout=3, write_timeout=3
        ) as port:
            port.write(b"AT+MDIALUP?\r\n")
            time.sleep(0.5)
            response = port.read(256).decode(errors="replace")
            if "1,1" not in response:
                port.write(b"AT+MDIALUP=1,1\r\n")
                time.sleep(5)
                response = port.read(256).decode(errors="replace")
                print(
                    "[BOOTFIX] dial: %s"
                    % response[:100].replace("\r\n", " "),
                    flush=True,
                )
            else:
                print("[BOOTFIX] already dialed", flush=True)
            port.write(b"AT+CSQ\r\n")
            time.sleep(0.3)
            signal_text = (
                port.read(128)
                .decode(errors="replace")
                .strip()
                .replace("\r\n", " ")
            )
            print("[BOOTFIX] signal: %s" % signal_text, flush=True)
    except (OSError, ValueError) as error:
        print("[BOOTFIX] serial error: %s" % str(error)[:80], flush=True)
else:
    print("[BOOTFIX] AT port unavailable", flush=True)

print("[BOOTFIX] 3/4 RNDIS...", flush=True)
rndis_interface = None
for _ in range(15):
    interfaces = sorted(glob.glob("/sys/class/net/enx*"))
    if interfaces:
        rndis_interface = os.path.basename(interfaces[0])
        print("[BOOTFIX] %s available" % rndis_interface, flush=True)
        break
    time.sleep(1)
if not rndis_interface:
    print("[BOOTFIX] no RNDIS; WiFi remains available", flush=True)

print("[BOOTFIX] 4/4 connectivity...", flush=True)
for _ in range(10):
    if call(["/usr/bin/ping", "-c", "1", "-W", "3", "8.8.8.8"]) == 0:
        print("[BOOTFIX] network OK", flush=True)
        break
    if call(["/usr/bin/ping", "-c", "1", "-W", "3", "223.5.5.5"]) == 0:
        print("[BOOTFIX] network OK", flush=True)
        break
    time.sleep(3)

print("[BOOTFIX] complete", flush=True)
