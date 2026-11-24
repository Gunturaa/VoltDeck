"""Security manager: 6-digit PIN, brute-force protection, HMAC sessions, and device registry."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import DEFAULT_TRUSTED_FILE

PIN_LENGTH = 6
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 60
SESSION_TTL_SECONDS = 24 * 3600
SESSION_COOKIE_NAME = "lanshare_sid"
TRUST_COOKIE_NAME = "lanshare_trust"


@dataclass
class TrustedDeviceRecord:
    trust_id: str
    secret_hash: str
    device_name: str
    created_at: float
    last_used: float

    def to_storage_dict(self) -> dict:
        return {
            "trust_id": self.trust_id,
            "secret_hash": self.secret_hash,
            "device_name": self.device_name,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }

    def to_public_dict(self) -> dict:
        return {
            "trust_id": self.trust_id,
            "device_name": self.device_name,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }


@dataclass
class ConnectedDevice:
    session_id: str
    device_name: str
    client_ip: str
    user_agent: str
    connected_at: float
    last_seen: float
    network_zone: str = "lan"
    trust_id: Optional[str] = None
    pending_trust_cookie: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.session_id[:12],
            "device_name": self.device_name,
            "client_ip": self.client_ip,
            "network_zone": self.network_zone,
            "trusted": self.trust_id is not None,
            "trust_id": self.trust_id,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "active_ago": max(0, int(time.time() - self.last_seen)),
        }


def parse_device_label(user_agent: str) -> str:
    ua = user_agent or ""
    if "iPhone" in ua:
        os_ver = ""
        m = re.search(r"iPhone OS (\d+)[_.](\d+)", ua)
        if m:
            os_ver = f" (iOS {m.group(1)})"
        browser = "Safari"
        if "CriOS" in ua:
            browser = "Chrome"
        elif "FxiOS" in ua:
            browser = "Firefox"
        return f"iPhone{os_ver} · {browser}"
    if "iPad" in ua:
        return "iPad · Safari"
    if "Android" in ua:
        return "Android Device"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "Mac · Browser"
    if "Linux" in ua:
        return "Linux · Browser"
    if "Windows" in ua:
        return "Windows · Browser"
    return "LAN Device"


class SecurityManager:
    """Thread-safe authentication, persistent trusted devices, & session manager."""

    def __init__(self, trusted_file: Path = DEFAULT_TRUSTED_FILE) -> None:
        self._lock = threading.RLock()
        self._trusted_file = trusted_file
        self._signing_key: bytes = secrets.token_bytes(32)
        self.desktop_token: str = secrets.token_urlsafe(32)
        self._pin: str = self._new_pin()
        self._pin_created_at: float = time.time()
        self._sessions: dict[str, ConnectedDevice] = {}
        self._short_id_map: dict[str, str] = {}
        self._trusted: dict[str, TrustedDeviceRecord] = {}
        # client_ip -> (fail_count, locked_until_timestamp)
        self._rate_limits: dict[str, tuple[int, float]] = {}
        self._load_trusted()

    def _load_trusted(self) -> None:
        if not self._trusted_file.is_file():
            return
        try:
            raw = json.loads(self._trusted_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("trust_id") and item.get("secret_hash"):
                        rec = TrustedDeviceRecord(
                            trust_id=str(item["trust_id"]),
                            secret_hash=str(item["secret_hash"]),
                            device_name=str(item.get("device_name", "Trusted iPhone")),
                            created_at=float(item.get("created_at", time.time())),
                            last_used=float(item.get("last_used", time.time())),
                        )
                        self._trusted[rec.trust_id] = rec
        except Exception:
            pass

    def _save_trusted(self) -> None:
        try:
            self._trusted_file.parent.mkdir(parents=True, exist_ok=True)
            data = [rec.to_storage_dict() for rec in self._trusted.values()]
            tmp = self._trusted_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self._trusted_file)
        except OSError:
            pass

    @staticmethod
    def _hash_secret(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_pin() -> str:
        # Cryptographically secure 6-digit numeric PIN (000000 - 999999)
        return f"{secrets.randbelow(10 ** PIN_LENGTH):0{PIN_LENGTH}d}"

    @property
    def pin(self) -> str:
        with self._lock:
            return self._pin

    @property
    def pin_created_at(self) -> float:
        with self._lock:
            return self._pin_created_at

    def rotate_pin(self, revoke_sessions: bool = False) -> str:
        with self._lock:
            self._pin = self._new_pin()
            self._pin_created_at = time.time()
            self._rate_limits.clear()
            if revoke_sessions:
                self._sessions.clear()
                self._short_id_map.clear()
            return self._pin

    def is_desktop_token_valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        return hmac.compare_digest(token, self.desktop_token)

    def check_rate_limit(self, client_ip: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            fails, locked_until = self._rate_limits.get(client_ip, (0, 0.0))
            if locked_until > now:
                return False, max(1, int(locked_until - now))
            if locked_until > 0 and locked_until <= now:
                self._rate_limits.pop(client_ip, None)
            return True, 0

    def verify_pin_and_create_session(
        self,
        pin_attempt: str,
        client_ip: str,
        user_agent: str,
        custom_name: str = "",
        network_zone: str = "lan",
    ) -> tuple[bool, Optional[str], Optional[ConnectedDevice], str, int]:
        """
        Verify 6-digit PIN and issue signed session cookie value.
        Returns: (ok, cookie_value, device, error_message, retry_after_sec)
        """
        cleaned = re.sub(r"\s+", "", str(pin_attempt or ""))
        allowed, retry_after = self.check_rate_limit(client_ip)
        if not allowed:
            return (
                False,
                None,
                None,
                f"Terlalu banyak percobaan gagal. Coba lagi dalam {retry_after} detik.",
                retry_after,
            )

        with self._lock:
            expected = self._pin
            is_valid = len(cleaned) == PIN_LENGTH and cleaned.isdigit() and hmac.compare_digest(cleaned, expected)

            if not is_valid:
                fails, _ = self._rate_limits.get(client_ip, (0, 0.0))
                fails += 1
                if fails >= MAX_FAILED_ATTEMPTS:
                    locked_until = time.time() + LOCKOUT_SECONDS
                    self._rate_limits[client_ip] = (fails, locked_until)
                    return (
                        False,
                        None,
                        None,
                        f"PIN salah {MAX_FAILED_ATTEMPTS}x. Akses dikunci {LOCKOUT_SECONDS} detik.",
                        LOCKOUT_SECONDS,
                    )
                self._rate_limits[client_ip] = (fails, 0.0)
                remaining = MAX_FAILED_ATTEMPTS - fails
                return (
                    False,
                    None,
                    None,
                    f"PIN tidak valid. Sisa {remaining} percobaan.",
                    0,
                )

            # Valid PIN — clear rate limit & create session
            self._rate_limits.pop(client_ip, None)
            session_id = secrets.token_hex(24)
            now = time.time()
            label = custom_name.strip()[:40] if custom_name.strip() else parse_device_label(user_agent)
            device = ConnectedDevice(
                session_id=session_id,
                device_name=label,
                client_ip=client_ip,
                user_agent=user_agent[:200],
                connected_at=now,
                last_seen=now,
                network_zone=network_zone,
            )
            self._sessions[session_id] = device
            self._short_id_map[session_id[:12]] = session_id
            signed_cookie = self._sign_session_id(session_id)
            return True, signed_cookie, device, "", 0

    def _sign_session_id(self, session_id: str) -> str:
        sig = hmac.new(self._signing_key, session_id.encode("ascii"), hashlib.sha256).hexdigest()[:32]
        return f"{session_id}.{sig}"

    def verify_session_cookie(self, cookie_value: Optional[str]) -> Optional[ConnectedDevice]:
        if not cookie_value or "." not in cookie_value:
            return None
        session_id, sig = cookie_value.split(".", 1)
        expected = hmac.new(self._signing_key, session_id.encode("ascii"), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None

        now = time.time()
        with self._lock:
            device = self._sessions.get(session_id)
            if device is None:
                return None
            if now - device.connected_at > SESSION_TTL_SECONDS:
                self._sessions.pop(session_id, None)
                self._short_id_map.pop(session_id[:12], None)
                return None
            device.last_seen = now
            return device

    def revoke_session(self, id_or_short: str) -> Optional[ConnectedDevice]:
        with self._lock:
            full_id = self._short_id_map.get(id_or_short, id_or_short)
            device = self._sessions.pop(full_id, None)
            if device:
                self._short_id_map.pop(full_id[:12], None)
            return device

    def revoke_all_sessions(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            self._short_id_map.clear()
            return count

    def revoke_sessions_by_zone(self, zone: str) -> list[str]:
        """Revoke all sessions matching a given network_zone (e.g. 'tailscale') and return their session IDs."""
        revoked: list[str] = []
        with self._lock:
            for sid, dev in list(self._sessions.items()):
                if dev.network_zone == zone:
                    self._sessions.pop(sid, None)
                    self._short_id_map.pop(sid[:12], None)
                    revoked.append(sid)
        return revoked

    def list_devices(self) -> list[dict]:
        now = time.time()
        with self._lock:
            expired = [
                sid
                for sid, dev in self._sessions.items()
                if now - dev.connected_at > SESSION_TTL_SECONDS
            ]
            for sid in expired:
                self._sessions.pop(sid, None)
                self._short_id_map.pop(sid[:12], None)
            devices = sorted(
                self._sessions.values(),
                key=lambda d: d.last_seen,
                reverse=True,
            )
            return [d.to_dict() for d in devices]

    # --- Persistent Trusted Devices (survives laptop/LANShare restart!) ---

    def trust_connected_device(self, id_or_short: str) -> Optional[tuple[ConnectedDevice, TrustedDeviceRecord]]:
        """Promote an active authenticated device to persistent Trusted Device."""
        with self._lock:
            full_id = self._short_id_map.get(id_or_short, id_or_short)
            dev = self._sessions.get(full_id)
            if not dev:
                return None

            # Replace existing trust record if already trusted
            if dev.trust_id and dev.trust_id in self._trusted:
                self._trusted.pop(dev.trust_id, None)

            trust_id = secrets.token_hex(6)
            raw_secret = secrets.token_urlsafe(32)
            now = time.time()
            rec = TrustedDeviceRecord(
                trust_id=trust_id,
                secret_hash=self._hash_secret(raw_secret),
                device_name=dev.device_name,
                created_at=now,
                last_used=now,
            )
            self._trusted[trust_id] = rec
            dev.trust_id = trust_id
            dev.pending_trust_cookie = f"{trust_id}.{raw_secret}"
            self._save_trusted()
            return dev, rec

    def claim_pending_trust_cookie(self, session_id: str) -> Optional[str]:
        """One-time claim of the persistent trust cookie by the active mobile session."""
        with self._lock:
            dev = self._sessions.get(session_id)
            if not dev or not dev.pending_trust_cookie:
                return None
            cookie_val = dev.pending_trust_cookie
            dev.pending_trust_cookie = None
            return cookie_val

    def authenticate_trust_cookie(
        self,
        cookie_value: Optional[str],
        client_ip: str,
        user_agent: str,
        network_zone: str = "lan",
    ) -> Optional[tuple[str, ConnectedDevice]]:
        """Verify persistent trust cookie and mint a fresh active session across restarts."""
        if not cookie_value or "." not in cookie_value:
            return None
        trust_id, raw_secret = cookie_value.split(".", 1)
        if not trust_id or not raw_secret:
            return None
        candidate_hash = self._hash_secret(raw_secret)

        with self._lock:
            rec = self._trusted.get(trust_id)
            if not rec or not hmac.compare_digest(candidate_hash, rec.secret_hash):
                return None

            now = time.time()
            rec.last_used = now
            self._save_trusted()

            session_id = secrets.token_hex(24)
            dev = ConnectedDevice(
                session_id=session_id,
                device_name=rec.device_name or parse_device_label(user_agent),
                client_ip=client_ip,
                user_agent=user_agent[:200],
                connected_at=now,
                last_seen=now,
                network_zone=network_zone,
                trust_id=trust_id,
            )
            self._sessions[session_id] = dev
            self._short_id_map[session_id[:12]] = session_id
            return self._sign_session_id(session_id), dev

    def revoke_trusted_device(self, trust_id: str) -> tuple[bool, list[str]]:
        """Remove a trusted device permanently and revoke any active sessions using it."""
        revoked_sids: list[str] = []
        with self._lock:
            existed = self._trusted.pop(trust_id, None) is not None
            if existed:
                self._save_trusted()
            for sid, dev in list(self._sessions.items()):
                if dev.trust_id == trust_id:
                    self._sessions.pop(sid, None)
                    self._short_id_map.pop(sid[:12], None)
                    revoked_sids.append(sid)
        return existed, revoked_sids

    def list_trusted_devices(self) -> list[dict]:
        with self._lock:
            recs = sorted(self._trusted.values(), key=lambda r: r.last_used, reverse=True)
            return [r.to_public_dict() for r in recs]
