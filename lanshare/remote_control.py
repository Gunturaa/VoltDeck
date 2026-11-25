"""Linux Mint Remote Control: Telemetry, Brightness, Audio Sinks, MPRIS2 Media/Seek, Live Screen MJPEG, X11 Touchpad/Keys, and Power."""

from __future__ import annotations

import io
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image

try:
    import dbus  # type: ignore
except ImportError:  # pragma: no cover
    dbus = None

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None

try:
    from Xlib import X, XK, display as xdisplay  # type: ignore
    from Xlib.ext import xtest  # type: ignore
except ImportError:  # pragma: no cover
    X = None
    XK = None
    xdisplay = None
    xtest = None


class RemoteControlManager:
    """Comprehensive, zero-extra-dependency Linux Mint remote controller."""

    def __init__(
        self,
        on_state_change: Optional[Callable[[dict[str, Any]], None]] = None,
        on_notify: Optional[Callable[[str, str], None]] = None,
        command_runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._input_lock = threading.Lock()
        self._on_state_change = on_state_change
        self._on_notify = on_notify
        self._runner = command_runner or self._default_run

        self._power_timer: Optional[threading.Timer] = None
        self._scheduled_action: Optional[str] = None  # "shutdown" | "sleep" | "restart"
        self._execute_at: float = 0.0
        self._requested_by: str = ""

        # Cached brightness value for xrandr software fallback
        self._xrandr_brightness: Optional[int] = None
        # Sticky MPRIS2 player tracking (prevents jumping from Spotify -> Chrome on pause)
        self._last_mpris_bus: Optional[str] = None
        self._last_playing_buses: set[str] = set()
        # Keep Awake (Caffeine), Battery Guard alert state, and Screen Recorder subprocess
        self._inhibit_proc: Optional[subprocess.Popen] = None
        self._keep_awake: bool = False
        self._battery_guard_percent: int = 0
        self._battery_alerted: bool = False
        self._rec_proc: Optional[subprocess.Popen] = None
        self._rec_path: Optional[Path] = None
        self._rec_started_at: float = 0.0
        # Prime psutil CPU counter
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

    # =========================================================================
    # 0. Safe Curated Linux Mint App Launcher & Laptop Webcam Capture
    # =========================================================================

    APP_CATALOG: list[dict[str, Any]] = [
        {"id": "chrome", "name": "Google Chrome", "category": "Browser", "bins": ["google-chrome", "google-chrome-stable", "/opt/google/chrome/chrome"]},
        {"id": "firefox", "name": "Firefox", "category": "Browser", "bins": ["firefox"]},
        {"id": "nemo", "name": "File Manager", "category": "System", "bins": ["nemo"]},
        {"id": "terminal", "name": "Terminal", "category": "Dev", "bins": ["gnome-terminal", "xfce4-terminal", "x-terminal-emulator"]},
        {"id": "vscode", "name": "VS Code", "category": "Dev", "bins": ["code"]},
        {"id": "spotify", "name": "Spotify", "category": "Media", "bins": ["spotify"]},
        {"id": "vlc", "name": "VLC Player", "category": "Media", "bins": ["vlc"]},
        {"id": "celluloid", "name": "Celluloid", "category": "Media", "bins": ["celluloid"]},
        {"id": "rhythmbox", "name": "Rhythmbox", "category": "Media", "bins": ["rhythmbox"]},
        {"id": "libreoffice", "name": "LibreOffice", "category": "Office", "bins": ["libreoffice"]},
        {"id": "xed", "name": "Text Editor", "category": "Utility", "bins": ["xed", "gedit"]},
        {"id": "calculator", "name": "Kalkulator", "category": "Utility", "bins": ["gnome-calculator", "galculator"]},
        {"id": "settings", "name": "System Settings", "category": "System", "bins": ["cinnamon-settings"]},
        {"id": "gimp", "name": "GIMP", "category": "Creative", "bins": ["gimp"]},
        {"id": "obs", "name": "OBS Studio", "category": "Media", "bins": ["obs"]},
        {"id": "discord", "name": "Discord", "category": "Chat", "bins": ["discord"]},
        {"id": "telegram", "name": "Telegram", "category": "Chat", "bins": ["telegram-desktop"]},
    ]

    @classmethod
    def _resolve_binary(cls, candidates: list[str]) -> Optional[str]:
        for cand in candidates:
            if cand.startswith("/"):
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
            else:
                found = shutil.which(cand)
                if found:
                    return found
        return None

    def list_launchable_apps(self) -> list[dict[str, str]]:
        """Return only allowlisted desktop apps actually installed on this Linux Mint machine."""
        apps: list[dict[str, str]] = []
        for entry in self.APP_CATALOG:
            if self._resolve_binary(entry["bins"]):
                apps.append(
                    {
                        "id": entry["id"],
                        "name": entry["name"],
                        "category": entry["category"],
                    }
                )
        return apps

    def launch_app(self, app_id: str, requested_by: str = "iPhone") -> dict[str, Any]:
        """Safely launch an allowlisted desktop app without shell interpolation."""
        for entry in self.APP_CATALOG:
            if entry["id"] == app_id:
                binary = self._resolve_binary(entry["bins"])
                if not binary:
                    return {"ok": False, "error": f"{entry['name']} tidak terpasang di laptop."}
                try:
                    subprocess.Popen(
                        [binary],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    if self._on_notify:
                        self._on_notify("Aplikasi Dibuka", f"{entry['name']} dijalankan dari {requested_by}.")
                    return {"ok": True, "app": {"id": entry["id"], "name": entry["name"]}}
                except Exception as exc:
                    return {"ok": False, "error": f"Gagal membuka {entry['name']}: {exc}"}
        return {"ok": False, "error": "Aplikasi tidak ada di daftar yang diizinkan."}

    def is_webcam_available(self) -> bool:
        return Path("/dev/video0").exists() and bool(
            shutil.which("gst-launch-1.0") or shutil.which("fswebcam") or shutil.which("ffmpeg")
        )

    def capture_webcam_jpeg(self, quality: int = 82) -> Optional[bytes]:
        """Capture a single photo from laptop webcam (/dev/video0) using GStreamer/fswebcam/ffmpeg."""
        if not Path("/dev/video0").exists():
            return None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="lanshare_cam_")
            os.close(fd)
            cmds: list[list[str]] = []
            if shutil.which("gst-launch-1.0"):
                cmds.append(
                    [
                        "gst-launch-1.0", "-q",
                        "v4l2src", "device=/dev/video0", "num-buffers=1",
                        "!", "videoconvert",
                        "!", f"jpegenc", f"quality={max(40, min(95, int(quality)))}",
                        "!", "filesink", f"location={tmp_path}",
                    ]
                )
            if shutil.which("fswebcam"):
                cmds.append(["fswebcam", "-q", "--no-banner", "-r", "1280x720", tmp_path])
            if shutil.which("ffmpeg"):
                cmds.append(["ffmpeg", "-y", "-f", "video4linux2", "-i", "/dev/video0", "-frames:v", "1", tmp_path])

            for cmd in cmds:
                rc, _ = self._runner(cmd)
                if rc == 0 and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 512:
                    return Path(tmp_path).read_bytes()
        except Exception:
            pass
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return None

    @staticmethod
    def _default_run(cmd: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3.5,
                check=False,
            )
            return proc.returncode, (proc.stdout or proc.stderr or "").strip()
        except Exception as exc:
            return 1, str(exc)

    # =========================================================================
    # 1. System Telemetry (Battery, CPU, RAM, Temperature)
    # =========================================================================

    def set_battery_guard(self, percent: int) -> int:
        val = int(percent) if int(percent) in (0, 80, 85, 90, 95, 100) else 0
        with self._lock:
            self._battery_guard_percent = val
            self._battery_alerted = False
        return val

    def get_telemetry(self) -> dict[str, Any]:
        if psutil is None:
            return {"available": False}

        cpu_pct = 0.0
        ram_pct = 0.0
        ram_used_gb = 0.0
        ram_total_gb = 0.0
        disk_pct = 0.0
        disk_free_gb = 0.0
        disk_total_gb = 0.0
        battery_pct: Optional[int] = None
        battery_plugged: Optional[bool] = None
        temp_c: Optional[int] = None

        try:
            cpu_pct = round(psutil.cpu_percent(interval=None), 1)
        except Exception:
            pass

        try:
            vm = psutil.virtual_memory()
            ram_pct = round(vm.percent, 1)
            ram_used_gb = round((vm.total - vm.available) / (1024 ** 3), 1)
            ram_total_gb = round(vm.total / (1024 ** 3), 1)
        except Exception:
            pass

        try:
            du = psutil.disk_usage(str(Path.home()))
            disk_pct = round(du.percent, 1)
            disk_free_gb = round(du.free / (1024 ** 3), 1)
            disk_total_gb = round(du.total / (1024 ** 3), 1)
        except Exception:
            pass

        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                battery_pct = int(round(bat.percent))
                battery_plugged = bool(bat.power_plugged)
        except Exception:
            pass

        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ("coretemp", "k10temp", "zenpower", "acpitz", "cpu_thermal"):
                    if key in temps and temps[key]:
                        readings = [t.current for t in temps[key] if t.current and t.current > 0]
                        if readings:
                            temp_c = int(round(max(readings)))
                            break
                if temp_c is None:
                    for entries in temps.values():
                        readings = [t.current for t in entries if t.current and t.current > 0]
                        if readings:
                            temp_c = int(round(max(readings)))
                            break
        except Exception:
            pass

        guard_triggered = False
        with self._lock:
            guard_pct = self._battery_guard_percent
            if guard_pct > 0 and battery_pct is not None and battery_plugged:
                if battery_pct >= guard_pct:
                    guard_triggered = True
                    if not self._battery_alerted:
                        self._battery_alerted = True
                        if self._on_notify:
                            self._on_notify(
                                "Baterai Penuh (Battery Guard)",
                                f"Baterai laptop sudah mencapai {battery_pct}%. Silakan cabut charger agar baterai tetap awet.",
                            )
                elif battery_pct <= max(0, guard_pct - 4):
                    self._battery_alerted = False
            elif not battery_plugged:
                self._battery_alerted = False

        return {
            "available": True,
            "cpu_percent": cpu_pct,
            "ram_percent": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "disk_percent": disk_pct,
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "battery_percent": battery_pct,
            "battery_plugged": battery_plugged,
            "battery_guard_percent": guard_pct,
            "battery_guard_triggered": guard_triggered,
            "temp_c": temp_c,
        }

    # =========================================================================
    # 2. Audio Volume & Output Sink Switcher (PulseAudio / PipeWire)
    # =========================================================================

    def get_volume(self) -> dict[str, Any]:
        if shutil.which("pactl"):
            rc_v, out_v = self._runner(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
            rc_m, out_m = self._runner(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
            if rc_v == 0:
                m = re.search(r"(\d+)%", out_v)
                pct = int(m.group(1)) if m else 0
                muted = "yes" in out_m.lower() if rc_m == 0 else False
                return {"available": True, "percent": min(150, max(0, pct)), "muted": muted}
        if shutil.which("wpctl"):
            rc, out = self._runner(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
            if rc == 0:
                m = re.search(r"Volume:\s*([0-9.]+)", out)
                pct = int(round(float(m.group(1)) * 100)) if m else 0
                muted = "MUTED" in out.upper()
                return {"available": True, "percent": min(150, max(0, pct)), "muted": muted}
        return {"available": False, "percent": 0, "muted": False}

    def set_volume(self, percent: int) -> dict[str, Any]:
        clamped = max(0, min(100, int(percent)))
        if shutil.which("pactl"):
            self._runner(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
            self._runner(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{clamped}%"])
        elif shutil.which("wpctl"):
            self._runner(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
            self._runner(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{clamped}%"])
        elif shutil.which("amixer"):
            self._runner(["amixer", "-q", "sset", "Master", f"{clamped}%", "unmute"])
        return self.get_volume()

    def adjust_volume(self, delta: int) -> dict[str, Any]:
        cur = self.get_volume()
        if cur["available"]:
            return self.set_volume(cur["percent"] + int(delta))
        sign = "+" if delta >= 0 else "-"
        step = abs(int(delta))
        if shutil.which("pactl"):
            self._runner(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{step}%"])
        return self.get_volume()

    def toggle_mute(self) -> dict[str, Any]:
        if shutil.which("pactl"):
            self._runner(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
        elif shutil.which("wpctl"):
            self._runner(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
        elif shutil.which("amixer"):
            self._runner(["amixer", "-q", "sset", "Master", "toggle"])
        return self.get_volume()

    def list_audio_sinks(self) -> list[dict[str, Any]]:
        if not shutil.which("pactl"):
            return []
        rc_def, def_sink = self._runner(["pactl", "get-default-sink"])
        default_name = def_sink.strip() if rc_def == 0 else ""

        rc, out = self._runner(["pactl", "list", "sinks"])
        if rc != 0 or not out:
            return []

        sinks: list[dict[str, Any]] = []
        cur_name = ""
        cur_desc = ""
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sink #"):
                if cur_name:
                    sinks.append(
                        {
                            "name": cur_name,
                            "description": cur_desc or cur_name,
                            "active": cur_name == default_name,
                        }
                    )
                cur_name = ""
                cur_desc = ""
            elif stripped.startswith("Name:"):
                cur_name = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Description:"):
                cur_desc = stripped.split(":", 1)[1].strip()
        if cur_name:
            sinks.append(
                {
                    "name": cur_name,
                    "description": cur_desc or cur_name,
                    "active": cur_name == default_name,
                }
            )
        return sinks

    def set_audio_sink(self, sink_name: str) -> dict[str, Any]:
        if not sink_name or not shutil.which("pactl"):
            return {"ok": False, "error": "Output audio tidak tersedia."}
        # Verify sink exists in current list to avoid arbitrary argument injection
        sinks = self.list_audio_sinks()
        valid_names = {s["name"] for s in sinks}
        if sink_name not in valid_names:
            return {"ok": False, "error": "Perangkat output audio tidak ditemukan."}

        rc, err = self._runner(["pactl", "set-default-sink", sink_name])
        if rc != 0:
            return {"ok": False, "error": err or "Gagal mengganti output audio."}

        # Move currently playing streams (YouTube, Spotify, etc.) immediately to the new sink
        rc_in, out_in = self._runner(["pactl", "list", "short", "sink-inputs"])
        if rc_in == 0 and out_in:
            for line in out_in.splitlines():
                parts = line.split()
                if parts and parts[0].isdigit():
                    self._runner(["pactl", "move-sink-input", parts[0], sink_name])

        return {"ok": True, "sinks": self.list_audio_sinks(), "volume": self.get_volume()}

    def list_audio_streams(self) -> list[dict[str, Any]]:
        """List active per-app PulseAudio/PipeWire audio streams (Spotify, Chrome, VLC, Discord)."""
        if not shutil.which("pactl"):
            return []
        rc, out = self._runner(["pactl", "list", "sink-inputs"])
        if rc != 0 or not out:
            return []
        streams: list[dict[str, Any]] = []
        cur_id: Optional[int] = None
        cur_muted = False
        cur_vol = 100
        cur_app = ""
        cur_media = ""

        def _flush() -> None:
            if cur_id is not None:
                label = cur_app or cur_media or f"App #{cur_id}"
                streams.append(
                    {
                        "id": cur_id,
                        "name": label[:42],
                        "volume": min(150, max(0, cur_vol)),
                        "muted": cur_muted,
                    }
                )

        for line in out.splitlines():
            s = line.strip()
            m_id = re.match(r"^Sink Input #(\d+)", s)
            if m_id:
                _flush()
                cur_id = int(m_id.group(1))
                cur_muted = False
                cur_vol = 100
                cur_app = ""
                cur_media = ""
            elif s.startswith("Mute:"):
                cur_muted = "yes" in s.lower()
            elif s.startswith("Volume:"):
                mv = re.search(r"(\d+)%", s)
                if mv:
                    cur_vol = int(mv.group(1))
            elif s.startswith("application.name ="):
                cur_app = s.split("=", 1)[1].strip().strip('"')
            elif s.startswith("media.name ="):
                cur_media = s.split("=", 1)[1].strip().strip('"')
        _flush()
        return streams

    def set_audio_stream(
        self,
        stream_id: int,
        percent: Optional[int] = None,
        mute_toggle: bool = False,
    ) -> dict[str, Any]:
        """Set per-app volume or mute for an active audio stream ID."""
        if not shutil.which("pactl"):
            return {"ok": False, "error": "pactl tidak tersedia."}
        sid = int(stream_id)
        current = self.list_audio_streams()
        if sid not in {s["id"] for s in current}:
            return {"ok": False, "error": "Aplikasi audio sudah tidak aktif."}
        if mute_toggle:
            self._runner(["pactl", "set-sink-input-mute", str(sid), "toggle"])
        if percent is not None:
            clamped = max(0, min(100, int(percent)))
            self._runner(["pactl", "set-sink-input-mute", str(sid), "0"])
            self._runner(["pactl", "set-sink-input-volume", str(sid), f"{clamped}%"])
        return {"ok": True, "streams": self.list_audio_streams()}

    # =========================================================================
    # 3. Screen Brightness & Display Off
    # =========================================================================

    def _get_cinnamon_brightness_iface(self) -> Any:
        if dbus is None:
            return None
        try:
            bus = dbus.SessionBus()
            obj = bus.get_object("org.cinnamon.SettingsDaemon.Power", "/org/cinnamon/SettingsDaemon/Power")
            return dbus.Interface(obj, "org.cinnamon.SettingsDaemon.Power.Screen")
        except Exception:
            return None

    def _get_sysfs_backlight_dir(self) -> Optional[Path]:
        base = Path("/sys/class/backlight")
        if not base.is_dir():
            return None
        for entry in sorted(base.iterdir()):
            if (entry / "brightness").is_file() and (entry / "max_brightness").is_file():
                return entry
        return None

    def _get_primary_xrandr_output(self) -> Optional[str]:
        if not shutil.which("xrandr"):
            return None
        rc, out = self._runner(["xrandr", "--query"])
        if rc != 0:
            return None
        fallback = None
        for line in out.splitlines():
            if " connected primary" in line:
                return line.split()[0]
            if " connected" in line and fallback is None:
                fallback = line.split()[0]
        return fallback

    def get_brightness(self) -> dict[str, Any]:
        # 1. Cinnamon Power D-Bus (works cleanly without sudo on Linux Mint!)
        iface = self._get_cinnamon_brightness_iface()
        if iface is not None:
            try:
                pct = int(iface.GetPercentage())
                return {"available": True, "percent": max(5, min(100, pct)), "backend": "cinnamon"}
            except Exception:
                pass

        # 2. Read sysfs backlight
        bl = self._get_sysfs_backlight_dir()
        if bl is not None:
            try:
                cur = int((bl / "brightness").read_text().strip())
                mx = max(1, int((bl / "max_brightness").read_text().strip()))
                pct = int(round((cur / mx) * 100))
                return {"available": True, "percent": max(5, min(100, pct)), "backend": "sysfs"}
            except Exception:
                pass

        # 3. xrandr software fallback
        if self._get_primary_xrandr_output():
            return {
                "available": True,
                "percent": self._xrandr_brightness if self._xrandr_brightness is not None else 100,
                "backend": "xrandr",
            }
        return {"available": False, "percent": 100, "backend": "none"}

    def set_brightness(self, percent: int) -> dict[str, Any]:
        clamped = max(10, min(100, int(percent)))

        # 1. Cinnamon D-Bus (native hardware backlight, zero sudo!)
        iface = self._get_cinnamon_brightness_iface()
        if iface is not None:
            try:
                iface.SetPercentage(dbus.UInt32(clamped))
                return {"available": True, "percent": clamped, "backend": "cinnamon"}
            except Exception:
                pass

        # 2. brightnessctl if installed
        if shutil.which("brightnessctl"):
            rc, _ = self._runner(["brightnessctl", "set", f"{clamped}%"])
            if rc == 0:
                return {"available": True, "percent": clamped, "backend": "brightnessctl"}

        # 3. Direct sysfs write if user has video group permission
        bl = self._get_sysfs_backlight_dir()
        if bl is not None:
            try:
                mx = max(1, int((bl / "max_brightness").read_text().strip()))
                val = max(1, int(round((clamped / 100.0) * mx)))
                (bl / "brightness").write_text(str(val))
                return {"available": True, "percent": clamped, "backend": "sysfs"}
            except OSError:
                pass

        # 4. xrandr software brightness fallback (works everywhere on X11)
        out_name = self._get_primary_xrandr_output()
        if out_name:
            ratio = round(clamped / 100.0, 2)
            rc, _ = self._runner(["xrandr", "--output", out_name, "--brightness", f"{ratio:.2f}"])
            if rc == 0:
                self._xrandr_brightness = clamped
                return {"available": True, "percent": clamped, "backend": "xrandr"}

        return self.get_brightness()

    def display_off(self, requested_by: str = "iPhone") -> dict[str, Any]:
        if not shutil.which("xset"):
            return {"ok": False, "error": "Perintah xset tidak tersedia."}
        rc, err = self._runner(["xset", "dpms", "force", "off"])
        if rc == 0:
            if self._on_notify:
                self._on_notify("Layar Dimatikan", f"Display off diminta dari {requested_by}.")
            return {"ok": True}
        return {"ok": False, "error": err or "Gagal mematikan layar."}

    # =========================================================================
    # 4. Media Controls & Seek ±10s via Session D-Bus MPRIS2
    # =========================================================================

    def _list_mpris_bus_names(self) -> list[str]:
        if dbus is None:
            return []
        try:
            bus = dbus.SessionBus()
            names = bus.list_names()
            return [str(n) for n in names if str(n).startswith("org.mpris.MediaPlayer2.")]
        except Exception:
            return []

    @staticmethod
    def _friendly_player_name(bus_name: str, identity: str = "") -> str:
        ident = (identity or "").strip()
        if ident:
            return ident
        raw = bus_name.replace("org.mpris.MediaPlayer2.", "").split(".")[0].lower()
        aliases = {
            "chromium": "Chrome",
            "chrome": "Chrome",
            "google-chrome": "Chrome",
            "firefox": "Firefox",
            "spotify": "Spotify",
            "vlc": "VLC",
            "celluloid": "Celluloid",
            "rhythmbox": "Rhythmbox",
            "brave": "Brave",
            "edge": "Edge",
        }
        return aliases.get(raw, raw.capitalize() or "Media")

    def _read_all_mpris_players(self) -> list[dict[str, Any]]:
        players = self._list_mpris_bus_names()
        if not players or dbus is None:
            return []
        bus = dbus.SessionBus()
        items: list[dict[str, Any]] = []
        for name in players:
            try:
                obj = bus.get_object(name, "/org/mpris/MediaPlayer2")
                props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
                status = str(props.Get("org.mpris.MediaPlayer2.Player", "PlaybackStatus"))
                meta = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
                identity = ""
                try:
                    identity = str(props.Get("org.mpris.MediaPlayer2", "Identity"))
                except Exception:
                    identity = ""
                title = str(meta.get("xesam:title", "")) if meta else ""
                artists = meta.get("xesam:artist", []) if meta else []
                artist = ", ".join(str(a) for a in artists) if artists else ""
                items.append(
                    {
                        "available": True,
                        "bus_name": name,
                        "player": self._friendly_player_name(name, identity),
                        "status": status,
                        "title": title,
                        "artist": artist,
                    }
                )
            except Exception:
                continue
        return items

    def _invoke_mpris_command(self, target_bus: str, action: str, seek_seconds: int = 0) -> None:
        bus = dbus.SessionBus()
        obj = bus.get_object(target_bus, "/org/mpris/MediaPlayer2")
        player_iface = dbus.Interface(obj, "org.mpris.MediaPlayer2.Player")
        if action == "seek":
            offset_us = dbus.Int64(int(seek_seconds) * 1_000_000)
            player_iface.Seek(offset_us)
            return
        method_map = {
            "play_pause": "PlayPause",
            "play": "Play",
            "pause": "Pause",
            "next": "Next",
            "previous": "Previous",
            "stop": "Stop",
        }
        method = method_map.get(action)
        if not method:
            raise ValueError("Aksi media tidak dikenal.")
        getattr(player_iface, method)()

    def get_media_status(self, preferred_bus: Optional[str] = None) -> dict[str, Any]:
        all_players = self._read_all_mpris_players()
        if not all_players:
            with self._lock:
                self._last_playing_buses.clear()
                self._last_mpris_bus = None
            return {
                "available": False,
                "bus_name": "",
                "player": "",
                "status": "Stopped",
                "title": "",
                "artist": "",
                "players": [],
            }

        by_bus = {p["bus_name"]: p for p in all_players}
        playing_now = {p["bus_name"] for p in all_players if p["status"] == "Playing"}

        with self._lock:
            newly_playing = [
                p for p in all_players
                if p["status"] == "Playing" and p["bus_name"] not in self._last_playing_buses
            ]
            self._last_playing_buses = playing_now

            chosen: Optional[dict[str, Any]] = None
            # 1. Explicit target player requested (chip click or targeted control)
            if preferred_bus and preferred_bus in by_bus:
                chosen = by_bus[preferred_bus]
            # 2. A player just transitioned into Playing on the laptop -> auto-follow it!
            elif newly_playing:
                chosen = newly_playing[0]
            # 3. Stay sticky on the last active/controlled player (e.g. Spotify right after Pause!)
            elif self._last_mpris_bus and self._last_mpris_bus in by_bus:
                chosen = by_bus[self._last_mpris_bus]
            # 4. Initial selection fallback: Playing -> Paused with metadata -> first available
            else:
                for p in all_players:
                    if p["status"] == "Playing":
                        chosen = p
                        break
                if chosen is None:
                    for p in all_players:
                        if p.get("title"):
                            chosen = p
                            break
                if chosen is None:
                    chosen = all_players[0]

            self._last_mpris_bus = chosen["bus_name"]

        result = dict(chosen)
        result["players"] = [
            {
                "bus_name": p["bus_name"],
                "player": p["player"],
                "status": p["status"],
                "title": p["title"],
                "active": p["bus_name"] == chosen["bus_name"],
            }
            for p in all_players
        ]
        return result

    def media_action(
        self,
        action: str,
        seek_seconds: int = 0,
        bus_name: Optional[str] = None,
    ) -> dict[str, Any]:
        if action == "select":
            status = self.get_media_status(preferred_bus=bus_name)
            if not status.get("available"):
                return {"ok": False, "error": "Tidak ada pemutar media aktif (YouTube/Spotify/VLC)."}
            return {"ok": True, "media": status}

        if action not in ("play_pause", "play", "pause", "next", "previous", "stop", "seek"):
            return {"ok": False, "error": "Aksi media tidak dikenal."}

        status = self.get_media_status(preferred_bus=bus_name)
        target_bus = status.get("bus_name")
        if not target_bus:
            return {"ok": False, "error": "Tidak ada pemutar media aktif (YouTube/Spotify/VLC)."}

        with self._lock:
            self._last_mpris_bus = target_bus
        prev_sig = (status.get("status"), status.get("title"))

        try:
            self._invoke_mpris_command(target_bus, action, seek_seconds=seek_seconds)
            # Briefly wait for Spotify/Chrome MPRIS D-Bus to reflect the updated status/title
            updated = status
            checks = 1 if action == "seek" else 7
            for _ in range(checks):
                time.sleep(0.045)
                updated = self.get_media_status(preferred_bus=target_bus)
                if (updated.get("status"), updated.get("title")) != prev_sig:
                    break
            return {"ok": True, "media": updated}
        except Exception as exc:
            return {"ok": False, "error": f"Gagal mengontrol media: {exc}"}

    # =========================================================================
    # 5. Live Screen Capture (X11 ZPixmap -> JPEG for MJPEG Stream & Snapshot)
    # =========================================================================

    def capture_screen_jpeg(self, max_width: int = 960, quality: int = 62) -> Optional[bytes]:
        """Capture current Linux Mint desktop screen as compressed JPEG bytes."""
        # Fast direct X11 capture via python3-xlib (thread-safe per connection, ~25-45ms!)
        if xdisplay is not None and X is not None:
            dpy = None
            try:
                dpy = xdisplay.Display()
                root = dpy.screen().root
                geom = root.get_geometry()
                w, h = int(geom.width), int(geom.height)
                if w > 0 and h > 0:
                    raw = root.get_image(0, 0, w, h, X.ZPixmap, 0xFFFFFFFF)
                    img = Image.frombytes("RGB", (w, h), raw.data, "raw", "BGRX")
                    if max_width > 0 and w > max_width:
                        new_h = max(1, int(round(h * (max_width / w))))
                        img = img.resize((max_width, new_h), Image.Resampling.BILINEAR)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=max(25, min(92, int(quality))), optimize=False)
                    return buf.getvalue()
            except Exception:
                pass
            finally:
                if dpy is not None:
                    try:
                        dpy.close()
                    except Exception:
                        pass

        # Fallback: gnome-screenshot to temp file
        if shutil.which("gnome-screenshot"):
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="lanshare_scr_")
                os.close(fd)
                rc, _ = self._runner(["gnome-screenshot", "-f", tmp_path])
                if rc == 0 and os.path.exists(tmp_path):
                    with Image.open(tmp_path) as img:
                        img = img.convert("RGB")
                        if max_width > 0 and img.width > max_width:
                            new_h = max(1, int(round(img.height * (max_width / img.width))))
                            img = img.resize((max_width, new_h), Image.Resampling.BILINEAR)
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=max(25, min(92, int(quality))))
                        return buf.getvalue()
            except Exception:
                pass
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        return None

    # =========================================================================
    # 6. X11 Touchpad, Tap-to-Click, Presentation Clicker, & Keyboard Typing
    # =========================================================================

    def _with_xtest_display(self, fn: Callable[[Any], None]) -> dict[str, Any]:
        if xdisplay is None or xtest is None or X is None:
            return {"ok": False, "error": "X11 input (python3-xlib) tidak tersedia."}
        with self._input_lock:
            dpy = None
            try:
                dpy = xdisplay.Display()
                fn(dpy)
                dpy.sync()
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": f"Gagal mengirim input ke layar: {exc}"}
            finally:
                if dpy is not None:
                    try:
                        dpy.close()
                    except Exception:
                        pass

    def move_mouse_relative(self, dx: int, dy: int) -> dict[str, Any]:
        dx_c = max(-500, min(500, int(dx)))
        dy_c = max(-500, min(500, int(dy)))

        def _do(dpy: Any) -> None:
            xtest.fake_input(dpy, X.MotionNotify, detail=True, x=dx_c, y=dy_c)

        return self._with_xtest_display(_do)

    def click_at_ratio(self, x_ratio: float, y_ratio: float, button: str = "left") -> dict[str, Any]:
        xr = max(0.0, min(1.0, float(x_ratio)))
        yr = max(0.0, min(1.0, float(y_ratio)))
        btn_code = {"left": 1, "middle": 2, "right": 3}.get(button, 1)

        def _do(dpy: Any) -> None:
            geom = dpy.screen().root.get_geometry()
            tx = int(round(xr * max(1, geom.width - 1)))
            ty = int(round(yr * max(1, geom.height - 1)))
            xtest.fake_input(dpy, X.MotionNotify, x=tx, y=ty)
            xtest.fake_input(dpy, X.ButtonPress, btn_code)
            xtest.fake_input(dpy, X.ButtonRelease, btn_code)

        return self._with_xtest_display(_do)

    def mouse_click(self, button: str = "left", double: bool = False) -> dict[str, Any]:
        btn_code = {"left": 1, "middle": 2, "right": 3}.get(button, 1)

        def _do(dpy: Any) -> None:
            count = 2 if double else 1
            for _ in range(count):
                xtest.fake_input(dpy, X.ButtonPress, btn_code)
                xtest.fake_input(dpy, X.ButtonRelease, btn_code)

        return self._with_xtest_display(_do)

    def mouse_scroll(self, delta_y: int) -> dict[str, Any]:
        steps = max(-10, min(10, int(delta_y)))
        if steps == 0:
            return {"ok": True}
        btn_code = 4 if steps > 0 else 5  # X11: 4 = Scroll Up, 5 = Scroll Down

        def _do(dpy: Any) -> None:
            for _ in range(abs(steps)):
                xtest.fake_input(dpy, X.ButtonPress, btn_code)
                xtest.fake_input(dpy, X.ButtonRelease, btn_code)

        return self._with_xtest_display(_do)

    def press_key(self, key_name: str) -> dict[str, Any]:
        if XK is None:
            return {"ok": False, "error": "XK tidak tersedia."}
        key_map = {
            "left": "Left",
            "right": "Right",
            "up": "Up",
            "down": "Down",
            "space": "space",
            "enter": "Return",
            "backspace": "BackSpace",
            "escape": "Escape",
            "tab": "Tab",
            "f5": "F5",
            "f11": "F11",
            "fullscreen": "f",
            "page_up": "Prior",
            "page_down": "Next",
        }
        sym_name = key_map.get(key_name.lower())
        if not sym_name:
            return {"ok": False, "error": "Tombol tidak dikenal."}

        def _do(dpy: Any) -> None:
            keysym = XK.string_to_keysym(sym_name)
            keycode = dpy.keysym_to_keycode(keysym)
            if not keycode:
                raise RuntimeError(f"Keycode tidak ditemukan untuk {sym_name}")
            xtest.fake_input(dpy, X.KeyPress, keycode)
            xtest.fake_input(dpy, X.KeyRelease, keycode)

        return self._with_xtest_display(_do)

    def type_text(self, text: str) -> dict[str, Any]:
        if XK is None:
            return {"ok": False, "error": "XK tidak tersedia."}
        cleaned = str(text or "")[:300]
        if not cleaned:
            return {"ok": True}

        special_syms = {
            " ": "space",
            "\n": "Return",
            "\t": "Tab",
            ".": "period",
            ",": "comma",
            "/": "slash",
            ":": "colon",
            ";": "semicolon",
            "-": "minus",
            "_": "underscore",
            "=": "equal",
            "+": "plus",
            "?": "question",
            "!": "exclam",
            "@": "at",
            "#": "numbersign",
            "$": "dollar",
            "%": "percent",
            "&": "ampersand",
            "*": "asterisk",
            "(": "parenleft",
            ")": "parenright",
            "'": "apostrophe",
            '"': "quotedbl",
        }
        shifted_chars = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()_+{}|:"<>?~')

        def _do(dpy: Any) -> None:
            shift_code = dpy.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
            for ch in cleaned:
                sym_name = special_syms.get(ch, ch)
                keysym = XK.string_to_keysym(sym_name)
                if not keysym:
                    continue
                keycode = dpy.keysym_to_keycode(keysym)
                if not keycode:
                    continue
                need_shift = ch in shifted_chars
                if need_shift and shift_code:
                    xtest.fake_input(dpy, X.KeyPress, shift_code)
                xtest.fake_input(dpy, X.KeyPress, keycode)
                xtest.fake_input(dpy, X.KeyRelease, keycode)
                if need_shift and shift_code:
                    xtest.fake_input(dpy, X.KeyRelease, shift_code)

        return self._with_xtest_display(_do)

    # =========================================================================
    # 7. Lock Screen, Sleep, Restart & Cancelable Shutdown
    # =========================================================================

    def lock_screen(self, requested_by: str = "iPhone") -> dict[str, Any]:
        cmds = []
        if shutil.which("cinnamon-screensaver-command"):
            cmds.append(["cinnamon-screensaver-command", "-l"])
        if shutil.which("loginctl"):
            cmds.append(["loginctl", "lock-session"])
        for cmd in cmds:
            rc, _ = self._runner(cmd)
            if rc == 0:
                if self._on_notify:
                    self._on_notify("Layar Dikunci", f"Dikunci dari jauh oleh {requested_by}.")
                return {"ok": True, "action": "lock"}
        return {"ok": False, "error": "Perintah kunci layar tidak tersedia."}

    def get_power_state(self) -> dict[str, Any]:
        with self._lock:
            if not self._scheduled_action:
                return {
                    "scheduled": False,
                    "action": None,
                    "remaining_seconds": 0,
                    "execute_at": 0,
                    "requested_by": "",
                }
            rem = max(0, int(round(self._execute_at - time.time())))
            return {
                "scheduled": True,
                "action": self._scheduled_action,
                "remaining_seconds": rem,
                "execute_at": self._execute_at,
                "requested_by": self._requested_by,
            }

    def schedule_power_action(
        self,
        action: str,
        delay_seconds: int = 15,
        requested_by: str = "iPhone",
    ) -> dict[str, Any]:
        if action not in ("shutdown", "sleep", "restart"):
            return {"ok": False, "error": "Aksi daya harus 'shutdown', 'restart', atau 'sleep'."}

        delay = max(5, min(4 * 3600, int(delay_seconds)))
        with self._lock:
            if self._power_timer is not None:
                self._power_timer.cancel()
                self._power_timer = None
            self._scheduled_action = action
            self._execute_at = time.time() + delay
            self._requested_by = requested_by
            timer = threading.Timer(delay, self._execute_power_action, args=(action,))
            timer.daemon = True
            self._power_timer = timer
            timer.start()

        labels = {
            "shutdown": "Mematikan Laptop",
            "restart": "Restart Laptop",
            "sleep": "Mode Sleep Laptop",
        }
        time_txt = f"{delay} detik" if delay < 120 else f"{delay // 60} menit"
        if self._on_notify:
            self._on_notify(
                f"{labels[action]} Dijadwalkan",
                f"Diminta dari {requested_by} dalam {time_txt}. Klik Batalkan di LANShare untuk membatalkan.",
            )
        state = self.get_power_state()
        self._emit_state()
        return {"ok": True, "power": state}

    def cancel_power_action(self) -> dict[str, Any]:
        with self._lock:
            was_active = self._scheduled_action is not None
            if self._power_timer is not None:
                self._power_timer.cancel()
                self._power_timer = None
            self._scheduled_action = None
            self._execute_at = 0.0
            self._requested_by = ""
        if was_active and self._on_notify:
            self._on_notify("Jadwal Daya Dibatalkan", "Laptop tidak jadi dimatikan / restart / sleep.")
        state = self.get_power_state()
        self._emit_state()
        return {"ok": True, "cancelled": was_active, "power": state}

    def _execute_power_action(self, action: str) -> None:
        with self._lock:
            if self._scheduled_action != action:
                return
            self._power_timer = None
            self._scheduled_action = None
            self._execute_at = 0.0
        self._emit_state()

        if action == "shutdown":
            rc, _ = self._runner(["systemctl", "poweroff"])
            if rc != 0:
                self._runner(
                    [
                        "dbus-send", "--system", "--print-reply",
                        "--dest=org.freedesktop.login1", "/org/freedesktop/login1",
                        "org.freedesktop.login1.Manager.PowerOff", "boolean:true",
                    ]
                )
        elif action == "restart":
            rc, _ = self._runner(["systemctl", "reboot"])
            if rc != 0:
                self._runner(
                    [
                        "dbus-send", "--system", "--print-reply",
                        "--dest=org.freedesktop.login1", "/org/freedesktop/login1",
                        "org.freedesktop.login1.Manager.Reboot", "boolean:true",
                    ]
                )
        elif action == "sleep":
            rc, _ = self._runner(["systemctl", "suspend"])
            if rc != 0:
                self._runner(
                    [
                        "dbus-send", "--system", "--print-reply",
                        "--dest=org.freedesktop.login1", "/org/freedesktop/login1",
                        "org.freedesktop.login1.Manager.Suspend", "boolean:true",
                    ]
                )

    def get_live_status(self) -> dict[str, Any]:
        """Lightweight live sync payload for active Remote tab (telemetry + media + volume + brightness + record)."""
        return {
            "telemetry": self.get_telemetry(),
            "media": self.get_media_status(),
            "volume": self.get_volume(),
            "brightness": self.get_brightness(),
            "keep_awake": self.get_keep_awake(),
            "recording": self.get_screen_record_status(),
        }

    def get_full_status(self, enabled: bool = True) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "telemetry": self.get_telemetry(),
            "volume": self.get_volume(),
            "sinks": self.list_audio_sinks(),
            "audio_streams": self.list_audio_streams(),
            "brightness": self.get_brightness(),
            "media": self.get_media_status(),
            "apps": self.list_launchable_apps(),
            "keep_awake": self.get_keep_awake(),
            "recording": self.get_screen_record_status(),
            "webcam_available": self.is_webcam_available(),
            "power": self.get_power_state(),
        }

    # =========================================================================
    # 7B. Window Switcher, Smart Shortcuts, Keep Awake (Caffeine), Bluetooth & Recorder
    # =========================================================================

    def list_open_windows(self) -> list[dict[str, Any]]:
        """On-demand list of open desktop windows via wmctrl."""
        if not shutil.which("wmctrl"):
            return []
        rc, out = self._runner(["wmctrl", "-lp"])
        if rc != 0 or not out:
            return []
        windows: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            win_id, desktop, pid_str, _host, title = parts
            if desktop == "-1":
                continue
            if not re.match(r"^0x[0-9a-fA-F]+$", win_id):
                continue
            clean_title = title.strip()
            if not clean_title or clean_title.lower() in ("desktop", "cinnamon"):
                continue
            windows.append(
                {
                    "id": win_id,
                    "pid": int(pid_str) if pid_str.isdigit() else 0,
                    "title": clean_title[:72],
                }
            )
        return windows[:20]

    def window_action(self, win_id: str, action: str) -> dict[str, Any]:
        """Focus, maximize, fullscreen, or close an open desktop window safely."""
        if not shutil.which("wmctrl"):
            return {"ok": False, "error": "wmctrl tidak terpasang."}
        if not re.match(r"^0x[0-9a-fA-F]+$", str(win_id or "")):
            return {"ok": False, "error": "ID jendela tidak valid."}
        if action == "focus":
            rc, err = self._runner(["wmctrl", "-i", "-a", win_id])
        elif action == "close":
            rc, err = self._runner(["wmctrl", "-i", "-c", win_id])
        elif action == "maximize":
            rc, err = self._runner(["wmctrl", "-i", "-r", win_id, "-b", "toggle,maximized_vert,maximized_horz"])
        elif action == "fullscreen":
            rc, err = self._runner(["wmctrl", "-i", "-r", win_id, "-b", "toggle,fullscreen"])
        else:
            return {"ok": False, "error": "Aksi jendela tidak dikenal."}
        if rc != 0:
            return {"ok": False, "error": err or "Gagal mengatur jendela."}
        time.sleep(0.08)
        return {"ok": True, "windows": self.list_open_windows()}

    def quick_shortcut(self, name: str) -> dict[str, Any]:
        """Execute common Linux desktop shortcuts cleanly."""
        if name == "show_desktop":
            if shutil.which("wmctrl"):
                rc, out = self._runner(["wmctrl", "-m"])
                is_showing = rc == 0 and "showing the desktop\" mode: on" in out.lower()
                self._runner(["wmctrl", "-k", "off" if is_showing else "on"])
                return {"ok": True, "shortcut": name}
            return {"ok": False, "error": "wmctrl tidak tersedia."}
        if name == "fullscreen":
            return self.press_key("F11")
        if name == "alt_tab":
            if xdisplay is None or xtest is None or X is None or XK is None:
                return {"ok": False, "error": "X11 input tidak tersedia."}
            def _do(dpy: Any) -> None:
                alt_code = dpy.keysym_to_keycode(XK.XK_Alt_L)
                tab_code = dpy.keysym_to_keycode(XK.XK_Tab)
                xtest.fake_input(dpy, X.KeyPress, alt_code)
                xtest.fake_input(dpy, X.KeyPress, tab_code)
                xtest.fake_input(dpy, X.KeyRelease, tab_code)
                xtest.fake_input(dpy, X.KeyRelease, alt_code)
            return self._with_xtest_display(_do)
        return {"ok": False, "error": "Shortcut tidak dikenal."}

    def get_keep_awake(self) -> dict[str, Any]:
        with self._lock:
            if self._inhibit_proc is not None and self._inhibit_proc.poll() is not None:
                self._inhibit_proc = None
                self._keep_awake = False
            return {"enabled": self._keep_awake}

    def set_keep_awake(self, enabled: bool, requested_by: str = "Phone") -> dict[str, Any]:
        """Toggle Caffeine / Keep Awake via systemd-inhibit + xset (0% CPU)."""
        with self._lock:
            if enabled:
                if self._inhibit_proc is None or self._inhibit_proc.poll() is not None:
                    if shutil.which("systemd-inhibit"):
                        try:
                            self._inhibit_proc = subprocess.Popen(
                                [
                                    "systemd-inhibit",
                                    "--what=idle:sleep",
                                    "--who=VoltDeck",
                                    "--why=Keep Awake enabled from VoltDeck",
                                    "sleep",
                                    "86400",
                                ],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True,
                            )
                        except Exception:
                            self._inhibit_proc = None
                if shutil.which("xset"):
                    self._runner(["xset", "s", "off", "-dpms"])
                self._keep_awake = True
                if self._on_notify:
                    self._on_notify("Keep Awake Aktif", f"Laptop ditahan tetap menyala dari {requested_by}.")
            else:
                if self._inhibit_proc is not None:
                    try:
                        self._inhibit_proc.terminate()
                        self._inhibit_proc.wait(timeout=1.5)
                    except Exception:
                        try:
                            self._inhibit_proc.kill()
                        except Exception:
                            pass
                    self._inhibit_proc = None
                if shutil.which("xset"):
                    self._runner(["xset", "s", "on", "+dpms"])
                self._keep_awake = False
        return {"ok": True, "enabled": self._keep_awake}

    def list_bluetooth_devices(self) -> dict[str, Any]:
        """On-demand Bluetooth controller & paired device status via bluetoothctl."""
        if not shutil.which("bluetoothctl"):
            return {"available": False, "powered": False, "devices": []}
        rc_s, out_s = self._runner(["bluetoothctl", "show"])
        if rc_s != 0 or "No default controller" in out_s:
            return {"available": False, "powered": False, "devices": []}
        powered = "Powered: yes" in out_s

        rc_d, out_d = self._runner(["bluetoothctl", "devices"])
        devices: list[dict[str, Any]] = []
        if rc_d == 0 and out_d:
            for line in out_d.splitlines():
                m = re.match(r"^Device\s+([0-9A-Fa-f:]{17})\s+(.+)$", line.strip())
                if not m:
                    continue
                mac, name = m.group(1).upper(), m.group(2).strip()
                rc_i, out_i = self._runner(["bluetoothctl", "info", mac])
                connected = rc_i == 0 and ("Connected: yes" in out_i)
                paired = rc_i == 0 and ("Paired: yes" in out_i or "Trusted: yes" in out_i)
                if not paired and not connected:
                    continue
                icon = "device"
                mi = re.search(r"Icon:\s*(\S+)", out_i) if rc_i == 0 else None
                if mi:
                    icon = mi.group(1).lower()
                devices.append({"mac": mac, "name": name[:40], "connected": connected, "icon": icon})
        devices.sort(key=lambda d: (not d["connected"], d["name"].lower()))
        return {"available": True, "powered": powered, "devices": devices}

    def bluetooth_action(
        self,
        action: str,
        mac: str = "",
        power: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Toggle Bluetooth power or connect/disconnect a paired device safely."""
        if not shutil.which("bluetoothctl"):
            return {"ok": False, "error": "bluetoothctl tidak tersedia."}
        if action == "power" or power is not None:
            state_str = "on" if bool(power) else "off"
            rc, err = self._runner(["bluetoothctl", "power", state_str])
            if rc != 0:
                return {"ok": False, "error": err or "Gagal mengubah daya Bluetooth."}
            time.sleep(0.25)
            return {"ok": True, **self.list_bluetooth_devices()}

        clean_mac = str(mac or "").strip().upper()
        if not re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", clean_mac):
            return {"ok": False, "error": "Alamat MAC Bluetooth tidak valid."}
        if action not in ("connect", "disconnect"):
            return {"ok": False, "error": "Aksi Bluetooth tidak dikenal."}
        try:
            proc = subprocess.run(
                ["bluetoothctl", action, clean_mac],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
            if proc.returncode != 0 and "successful" not in (proc.stdout or "").lower():
                msg = (proc.stderr or proc.stdout or "").strip() or f"Gagal {action} Bluetooth."
                return {"ok": False, "error": msg[:140]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Perangkat Bluetooth tidak merespons (pastikan menyala)."}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **self.list_bluetooth_devices()}

    def is_screen_recorder_available(self) -> bool:
        return bool(shutil.which("ffmpeg") or shutil.which("gst-launch-1.0"))

    def get_screen_record_status(self) -> dict[str, Any]:
        with self._lock:
            recording = self._rec_proc is not None and self._rec_proc.poll() is None
            if not recording and self._rec_proc is not None:
                self._rec_proc = None
            duration = int(max(0, time.time() - self._rec_started_at)) if recording else 0
            fname = self._rec_path.name if (recording and self._rec_path) else ""
            return {
                "available": self.is_screen_recorder_available(),
                "recording": recording,
                "duration_sec": duration,
                "filename": fname,
            }

    def start_screen_recording(self, output_dir: Path, requested_by: str = "Phone") -> dict[str, Any]:
        """Start lightweight on-demand laptop screen recording (ffmpeg MP4 or native GStreamer WebM)."""
        if not self.is_screen_recorder_available():
            return {"ok": False, "error": "Perekam layar (ffmpeg/gst-launch-1.0) tidak tersedia."}
        with self._lock:
            if self._rec_proc is not None and self._rec_proc.poll() is None:
                return {"ok": True, **self.get_screen_record_status()}
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            display_env = os.environ.get("DISPLAY", ":0")
            if shutil.which("ffmpeg"):
                out_path = output_dir / f"VoltDeck_Rec_{stamp}.mp4"
                cmd = [
                    "ffmpeg", "-y", "-f", "x11grab", "-framerate", "15",
                    "-i", display_env, "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path),
                ]
            else:
                out_path = output_dir / f"VoltDeck_Rec_{stamp}.webm"
                cmd = [
                    "gst-launch-1.0", "-e", "-q",
                    "ximagesrc", "use-damage=false",
                    "!", "video/x-raw,framerate=10/1",
                    "!", "videoconvert",
                    "!", "vp8enc", "deadline=1", "cpu-used=6", "target-bitrate=1800000",
                    "!", "webmmux",
                    "!", "filesink", f"location={out_path}",
                ]
            try:
                self._rec_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._rec_path = out_path
                self._rec_started_at = time.time()
            except Exception as exc:
                self._rec_proc = None
                self._rec_path = None
                return {"ok": False, "error": f"Gagal memulai rekaman layar: {exc}"}
        if self._on_notify:
            self._on_notify("Rekam Layar Dimulai", f"Perekaman layar dimulai dari {requested_by}.")
        return {"ok": True, **self.get_screen_record_status()}

    def stop_screen_recording(self) -> dict[str, Any]:
        """Stop active recording cleanly (SIGINT flushes EOS header) and return saved file info."""
        with self._lock:
            proc = self._rec_proc
            out_path = self._rec_path
            started = self._rec_started_at
            self._rec_proc = None
            self._rec_path = None
            self._rec_started_at = 0.0
        if proc is None:
            return {"ok": False, "error": "Tidak ada rekaman layar yang sedang berjalan."}
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=4.0)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=2.0)
        except Exception:
            pass
        duration = int(max(1, time.time() - started)) if started else 0
        if out_path and out_path.is_file() and out_path.stat().st_size > 0:
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "size": out_path.stat().st_size,
                "duration_sec": duration,
                "recording": self.get_screen_record_status(),
            }
        return {"ok": False, "error": "Rekaman dihentikan tetapi file video belum terbentuk."}

    # =========================================================================
    # 8. On-Demand Task Monitor (User Processes Only) & 1-Tap Privacy Mode
    # =========================================================================

    PROTECTED_PROCESS_NAMES = {
        "systemd",
        "init",
        "dbus-daemon",
        "dbus-broker",
        "cinnamon",
        "cinnamon-session",
        "xorg",
        "xwayland",
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "tailscaled",
        "networkmanager",
        "gnome-keyring-daemon",
        "polkitd",
        "sshd",
        "lightdm",
        "gdm",
    }

    def list_top_processes(self, limit: int = 8) -> list[dict[str, Any]]:
        """On-demand snapshot of top user processes sorted by RAM usage (zero idle cost)."""
        if psutil is None:
            return []
        my_uid = os.getuid()
        my_pid = os.getpid()
        rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "uids"]):
            try:
                info = proc.info
                uids = info.get("uids")
                if uids is None or uids.real != my_uid:
                    continue
                pid = int(info["pid"])
                name = str(info.get("name") or "unknown")
                mem = info.get("memory_info")
                rss_mb = round((mem.rss / (1024 * 1024)), 1) if mem else 0.0
                if rss_mb < 15.0:
                    continue
                is_protected = (pid == my_pid) or (name.lower() in self.PROTECTED_PROCESS_NAMES)
                rows.append(
                    {
                        "pid": pid,
                        "name": name,
                        "ram_mb": rss_mb,
                        "cpu_percent": round(float(info.get("cpu_percent") or 0.0), 1),
                        "protected": is_protected,
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                continue
        rows.sort(key=lambda r: r["ram_mb"], reverse=True)
        return rows[: max(1, min(20, int(limit)))]

    def terminate_user_process(self, pid: int, requested_by: str = "Phone") -> dict[str, Any]:
        """Gracefully terminate a non-protected user process owned by current user."""
        if psutil is None:
            return {"ok": False, "error": "psutil tidak tersedia."}
        target_pid = int(pid)
        if target_pid <= 1 or target_pid == os.getpid():
            return {"ok": False, "error": "Proses ini dilindungi sistem."}
        try:
            proc = psutil.Process(target_pid)
            if proc.uids().real != os.getuid():
                return {"ok": False, "error": "Hanya proses milik user aktif yang boleh ditutup."}
            pname = proc.name()
            if pname.lower() in self.PROTECTED_PROCESS_NAMES:
                return {"ok": False, "error": f"Proses inti '{pname}' dilindungi dan tidak boleh ditutup."}
            proc.terminate()
            if self._on_notify:
                self._on_notify("Aplikasi Ditutup", f"{pname} (PID {target_pid}) ditutup dari {requested_by}.")
            return {"ok": True, "pid": target_pid, "name": pname}
        except psutil.NoSuchProcess:
            return {"ok": False, "error": "Proses sudah tidak berjalan."}
        except psutil.AccessDenied:
            return {"ok": False, "error": "Izin ditolak untuk menutup proses ini."}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def activate_privacy_mode(self, requested_by: str = "Phone") -> dict[str, Any]:
        """1-Tap Privacy Mode: Pause active media + Mute audio + Lock screen + Turn off display."""
        actions_done: list[str] = []
        # 1. Pause media if playing
        try:
            media = self.get_media_status()
            if media.get("available") and media.get("status") == "Playing":
                self.media_action("pause")
                actions_done.append("media_paused")
        except Exception:
            pass
        # 2. Mute audio explicitly
        if shutil.which("pactl"):
            rc, _ = self._runner(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"])
            if rc == 0:
                actions_done.append("muted")
        elif shutil.which("wpctl"):
            rc, _ = self._runner(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
            if rc == 0:
                actions_done.append("muted")
        # 3. Lock screen
        lock_res = self.lock_screen(requested_by=requested_by)
        if lock_res.get("ok"):
            actions_done.append("locked")
        # 4. Turn off display
        disp_res = self.display_off(requested_by=requested_by)
        if disp_res.get("ok"):
            actions_done.append("display_off")
        return {"ok": True, "actions": actions_done}

    def shutdown_cleanup(self) -> None:
        with self._lock:
            if self._power_timer is not None:
                self._power_timer.cancel()
                self._power_timer = None
            self._scheduled_action = None
            if self._inhibit_proc is not None:
                try:
                    self._inhibit_proc.terminate()
                except Exception:
                    pass
                self._inhibit_proc = None
                self._keep_awake = False
            if self._rec_proc is not None:
                try:
                    self._rec_proc.send_signal(signal.SIGINT)
                except Exception:
                    pass
                self._rec_proc = None

    def _emit_state(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change(self.get_power_state())
            except Exception:
                pass
