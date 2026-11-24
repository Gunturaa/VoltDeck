"""LAN network interface detection and QR code generation."""

from __future__ import annotations

import base64
import io
import ipaddress
import json
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

import qrcode
from qrcode.image.svg import SvgPathImage

try:
    import netifaces  # type: ignore
except ImportError:  # pragma: no cover
    netifaces = None


VIRTUAL_PREFIXES = (
    "lo",
    "docker",
    "br-",
    "veth",
    "virbr",
    "vmnet",
    "vboxnet",
    "tun",
    "tap",
    "tailscale",
    "wg",
    "zt",
    "cni",
    "flannel",
    "podman",
    "lxc",
)


@dataclass
class NetworkInterface:
    name: str
    ip: str
    kind: str  # wifi, ethernet, other
    is_primary: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ip": self.ip,
            "kind": self.kind,
            "is_primary": self.is_primary,
        }


def _classify_interface(name: str) -> str:
    lower = name.lower()
    if lower.startswith(("wl", "wifi", "wlan")):
        return "wifi"
    if lower.startswith(("en", "eth", "em", "p")):
        return "ethernet"
    return "other"


def _is_virtual(name: str) -> bool:
    lower = name.lower()
    return any(lower.startswith(prefix) for prefix in VIRTUAL_PREFIXES)


def _get_default_route_ip() -> Optional[str]:
    """Determine the local IP used to reach the LAN/default route without sending packets."""
    for target in ("192.168.1.1", "10.0.0.1", "1.1.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.2)
            sock.connect((target, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    return None


def list_lan_interfaces() -> list[NetworkInterface]:
    """Return sorted list of candidate LAN IPv4 interfaces."""
    default_ip = _get_default_route_ip()
    found: list[tuple[int, NetworkInterface]] = []
    seen_ips: set[str] = set()

    if netifaces is not None:
        for iface_name in netifaces.interfaces():
            if iface_name == "lo":
                continue
            try:
                addrs = netifaces.ifaddresses(iface_name).get(netifaces.AF_INET, [])
            except Exception:
                continue
            for entry in addrs:
                ip = entry.get("addr", "")
                if not ip or ip in seen_ips:
                    continue
                try:
                    ip_obj = ipaddress.IPv4Address(ip)
                except ValueError:
                    continue
                if ip_obj.is_loopback or ip_obj.is_link_local:
                    continue

                seen_ips.add(ip)
                kind = _classify_interface(iface_name)
                virtual = _is_virtual(iface_name)

                # Priority score (lower is better)
                score = 50
                if ip == default_ip:
                    score -= 35
                if ip_obj.is_private:
                    score -= 10
                if kind == "wifi":
                    score -= 8
                elif kind == "ethernet":
                    score -= 6
                if virtual:
                    score += 100

                found.append(
                    (
                        score,
                        NetworkInterface(
                            name=iface_name,
                            ip=ip,
                            kind=kind,
                            is_primary=(ip == default_ip),
                        ),
                    )
                )

    if not found and default_ip:
        found.append(
            (
                0,
                NetworkInterface(
                    name="default",
                    ip=default_ip,
                    kind="other",
                    is_primary=True,
                ),
            )
        )

    if not found:
        found.append(
            (
                999,
                NetworkInterface(
                    name="lo",
                    ip="127.0.0.1",
                    kind="other",
                    is_primary=False,
                ),
            )
        )

    found.sort(key=lambda item: (item[0], item[1].name))
    interfaces = [item[1] for item in found]
    if interfaces and not any(i.is_primary for i in interfaces):
        interfaces[0].is_primary = True
    return interfaces


def pick_active_interface(preferred: str = "") -> NetworkInterface:
    interfaces = list_lan_interfaces()
    if preferred:
        for iface in interfaces:
            if iface.name == preferred or iface.ip == preferred:
                return iface
    return interfaces[0]


def find_available_port(preferred_port: int = 8765, max_tries: int = 25) -> int:
    """Return preferred_port if bindable on 0.0.0.0, else probe nearby ports."""
    for offset in range(max_tries):
        port = preferred_port + offset
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    # Let OS pick an ephemeral port as last resort
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("0.0.0.0", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def get_mdns_hostname() -> Optional[str]:
    """Return mDNS .local hostname (Avahi/Bonjour) if usable on Linux Mint."""
    host = socket.gethostname().strip().lower()
    if not host or host in ("localhost", "localhost.localdomain"):
        return None
    if host.endswith(".local"):
        base = host[:-6]
    else:
        base = host.split(".")[0]
    cleaned = re.sub(r"[^a-z0-9-]", "-", base).strip("-")
    if not cleaned:
        return None
    return f"{cleaned}.local"


def is_avahi_available() -> bool:
    return shutil.which("avahi-daemon") is not None


TAILSCALE_V4_NET = ipaddress.IPv4Network("100.64.0.0/10")
TAILSCALE_V6_NET = ipaddress.IPv6Network("fd7a:115c:a1e0::/48")


def classify_client_ip(raw_ip: str) -> str:
    """Classify client IP into 'loopback', 'tailscale', 'lan', or 'public'."""
    ip_str = (raw_ip or "").strip()
    if ip_str.startswith("::ffff:"):
        ip_str = ip_str[len("::ffff:"):]
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return "public"

    if ip_obj.is_loopback:
        return "loopback"
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in TAILSCALE_V4_NET:
        return "tailscale"
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in TAILSCALE_V6_NET:
        return "tailscale"
    if ip_obj.is_private or ip_obj.is_link_local:
        return "lan"
    return "public"


def _find_tailscale_iface_ip() -> Optional[str]:
    if netifaces is None:
        return None
    for name in netifaces.interfaces():
        if not name.lower().startswith("tailscale"):
            continue
        try:
            addrs = netifaces.ifaddresses(name).get(netifaces.AF_INET, [])
        except Exception:
            continue
        for entry in addrs:
            ip = entry.get("addr", "")
            if ip and classify_client_ip(ip) == "tailscale":
                return ip
    return None


def get_tailscale_status() -> dict[str, Any]:
    """Inspect local Tailscale VPN status safely without blocking or requiring sudo."""
    ts_bin = shutil.which("tailscale")
    iface_ip = _find_tailscale_iface_ip()
    base: dict[str, Any] = {
        "installed": ts_bin is not None,
        "running": False,
        "backend_state": "NotInstalled" if ts_bin is None else "Stopped",
        "ip": iface_ip,
        "dns_name": None,
        "hostname": None,
        "online_peers": 0,
    }
    if ts_bin is None:
        return base

    try:
        proc = subprocess.run(
            [ts_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            if iface_ip:
                base["running"] = True
                base["backend_state"] = "Running"
            return base

        data = json.loads(proc.stdout)
        backend_state = str(data.get("BackendState", "Stopped"))
        self_node = data.get("Self") or {}
        ts_ips = self_node.get("TailscaleIPs") or []
        v4_ip = next((ip for ip in ts_ips if ":" not in str(ip)), None) or iface_ip
        dns_raw = str(self_node.get("DNSName") or "").rstrip(".")
        host_raw = str(self_node.get("HostName") or "")

        peers = data.get("Peer") or {}
        online_peers = sum(1 for p in peers.values() if isinstance(p, dict) and p.get("Online"))

        base.update(
            {
                "running": backend_state == "Running" and bool(v4_ip),
                "backend_state": backend_state,
                "ip": v4_ip,
                "dns_name": dns_raw or None,
                "hostname": host_raw or None,
                "online_peers": online_peers,
            }
        )
        return base
    except Exception:
        if iface_ip:
            base["running"] = True
            base["backend_state"] = "Running"
        return base


def generate_qr_svg(data: str) -> str:
    """Generate a crisp, clean inline SVG QR code with dark modules on transparent bg."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg_text = buf.getvalue().decode("utf-8")
    # Make responsive while preserving crisp edges
    svg_text = svg_text.replace("<svg ", '<svg class="qr-svg" shape-rendering="crispEdges" ', 1)
    return svg_text


def generate_qr_data_uri(data: str) -> str:
    """Return a data:image/svg+xml;base64,... URI for easy embedding."""
    svg = generate_qr_svg(data)
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
