"""GTK3 + WebKit2 Single-Instance Desktop Application for LANShare."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GdkPixbuf", "2.0")

_WEBKIT_VERSION = None
for _wk_ver in ("4.1", "4.0"):
    try:
        gi.require_version("WebKit2", _wk_ver)
        _WEBKIT_VERSION = _wk_ver
        break
    except ValueError:
        continue

if _WEBKIT_VERSION is None:
    raise RuntimeError("WebKit2 (4.1 or 4.0) GTK3 binding is not installed.")

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, WebKit2  # noqa: E402

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # noqa: E402
except Exception:
    Notify = None

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
except Exception:
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3  # noqa: E402
    except Exception:
        AppIndicator3 = None

from . import __app_id__, __app_name__  # noqa: E402
from .clipboard_store import ClipboardStore  # noqa: E402
from .config import AppConfig  # noqa: E402
from .server import LANShareServer  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_PNG_PATH = PROJECT_ROOT / "assets" / "icons" / "lanshare-256.png"
ICON_TRAY_PATH = PROJECT_ROOT / "assets" / "voltdeck-tray-white.svg"
ICON_NOTIF_PATH = PROJECT_ROOT / "assets" / "icons" / "lanshare-64.png"
ICON_SVG_PATH = PROJECT_ROOT / "assets" / "lanshare.svg"


class LANShareWindow(Gtk.ApplicationWindow):
    """Main Precision Dark GTK3 + WebKit2 window."""

    def __init__(self, app: "LANShareApplication", server: LANShareServer, dev_mode: bool = False) -> None:
        super().__init__(application=app, title=__app_name__)
        self.app_ref = app
        self.server = server
        self.dev_mode = dev_mode

        self.set_wmclass("voltdeck", "VoltDeck")
        self.set_default_size(app.config.window_width, app.config.window_height)
        self.set_size_request(780, 560)
        self.set_position(Gtk.WindowPosition.CENTER)

        # Apply midnight dark window background to avoid any startup white flash
        css = Gtk.CssProvider()
        css.load_from_data(b"window, box { background-color: #060913; color: #f1f5f9; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Window icon
        for icon_path in (ICON_PNG_PATH, ICON_SVG_PATH):
            if icon_path.is_file():
                try:
                    self.set_icon_from_file(str(icon_path))
                    break
                except Exception:
                    pass

        # WebKit2 UserContentManager for native JS <-> Python bridge
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("lanshare")
        ucm.connect("script-message-received::lanshare", self._on_js_message)

        settings = WebKit2.Settings()
        settings.set_enable_javascript(True)
        settings.set_enable_developer_extras(dev_mode)
        settings.set_enable_smooth_scrolling(True)
        settings.set_enable_webgl(False)
        settings.set_javascript_can_open_windows_automatically(False)

        self.webview = WebKit2.WebView.new_with_user_content_manager(ucm)
        self.webview.set_settings(settings)
        self.webview.set_background_color(Gdk.RGBA(0.024, 0.035, 0.075, 1.0))
        self.webview.connect("decide-policy", self._on_decide_policy)
        if not dev_mode:
            self.webview.connect("context-menu", lambda *_args: True)

        # Native GTK Drag & Drop for zero-copy local file sharing from Nemo
        target_entry = Gtk.TargetEntry.new("text/uri-list", 0, 0)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [target_entry], Gdk.DragAction.COPY)
        self.connect("drag-data-received", self._on_drag_data_received)

        self.add(self.webview)
        self.connect("delete-event", self._on_delete_event)
        self.connect("configure-event", self._on_configure_event)

        self.webview.load_uri(self.server.state.get_desktop_url())

    def _on_configure_event(self, _widget: Gtk.Widget, _event: Gdk.EventConfigure) -> bool:
        w, h = self.get_size()
        self.app_ref.config.window_width = w
        self.app_ref.config.window_height = h
        return False

    def _on_delete_event(self, _widget: Gtk.Widget, _event: Gdk.Event) -> bool:
        self.app_ref.config.save()
        if self.app_ref.config.minimize_to_tray and self.app_ref.indicator is not None:
            self.hide()
            return True
        self.app_ref.quit_completely()
        return False

    def _on_decide_policy(
        self,
        _webview: WebKit2.WebView,
        decision: WebKit2.PolicyDecision,
        decision_type: WebKit2.PolicyDecisionType,
    ) -> bool:
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            nav_action = decision.get_navigation_action()
            uri = nav_action.get_request().get_uri() or ""
            # Intercept accidental file:// drops so WebKit shares them zero-copy instead of navigating!
            if uri.startswith("file://"):
                decision.ignore()
                local_path = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
                if local_path:
                    self._share_local_paths([local_path])
                return True
            # Restrict navigation strictly to our local server
            expected_prefix = f"http://127.0.0.1:{self.server.port}"
            if uri and not uri.startswith(expected_prefix) and not uri.startswith("about:"):
                decision.ignore()
                try:
                    subprocess.Popen(["xdg-open", uri], start_new_session=True)
                except Exception:
                    pass
                return True
        return False

    def _on_drag_data_received(
        self,
        _widget: Gtk.Widget,
        _drag_context: Gdk.DragContext,
        _x: int,
        _y: int,
        data: Gtk.SelectionData,
        _info: int,
        _time: int,
    ) -> None:
        uris = data.get_uris() or []
        paths: list[str] = []
        for uri in uris:
            if uri.startswith("file://"):
                p = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
                if p:
                    paths.append(p)
        if paths:
            self._share_local_paths(paths)

    def _share_local_paths(self, paths: list[str]) -> None:
        added = self.server.state.add_local_paths(paths)
        if added:
            msg = f"{len(added)} file siap diunduh di Safari iPhone"
            self._eval_toast(msg, "success")

    def _eval_toast(self, message: str, kind: str = "info") -> None:
        js = f"window.__lanshareToast && window.__lanshareToast({json.dumps(message)}, {json.dumps(kind)});"
        try:
            self.webview.run_javascript(js, None, None, None)
        except Exception:
            pass

    def _on_js_message(self, _ucm: WebKit2.UserContentManager, js_result: WebKit2.JavascriptResult) -> None:
        try:
            raw = js_result.get_js_value().to_string()
            payload = json.loads(raw)
        except Exception:
            return

        action = payload.get("action")
        if action == "pick_files":
            self._open_file_picker()
        elif action == "pick_receive_dir":
            self._open_dir_picker()
        elif action == "open_receive_dir":
            self._xdg_open(str(self.server.state.storage.receive_dir))
        elif action == "open_file":
            path = str(payload.get("path", ""))
            if path and Path(path).exists():
                self._xdg_open(path)
        elif action == "reveal_file":
            path = str(payload.get("path", ""))
            if path:
                self._reveal_in_file_manager(path)
        elif action == "copy_text":
            text = str(payload.get("text", ""))
            if text:
                self.app_ref.set_system_clipboard(text)
        elif action == "open_url":
            url = str(payload.get("url", ""))
            if url.startswith(("http://", "https://")):
                self._xdg_open(url)
        elif action == "paste_clipboard_image":
            self._share_clipboard_image()

    def _share_clipboard_image(self) -> None:
        """Read image/screenshot from Linux Mint GTK clipboard and share directly to iPhone."""
        import time

        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        pixbuf = clip.wait_for_image()
        if pixbuf is None:
            self._eval_toast("Tidak ada gambar/screenshot di clipboard Linux Mint", "info")
            return
        shots_dir = self.server.state.storage.receive_dir / ".screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        fname = f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
        out_path = shots_dir / fname
        try:
            pixbuf.savev(str(out_path), "png", [], [])
            entry = self.server.state.storage.register_local_file(
                out_path,
                direction="outbound",
                sender=self.server.state.config.device_name,
                owned_by_receive_dir=True,
            )
            if entry:
                self.server.state.broadcast_files()
                self._eval_toast(f"Screenshot {fname} siap diunduh di iPhone!", "success")
        except Exception as exc:
            self._eval_toast(f"Gagal mengambil screenshot: {exc}", "error")

    def _open_file_picker(self) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Pilih File untuk Dikirim ke iPhone",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            "Bagikan",
            Gtk.ResponseType.OK,
        )
        dialog.set_select_multiple(True)
        resp = dialog.run()
        paths = dialog.get_filenames() if resp == Gtk.ResponseType.OK else []
        dialog.destroy()
        if paths:
            self._share_local_paths(paths)

    def _open_dir_picker(self) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Pilih Folder Penyimpanan File dari iPhone",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            "Pilih Folder",
            Gtk.ResponseType.OK,
        )
        dialog.set_current_folder(str(self.server.state.storage.receive_dir))
        resp = dialog.run()
        folder = dialog.get_filename() if resp == Gtk.ResponseType.OK else None
        dialog.destroy()
        if folder:
            new_path = Path(folder)
            self.app_ref.config.receive_dir = str(new_path)
            self.app_ref.config.save()
            self.server.state.storage.set_receive_dir(new_path)
            self.server.state.broadcast_security()
            self._eval_toast(f"Folder penerima diubah ke {folder}", "success")

    @staticmethod
    def _xdg_open(target: str) -> None:
        try:
            subprocess.Popen(["xdg-open", target], start_new_session=True)
        except Exception:
            pass

    @staticmethod
    def _reveal_in_file_manager(target: str) -> None:
        p = Path(target)
        if not p.exists():
            return
        nemo = shutil.which("nemo")
        try:
            if nemo:
                subprocess.Popen([nemo, "--no-desktop", str(p)], start_new_session=True)
            else:
                subprocess.Popen(["xdg-open", str(p.parent)], start_new_session=True)
        except Exception:
            pass


class LANShareApplication(Gtk.Application):
    """Single-Instance GTK3 Application managing HTTP server, clipboard sync, lightweight tray standby, and window."""

    def __init__(
        self,
        port_override: Optional[int] = None,
        dev_mode: bool = False,
        start_in_tray: bool = False,
    ) -> None:
        super().__init__(
            application_id=__app_id__,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.port_override = port_override
        self.dev_mode = dev_mode
        self.start_in_tray = start_in_tray
        self._is_held = False
        self.config = AppConfig.load()
        if port_override:
            self.config.port = port_override

        self.server: Optional[LANShareServer] = None
        self.window: Optional[LANShareWindow] = None
        self.indicator = None
        self._gtk_clipboard: Optional[Gtk.Clipboard] = None
        self._last_system_clip_hash: str = ""
        self._last_system_img_hash: str = ""
        self._clip_timer_id: Optional[int] = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)

        # Force native GTK3 dark theme (matches Linux Mint dark & Precision Dark UI)
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            gtk_settings.set_property("gtk-application-prefer-dark-theme", True)

        if Notify is not None:
            try:
                Notify.init(__app_name__)
            except Exception:
                pass

        self.server = LANShareServer(
            config=self.config,
            on_mobile_clipboard=self._on_mobile_clipboard_received,
            on_mobile_clipboard_image=self._on_mobile_clipboard_image_received,
            on_notify=self._on_notify_request,
            on_open_url=self._on_open_url_received,
        )
        self.server.start()

        self._setup_clipboard_sync()
        self._setup_tray_indicator()

        # Keep app alive in tray even without an open WebKit2 window (ultra-low RAM standby!)
        if self.indicator is not None and (self.start_in_tray or self.config.minimize_to_tray):
            self.hold()
            self._is_held = True

    def do_activate(self) -> None:
        if self.window is None:
            assert self.server is not None
            self.window = LANShareWindow(self, self.server, dev_mode=self.dev_mode)
            self.window.connect("destroy", self._on_window_destroyed)
        self.window.show_all()
        self.window.deiconify()
        self.window.present()

    def _on_window_destroyed(self, _widget: Gtk.Widget) -> None:
        self.window = None

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        args = command_line.get_arguments()[1:]
        tray_requested = ("--tray" in args) or ("--standby" in args)
        file_paths: list[str] = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in ("--port", "-p"):
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            p = Path(arg).expanduser()
            if p.exists():
                file_paths.append(str(p.resolve()))

        # If launched with --tray (e.g. at Linux Mint login) and no files, stay silent in system tray
        if tray_requested and not file_paths and self.indicator is not None:
            if not self._is_held:
                self.hold()
                self._is_held = True
            return 0

        self.activate()

        if file_paths and self.server is not None:
            added = self.server.state.add_local_paths(file_paths)
            if added and self.window is not None:
                GLib.timeout_add(
                    400,
                    lambda: (
                        self.window._eval_toast(f"{len(added)} file ditambahkan ke antrean kirim", "success"),
                        False,
                    )[1],
                )
        return 0

    def quit_completely(self) -> None:
        if self._is_held:
            self._is_held = False
            try:
                self.release()
            except Exception:
                pass
        self.quit()

    def do_shutdown(self) -> None:
        if self._clip_timer_id:
            try:
                GLib.source_remove(self._clip_timer_id)
            except Exception:
                pass
        self.config.save()
        if self.server is not None:
            self.server.stop()
        if Notify is not None:
            try:
                Notify.uninit()
            except Exception:
                pass
        Gtk.Application.do_shutdown(self)

    # --- Two-Way Native Gtk.Clipboard Sync ---

    def _setup_clipboard_sync(self) -> None:
        self._gtk_clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        # Seed hash with current clipboard text so startup doesn't unexpectedly push old clipboard
        try:
            initial = self._gtk_clipboard.wait_for_text()
            if initial:
                self._last_system_clip_hash = ClipboardStore.hash_text(initial)
        except Exception:
            pass
        self._gtk_clipboard.connect("owner-change", self._on_gtk_clipboard_owner_change)
        # Lightweight safety polling fallback (some X11 apps update clipboard subtly)
        self._clip_timer_id = GLib.timeout_add(1200, self._poll_gtk_clipboard)

    def _on_gtk_clipboard_owner_change(self, clipboard: Gtk.Clipboard, _event: Gdk.EventOwnerChange) -> None:
        if not self.config.clipboard_auto_sync:
            return
        clipboard.request_text(self._handle_gtk_clipboard_text)
        clipboard.request_image(self._handle_gtk_clipboard_image)

    def _poll_gtk_clipboard(self) -> bool:
        if self.config.clipboard_auto_sync and self._gtk_clipboard is not None:
            self._gtk_clipboard.request_text(self._handle_gtk_clipboard_text)
        return True

    def _handle_gtk_clipboard_image(self, _clipboard: Gtk.Clipboard, pixbuf: Optional[GdkPixbuf.Pixbuf]) -> None:
        if pixbuf is None or self.server is None:
            return
        try:
            ok, buf = pixbuf.save_to_bufferv("png", [], [])
            if not ok or not buf:
                return
            png_bytes = bytes(buf)
            digest = ClipboardStore.hash_bytes(png_bytes)
            if digest == self._last_system_img_hash:
                return
            self._last_system_img_hash = digest
            if digest != self.server.state.clipboard.latest_image_hash():
                self.server.state.push_desktop_clipboard_image(png_bytes)
        except Exception:
            pass

    def _handle_gtk_clipboard_text(self, _clipboard: Gtk.Clipboard, text: Optional[str]) -> None:
        if not text or not text.strip() or self.server is None:
            return
        digest = ClipboardStore.hash_text(text)
        if digest == self._last_system_clip_hash:
            return
        self._last_system_clip_hash = digest
        if digest != self.server.state.clipboard.latest_hash():
            self.server.state.push_desktop_clipboard(text)

    def set_system_clipboard(self, text: str) -> None:
        """Write text directly to Linux Mint's GTK clipboard without re-triggering sync loop."""
        if self._gtk_clipboard is None:
            return
        self._last_system_clip_hash = ClipboardStore.hash_text(text)
        self._gtk_clipboard.set_text(text, -1)
        self._gtk_clipboard.store()

    def set_system_clipboard_image(self, png_bytes: bytes) -> None:
        """Write PNG bytes directly to Linux Mint's GTK clipboard so user can Ctrl+V immediately."""
        if self._gtk_clipboard is None or not png_bytes:
            return
        try:
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(png_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is None:
                return
            self._last_system_img_hash = ClipboardStore.hash_bytes(png_bytes)
            self._gtk_clipboard.set_image(pixbuf)
            self._gtk_clipboard.store()
        except Exception:
            pass

    def _on_mobile_clipboard_received(self, text: str, _source: str) -> None:
        GLib.idle_add(self._apply_mobile_clipboard_idle, text)

    def _apply_mobile_clipboard_idle(self, text: str) -> bool:
        self.set_system_clipboard(text)
        return False

    def _on_mobile_clipboard_image_received(self, png_bytes: bytes, _source: str) -> None:
        GLib.idle_add(self._apply_mobile_clipboard_image_idle, png_bytes)

    def _apply_mobile_clipboard_image_idle(self, png_bytes: bytes) -> bool:
        self.set_system_clipboard_image(png_bytes)
        return False

    def _on_open_url_received(self, url: str, source: str) -> None:
        GLib.idle_add(self._open_url_idle, url, source)

    def _open_url_idle(self, url: str, source: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        try:
            subprocess.Popen(["xdg-open", url], start_new_session=True)
            host = urllib.parse.urlsplit(url).netloc or url
            self._show_notification_idle("Membuka Tautan", f"{host} dikirim dari {source}")
            if self.window is not None:
                self.window._eval_toast(f"Membuka {host} di browser", "success")
        except Exception:
            pass
        return False

    # --- Desktop Notifications ---

    def _on_notify_request(self, title: str, body: str) -> None:
        GLib.idle_add(self._show_notification_idle, title, body)

    def _show_notification_idle(self, title: str, body: str) -> bool:
        icon = str(ICON_NOTIF_PATH) if ICON_NOTIF_PATH.is_file() else "network-transmit-receive"
        if Notify is not None:
            try:
                notif = Notify.Notification.new(title, body, icon)
                notif.show()
                return False
            except Exception:
                pass
        try:
            subprocess.Popen(["notify-send", "-i", icon, title, body], start_new_session=True)
        except Exception:
            pass
        return False

    # --- Optional Linux Mint System Tray (AyatanaAppIndicator3) ---

    def _setup_tray_indicator(self) -> None:
        if AppIndicator3 is None or self.server is None:
            return
        try:
            if ICON_TRAY_PATH.is_file():
                ind = AppIndicator3.Indicator.new_with_path(
                    "voltdeck-tray",
                    "voltdeck-tray-white",
                    AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
                    str(ICON_TRAY_PATH.parent),
                )
            else:
                ind = AppIndicator3.Indicator.new(
                    "voltdeck-tray",
                    "network-transmit-receive",
                    AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
                )
            ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

            menu = Gtk.Menu()
            item_open = Gtk.MenuItem(label="Buka VoltDeck")
            item_open.connect("activate", lambda *_: self.activate())
            menu.append(item_open)

            item_copy = Gtk.MenuItem(label="Salin URL Aktif")
            item_copy.connect(
                "activate",
                lambda *_: self.set_system_clipboard(
                    (self.server.state.get_anywhere_url() if self.config.access_mode == "anywhere" else None)
                    or self.server.state.get_lan_url(include_pin=False)
                ),
            )
            menu.append(item_copy)

            menu.append(Gtk.SeparatorMenuItem())

            item_off = Gtk.MenuItem(label="Mode Offline (LAN Only)")
            item_off.connect("activate", lambda *_: self.server.state.set_access_mode("offline"))
            menu.append(item_off)

            item_any = Gtk.MenuItem(label="Mode Anywhere (Tailscale VPN)")
            item_any.connect("activate", lambda *_: self.server.state.set_access_mode("anywhere"))
            menu.append(item_any)

            menu.append(Gtk.SeparatorMenuItem())

            item_quit = Gtk.MenuItem(label="Keluar VoltDeck")
            item_quit.connect("activate", lambda *_: self.quit_completely())
            menu.append(item_quit)

            menu.show_all()
            ind.set_menu(menu)
            self.indicator = ind
        except Exception:
            self.indicator = None


def run_desktop_app(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv if argv is None else argv)
    port_override: Optional[int] = None
    dev_mode = "--dev" in args
    start_in_tray = ("--tray" in args) or ("--standby" in args)

    for idx, token in enumerate(args):
        if token in ("--port", "-p") and idx + 1 < len(args):
            try:
                port_override = int(args[idx + 1])
            except ValueError:
                pass

    app = LANShareApplication(
        port_override=port_override,
        dev_mode=dev_mode,
        start_in_tray=start_in_tray,
    )
    return app.run(args)
