"""Configuration management for LANShare."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "lanshare"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_TRUSTED_FILE = DEFAULT_CONFIG_DIR / "trusted_devices.json"
DEFAULT_AUTOSTART_FILE = Path.home() / ".config" / "autostart" / "lanshare.desktop"
DEFAULT_RECEIVE_DIR = Path.home() / "Downloads" / "LANShare"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_autostart_enabled(autostart_file: Path = DEFAULT_AUTOSTART_FILE) -> bool:
    return autostart_file.is_file()


def set_autostart_enabled(
    enabled: bool,
    app_dir: Path = PROJECT_ROOT,
    autostart_file: Path = DEFAULT_AUTOSTART_FILE,
) -> bool:
    try:
        if enabled:
            autostart_file.parent.mkdir(parents=True, exist_ok=True)
            entry = (
                "[Desktop Entry]\n"
                "Version=1.0\n"
                "Type=Application\n"
                "Name=VoltDeck (Standby)\n"
                "Comment=VoltDeck standby di system tray untuk transfer & remote dari iOS\n"
                f"Exec=/usr/bin/python3 {app_dir / 'lanshare.py'} --tray\n"
                "Icon=voltdeck\n"
                "Terminal=false\n"
                "Categories=Network;Utility;\n"
                "X-GNOME-Autostart-enabled=true\n"
                "X-GNOME-Autostart-Delay=4\n"
            )
            autostart_file.write_text(entry, encoding="utf-8")
            return True
        else:
            if autostart_file.exists():
                autostart_file.unlink()
            return False
    except OSError:
        return is_autostart_enabled(autostart_file)


@dataclass
class AppConfig:
    port: int = 8765
    receive_dir: str = field(default_factory=lambda: str(DEFAULT_RECEIVE_DIR))
    device_name: str = field(default_factory=lambda: socket.gethostname())
    preferred_interface: str = ""
    clipboard_auto_sync: bool = True
    qr_include_pin: bool = False
    use_mdns_qr: bool = False
    access_mode: str = "offline"  # "offline" (LAN only) | "anywhere" (LAN + Tailscale VPN)
    anywhere_allow_power: bool = True
    remote_control_enabled: bool = True
    language: str = "id"  # "id" | "en"
    theme: str = "dark"  # "dark" | "light"
    custom_pin: str = ""  # Empty until user creates or saves a persistent 6-digit PIN
    battery_guard_percent: int = 0  # 0 (off) | 90 | 95
    minimize_to_tray: bool = True
    notifications_enabled: bool = True
    window_width: int = 1080
    window_height: int = 720

    def receive_path(self) -> Path:
        path = Path(os.path.expanduser(self.receive_dir))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_FILE) -> "AppConfig":
        cfg = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
                    for k, v in data.items():
                        if k in valid_keys:
                            setattr(cfg, k, v)
            except Exception:
                pass
        # Ensure receive dir exists; gracefully fall back only if filesystem is read-only
        try:
            cfg.receive_path()
        except OSError:
            try:
                cfg.receive_dir = str(DEFAULT_RECEIVE_DIR)
                cfg.receive_path()
            except OSError:
                cfg.receive_dir = "/tmp/LANShare"
                cfg.receive_path()
        return cfg

    def save(self, path: Path = DEFAULT_CONFIG_FILE) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(path)
        except Exception:
            pass
