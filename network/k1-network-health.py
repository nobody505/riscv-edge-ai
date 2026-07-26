#!/usr/bin/env python3
"""Runtime RNDIS/DNS health guard for the K1 elder-care device.

This daemon is deliberately isolated from application services:

* It never opens an ML307A serial port and never sends AT commands.
* It never starts, stops, reloads, or restarts an elder-care service.
* It never edits a persistent NetworkManager connection profile.
* It only rebinds the USB *network* interfaces (classes e0/0a), never the
  vendor-specific serial interfaces (class ff).
* Wi-Fi failover routes are runtime-only, uniquely tagged with protocol 99,
  and removed after RNDIS recovery or clean service shutdown.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence


BUILD = "20260726-rndis-dns-health-r2"
ML307_VENDOR = "2ecc"
ML307_PRODUCT = "3012"
RNDIS_DRIVER = "rndis_host"
OPTION_DRIVER = "option"

CHECK_INTERVAL = 20.0
FAILURES_BEFORE_REPAIR = 3
SUCCESSES_BEFORE_RESTORE = 2
ACTION_COOLDOWN = 120.0
SOCKET_TIMEOUT = 3.0
FAILOVER_METRIC = 50
FAILOVER_PROTOCOL = "99"

RUNTIME_DIR = Path("/run/k1-network-health")
STATUS_FILE = RUNTIME_DIR / "status.json"
STATE_FILE = RUNTIME_DIR / "state.json"

DNS_PROBES = (
    "mqtts.heclouds.com",
    "iot-api.heclouds.com",
    "apis.map.qq.com",
)

TCP_PROBES = (
    ("mqtts.heclouds.com", 8883),
    ("iot-api.heclouds.com", 443),
    ("apis.map.qq.com", 443),
)

WIFI_INTERFACE = "wlan0"


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class DefaultRoute:
    family: int
    interface: str
    gateway: str
    metric: Optional[int] = None


@dataclass
class LinkProbe:
    interface: Optional[str]
    present: bool
    has_address: bool = False
    has_default_route: bool = False
    dns_ok: bool = False
    tcp_ok: bool = False
    healthy: bool = False
    addresses: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class GuardState:
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_dns_repair: float = 0.0
    last_link_repair: float = 0.0
    last_rebind: float = 0.0
    last_failover: float = 0.0
    failover_active: bool = False
    last_action: str = "none"
    last_error: str = ""


def log(message: str) -> None:
    print(f"[NET-HEALTH] {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


def default_runner(command: Sequence[str], timeout: float = 10.0) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(124, "", str(exc))


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""


def driver_name(interface_dir: Path) -> str:
    try:
        return interface_dir.joinpath("driver").resolve().name
    except OSError:
        return ""


def iter_ml307_interfaces(usb_root: Path = Path("/sys/bus/usb/devices")) -> list[Path]:
    found: list[Path] = []
    for device in sorted(usb_root.glob("*")):
        if ":" in device.name or not device.is_dir():
            continue
        if read_text(device / "idVendor").lower() != ML307_VENDOR:
            continue
        if read_text(device / "idProduct").lower() != ML307_PRODUCT:
            continue
        found.extend(sorted(usb_root.glob(f"{device.name}:*")))
    return found


def find_rndis_interface(sys_class_net: Path = Path("/sys/class/net")) -> Optional[str]:
    for net_dir in sorted(sys_class_net.glob("*")):
        try:
            driver = net_dir.joinpath("device", "driver").resolve().name
        except OSError:
            continue
        if driver == RNDIS_DRIVER:
            return net_dir.name
    return None


def parse_default_routes(output: str, family: int) -> list[DefaultRoute]:
    routes: list[DefaultRoute] = []
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] != "default" or "dev" not in parts:
            continue
        interface = parts[parts.index("dev") + 1]
        gateway = parts[parts.index("via") + 1] if "via" in parts else ""
        metric: Optional[int] = None
        if "metric" in parts:
            try:
                metric = int(parts[parts.index("metric") + 1])
            except (ValueError, IndexError):
                metric = None
        routes.append(DefaultRoute(family, interface, gateway, metric))
    return routes


def default_routes_for(
    interface: str,
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> list[DefaultRoute]:
    routes: list[DefaultRoute] = []
    for flag, family in (("-4", socket.AF_INET), ("-6", socket.AF_INET6)):
        # Query all defaults and filter after parsing. When `ip route` itself is
        # filtered by `dev`, iproute2 may omit the `dev` token from its output,
        # which makes ownership ambiguous and caused an early r1 false alarm.
        result = runner(("ip", flag, "route", "show", "default"), 5.0)
        if result.returncode == 0:
            routes.extend(route for route in parse_default_routes(result.stdout, family) if route.interface == interface)
    return routes


def interface_addresses(
    interface: str,
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> list[str]:
    addresses: list[str] = []
    for flag in ("-4", "-6"):
        result = runner(("ip", flag, "-o", "addr", "show", "dev", interface, "scope", "global"), 5.0)
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = line.split()
            family_token = "inet" if flag == "-4" else "inet6"
            if family_token in parts:
                value = parts[parts.index(family_token) + 1]
                addresses.append(value.split("/", 1)[0])
    return addresses


def interface_dns_healthy(
    interface: str,
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> bool:
    # One successful independent name is enough to prove the resolver path.
    # Requiring every vendor endpoint would turn a remote service incident into
    # a local repair loop.
    for hostname in DNS_PROBES:
        result = runner(
            ("resolvectl", "query", f"--interface={interface}", "--legend=no", "--type=A", hostname),
            5.0,
        )
        if result.returncode == 0 and result.stdout:
            return True
    return False


def resolve_probe_addresses() -> dict[tuple[str, int], list[tuple[int, tuple]]]:
    resolved: dict[tuple[str, int], list[tuple[int, tuple]]] = {}
    for hostname, port in TCP_PROBES:
        values: list[tuple[int, tuple]] = []
        try:
            infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            infos = []
        for family, socktype, proto, _canonname, sockaddr in infos:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            item = (family, sockaddr)
            if item not in values:
                values.append(item)
        resolved[(hostname, port)] = values
    return resolved


def interface_tcp_healthy(
    interface: str,
    resolved: Optional[dict[tuple[str, int], list[tuple[int, tuple]]]] = None,
    connector: Optional[Callable[[str, int, tuple, float], bool]] = None,
) -> bool:
    resolved = resolved if resolved is not None else resolve_probe_addresses()

    def connect(iface: str, family: int, sockaddr: tuple, timeout: float) -> bool:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            # SO_BINDTODEVICE is Linux-specific. The trailing NUL is required.
            sock.setsockopt(socket.SOL_SOCKET, 25, iface.encode("ascii") + b"\0")
            sock.connect(sockaddr)
            return True
        except OSError:
            return False
        finally:
            sock.close()

    connector = connector or connect
    for endpoint in TCP_PROBES:
        for family, sockaddr in resolved.get(endpoint, []):
            if connector(interface, family, sockaddr, SOCKET_TIMEOUT):
                return True
    return False


def probe_link(
    interface: Optional[str],
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
    resolved: Optional[dict[tuple[str, int], list[tuple[int, tuple]]]] = None,
    connector: Optional[Callable[[str, int, tuple, float], bool]] = None,
) -> LinkProbe:
    if not interface or not Path("/sys/class/net", interface).exists():
        return LinkProbe(interface=interface, present=False, detail="interface-missing")
    addresses = interface_addresses(interface, runner)
    routes = default_routes_for(interface, runner)
    dns_ok = interface_dns_healthy(interface, runner)
    tcp_ok = interface_tcp_healthy(interface, resolved, connector)
    has_address = bool(addresses)
    has_route = bool(routes)
    healthy = has_address and has_route and dns_ok and tcp_ok
    failures = []
    if not has_address:
        failures.append("no-address")
    if not has_route:
        failures.append("no-default-route")
    if not dns_ok:
        failures.append("dns-failed")
    if not tcp_ok:
        failures.append("tcp-failed")
    return LinkProbe(
        interface=interface,
        present=True,
        has_address=has_address,
        has_default_route=has_route,
        dns_ok=dns_ok,
        tcp_ok=tcp_ok,
        healthy=healthy,
        addresses=addresses,
        detail=",".join(failures) if failures else "ok",
    )


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    # Network state can contain local addresses and is root-private at runtime.
    os.chmod(path, 0o640)


def save_state(state: GuardState) -> None:
    atomic_json(STATE_FILE, asdict(state))


def write_status(state: GuardState, rndis: LinkProbe, wifi: Optional[LinkProbe]) -> None:
    atomic_json(
        STATUS_FILE,
        {
            "build": BUILD,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "rndis": asdict(rndis),
            "wifi": asdict(wifi) if wifi is not None else None,
            "guard": asdict(state),
        },
    )


def action_allowed(last_action_at: float, now: float) -> bool:
    return now - last_action_at >= ACTION_COOLDOWN


def repair_dns(
    interface: str,
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> bool:
    commands = (
        ("resolvectl", "flush-caches"),
        ("resolvectl", "reset-server-features", interface),
        ("nmcli", "device", "reapply", interface),
    )
    ok = True
    for command in commands:
        result = runner(command, 15.0)
        if result.returncode != 0:
            ok = False
            log(f"DNS repair command failed rc={result.returncode}: {' '.join(command)}")
    return ok


def safe_reapply_link(
    interface: str,
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> bool:
    # Reapply is intentionally used instead of connection down/up so an
    # established OneNET socket is not broken by the guard itself.
    result = runner(("nmcli", "device", "reapply", interface), 15.0)
    if result.returncode != 0:
        log(f"NetworkManager reapply failed for {interface}: rc={result.returncode}")
        return False
    return True


def _write_driver_control(path: Path, interface_name: str) -> None:
    with path.open("w", encoding="ascii") as handle:
        handle.write(interface_name)


def rebind_rndis_network_interfaces(
    usb_root: Path = Path("/sys/bus/usb/devices"),
    option_unbind: Path = Path("/sys/bus/usb/drivers/option/unbind"),
    rndis_bind: Path = Path("/sys/bus/usb/drivers/rndis_host/bind"),
    writer: Callable[[Path, str], None] = _write_driver_control,
    sleeper: Callable[[float], None] = time.sleep,
    finder: Callable[[], Optional[str]] = find_rndis_interface,
) -> bool:
    interfaces = iter_ml307_interfaces(usb_root)
    control = [item for item in interfaces if read_text(item / "bInterfaceClass").lower() == "e0"]
    data = [item for item in interfaces if read_text(item / "bInterfaceClass").lower() == "0a"]
    serial = [item for item in interfaces if read_text(item / "bInterfaceClass").lower() == "ff"]

    if not control or not data:
        log("RNDIS rebind skipped: ML307A e0/0a network interfaces are incomplete")
        return False
    if len(serial) < 1:
        log("RNDIS rebind skipped: ML307A serial interfaces are not enumerated")
        return False

    # Only e0/0a are touched. Vendor-specific ff interfaces own the real AT
    # ports and must remain bound and uninterrupted.
    for item in control + data:
        if driver_name(item) == OPTION_DRIVER:
            writer(option_unbind, item.name)
            log(f"unbound network-class interface {item.name} from option")

    sleeper(0.5)
    targets = control + data
    for item in targets:
        try:
            writer(rndis_bind, item.name)
        except OSError as exc:
            log(f"RNDIS bind attempt failed for {item.name}: {exc}")
            continue
        sleeper(1.0)
        if finder() is not None:
            return True
    return finder() is not None


def repair_missing_rndis(
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> bool:
    if not rebind_rndis_network_interfaces():
        return False
    runner(("udevadm", "settle", "--timeout=10"), 12.0)
    for _ in range(15):
        interface = find_rndis_interface()
        if interface:
            runner(("nmcli", "device", "set", interface, "managed", "yes"), 10.0)
            # connect is only issued for a newly materialized device; it is
            # never used against a healthy active RNDIS connection.
            runner(("nmcli", "device", "connect", interface), 20.0)
            return True
        time.sleep(1.0)
    return False


def has_guard_failover(
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> bool:
    for flag in ("-4", "-6"):
        result = runner(("ip", flag, "route", "show", "default", "proto", FAILOVER_PROTOCOL), 5.0)
        if result.returncode == 0 and result.stdout:
            return True
    return False


def install_failover_routes(
    wifi_interface: str,
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> bool:
    routes = default_routes_for(wifi_interface, runner)
    installed = False
    for route in routes:
        if not route.gateway:
            continue
        flag = "-4" if route.family == socket.AF_INET else "-6"
        command = (
            "ip", flag, "route", "replace", "default", "via", route.gateway,
            "dev", wifi_interface, "proto", FAILOVER_PROTOCOL, "metric", str(FAILOVER_METRIC),
        )
        result = runner(command, 10.0)
        if result.returncode == 0:
            installed = True
        else:
            log(f"Wi-Fi failover route failed rc={result.returncode} family={flag}")
    return installed


def remove_failover_routes(
    runner: Callable[[Sequence[str], float], CommandResult] = default_runner,
) -> bool:
    ok = True
    for flag in ("-4", "-6"):
        result = runner(("ip", flag, "route", "show", "default", "proto", FAILOVER_PROTOCOL), 5.0)
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts or parts[0] != "default":
                continue
            delete = runner(("ip", flag, "route", "del", *parts), 10.0)
            if delete.returncode != 0:
                ok = False
                log(f"failed to remove guard failover route: {line}")
    return ok


class NetworkHealthGuard:
    def __init__(self) -> None:
        self.state = GuardState(failover_active=has_guard_failover())
        self.running = True

    def stop(self, _signum: int, _frame: object) -> None:
        self.running = False

    def _maybe_failover(self, resolved: dict) -> Optional[LinkProbe]:
        wifi = probe_link(WIFI_INTERFACE, resolved=resolved)
        if not wifi.healthy:
            log(f"Wi-Fi failover not installed: backup link unhealthy ({wifi.detail})")
            return wifi
        now = time.monotonic()
        if not self.state.failover_active and action_allowed(self.state.last_failover, now):
            if install_failover_routes(WIFI_INTERFACE):
                self.state.failover_active = True
                self.state.last_failover = now
                self.state.last_action = "wifi-failover-installed"
                log("installed runtime-only Wi-Fi failover routes (proto 99)")
        return wifi

    def iteration(self) -> tuple[LinkProbe, Optional[LinkProbe]]:
        now = time.monotonic()
        resolved = resolve_probe_addresses()
        rndis_interface = find_rndis_interface()
        rndis = probe_link(rndis_interface, resolved=resolved)
        wifi: Optional[LinkProbe] = None

        if rndis.healthy:
            self.state.consecutive_failures = 0
            self.state.consecutive_successes += 1
            self.state.last_error = ""
            if self.state.failover_active and self.state.consecutive_successes >= SUCCESSES_BEFORE_RESTORE:
                if remove_failover_routes():
                    self.state.failover_active = False
                    self.state.last_action = "rndis-restored-failover-removed"
                    log("RNDIS healthy twice; removed runtime Wi-Fi failover routes")
        else:
            self.state.consecutive_successes = 0
            self.state.consecutive_failures += 1
            self.state.last_error = rndis.detail
            count = self.state.consecutive_failures
            log(f"RNDIS unhealthy {count}/{FAILURES_BEFORE_REPAIR}: {rndis.detail}")

            if count >= FAILURES_BEFORE_REPAIR:
                wifi = self._maybe_failover(resolved)

                if not rndis.present and action_allowed(self.state.last_rebind, now):
                    self.state.last_rebind = now
                    self.state.last_action = "rndis-network-interface-rebind"
                    if repair_missing_rndis():
                        log("RNDIS network-class interface rebind completed")
                    else:
                        log("RNDIS network-class interface rebind did not recover a net device")
                elif rndis.present and rndis.tcp_ok and not rndis.dns_ok and action_allowed(self.state.last_dns_repair, now):
                    self.state.last_dns_repair = now
                    self.state.last_action = "dns-cache-link-reapply"
                    repair_dns(rndis.interface or "")
                    log("requested DNS cache reset and RNDIS link reapply")
                elif rndis.present and action_allowed(self.state.last_link_repair, now):
                    self.state.last_link_repair = now
                    self.state.last_action = "rndis-link-reapply"
                    safe_reapply_link(rndis.interface or "")
                    log("requested non-disruptive NetworkManager RNDIS reapply")

        self.state.failover_active = has_guard_failover()
        save_state(self.state)
        write_status(self.state, rndis, wifi)
        return rndis, wifi

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log(f"starting build={BUILD} interval={CHECK_INTERVAL:.0f}s")
        try:
            while self.running:
                started = time.monotonic()
                try:
                    self.iteration()
                except Exception as exc:  # keep monitoring; never touch applications
                    self.state.last_error = f"guard-exception:{type(exc).__name__}:{exc}"
                    log(self.state.last_error)
                    save_state(self.state)
                remaining = CHECK_INTERVAL - (time.monotonic() - started)
                end = time.monotonic() + max(0.0, remaining)
                while self.running and time.monotonic() < end:
                    time.sleep(min(0.5, end - time.monotonic()))
        finally:
            if remove_failover_routes():
                log("clean shutdown: guard-owned failover routes removed")
        return 0


def cleanup() -> int:
    removed = remove_failover_routes()
    log("cleanup completed" if removed else "cleanup completed with route-removal errors")
    return 0 if removed else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one health iteration")
    parser.add_argument("--cleanup", action="store_true", help="remove guard-owned failover routes")
    parser.add_argument("--print-build", action="store_true")
    args = parser.parse_args(argv)

    if args.print_build:
        print(BUILD)
        return 0
    if args.cleanup:
        return cleanup()
    guard = NetworkHealthGuard()
    if args.once:
        guard.iteration()
        return 0
    return guard.run()


if __name__ == "__main__":
    raise SystemExit(main())
