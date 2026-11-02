"""Bounded, cancellable RTSP discovery on the active home IPv4 subnet."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import socket
import shutil
import struct
import subprocess
from pathlib import Path


def _interface_addresses():
    """Read shared-network interfaces without a host command (including Flatpak)."""
    import fcntl

    routes = []
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and int(fields[3], 16) & 1:
            default = fields[1] == "00000000"
            if default or fields[2] == "00000000":
                routes.append((not default, int(fields[6]), fields[0]))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        seen = set()
        for _not_default, _metric, device in sorted(routes):
            if device in seen:
                continue
            seen.add(device)
            request = struct.pack("256s", device.encode()[:15])
            try:
                address = socket.inet_ntoa(fcntl.ioctl(connection.fileno(), 0x8915, request)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(connection.fileno(), 0x891B, request)[20:24])
                prefix = ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen
                yield {"local": address, "prefixlen": prefix}
            except OSError:
                continue


PORTS = (554, 8554, 10554)
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def home_network():
    def ip_json(*args):
        return json.loads(subprocess.check_output(["ip", "-j", "-4", *args], timeout=3))

    if shutil.which("ip"):
        routes = [route for route in ip_json("route", "show")
                  if "dev" in route and (route.get("dst") == "default" or route.get("scope") == "link")]
        routes.sort(key=lambda route: (route.get("dst") != "default", route.get("metric", 0)))
        addresses = [address for route in routes
                     for interface in ip_json("addr", "show", "dev", route["dev"])
                     for address in interface.get("addr_info", [])]
    else:
        addresses = _interface_addresses()
    for address in addresses:
        local = ipaddress.ip_address(address["local"])
        if not any(local in network for network in PRIVATE_NETWORKS):
            continue
        # Keep discovery within one small directly connected range.
        prefix = max(24, address["prefixlen"])
        return ipaddress.ip_network(f"{local}/{prefix}", strict=False), str(local)
    raise ValueError("Connect to your home Wi-Fi or Ethernet network, then try again.")


def probe(host, port, cancelled):
    if cancelled.is_set():
        return None
    try:
        with socket.create_connection((host, port), timeout=.55) as connection:
            connection.settimeout(.8)
            request = f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            connection.sendall(request.encode("ascii"))
            try:
                response = connection.recv(1024)
            except (TimeoutError, OSError):
                response = b""
            return {"host": host, "port": port, "verified": response.startswith(b"RTSP/1.")}
    except OSError:
        return None


def scan(network, own_address, cancelled, on_found, on_progress):
    targets = [(str(host), port) for host in network.hosts() if str(host) != own_address for port in PORTS]
    executor = ThreadPoolExecutor(max_workers=48, thread_name_prefix="rtsp-scan")
    futures = [executor.submit(probe, host, port, cancelled) for host, port in targets]
    try:
        for completed, future in enumerate(as_completed(futures), 1):
            if cancelled.is_set():
                break
            result = future.result()
            if result:
                on_found(result)
            if completed % 12 == 0 or completed == len(targets):
                on_progress(completed / len(targets))
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
