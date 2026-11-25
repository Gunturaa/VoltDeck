"""Multi-threaded streaming HTTP + Server-Sent Events (SSE) server for LANShare."""

from __future__ import annotations

import http.cookies
import io
import json
import mimetypes
import os
import queue
import re
import socket
import threading
import time
import urllib.parse
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .clipboard_store import ClipboardStore
from .config import AppConfig, DEFAULT_TRUSTED_FILE, is_autostart_enabled, set_autostart_enabled
from .network import (
    NetworkInterface,
    classify_client_ip,
    find_available_port,
    generate_qr_data_uri,
    get_mdns_hostname,
    get_tailscale_status,
    is_avahi_available,
    list_lan_interfaces,
    pick_active_interface,
)
from .remote_control import RemoteControlManager
from .security import SESSION_COOKIE_NAME, TRUST_COOKIE_NAME, ConnectedDevice, SecurityManager
from .storage import STREAM_CHUNK_SIZE, FileStorageManager, SharedFile, sanitize_filename

DESKTOP_COOKIE_NAME = "lanshare_desktop"
WEB_DIR = Path(__file__).resolve().parent / "web"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
URL_EXTRACT_RE = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)


def extract_safe_http_url(text: str) -> Optional[str]:
    """Extract and validate the first http(s) URL from text; rejects unsafe schemes."""
    if not text:
        return None
    m = URL_EXTRACT_RE.search(text.strip())
    if not m:
        return None
    candidate = m.group(0).rstrip(".,;:!?)>]}")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    # Block control characters or whitespace
    if any(ord(ch) < 32 or ch.isspace() for ch in candidate):
        return None
    return candidate


class _SSESubscriber:
    def __init__(self, role: str, session_id: Optional[str] = None) -> None:
        self.role = role
        self.session_id = session_id
        self.queue: queue.Queue[Optional[tuple[str, dict]]] = queue.Queue(maxsize=128)
        self.closed = False

    def send(self, event: str, payload: dict) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait((event, payload))
        except queue.Full:
            # Drop oldest event to keep stream responsive
            try:
                self.queue.get_nowait()
                self.queue.put_nowait((event, payload))
            except Exception:
                pass

    def close(self) -> None:
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass


class EventBroker:
    """Thread-safe SSE pub/sub hub."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: set[_SSESubscriber] = set()

    def subscribe(self, role: str, session_id: Optional[str] = None) -> _SSESubscriber:
        sub = _SSESubscriber(role=role, session_id=session_id)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: _SSESubscriber) -> None:
        sub.close()
        with self._lock:
            self._subs.discard(sub)

    def broadcast(self, event: str, payload: dict, roles: Optional[set[str]] = None) -> None:
        with self._lock:
            targets = list(self._subs)
        for sub in targets:
            if roles is None or sub.role in roles:
                sub.send(event, payload)

    def send_to_session(self, session_id: str, event: str, payload: dict) -> None:
        with self._lock:
            targets = [s for s in self._subs if s.session_id == session_id]
        for sub in targets:
            sub.send(event, payload)

    def revoke_session(self, session_id: str) -> None:
        with self._lock:
            targets = [s for s in self._subs if s.session_id == session_id]
        for sub in targets:
            sub.send("session_revoked", {"reason": "revoked"})
            sub.close()

    def revoke_all_mobile(self) -> None:
        with self._lock:
            targets = [s for s in self._subs if s.role == "mobile"]
        for sub in targets:
            sub.send("session_revoked", {"reason": "revoked_all"})
            sub.close()

    def close_all(self) -> None:
        with self._lock:
            targets = list(self._subs)
            self._subs.clear()
        for sub in targets:
            sub.close()


class LANShareState:
    """Shared application state owned by LANShareServer."""

    def __init__(
        self,
        config: AppConfig,
        port: int,
        on_mobile_clipboard: Optional[Callable[[str, str], None]] = None,
        on_mobile_clipboard_image: Optional[Callable[[bytes, str], None]] = None,
        on_notify: Optional[Callable[[str, str], None]] = None,
        on_open_url: Optional[Callable[[str, str], None]] = None,
        remote_command_runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
        tailscale_status_provider: Optional[Callable[[], dict[str, Any]]] = None,
        trusted_file: Path = DEFAULT_TRUSTED_FILE,
    ) -> None:
        self.config = config
        if self.config.access_mode not in ("offline", "anywhere"):
            self.config.access_mode = "offline"
        if self.config.theme not in ("dark", "light"):
            self.config.theme = "dark"
        self.port = port
        self.security = SecurityManager(trusted_file=trusted_file, initial_pin=config.custom_pin)
        self.storage = FileStorageManager(config.receive_path(), device_name=config.device_name)
        self.clipboard = ClipboardStore()
        self.events = EventBroker()
        self.on_mobile_clipboard = on_mobile_clipboard
        self.on_mobile_clipboard_image = on_mobile_clipboard_image
        self.on_notify = on_notify
        self.on_open_url = on_open_url
        self._tailscale_provider = tailscale_status_provider or get_tailscale_status
        self._iface_lock = threading.RLock()
        self.active_interface: NetworkInterface = pick_active_interface(config.preferred_interface)
        self.tailscale: dict[str, Any] = self._tailscale_provider()
        self.remote = RemoteControlManager(
            on_state_change=self._on_power_state_change,
            on_notify=self.notify,
            command_runner=remote_command_runner,
        )
        self.remote.set_battery_guard(config.battery_guard_percent)

    def refresh_tailscale(self) -> dict[str, Any]:
        ts = self._tailscale_provider()
        with self._iface_lock:
            self.tailscale = ts
        return ts

    def get_anywhere_url(self) -> Optional[str]:
        with self._iface_lock:
            ts = self.tailscale
        if ts.get("ip"):
            return f"http://{ts['ip']}:{self.port}"
        return None

    def get_anywhere_dns_url(self) -> Optional[str]:
        with self._iface_lock:
            ts = self.tailscale
        if ts.get("dns_name"):
            return f"http://{ts['dns_name']}:{self.port}"
        return None

    def set_access_mode(self, mode: str) -> str:
        target = "anywhere" if mode == "anywhere" else "offline"
        self.config.access_mode = target
        self.config.save()
        self.refresh_tailscale()
        if target == "offline":
            # Immediately disconnect any active remote Tailscale VPN sessions
            revoked_sids = self.security.revoke_sessions_by_zone("tailscale")
            for sid in revoked_sids:
                self.events.revoke_session(sid)
            if revoked_sids:
                self.broadcast_devices()
        self.broadcast_security()
        return target

    def _on_power_state_change(self, power_state: dict[str, Any]) -> None:
        self.events.broadcast("power_changed", {"power": power_state})

    def refresh_interfaces(self) -> list[NetworkInterface]:
        ifaces = list_lan_interfaces()
        with self._iface_lock:
            names = {i.name: i for i in ifaces}
            if self.active_interface.name in names:
                self.active_interface = names[self.active_interface.name]
            else:
                self.active_interface = pick_active_interface(self.config.preferred_interface)
        return ifaces

    def set_interface(self, name_or_ip: str) -> NetworkInterface:
        ifaces = list_lan_interfaces()
        with self._iface_lock:
            for iface in ifaces:
                if iface.name == name_or_ip or iface.ip == name_or_ip:
                    self.active_interface = iface
                    self.config.preferred_interface = iface.name
                    self.config.save()
                    break
            return self.active_interface

    def get_mdns_url(self, include_pin: bool = False) -> Optional[str]:
        mdns_host = get_mdns_hostname()
        if not mdns_host:
            return None
        base = f"http://{mdns_host}:{self.port}"
        if include_pin:
            return f"{base}/?pin={self.security.pin}"
        return base

    def get_lan_url(self, include_pin: Optional[bool] = None, prefer_mdns: Optional[bool] = None) -> str:
        use_mdns = self.config.use_mdns_qr if prefer_mdns is None else prefer_mdns
        inc = self.config.qr_include_pin if include_pin is None else include_pin
        if use_mdns:
            m_url = self.get_mdns_url(include_pin=inc)
            if m_url:
                return m_url
        with self._iface_lock:
            ip = self.active_interface.ip
        base = f"http://{ip}:{self.port}"
        if inc:
            return f"{base}/?pin={self.security.pin}"
        return base

    def get_desktop_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/desktop?token={self.security.desktop_token}"

    def _compute_qr_url(self) -> str:
        if self.config.access_mode == "anywhere":
            # Strict security: never embed PIN in remote Anywhere QR!
            ts_url = self.get_anywhere_url()
            if ts_url:
                return ts_url
        return self.get_lan_url(include_pin=self.config.qr_include_pin, prefer_mdns=self.config.use_mdns_qr)

    def build_desktop_state(self) -> dict[str, Any]:
        ifaces = self.refresh_interfaces()
        ts = self.refresh_tailscale()
        base_url = self.get_lan_url(include_pin=False, prefer_mdns=False)
        mdns_url = self.get_mdns_url(include_pin=False)
        anywhere_url = self.get_anywhere_url()
        anywhere_dns_url = self.get_anywhere_dns_url()
        qr_url = self._compute_qr_url()
        return {
            "version": __version__,
            "device_name": self.config.device_name,
            "pin": self.security.pin,
            "pin_configured": self.security.pin_configured,
            "pin_created_at": self.security.pin_created_at,
            "port": self.port,
            "access_mode": self.config.access_mode,
            "base_url": base_url,
            "mdns_url": mdns_url,
            "anywhere_url": anywhere_url,
            "anywhere_dns_url": anywhere_dns_url,
            "tailscale": ts,
            "avahi_available": is_avahi_available(),
            "qr_url": qr_url,
            "qr_data_uri": generate_qr_data_uri(qr_url),
            "active_interface": self.active_interface.to_dict(),
            "interfaces": [i.to_dict() for i in ifaces],
            "receive_dir": str(self.storage.receive_dir),
            "config": {
                "clipboard_auto_sync": self.config.clipboard_auto_sync,
                "qr_include_pin": self.config.qr_include_pin,
                "use_mdns_qr": self.config.use_mdns_qr,
                "access_mode": self.config.access_mode,
                "anywhere_allow_power": self.config.anywhere_allow_power,
                "remote_control_enabled": self.config.remote_control_enabled,
                "language": self.config.language,
                "theme": self.config.theme,
                "minimize_to_tray": self.config.minimize_to_tray,
                "autostart_enabled": is_autostart_enabled(),
                "notifications_enabled": self.config.notifications_enabled,
            },
            "devices": self.security.list_devices(),
            "trusted_devices": self.security.list_trusted_devices(),
            "files": self.storage.list_files(),
            "transfers": self.storage.list_active_transfers(),
            "clipboard": self.clipboard.list_items(),
            "clipboard_image": self.clipboard.get_image_meta(),
            "power": self.remote.get_power_state(),
        }

    def build_mobile_state(self, device: Optional[ConnectedDevice], client_zone: str = "lan") -> dict[str, Any]:
        return {
            "version": __version__,
            "device_name": self.config.device_name,
            "peer_name": device.device_name if device else "Desktop",
            "access_mode": self.config.access_mode,
            "client_zone": client_zone,
            "language": self.config.language,
            "theme": self.config.theme,
            "trusted": bool(device and device.trust_id),
            "has_pending_trust": bool(device and device.pending_trust_cookie),
            "files": self.storage.list_files(),
            "transfers": self.storage.list_active_transfers(),
            "clipboard": self.clipboard.list_items(),
            "clipboard_image": self.clipboard.get_image_meta(),
            "remote_enabled": self.config.remote_control_enabled,
            "power": self.remote.get_power_state(),
        }

    def broadcast_files(self) -> None:
        self.events.broadcast("files_changed", {"files": self.storage.list_files()})

    def broadcast_transfers(self) -> None:
        self.events.broadcast("transfer_progress", {"transfers": self.storage.list_active_transfers()})

    def broadcast_clipboard(self) -> None:
        self.events.broadcast(
            "clipboard_updated",
            {
                "clipboard": self.clipboard.list_items(),
                "clipboard_image": self.clipboard.get_image_meta(),
            },
        )

    def broadcast_devices(self) -> None:
        self.events.broadcast(
            "devices_changed",
            {
                "devices": self.security.list_devices(),
                "trusted_devices": self.security.list_trusted_devices(),
            },
            roles={"desktop"},
        )

    def broadcast_security(self, network_changed: bool = False) -> None:
        ifaces = self.refresh_interfaces()
        base_url = self.get_lan_url(include_pin=False, prefer_mdns=False)
        mdns_url = self.get_mdns_url(include_pin=False)
        anywhere_url = self.get_anywhere_url()
        anywhere_dns_url = self.get_anywhere_dns_url()
        qr_url = self._compute_qr_url()
        self.events.broadcast(
            "security_changed",
            {
                "pin": self.security.pin,
                "pin_configured": self.security.pin_configured,
                "pin_created_at": self.security.pin_created_at,
                "access_mode": self.config.access_mode,
                "base_url": base_url,
                "mdns_url": mdns_url,
                "anywhere_url": anywhere_url,
                "anywhere_dns_url": anywhere_dns_url,
                "tailscale": self.tailscale,
                "qr_url": qr_url,
                "qr_data_uri": generate_qr_data_uri(qr_url),
                "active_interface": self.active_interface.to_dict(),
                "interfaces": [i.to_dict() for i in ifaces],
                "network_changed": network_changed,
                "receive_dir": str(self.storage.receive_dir),
                "config": {
                    "clipboard_auto_sync": self.config.clipboard_auto_sync,
                    "qr_include_pin": self.config.qr_include_pin,
                    "use_mdns_qr": self.config.use_mdns_qr,
                    "access_mode": self.config.access_mode,
                    "anywhere_allow_power": self.config.anywhere_allow_power,
                    "remote_control_enabled": self.config.remote_control_enabled,
                    "language": self.config.language,
                    "theme": self.config.theme,
                    "minimize_to_tray": self.config.minimize_to_tray,
                    "autostart_enabled": is_autostart_enabled(),
                    "notifications_enabled": self.config.notifications_enabled,
                },
            },
            roles={"desktop"},
        )
        self.events.broadcast(
            "remote_config_changed",
            {
                "remote_enabled": self.config.remote_control_enabled,
                "access_mode": self.config.access_mode,
                "language": self.config.language,
                "theme": self.config.theme,
            },
            roles={"mobile"},
        )

    def push_desktop_clipboard(self, text: str) -> bool:
        entry = self.clipboard.push(text=text, source=self.config.device_name, source_kind="desktop")
        if entry is None:
            return False
        self.broadcast_clipboard()
        return True

    def push_desktop_clipboard_image(self, png_bytes: bytes) -> bool:
        meta = self.clipboard.push_image(data=png_bytes, source=self.config.device_name, source_kind="desktop")
        if meta is None:
            return False
        self.broadcast_clipboard()
        return True

    def add_local_paths(self, paths: list[str]) -> list[dict]:
        added = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                # Add immediate files inside folder (non-recursive safety cap)
                for child in sorted(p.iterdir()):
                    if child.is_file() and not child.name.startswith("."):
                        entry = self.storage.register_local_file(
                            child, direction="outbound", sender=self.config.device_name
                        )
                        if entry:
                            added.append(entry.to_dict())
            elif p.is_file():
                entry = self.storage.register_local_file(
                    p, direction="outbound", sender=self.config.device_name
                )
                if entry:
                    added.append(entry.to_dict())
        if added:
            self.broadcast_files()
        return added

    def notify(self, title: str, body: str) -> None:
        if self.config.notifications_enabled and self.on_notify:
            try:
                self.on_notify(title, body)
            except Exception:
                pass


class _StreamingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, server_address: tuple[str, int], state: LANShareState) -> None:
        self.app_state = state
        self._stopping = False
        super().__init__(server_address, LANShareRequestHandler)

    def finish_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        try:
            request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        super().finish_request(request, client_address)


class LANShareRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _StreamingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy stdout access logs in production
        return

    @property
    def state(self) -> LANShareState:
        return self.server.app_state

    # --- Helpers ---

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "0.0.0.0"

    def _client_zone(self) -> str:
        # Allow automated tests on loopback to simulate Tailscale/public zones safely via header only when explicitly enabled
        sim = self.headers.get("X-Test-Client-IP") if getattr(self.server, "allow_test_ip_header", False) else None
        return classify_client_ip(sim or self._client_ip())

    def _is_client_allowed_by_mode(self) -> bool:
        zone = self._client_zone()
        if zone == "loopback":
            return True
        if zone == "lan":
            return True
        if zone == "tailscale":
            return self.state.config.access_mode == "anywhere"
        return False

    def _send_offline_blocked(self, is_api: bool) -> None:
        if is_api:
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "VoltDeck sedang dalam Mode OFFLINE (LAN Only). Aktifkan Mode Anywhere di laptop untuk akses jarak jauh.",
                access_mode=self.state.config.access_mode,
            )
            return
        html = (
            "<!doctype html><html lang='id'><head><meta charset='utf-8'/>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
            "<title>VoltDeck — Mode Offline</title>"
            "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#090b0e;color:#f1f5f9;"
            "font-family:-apple-system,system-ui,sans-serif;padding:20px;text-align:center}"
            ".card{max-width:360px;padding:24px;border-radius:14px;background:#0e1116;border:1px solid rgba(255,255,255,.12)}"
            ".tag{display:inline-block;padding:3px 8px;border-radius:5px;background:rgba(245,158,11,.15);color:#f59e0b;"
            "font:600 11px ui-monospace,monospace;letter-spacing:.08em;margin-bottom:12px}"
            "h1{font-size:17px;margin:0 0 8px}p{font-size:13.5px;color:#94a3b8;line-height:1.5;margin:0}</style>"
            "</head><body><div class='card'><div class='tag'>MODE OFFLINE · LAN ONLY</div>"
            "<h1>Akses Jarak Jauh Ditutup</h1>"
            "<p>VoltDeck di laptop sedang dalam <strong>Mode Offline</strong>. "
            "Hubungkan ke Wi-Fi lokal yang sama atau aktifkan <strong>Mode Anywhere</strong> di aplikasi laptop.</p>"
            "</div></body></html>"
        ).encode("utf-8")
        self._send_bytes(html, "text/html; charset=utf-8", status=HTTPStatus.FORBIDDEN, cache_control="no-store")

    def _is_loopback(self) -> bool:
        return self._client_ip() in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _parse_cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return {}
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return {}
        return {k: morsel.value for k, morsel in jar.items()}

    def _is_desktop_authed(self, query: dict[str, list[str]]) -> bool:
        if not self._is_loopback():
            return False
        token = self.headers.get("X-Desktop-Token")
        if not token and "token" in query:
            token = query["token"][0]
        if not token:
            token = self._parse_cookies().get(DESKTOP_COOKIE_NAME)
        return self.state.security.is_desktop_token_valid(token)

    def _get_mobile_device(self) -> Optional[ConnectedDevice]:
        cookie_val = self._parse_cookies().get(SESSION_COOKIE_NAME)
        return self.state.security.verify_session_cookie(cookie_val)

    def _try_auto_login_trusted(self) -> Optional[tuple[str, ConnectedDevice]]:
        trust_val = self._parse_cookies().get(TRUST_COOKIE_NAME) or self.headers.get("X-Trust-Token")
        if not trust_val:
            return None
        res = self.state.security.authenticate_trust_cookie(
            cookie_value=trust_val,
            client_ip=self._client_ip(),
            user_agent=self.headers.get("User-Agent", ""),
            network_zone=self._client_zone(),
        )
        if res:
            self.state.broadcast_devices()
            _, dev = res
            self.state.notify("Perangkat Terpercaya Terhubung", f"{dev.device_name} otomatis masuk ({dev.network_zone.upper()}).")
        return res

    def _authenticate_any(self, query: dict[str, list[str]]) -> tuple[Optional[str], Optional[ConnectedDevice]]:
        if self._is_desktop_authed(query):
            return "desktop", None
        dev = self._get_mobile_device()
        if dev:
            return "mobile", dev
        return None, None

    def _read_json_body(self, max_bytes: int = 1024 * 1024) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > max_bytes:
            return None
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _send_json(
        self,
        payload: dict[str, Any],
        status: int = HTTPStatus.OK,
        extra_headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
        cache_control: str = "no-cache",
        extra_headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, message: str, **extra: Any) -> None:
        data: dict[str, Any] = {"ok": False, "error": message}
        data.update(extra)
        self._send_json(data, status=status)

    def _serve_static_file(self, rel_name: str) -> None:
        safe = os.path.basename(rel_name)
        if not safe or safe != rel_name or safe.startswith("."):
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        candidate = WEB_DIR / safe
        if not candidate.is_file():
            # Allow icon assets from assets/ or assets/icons/
            alt_map = {
                "icon.svg": ASSETS_DIR / "lanshare.svg",
                "app-icon.png": ASSETS_DIR / "icons" / "lanshare-128.png",
                "apple-touch-icon.png": ASSETS_DIR / "icons" / "lanshare-180.png",
                "favicon.png": ASSETS_DIR / "icons" / "lanshare-64.png",
                "hero-character.png": ASSETS_DIR / "hero_character.png",
                "hero-character-mobile.png": ASSETS_DIR / "hero_character_mobile.png",
            }
            candidate = alt_map.get(safe, Path(""))

        if not candidate or not candidate.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "Asset not found")
            return

        mime, _ = mimetypes.guess_type(candidate.name)
        if candidate.suffix == ".svg":
            mime = "image/svg+xml; charset=utf-8"
        elif candidate.suffix in (".css", ".js", ".html"):
            mime = f"{mime or 'text/plain'}; charset=utf-8"
        data = candidate.read_bytes()
        cache = "public, max-age=3600" if candidate.suffix in (".png", ".svg") else "no-cache"
        self._send_bytes(data, mime or "application/octet-stream", cache_control=cache)

    # --- HTTP Verbs ---

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Enforce Dual-Mode Network Gate (Offline LAN Only vs Anywhere Tailscale VPN)
        if not self._is_client_allowed_by_mode():
            self._send_offline_blocked(is_api=path.startswith("/api/"))
            return

        # 1. Public pages & static assets
        if path == "/" or path == "/index.html":
            self._serve_static_file("mobile.html")
            return
        if path.startswith("/static/"):
            self._serve_static_file(path[len("/static/"):])
            return
        if path == "/apple-touch-icon.png":
            self._serve_static_file("apple-touch-icon.png")
            return
        if path == "/favicon.ico":
            self._serve_static_file("favicon.png")
            return

        # 2. Desktop WebKit2 entry
        if path == "/desktop":
            if not self._is_desktop_authed(query):
                self._send_error_json(HTTPStatus.FORBIDDEN, "Desktop access denied")
                return
            html = (WEB_DIR / "desktop.html").read_bytes()
            cookie = f"{DESKTOP_COOKIE_NAME}={self.state.security.desktop_token}; HttpOnly; SameSite=Strict; Path=/"
            self._send_bytes(
                html,
                "text/html; charset=utf-8",
                cache_control="no-store",
                extra_headers=[("Set-Cookie", cookie)],
            )
            return

        # 3. Public info endpoint (for PIN gate, connection check, & Trusted Device auto-login)
        if path == "/api/info":
            role, dev = self._authenticate_any(query)
            extra_headers: list[tuple[str, str]] = []
            if role is None:
                auto = self._try_auto_login_trusted()
                if auto:
                    signed_sid, dev = auto
                    role = "mobile"
                    extra_headers.append(
                        ("Set-Cookie", f"{SESSION_COOKIE_NAME}={signed_sid}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400")
                    )
            self._send_json(
                {
                    "ok": True,
                    "version": __version__,
                    "device_name": self.state.config.device_name,
                    "access_mode": self.state.config.access_mode,
                    "client_zone": self._client_zone(),
                    "language": self.state.config.language,
                    "theme": self.state.config.theme,
                    "authenticated": role is not None,
                    "trusted": bool(dev and dev.trust_id),
                    "role": role,
                    "peer_name": dev.device_name if dev else None,
                },
                extra_headers=extra_headers,
            )
            return

        # 4. Desktop-only state
        if path == "/api/desktop/state":
            if not self._is_desktop_authed(query):
                self._send_error_json(HTTPStatus.FORBIDDEN, "Desktop auth required")
                return
            self._send_json({"ok": True, **self.state.build_desktop_state()})
            return

        # 5. Authenticated shared endpoints (auto-reauthenticate seamlessly if trusted cookie/header is valid!)
        role, dev = self._authenticate_any(query)
        minted_sid_headers: list[tuple[str, str]] = []
        if role is None:
            auto = self._try_auto_login_trusted()
            if auto:
                signed_sid, dev = auto
                role = "mobile"
                minted_sid_headers.append(
                    ("Set-Cookie", f"{SESSION_COOKIE_NAME}={signed_sid}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400")
                )
        if role is None:
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required")
            return

        if path == "/api/state":
            self._send_json(
                {"ok": True, **self.state.build_mobile_state(dev, client_zone=self._client_zone())},
                extra_headers=minted_sid_headers,
            )
            return

        if path == "/api/events":
            self._handle_sse(role, dev)
            return

        if path == "/api/clipboard":
            self._send_json(
                {
                    "ok": True,
                    "clipboard": self.state.clipboard.list_items(),
                    "clipboard_image": self.state.clipboard.get_image_meta(),
                }
            )
            return

        if path == "/api/clipboard/image":
            img_bytes = self.state.clipboard.get_image_bytes()
            if not img_bytes:
                self._send_error_json(HTTPStatus.NOT_FOUND, "Belum ada gambar di clipboard.")
                return
            extra = []
            if query.get("download", ["0"])[0] == "1":
                extra.append(("Content-Disposition", 'attachment; filename="voltdeck_clipboard.png"'))
            self._send_bytes(img_bytes, "image/png", cache_control="no-store", extra_headers=extra)
            return

        if path == "/api/remote/status":
            status = self.state.remote.get_full_status(enabled=self.state.config.remote_control_enabled)
            self._send_json({"ok": True, **status})
            return

        if path == "/api/remote/telemetry":
            self._send_json({"ok": True, **self.state.remote.get_live_status()})
            return

        if path == "/api/remote/streams":
            if not self.state.config.remote_control_enabled:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Remote Control sedang dinonaktifkan.")
                return
            self._send_json({"ok": True, "streams": self.state.remote.list_audio_streams()})
            return

        if path == "/api/remote/windows":
            if not self.state.config.remote_control_enabled:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Remote Control sedang dinonaktifkan.")
                return
            self._send_json({"ok": True, "windows": self.state.remote.list_open_windows()})
            return

        if path == "/api/remote/bluetooth":
            if not self.state.config.remote_control_enabled:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Remote Control sedang dinonaktifkan.")
                return
            self._send_json({"ok": True, **self.state.remote.list_bluetooth_devices()})
            return

        if path == "/api/remote/screen/record":
            self._send_json({"ok": True, **self.state.remote.get_screen_record_status()})
            return

        if path == "/api/remote/processes":
            if not self.state.config.remote_control_enabled:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Remote Control sedang dinonaktifkan.")
                return
            procs = self.state.remote.list_top_processes(limit=8)
            self._send_json({"ok": True, "processes": procs})
            return

        if path == "/api/remote/screen/snapshot":
            if not self.state.config.remote_control_enabled:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Remote Control sedang dinonaktifkan.")
                return
            download = query.get("download", ["0"])[0] == "1"
            jpeg = self.state.remote.capture_screen_jpeg(max_width=1440 if download else 1080, quality=82 if download else 72)
            if not jpeg:
                self._send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Tangkapan layar tidak tersedia pada sesi ini.")
                return
            extra = []
            if download:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                extra.append(("Content-Disposition", f'attachment; filename="laptop_screen_{stamp}.jpg"'))
            self._send_bytes(jpeg, "image/jpeg", cache_control="no-store", extra_headers=extra)
            return

        if path == "/api/remote/screen/stream":
            if not self.state.config.remote_control_enabled:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Remote Control sedang dinonaktifkan.")
                return
            try:
                fps = max(1, min(10, int(query.get("fps", ["5"])[0])))
            except ValueError:
                fps = 5
            try:
                quality = max(35, min(85, int(query.get("q", ["58"])[0])))
            except ValueError:
                quality = 58
            self._handle_screen_mjpeg_stream(fps=fps, quality=quality)
            return

        if path == "/api/remote/webcam/snapshot":
            if not self.state.config.remote_control_enabled:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Remote Control sedang dinonaktifkan.")
                return
            download = query.get("download", ["0"])[0] == "1"
            jpeg = self.state.remote.capture_webcam_jpeg(quality=84)
            if not jpeg:
                self._send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Webcam laptop tidak tersedia atau sedang dipakai aplikasi lain.")
                return
            extra = []
            if download:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                extra.append(("Content-Disposition", f'attachment; filename="laptop_webcam_{stamp}.jpg"'))
            self._send_bytes(jpeg, "image/jpeg", cache_control="no-store", extra_headers=extra)
            return

        if path == "/api/gallery":
            folder = query.get("folder", ["all"])[0]
            data = self.state.storage.scan_laptop_images(folder_filter=folder)
            self._send_json({"ok": True, **data})
            return

        if path == "/api/explorer":
            root_name = query.get("root", [""])[0]
            rel_path = query.get("path", [""])[0]
            data = self.state.storage.list_explorer_dir(root_name=root_name, rel_path=rel_path)
            self._send_json({"ok": True, **data})
            return

        if path == "/api/explorer/download":
            root_name = query.get("root", [""])[0]
            rel_path = query.get("path", [""])[0]
            resolved = self.state.storage.resolve_explorer_path(root_name, rel_path)
            if not resolved or not resolved[2].is_file():
                self._send_error_json(HTTPStatus.NOT_FOUND, "File tidak ditemukan atau akses ditolak.")
                return
            self._stream_raw_path(resolved[2], inline=False)
            return

        m_gal = re.match(r"^/api/gallery/([a-f0-9]+)/(thumb|download|preview)$", path)
        if m_gal:
            gid, action = m_gal.group(1), m_gal.group(2)
            if action == "thumb":
                thumb = self.state.storage.get_gallery_thumbnail_jpeg(gid)
                if not thumb:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Thumbnail tidak tersedia")
                    return
                self._send_bytes(thumb, "image/jpeg", cache_control="private, max-age=300")
                return
            img_path = self.state.storage.get_gallery_image_path(gid)
            if not img_path:
                self._send_error_json(HTTPStatus.NOT_FOUND, "Gambar tidak ditemukan")
                return
            self._stream_raw_path(img_path, inline=(action == "preview"))
            return

        if path == "/api/files/zip":
            ids_param = query.get("ids", [""])[0]
            file_ids = [i.strip() for i in ids_param.split(",") if i.strip()] if ids_param else []
            self._handle_zip_download(file_ids)
            return

        # File download / preview / thumbnail
        m = re.match(r"^/api/files/([a-f0-9]+)/(download|preview|thumb)$", path)
        if m:
            file_id, action = m.group(1), m.group(2)
            if action == "thumb":
                self._handle_thumbnail(file_id)
            else:
                self._handle_file_stream(
                    file_id=file_id,
                    inline=(action == "preview"),
                    peer_label=dev.device_name if dev else self.state.config.device_name,
                )
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        role, _ = self._authenticate_any(query)
        if role is None:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        m = re.match(r"^/api/files/([a-f0-9]+)/(download|preview)$", parsed.path)
        if not m:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        entry = self.state.storage.get_file(m.group(1))
        if not entry or not entry.path.is_file():
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = entry.path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", entry.mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not self._is_client_allowed_by_mode():
            self._send_offline_blocked(is_api=True)
            return

        # 1. PIN Authentication
        if path == "/api/auth/verify":
            body = self._read_json_body()
            if body is None:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
                return
            pin_val = str(body.get("pin", ""))
            custom_name = str(body.get("device_name", ""))
            ua = self.headers.get("User-Agent", "")
            zone = self._client_zone()
            ok, cookie_val, device, err, retry_after = self.state.security.verify_pin_and_create_session(
                pin_attempt=pin_val,
                client_ip=self._client_ip(),
                user_agent=ua,
                custom_name=custom_name,
                network_zone=zone,
            )
            if not ok or not cookie_val or not device:
                status = HTTPStatus.TOO_MANY_REQUESTS if retry_after > 0 else HTTPStatus.UNAUTHORIZED
                self._send_error_json(status, err, retry_after=retry_after)
                return

            cookie = f"{SESSION_COOKIE_NAME}={cookie_val}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400"
            self.state.broadcast_devices()
            self.state.notify("Perangkat Terhubung", f"{device.device_name} ({device.client_ip}) terhubung ke VoltDeck.")
            self._send_json(
                {
                    "ok": True,
                    "device": device.to_dict(),
                    "device_name": self.state.config.device_name,
                },
                extra_headers=[("Set-Cookie", cookie)],
            )
            return

        if path == "/api/auth/logout":
            dev = self._get_mobile_device()
            if dev:
                self.state.security.revoke_session(dev.session_id)
                self.state.events.revoke_session(dev.session_id)
                self.state.broadcast_devices()
            clear_sid = f"{SESSION_COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
            clear_trust = f"{TRUST_COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
            self._send_json(
                {"ok": True},
                extra_headers=[("Set-Cookie", clear_sid), ("Set-Cookie", clear_trust)],
            )
            return

        if path == "/api/auth/claim-trust":
            dev = self._get_mobile_device()
            if not dev:
                self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required")
                return
            trust_cookie = self.state.security.claim_pending_trust_cookie(dev.session_id)
            if not trust_cookie:
                self._send_json({"ok": True, "claimed": False, "trusted": dev.trust_id is not None})
                return
            cookie_hdr = f"{TRUST_COOKIE_NAME}={trust_cookie}; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000"
            self._send_json(
                {"ok": True, "claimed": True, "trusted": True, "trust_token": trust_cookie},
                extra_headers=[("Set-Cookie", cookie_hdr)],
            )
            return

        # 2. Desktop-only management endpoints
        if path.startswith("/api/desktop/"):
            if not self._is_desktop_authed(query):
                self._send_error_json(HTTPStatus.FORBIDDEN, "Desktop auth required")
                return
            self._handle_desktop_post(path)
            return

        # 3. Authenticated shared endpoints
        role, dev = self._authenticate_any(query)
        if role is None:
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required")
            return

        if path == "/api/upload":
            self._handle_streaming_upload(role, dev)
            return

        m_cancel = re.match(r"^/api/transfers/([a-f0-9_-]+)/cancel$", path)
        if m_cancel:
            tid = m_cancel.group(1)
            cancelled = self.state.storage.request_cancel(tid)
            self.state.broadcast_transfers()
            self._send_json({"ok": True, "cancelled": cancelled})
            return

        if path == "/api/clipboard":
            body = self._read_json_body()
            if body is None:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
                return
            text = str(body.get("text", ""))
            if not text.strip():
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Clipboard text cannot be empty")
                return
            open_on_desktop = bool(body.get("open_on_desktop", False))
            source = self.state.config.device_name if role == "desktop" else (dev.device_name if dev else "LAN Device")
            entry = self.state.clipboard.push(
                text=text,
                source=source,
                source_kind=role,
                deduplicate_latest=False,
            )
            self.state.broadcast_clipboard()
            opened_url: Optional[str] = None
            if open_on_desktop:
                opened_url = extract_safe_http_url(text)
                if opened_url and self.state.on_open_url:
                    try:
                        self.state.on_open_url(opened_url, source)
                    except Exception:
                        pass
            if role == "mobile" and self.state.on_mobile_clipboard:
                try:
                    self.state.on_mobile_clipboard(text, source)
                except Exception:
                    pass
                if not opened_url:
                    self.state.notify("Clipboard Diterima", f"Teks dari {source} disalin ke clipboard Linux Mint.")
            self._send_json(
                {
                    "ok": True,
                    "item": entry.to_dict() if entry else None,
                    "opened_url": opened_url,
                }
            )
            return

        if path == "/api/open-url":
            body = self._read_json_body()
            if body is None:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
                return
            safe_url = extract_safe_http_url(str(body.get("url", "")))
            if not safe_url:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Hanya tautan http:// atau https:// yang diizinkan.")
                return
            source = self.state.config.device_name if role == "desktop" else (dev.device_name if dev else "LAN Device")
            if self.state.on_open_url:
                try:
                    self.state.on_open_url(safe_url, source)
                except Exception:
                    pass
            self._send_json({"ok": True, "url": safe_url})
            return

        if path.startswith("/api/remote/"):
            self._handle_remote_post(path, role, dev)
            return

        if path == "/api/clipboard/image":
            self._handle_clipboard_image_upload(role, dev)
            return

        if path == "/api/explorer/mkdir":
            body = self._read_json_body()
            if body is None:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
                return
            ok, err, listing = self.state.storage.create_explorer_folder(
                str(body.get("root", "")),
                str(body.get("path", "")),
                str(body.get("name", "")),
            )
            if not ok:
                self._send_error_json(HTTPStatus.BAD_REQUEST, err)
                return
            self._send_json({"ok": True, **(listing or {})})
            return

        if path == "/api/explorer/upload":
            self._handle_explorer_upload(query, role, dev)
            return

        if path == "/api/clipboard/clear":
            self.state.clipboard.clear()
            self.state.broadcast_clipboard()
            self._send_json({"ok": True})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not self._is_client_allowed_by_mode():
            self._send_offline_blocked(is_api=True)
            return

        role, _ = self._authenticate_any(query)
        if role is None:
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required")
            return

        m_file = re.match(r"^/api/files/([a-f0-9]+)$", path)
        if m_file:
            fid = m_file.group(1)
            delete_disk = role == "desktop" and query.get("delete_disk", ["0"])[0] == "1"
            removed = self.state.storage.remove_file(fid, delete_from_disk=delete_disk)
            if not removed:
                self._send_error_json(HTTPStatus.NOT_FOUND, "File not found")
                return
            self.state.broadcast_files()
            self._send_json({"ok": True})
            return

        m_clip = re.match(r"^/api/clipboard/([a-f0-9]+)$", path)
        if m_clip:
            ok = self.state.clipboard.delete_item(m_clip.group(1))
            if ok:
                self.state.broadcast_clipboard()
            self._send_json({"ok": ok})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found")

    # --- Desktop Management Handlers ---

    def _handle_desktop_post(self, path: str) -> None:
        body = self._read_json_body() or {}

        if path == "/api/desktop/pin/set":
            raw_pin = str(body.get("pin", ""))
            revoke = bool(body.get("revoke_sessions", False))
            ok, err, weak = self.state.security.set_custom_pin(raw_pin, revoke_sessions=revoke)
            if not ok:
                self._send_error_json(HTTPStatus.BAD_REQUEST, err)
                return
            self.state.config.custom_pin = self.state.security.pin
            self.state.config.save()
            if revoke:
                self.state.events.revoke_all_mobile()
                self.state.broadcast_devices()
            self.state.broadcast_security()
            self._send_json(
                {
                    "ok": True,
                    "pin": self.state.security.pin,
                    "pin_configured": True,
                    "weak_warning": weak,
                }
            )
            return

        if path == "/api/desktop/pin/rotate":
            revoke = bool(body.get("revoke_sessions", False))
            new_pin = self.state.security.rotate_pin(revoke_sessions=revoke)
            self.state.config.custom_pin = new_pin
            self.state.config.save()
            if revoke:
                self.state.events.revoke_all_mobile()
                self.state.broadcast_devices()
            self.state.broadcast_security()
            self._send_json({"ok": True, "pin": new_pin, "pin_configured": True})
            return

        if path == "/api/desktop/interface":
            iface_name = str(body.get("interface", ""))
            iface = self.state.set_interface(iface_name)
            self.state.broadcast_security()
            self._send_json({"ok": True, "active_interface": iface.to_dict()})
            return

        if path == "/api/desktop/mode":
            mode = str(body.get("mode", "offline"))
            applied = self.state.set_access_mode(mode)
            self._send_json(
                {
                    "ok": True,
                    "access_mode": applied,
                    "tailscale": self.state.tailscale,
                    "anywhere_url": self.state.get_anywhere_url(),
                }
            )
            return

        if path == "/api/desktop/tailscale/refresh":
            ts = self.state.refresh_tailscale()
            self.state.broadcast_security()
            self._send_json({"ok": True, "tailscale": ts})
            return

        if path == "/api/desktop/settings":
            cfg = self.state.config
            if "clipboard_auto_sync" in body:
                cfg.clipboard_auto_sync = bool(body["clipboard_auto_sync"])
            if "qr_include_pin" in body:
                cfg.qr_include_pin = bool(body["qr_include_pin"])
            if "use_mdns_qr" in body:
                cfg.use_mdns_qr = bool(body["use_mdns_qr"])
            if "anywhere_allow_power" in body:
                cfg.anywhere_allow_power = bool(body["anywhere_allow_power"])
            if "remote_control_enabled" in body:
                cfg.remote_control_enabled = bool(body["remote_control_enabled"])
            if "language" in body and body["language"] in ("id", "en"):
                cfg.language = str(body["language"])
            if "theme" in body and body["theme"] in ("dark", "light"):
                cfg.theme = str(body["theme"])
            if "minimize_to_tray" in body:
                cfg.minimize_to_tray = bool(body["minimize_to_tray"])
            if "autostart_enabled" in body:
                set_autostart_enabled(bool(body["autostart_enabled"]))
            if "notifications_enabled" in body:
                cfg.notifications_enabled = bool(body["notifications_enabled"])
            cfg.save()
            self.state.broadcast_security()
            self._send_json({"ok": True})
            return

        m_trust = re.match(r"^/api/desktop/devices/([a-f0-9]+)/trust$", path)
        if m_trust:
            res = self.state.security.trust_connected_device(m_trust.group(1))
            if not res:
                self._send_error_json(HTTPStatus.NOT_FOUND, "Perangkat tidak ditemukan")
                return
            dev, rec = res
            self.state.events.send_to_session(dev.session_id, "trust_granted", {"trust_id": rec.trust_id})
            self.state.broadcast_devices()
            self._send_json({"ok": True, "trusted_device": rec.to_public_dict()})
            return

        m_untrust = re.match(r"^/api/desktop/trusted/([a-f0-9]+)/revoke$", path)
        if m_untrust:
            existed, revoked_sids = self.state.security.revoke_trusted_device(m_untrust.group(1))
            for sid in revoked_sids:
                self.state.events.revoke_session(sid)
            self.state.broadcast_devices()
            self._send_json({"ok": existed})
            return

        if path == "/api/desktop/devices/revoke-all":
            count = self.state.security.revoke_all_sessions()
            self.state.events.revoke_all_mobile()
            self.state.broadcast_devices()
            self._send_json({"ok": True, "revoked": count})
            return

        m_rev = re.match(r"^/api/desktop/devices/([a-f0-9]+)/revoke$", path)
        if m_rev:
            dev = self.state.security.revoke_session(m_rev.group(1))
            if dev:
                self.state.events.revoke_session(dev.session_id)
                self.state.broadcast_devices()
            self._send_json({"ok": dev is not None})
            return

        if path == "/api/desktop/files/add-paths":
            raw_paths = body.get("paths", [])
            if not isinstance(raw_paths, list):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "paths must be a list")
                return
            added = self.state.add_local_paths([str(p) for p in raw_paths])
            self._send_json({"ok": True, "added": added})
            return

        if path == "/api/desktop/files/clear":
            direction = body.get("direction")
            if direction not in (None, "inbound", "outbound"):
                direction = None
            count = self.state.storage.clear_files(direction=direction)
            self.state.broadcast_files()
            self._send_json({"ok": True, "removed": count})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "Desktop endpoint not found")

    # --- Remote Control Handler (Media, Volume, Lock, Sleep, Shutdown) ---

    def _handle_remote_post(self, path: str, role: str, dev: Optional[ConnectedDevice]) -> None:
        body = self._read_json_body()
        if body is None:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
            return

        # Always allow cancelling a scheduled power action from either Desktop or Mobile
        if path == "/api/remote/power" and body.get("action") == "cancel":
            res = self.state.remote.cancel_power_action()
            self._send_json(res)
            return

        if not self.state.config.remote_control_enabled:
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Remote Control sedang dinonaktifkan di aplikasi Linux Mint.",
            )
            return

        peer = self.state.config.device_name if role == "desktop" else (dev.device_name if dev else "iPhone")

        if path == "/api/remote/media":
            action = str(body.get("action", ""))
            seek_sec = int(body.get("seek_seconds", 0))
            bus_name = str(body.get("bus_name", "")).strip() or None
            res = self.state.remote.media_action(action, seek_seconds=seek_sec, bus_name=bus_name)
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Media gagal")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/volume":
            if body.get("mute_toggle"):
                vol = self.state.remote.toggle_mute()
            elif "percent" in body:
                vol = self.state.remote.set_volume(int(body["percent"]))
            elif "delta" in body:
                vol = self.state.remote.adjust_volume(int(body["delta"]))
            else:
                vol = self.state.remote.get_volume()
            self._send_json({"ok": True, "volume": vol})
            return

        if path == "/api/remote/sink":
            sink_name = str(body.get("sink", ""))
            res = self.state.remote.set_audio_sink(sink_name)
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal mengganti output audio")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/streams":
            try:
                sid = int(body.get("id", -1))
            except (TypeError, ValueError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Stream ID tidak valid.")
                return
            pct = int(body["percent"]) if "percent" in body and body["percent"] is not None else None
            mute_t = bool(body.get("mute_toggle", False))
            res = self.state.remote.set_audio_stream(sid, percent=pct, mute_toggle=mute_t)
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal mengatur volume aplikasi")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/windows":
            if "shortcut" in body:
                res = self.state.remote.quick_shortcut(str(body.get("shortcut", "")))
            else:
                res = self.state.remote.window_action(str(body.get("id", "")), str(body.get("action", "focus")))
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal mengatur jendela")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/keep-awake":
            enabled = bool(body.get("enabled", False))
            res = self.state.remote.set_keep_awake(enabled, requested_by=peer)
            self._send_json(res)
            return

        if path == "/api/remote/bluetooth":
            action = str(body.get("action", ""))
            mac = str(body.get("mac", ""))
            power = body["power"] if "power" in body else None
            res = self.state.remote.bluetooth_action(action=action, mac=mac, power=power)
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal mengatur Bluetooth")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/battery-guard":
            try:
                pct = int(body.get("percent", 0))
            except (TypeError, ValueError):
                pct = 0
            applied = self.state.remote.set_battery_guard(pct)
            self.state.config.battery_guard_percent = applied
            self.state.config.save()
            self._send_json({"ok": True, "battery_guard_percent": applied, "telemetry": self.state.remote.get_telemetry()})
            return

        if path == "/api/remote/screen/record":
            action = str(body.get("action", ""))
            if action == "start":
                res = self.state.remote.start_screen_recording(self.state.storage.receive_dir, requested_by=peer)
                if not res.get("ok"):
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal merekam layar")))
                    return
                self._send_json(res)
                return
            if action == "stop":
                res = self.state.remote.stop_screen_recording()
                if not res.get("ok"):
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal menghentikan rekaman")))
                    return
                saved_path = Path(res["path"])
                entry = self.state.storage.register_local_file(
                    saved_path,
                    direction="outbound",
                    sender=self.state.config.device_name,
                    owned_by_receive_dir=True,
                )
                if entry:
                    self.state.broadcast_files()
                    res["file"] = entry.to_dict()
                self._send_json(res)
                return
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Aksi rekaman harus start atau stop.")
            return

        if path == "/api/remote/brightness":
            if "percent" in body:
                br = self.state.remote.set_brightness(int(body["percent"]))
            else:
                br = self.state.remote.get_brightness()
            self._send_json({"ok": True, "brightness": br})
            return

        if path == "/api/remote/display-off":
            res = self.state.remote.display_off(requested_by=peer)
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(res.get("error", "Gagal mematikan layar")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/apps/launch":
            app_id = str(body.get("app_id", ""))
            res = self.state.remote.launch_app(app_id, requested_by=peer)
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal membuka aplikasi")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/privacy-mode":
            res = self.state.remote.activate_privacy_mode(requested_by=peer)
            self._send_json(res)
            return

        if path == "/api/remote/processes/kill":
            try:
                pid = int(body.get("pid", 0))
            except (TypeError, ValueError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "PID tidak valid.")
                return
            res = self.state.remote.terminate_user_process(pid, requested_by=peer)
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal menutup proses")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/input":
            kind = str(body.get("type", ""))
            if kind == "move":
                res = self.state.remote.move_mouse_relative(int(body.get("dx", 0)), int(body.get("dy", 0)))
            elif kind == "click":
                res = self.state.remote.mouse_click(str(body.get("button", "left")), bool(body.get("double", False)))
            elif kind == "click_at":
                res = self.state.remote.click_at_ratio(
                    float(body.get("x", 0.5)),
                    float(body.get("y", 0.5)),
                    str(body.get("button", "left")),
                )
            elif kind == "scroll":
                res = self.state.remote.mouse_scroll(int(body.get("delta_y", 0)))
            elif kind == "key":
                res = self.state.remote.press_key(str(body.get("key", "")))
            elif kind == "text":
                res = self.state.remote.type_text(str(body.get("text", "")))
            else:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Tipe input tidak valid.")
                return
            if not res.get("ok"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Input gagal")))
                return
            self._send_json(res)
            return

        if path == "/api/remote/power":
            action = str(body.get("action", ""))
            if action == "lock":
                res = self.state.remote.lock_screen(requested_by=peer)
                if not res.get("ok"):
                    self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(res.get("error", "Gagal mengunci")))
                    return
                self._send_json(res)
                return
            if action in ("shutdown", "sleep", "restart"):
                if self._client_zone() == "tailscale" and not self.state.config.anywhere_allow_power:
                    self._send_error_json(
                        HTTPStatus.FORBIDDEN,
                        "Kontrol daya (Sleep/Restart/Matikan) dari jarak jauh sedang dinonaktifkan di laptop.",
                    )
                    return
                delay = int(body.get("delay_seconds", 15))
                res = self.state.remote.schedule_power_action(
                    action=action,
                    delay_seconds=delay,
                    requested_by=peer,
                )
                if not res.get("ok"):
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(res.get("error", "Gagal menjadwalkan")))
                    return
                self._send_json(res)
                return
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Aksi daya tidak valid.")
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "Remote endpoint not found")

    # --- Streaming Upload (iPhone -> Linux Mint or Desktop Drop -> Outbox) ---

    def _handle_streaming_upload(self, role: str, dev: Optional[ConnectedDevice]) -> None:
        raw_filename = self.headers.get("X-Filename", "")
        if raw_filename:
            filename = sanitize_filename(urllib.parse.unquote(raw_filename))
        else:
            filename = sanitize_filename(f"upload_{int(time.time())}.bin")

        try:
            content_length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            content_length = -1

        if content_length < 0:
            self._send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length header is required")
            return

        transfer_id = self.headers.get("X-Transfer-Id")
        if transfer_id and not re.match(r"^[a-f0-9_-]{4,32}$", transfer_id):
            transfer_id = None

        direction = "outbound" if (role == "desktop" and self.headers.get("X-Direction") == "outbound") else "inbound"
        peer = self.state.config.device_name if role == "desktop" else (dev.device_name if dev else self._client_ip())

        part_path, final_path = self.state.storage.allocate_receive_temp_path(filename)
        tx = self.state.storage.start_transfer(
            filename=final_path.name,
            direction=direction,
            peer=peer,
            total_bytes=content_length,
            transfer_id=transfer_id,
        )
        self.state.broadcast_transfers()

        received = 0
        last_emit = time.monotonic()

        try:
            with open(part_path, "wb") as fp:
                remaining = content_length
                while remaining > 0:
                    if self.state.storage.is_cancelled(tx.id):
                        raise _TransferCancelledError("Transfer dibatalkan.")
                    chunk = self.rfile.read(min(STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ConnectionResetError("Client terputus sebelum upload selesai.")
                    fp.write(chunk)
                    received += len(chunk)
                    remaining -= len(chunk)
                    self.state.storage.update_transfer(tx.id, received)
                    now = time.monotonic()
                    if now - last_emit >= 0.14:
                        self.state.broadcast_transfers()
                        last_emit = now

            if direction == "outbound":
                # Desktop drop fallback: move to receive dir and mark outbound
                dest = final_path
                os.replace(part_path, dest)
                entry = self.state.storage.register_local_file(
                    dest,
                    direction="outbound",
                    sender=self.state.config.device_name,
                    owned_by_receive_dir=True,
                )
            else:
                entry = self.state.storage.finalize_received_file(
                    part_path=part_path,
                    preferred_final=final_path,
                    sender=peer,
                )

            self.state.storage.finish_transfer(tx.id, status="completed")
            self.state.broadcast_transfers()
            self.state.broadcast_files()

            if entry and direction == "inbound":
                size_mb = entry.size / (1024 * 1024)
                size_str = f"{size_mb:.1f} MB" if size_mb >= 1 else f"{max(1, entry.size // 1024)} KB"
                self.state.notify("File Diterima", f"{entry.name} ({size_str}) dari {peer}")

            self._send_json({"ok": True, "file": entry.to_dict() if entry else None})

        except _TransferCancelledError as exc:
            self._cleanup_part(part_path)
            self.state.storage.finish_transfer(tx.id, status="cancelled", error=str(exc))
            self.state.broadcast_transfers()
            try:
                self.close_connection = True
                self._send_error_json(HTTPStatus.REQUEST_TIMEOUT, str(exc))
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._cleanup_part(part_path)
            self.state.storage.finish_transfer(tx.id, status="cancelled", error="Koneksi terputus")
            self.state.broadcast_transfers()
            self.close_connection = True
            _ = exc
        except Exception as exc:
            self._cleanup_part(part_path)
            self.state.storage.finish_transfer(tx.id, status="error", error=str(exc))
            self.state.broadcast_transfers()
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Upload gagal: {exc}")
            except OSError:
                pass

    @staticmethod
    def _cleanup_part(part_path: Path) -> None:
        try:
            if part_path.exists():
                part_path.unlink()
        except OSError:
            pass

    # --- Streaming Download & Preview with HTTP Range (206 Partial Content) ---

    def _handle_file_stream(self, file_id: str, inline: bool, peer_label: str) -> None:
        entry: Optional[SharedFile] = self.state.storage.get_file(file_id)
        if not entry or not entry.path.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "File tidak ditemukan atau sudah dipindahkan.")
            return

        file_size = entry.path.stat().st_size
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK

        range_header = self.headers.get("Range")
        if range_header and file_size > 0:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m:
                r_start, r_end = m.group(1), m.group(2)
                if r_start == "" and r_end != "":
                    suffix_len = int(r_end)
                    start = max(0, file_size - suffix_len)
                    end = file_size - 1
                else:
                    start = int(r_start) if r_start else 0
                    end = int(r_end) if r_end else file_size - 1
                if start >= file_size or end < start:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, file_size - 1)
                status = HTTPStatus.PARTIAL_CONTENT

        content_length = max(0, end - start + 1) if file_size > 0 else 0
        ascii_name = entry.name.encode("ascii", "ignore").decode("ascii").replace('"', "_") or "download"
        utf8_name = urllib.parse.quote(entry.name)
        disp_kind = "inline" if inline else "attachment"

        self.send_response(status)
        self.send_header("Content-Type", entry.mime)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header(
            "Content-Disposition",
            f'{disp_kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}',
        )
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "private, max-age=60" if inline else "no-store")
        self.end_headers()

        if content_length == 0:
            return

        # Track significant downloads in live transfer HUD
        track = (not inline) and content_length >= 64 * 1024
        tx = None
        if track:
            tx = self.state.storage.start_transfer(
                filename=entry.name,
                direction="outbound",
                peer=peer_label,
                total_bytes=content_length,
            )
            self.state.broadcast_transfers()

        sent = 0
        last_emit = time.monotonic()

        try:
            with open(entry.path, "rb") as fp:
                fp.seek(start)
                remaining = content_length
                while remaining > 0:
                    if tx and self.state.storage.is_cancelled(tx.id):
                        raise _TransferCancelledError("Download dibatalkan")
                    chunk = fp.read(min(STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
                    if tx:
                        self.state.storage.update_transfer(tx.id, sent)
                        now = time.monotonic()
                        if now - last_emit >= 0.14:
                            self.state.broadcast_transfers()
                            last_emit = now

            if not inline:
                self.state.storage.increment_downloads(entry.id)
                self.state.broadcast_files()
            if tx:
                self.state.storage.finish_transfer(tx.id, status="completed")
                self.state.broadcast_transfers()

        except (_TransferCancelledError, BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            if tx:
                self.state.storage.finish_transfer(tx.id, status="cancelled")
                self.state.broadcast_transfers()

    def _handle_thumbnail(self, file_id: str) -> None:
        data = self.state.storage.get_thumbnail_jpeg(file_id)
        if not data:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Thumbnail unavailable")
            return
        self._send_bytes(data, "image/jpeg", cache_control="private, max-age=300")

    def _stream_raw_path(self, file_path: Path, inline: bool = False) -> None:
        if not file_path.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "File tidak ditemukan")
            return
        mime, _ = mimetypes.guess_type(file_path.name)
        mime = mime or "application/octet-stream"
        size = file_path.stat().st_size
        ascii_name = file_path.name.encode("ascii", "ignore").decode("ascii").replace('"', "_") or "image.jpg"
        utf8_name = urllib.parse.quote(file_path.name)
        disp = "inline" if inline else "attachment"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'{disp}; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}')
        self.send_header("Cache-Control", "private, max-age=60" if inline else "no-store")
        self.end_headers()
        try:
            with open(file_path, "rb") as fp:
                while True:
                    chunk = fp.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _handle_zip_download(self, file_ids: list[str]) -> None:
        entries: list[SharedFile] = []
        if file_ids:
            for fid in file_ids:
                e = self.state.storage.get_file(fid)
                if e and e.path.is_file():
                    entries.append(e)
        else:
            for item in self.state.storage.list_files():
                if item["direction"] == "outbound" and item["exists"]:
                    e = self.state.storage.get_file(item["id"])
                    if e and e.path.is_file():
                        entries.append(e)

        if not entries:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Tidak ada file untuk diunduh.")
            return

        # Stream ZIP directly over socket without buffering entire archive in RAM
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="VoltDeck-Files.zip"')
        self.send_header("Connection", "close")
        self.end_headers()

        class _StreamWriter:
            def __init__(self, raw_stream: Any) -> None:
                self._raw = raw_stream
                self._pos = 0

            def write(self, data: bytes) -> int:
                self._raw.write(data)
                self._pos += len(data)
                return len(data)

            def tell(self) -> int:
                return self._pos

            def flush(self) -> None:
                self._raw.flush()

            def seekable(self) -> bool:
                return False

        writer = _StreamWriter(self.wfile)
        used_names: dict[str, int] = {}
        try:
            with zipfile.ZipFile(writer, mode="w", compression=zipfile.ZIP_STORED) as zf:
                for e in entries:
                    arcname = e.name
                    if arcname in used_names:
                        used_names[arcname] += 1
                        p = Path(arcname)
                        arcname = f"{p.stem} ({used_names[arcname]}){p.suffix}"
                    else:
                        used_names[arcname] = 0
                    zinfo = zipfile.ZipInfo.from_file(e.path, arcname=arcname)
                    zinfo.compress_type = zipfile.ZIP_STORED
                    with zf.open(zinfo, mode="w") as dest, open(e.path, "rb") as src:
                        while True:
                            chunk = src.read(STREAM_CHUNK_SIZE)
                            if not chunk:
                                break
                            dest.write(chunk)
            writer.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # --- Live Screen MJPEG Stream (native iOS Safari <img> streaming!) ---

    def _handle_screen_mjpeg_stream(self, fps: int = 5, quality: int = 58) -> None:
        boundary = "lanshareframe"
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        interval = 1.0 / max(1, fps)
        try:
            while self.state.config.remote_control_enabled:
                t0 = time.monotonic()
                jpeg = self.state.remote.capture_screen_jpeg(max_width=960, quality=quality)
                if not jpeg:
                    time.sleep(0.5)
                    continue
                header = (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode("ascii")
                self.wfile.write(header)
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                elapsed = time.monotonic() - t0
                if elapsed < interval:
                    time.sleep(interval - elapsed)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_clipboard_image_upload(self, role: str, dev: Optional[ConnectedDevice]) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 10 * 1024 * 1024:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Ukuran gambar clipboard harus di bawah 10 MB.")
            return
        raw = self.rfile.read(length)
        png_bytes = raw
        try:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(raw)) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                png_bytes = buf.getvalue()
        except Exception:
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Format gambar tidak dikenali.")
                return
        source = self.state.config.device_name if role == "desktop" else (dev.device_name if dev else "Phone")
        meta = self.state.clipboard.push_image(png_bytes, source=source, source_kind=role, deduplicate_latest=False)
        if not meta:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Gagal menyimpan gambar clipboard.")
            return
        self.state.broadcast_clipboard()
        if role == "mobile" and self.state.on_mobile_clipboard_image:
            try:
                self.state.on_mobile_clipboard_image(png_bytes, source)
            except Exception:
                pass
            self.state.notify("Gambar Clipboard Diterima", f"Gambar dari {source} siap ditempel (Ctrl+V) di Linux.")
        self._send_json({"ok": True, "clipboard_image": meta})

    def _handle_explorer_upload(
        self,
        query: dict[str, list[str]],
        role: str,
        dev: Optional[ConnectedDevice],
    ) -> None:
        root_name = query.get("root", [""])[0]
        rel_path = query.get("path", [""])[0]
        raw_name = urllib.parse.unquote(self.headers.get("X-Filename") or query.get("filename", ["upload.bin"])[0])
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Empty file upload.")
            return
        target = self.state.storage.reserve_explorer_upload_target(root_name, rel_path, raw_name)
        if not target:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Folder tujuan tidak valid atau akses ditolak.")
            return
        tmp_path = target.with_name(f".{target.name}.vdpart")
        remaining = content_length
        try:
            with open(tmp_path, "wb") as dst:
                while remaining > 0:
                    chunk = self.rfile.read(min(STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        raise OSError("Unexpected EOF")
                    dst.write(chunk)
                    remaining -= len(chunk)
            tmp_path.replace(target)
        except Exception as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Gagal mengunggah file: {exc}")
            return
        peer = dev.device_name if dev else self.state.config.device_name
        self.state.notify("File Tersimpan di Explorer", f"{target.name} diunggah ke {root_name} dari {peer}.")
        listing = self.state.storage.list_explorer_dir(root_name, rel_path)
        self._send_json({"ok": True, "filename": target.name, **listing})

    # --- Server-Sent Events (SSE) ---

    def _handle_sse(self, role: str, dev: Optional[ConnectedDevice]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        sub = self.state.events.subscribe(role=role, session_id=dev.session_id if dev else None)
        try:
            # Send initial connected comment
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            while not sub.closed:
                try:
                    msg = sub.queue.get(timeout=15.0)
                except queue.Empty:
                    # Heartbeat ping keeps iOS Safari SSE alive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue

                if msg is None:
                    break
                event_name, payload = msg
                data_str = json.dumps(payload, ensure_ascii=False)
                frame = f"event: {event_name}\ndata: {data_str}\n\n".encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.state.events.unsubscribe(sub)
            self.close_connection = True


class _TransferCancelledError(Exception):
    pass


class LANShareServer:
    """Lifecycle wrapper around the threaded streaming HTTP server."""

    def __init__(
        self,
        config: AppConfig,
        on_mobile_clipboard: Optional[Callable[[str, str], None]] = None,
        on_mobile_clipboard_image: Optional[Callable[[bytes, str], None]] = None,
        on_notify: Optional[Callable[[str, str], None]] = None,
        on_open_url: Optional[Callable[[str, str], None]] = None,
        remote_command_runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
        tailscale_status_provider: Optional[Callable[[], dict[str, Any]]] = None,
        trusted_file: Path = DEFAULT_TRUSTED_FILE,
    ) -> None:
        self.port = find_available_port(config.port)
        self.state = LANShareState(
            config=config,
            port=self.port,
            on_mobile_clipboard=on_mobile_clipboard,
            on_mobile_clipboard_image=on_mobile_clipboard_image,
            on_notify=on_notify,
            on_open_url=on_open_url,
            remote_command_runner=remote_command_runner,
            tailscale_status_provider=tailscale_status_provider,
            trusted_file=trusted_file,
        )
        self._httpd = _StreamingHTTPServer(("0.0.0.0", self.port), self.state)
        self._thread: Optional[threading.Thread] = None
        self._net_stop = threading.Event()
        self._net_thread: Optional[threading.Thread] = None

    def _network_watcher_loop(self) -> None:
        """Auto-detect Wi-Fi / LAN IP or Tailscale changes every 4s and push live QR/URL updates."""
        last_sig = self._current_network_signature()
        while not self._net_stop.wait(4.0):
            try:
                cur_sig = self._current_network_signature()
                if cur_sig != last_sig:
                    last_sig = cur_sig
                    self.state.broadcast_security(network_changed=True)
            except Exception:
                pass

    def _current_network_signature(self) -> tuple:
        ifaces = self.state.refresh_interfaces()
        ts = self.state.refresh_tailscale()
        return (
            tuple((i.name, i.ip) for i in ifaces),
            self.state.active_interface.ip,
            ts.get("running"),
            ts.get("ip"),
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._net_stop.clear()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="lanshare-http",
            daemon=True,
        )
        self._thread.start()
        self._net_thread = threading.Thread(
            target=self._network_watcher_loop,
            name="voltdeck-netwatch",
            daemon=True,
        )
        self._net_thread.start()

    def stop(self) -> None:
        self._net_stop.set()
        self._httpd._stopping = True
        self.state.remote.shutdown_cleanup()
        self.state.events.close_all()
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
