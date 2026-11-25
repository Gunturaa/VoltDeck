"""Linux Mint Remote Control: Telemetry, Brightness, Audio Sinks, MPRIS2 Media/Seek, Live Screen MJPEG, X11 Touchpad/Keys, and Power."""

from __future__ import annotations

import io
import os
import re
import shutil
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

    def get_telemetry(self) -> dict[str, Any]:
        if psutil is None:
            return {"available": False}

        cpu_pct = 0.0
        ram_pct = 0.0
        ram_used_gb = 0.0
        ram_total_gb = 0.0
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

        return {
            "available": True,
            "cpu_percent": cpu_pct,
            "ram_percent": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "battery_percent": battery_pct,
            "battery_plugged": battery_plugged,
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

    def get_media_status(self) -> dict[str, Any]:
        players = self._list_mpris_bus_names()
        if not players or dbus is None:
            return {"available": False, "player": "", "status": "Stopped", "title": "", "artist": ""}

        bus = dbus.SessionBus()
        chosen: Optional[dict[str, Any]] = None
        for name in players:
            try:
                obj = bus.get_object(name, "/org/mpris/MediaPlayer2")
                props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
                status = str(props.Get("org.mpris.MediaPlayer2.Player", "PlaybackStatus"))
                meta = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
                title = str(meta.get("xesam:title", "")) if meta else ""
                artists = meta.get("xesam:artist", []) if meta else []
                artist = ", ".join(str(a) for a in artists) if artists else ""
                short_name = name.replace("org.mpris.MediaPlayer2.", "").split(".")[0].capitalize()
                info = {
                    "available": True,
                    "bus_name": name,
                    "player": short_name,
                    "status": status,
                    "title": title,
                    "artist": artist,
                }
                if status == "Playing":
                    return info
                if chosen is None:
                    chosen = info
            except Exception:
                continue
        return chosen or {"available": False, "player": "", "status": "Stopped", "title": "", "artist": ""}

    def media_action(self, action: str, seek_seconds: int = 0) -> dict[str, Any]:
        if dbus is None:
            return {"ok": False, "error": "D-Bus tidak tersedia."}

        status = self.get_media_status()
        target_bus = status.get("bus_name")
        if not target_bus:
            return {"ok": False, "error": "Tidak ada pemutar media aktif (YouTube/Spotify/VLC)."}

        try:
            bus = dbus.SessionBus()
            obj = bus.get_object(target_bus, "/org/mpris/MediaPlayer2")
            player_iface = dbus.Interface(obj, "org.mpris.MediaPlayer2.Player")

            if action == "seek":
                offset_us = dbus.Int64(int(seek_seconds) * 1_000_000)
                player_iface.Seek(offset_us)
            else:
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
                    return {"ok": False, "error": "Aksi media tidak dikenal."}
                getattr(player_iface, method)()

            time.sleep(0.06)
            return {"ok": True, "media": self.get_media_status()}
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

    def get_full_status(self, enabled: bool = True) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "telemetry": self.get_telemetry(),
            "volume": self.get_volume(),
            "sinks": self.list_audio_sinks(),
            "brightness": self.get_brightness(),
            "media": self.get_media_status(),
            "apps": self.list_launchable_apps(),
            "webcam_available": self.is_webcam_available(),
            "power": self.get_power_state(),
        }

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

    def _emit_state(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change(self.get_power_state())
            except Exception:
                pass
