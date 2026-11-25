"""Automated end-to-end test suite for LANShare."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path

import requests
from PIL import Image

from lanshare.config import AppConfig
from lanshare.network import generate_qr_data_uri, list_lan_interfaces
from lanshare.security import MAX_FAILED_ATTEMPTS
from lanshare.server import LANShareServer


class LANShareEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.receive_dir = Path(self.tmpdir.name) / "received"
        self.source_dir = Path(self.tmpdir.name) / "source"
        self.trusted_file = Path(self.tmpdir.name) / "trusted_devices.json"
        self.source_dir.mkdir(parents=True, exist_ok=True)

        self.mobile_clip_events: list[tuple[str, str]] = []
        self.notify_events: list[tuple[str, str]] = []
        self.opened_urls: list[tuple[str, str]] = []
        self.remote_cmds: list[list[str]] = []

        def _fake_runner(cmd: list[str]) -> tuple[int, str]:
            self.remote_cmds.append(cmd)
            if cmd[:2] == ["pactl", "get-sink-volume"]:
                return 0, "Volume: front-left: 42598 /  65% / -11.24 dB"
            if cmd[:2] == ["pactl", "get-sink-mute"]:
                return 0, "Mute: no"
            if cmd[:2] == ["pactl", "get-default-sink"]:
                return 0, "alsa_output.speaker"
            if cmd[:3] == ["pactl", "list", "sinks"]:
                return 0, (
                    "Sink #1\n\tName: alsa_output.speaker\n\tDescription: Built-in Speaker\n"
                    "Sink #2\n\tName: bluez_output.airpods\n\tDescription: AirPods Pro\n"
                )
            if cmd[:3] == ["pactl", "list", "short"]:
                return 0, "42\t1\t10\tfloat32le 2ch 48000Hz"
            if cmd[:2] == ["xrandr", "--query"]:
                return 0, "eDP-1 connected primary 1920x1080+0+0"
            return 0, ""

        self.cfg = AppConfig(
            port=18900,
            receive_dir=str(self.receive_dir),
            device_name="mint-test",
            notifications_enabled=True,
        )
        self.server = LANShareServer(
            config=self.cfg,
            on_mobile_clipboard=lambda text, src: self.mobile_clip_events.append((text, src)),
            on_notify=lambda title, body: self.notify_events.append((title, body)),
            on_open_url=lambda url, src: self.opened_urls.append((url, src)),
            remote_command_runner=_fake_runner,
            tailscale_status_provider=lambda: {
                "installed": True,
                "running": True,
                "backend_state": "Running",
                "ip": "100.88.12.34",
                "dns_name": "mint-test.tail1234.ts.net",
                "hostname": "mint-test",
                "online_peers": 2,
            },
            trusted_file=self.trusted_file,
        )
        # Enable simulated remote IP header ONLY for this unit test server instance
        self.server._httpd.allow_test_ip_header = True  # type: ignore
        self.server.start()
        self.base_url = f"http://127.0.0.1:{self.server.port}"
        self.desktop_headers = {"X-Desktop-Token": self.server.state.security.desktop_token}

    def tearDown(self) -> None:
        self.server.stop()
        self.tmpdir.cleanup()

    def _auth_mobile_session(self) -> requests.Session:
        sess = requests.Session()
        pin = self.server.state.security.pin
        r = sess.post(
            f"{self.base_url}/api/auth/verify",
            json={"pin": pin},
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) Safari/604.1"},
            timeout=5,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        return sess

    def test_01_pin_security_and_rate_limiting(self) -> None:
        pin = self.server.state.security.pin
        self.assertEqual(len(pin), 6)
        self.assertTrue(pin.isdigit())

        # Unauthenticated requests blocked
        self.assertEqual(requests.get(f"{self.base_url}/api/state", timeout=5).status_code, 401)
        self.assertEqual(requests.get(f"{self.base_url}/api/desktop/state", timeout=5).status_code, 403)

        # Public landing & info work without auth and never leak PIN
        info = requests.get(f"{self.base_url}/api/info", timeout=5).json()
        self.assertTrue(info["ok"])
        self.assertFalse(info["authenticated"])
        self.assertNotIn("pin", info)

        # Brute-force lockout after MAX_FAILED_ATTEMPTS wrong attempts
        wrong_pin = "000000" if pin != "000000" else "999999"
        for i in range(MAX_FAILED_ATTEMPTS - 1):
            r = requests.post(f"{self.base_url}/api/auth/verify", json={"pin": wrong_pin}, timeout=5)
            self.assertEqual(r.status_code, 401, f"Attempt {i + 1}")
        r_lock = requests.post(f"{self.base_url}/api/auth/verify", json={"pin": wrong_pin}, timeout=5)
        self.assertEqual(r_lock.status_code, 429)
        self.assertGreater(r_lock.json()["retry_after"], 0)

        # Even valid PIN blocked while locked out
        r_still_locked = requests.post(f"{self.base_url}/api/auth/verify", json={"pin": pin}, timeout=5)
        self.assertEqual(r_still_locked.status_code, 429)

        # Rotating PIN clears lockouts and issues fresh 6-digit PIN
        r_rot = requests.post(
            f"{self.base_url}/api/desktop/pin/rotate",
            headers=self.desktop_headers,
            json={"revoke_sessions": True},
            timeout=5,
        )
        self.assertEqual(r_rot.status_code, 200)
        new_pin = r_rot.json()["pin"]
        self.assertEqual(len(new_pin), 6)
        self.assertTrue(new_pin.isdigit())

    def test_02_streaming_upload_iphone_to_linux_and_sanitization(self) -> None:
        sess = self._auth_mobile_session()
        payload = os.urandom(1024 * 1024 + 137)  # >1 MB multi-chunk stream
        expected_sha = hashlib.sha256(payload).hexdigest()

        # Upload with path-traversal attempt in filename
        r1 = sess.post(
            f"{self.base_url}/api/upload",
            data=payload,
            headers={
                "X-Filename": urllib.parse.quote("../../secret/foto liburan.bin"),
                "Content-Length": str(len(payload)),
            },
            timeout=10,
        )
        self.assertEqual(r1.status_code, 200)
        f1 = r1.json()["file"]
        self.assertEqual(f1["name"], "foto liburan.bin")
        saved_path = Path(f1["path"])
        self.assertEqual(saved_path.parent.resolve(), self.receive_dir.resolve())
        self.assertEqual(hashlib.sha256(saved_path.read_bytes()).hexdigest(), expected_sha)

        # Upload same name again -> collision handled cleanly as "foto liburan (1).bin"
        r2 = sess.post(
            f"{self.base_url}/api/upload",
            data=b"hello-collision",
            headers={
                "X-Filename": urllib.parse.quote("foto liburan.bin"),
                "Content-Length": str(len(b"hello-collision")),
            },
            timeout=5,
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["file"]["name"], "foto liburan (1).bin")

    def test_03_streaming_download_range_thumbnail_and_zip(self) -> None:
        sess = self._auth_mobile_session()

        # Create a real image and a binary file on Linux Mint
        img_path = self.source_dir / "precision.png"
        Image.new("RGB", (640, 480), color=(34, 211, 238)).save(img_path, format="PNG")

        bin_path = self.source_dir / "video_sample.bin"
        bin_data = os.urandom(600 * 1024)
        bin_path.write_bytes(bin_data)

        # Register via desktop zero-copy endpoint
        r_add = requests.post(
            f"{self.base_url}/api/desktop/files/add-paths",
            headers=self.desktop_headers,
            json={"paths": [str(img_path), str(bin_path)]},
            timeout=5,
        )
        self.assertEqual(r_add.status_code, 200)
        added = r_add.json()["added"]
        self.assertEqual(len(added), 2)
        by_name = {f["name"]: f for f in added}

        # Thumbnail endpoint for image
        r_thumb = sess.get(f"{self.base_url}/api/files/{by_name['precision.png']['id']}/thumb", timeout=5)
        self.assertEqual(r_thumb.status_code, 200)
        self.assertEqual(r_thumb.headers["Content-Type"], "image/jpeg")

        # Full streaming download
        vid_id = by_name["video_sample.bin"]["id"]
        r_dl = sess.get(f"{self.base_url}/api/files/{vid_id}/download", timeout=10)
        self.assertEqual(r_dl.status_code, 200)
        self.assertEqual(r_dl.content, bin_data)
        self.assertEqual(r_dl.headers.get("Accept-Ranges"), "bytes")

        # HTTP Range 206 Partial Content
        r_part = sess.get(
            f"{self.base_url}/api/files/{vid_id}/download",
            headers={"Range": "bytes=1000-1999"},
            timeout=5,
        )
        self.assertEqual(r_part.status_code, 206)
        self.assertEqual(r_part.content, bin_data[1000:2000])
        self.assertEqual(r_part.headers["Content-Range"], f"bytes 1000-1999/{len(bin_data)}")

        # Multi-file streaming ZIP download
        r_zip = sess.get(f"{self.base_url}/api/files/zip", timeout=10)
        self.assertEqual(r_zip.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(r_zip.content), "r") as zf:
            self.assertEqual(set(zf.namelist()), {"precision.png", "video_sample.bin"})
            self.assertEqual(zf.read("video_sample.bin"), bin_data)

    def test_04_shared_clipboard_two_way_and_sse(self) -> None:
        sess = self._auth_mobile_session()
        sse_events: list[str] = []

        def _read_sse():
            with sess.get(f"{self.base_url}/api/events", stream=True, timeout=5) as resp:
                for line in resp.iter_lines(decode_unicode=True):
                    if line and line.startswith("event: "):
                        sse_events.append(line[len("event: "):])
                        if "session_revoked" in sse_events:
                            break

        t = threading.Thread(target=_read_sse, daemon=True)
        t.start()
        time.sleep(0.15)

        # Desktop -> Mobile clipboard
        pushed = self.server.state.push_desktop_clipboard("https://linuxmint.com")
        self.assertTrue(pushed)
        # Duplicate suppressed
        self.assertFalse(self.server.state.push_desktop_clipboard("https://linuxmint.com"))

        # Mobile -> Desktop clipboard
        r_clip = sess.post(f"{self.base_url}/api/clipboard", json={"text": "OTP: 849201"}, timeout=5)
        self.assertEqual(r_clip.status_code, 200)
        self.assertEqual(len(self.mobile_clip_events), 1)
        self.assertEqual(self.mobile_clip_events[0][0], "OTP: 849201")

        # Revoke mobile device from Desktop -> triggers session_revoked SSE & rejects future calls
        devices = self.server.state.security.list_devices()
        self.assertEqual(len(devices), 1)
        requests.post(
            f"{self.base_url}/api/desktop/devices/{devices[0]['id']}/revoke",
            headers=self.desktop_headers,
            json={},
            timeout=5,
        )
        t.join(timeout=2.5)
        self.assertIn("clipboard_updated", sse_events)
        self.assertIn("session_revoked", sse_events)
        self.assertEqual(sess.get(f"{self.base_url}/api/state", timeout=5).status_code, 401)

    def test_05_qr_and_network_interfaces(self) -> None:
        ifaces = list_lan_interfaces()
        self.assertGreaterEqual(len(ifaces), 1)
        uri = generate_qr_data_uri("http://192.168.1.50:8765")
        self.assertTrue(uri.startswith("data:image/svg+xml;base64,"))

        # Desktop HTML renders with valid desktop token
        r_desk = requests.get(self.server.state.get_desktop_url(), timeout=5)
        self.assertEqual(r_desk.status_code, 200)
        self.assertIn("VOLTDECK", r_desk.text)

        # Mobile HTML renders publicly with boot splash
        r_mob = requests.get(f"{self.base_url}/", timeout=5)
        self.assertEqual(r_mob.status_code, 200)
        self.assertIn("pin-slots", r_mob.text)
        self.assertIn("boot-splash", r_mob.text)
        self.assertIn("boot-bolt-outline", r_desk.text)

    def test_06_open_url_from_whatsapp_text_and_scheme_security(self) -> None:
        sess = self._auth_mobile_session()

        # WhatsApp message with embedded link + open_on_desktop=True
        r = sess.post(
            f"{self.base_url}/api/clipboard",
            json={"text": "Cek materi ini bro: https://linuxmint.com/download.php.", "open_on_desktop": True},
            timeout=5,
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["opened_url"], "https://linuxmint.com/download.php")
        self.assertEqual(len(self.opened_urls), 1)
        self.assertEqual(self.opened_urls[0][0], "https://linuxmint.com/download.php")

        # Direct /api/open-url accepts https://
        r_ok = sess.post(f"{self.base_url}/api/open-url", json={"url": "https://example.org/docs"}, timeout=5)
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(self.opened_urls[-1][0], "https://example.org/docs")

        # Unsafe schemes strictly blocked (file://, javascript:)
        r_bad1 = sess.post(f"{self.base_url}/api/open-url", json={"url": "file:///etc/passwd"}, timeout=5)
        self.assertEqual(r_bad1.status_code, 400)
        r_bad2 = sess.post(f"{self.base_url}/api/open-url", json={"url": "javascript:alert(1)"}, timeout=5)
        self.assertEqual(r_bad2.status_code, 400)

    def test_07_remote_control_power_volume_and_mdns(self) -> None:
        sess = self._auth_mobile_session()

        # Desktop state exposes mDNS .local URL and supports switching QR to .local
        dstate = requests.get(f"{self.base_url}/api/desktop/state", headers=self.desktop_headers, timeout=5).json()
        self.assertTrue(dstate["mdns_url"].endswith(f".local:{self.server.port}"))
        requests.post(
            f"{self.base_url}/api/desktop/settings",
            headers=self.desktop_headers,
            json={"use_mdns_qr": True},
            timeout=5,
        )
        dstate2 = requests.get(f"{self.base_url}/api/desktop/state", headers=self.desktop_headers, timeout=5).json()
        self.assertEqual(dstate2["qr_url"], dstate["mdns_url"])

        # Volume control via remote API
        r_vol = sess.post(f"{self.base_url}/api/remote/volume", json={"percent": 75}, timeout=5)
        self.assertEqual(r_vol.status_code, 200)
        self.assertTrue(r_vol.json()["ok"])

        # Lock screen
        r_lock = sess.post(f"{self.base_url}/api/remote/power", json={"action": "lock"}, timeout=5)
        self.assertEqual(r_lock.status_code, 200)

        # Schedule cancelable shutdown + cancel cleanly
        r_sd = sess.post(
            f"{self.base_url}/api/remote/power",
            json={"action": "shutdown", "delay_seconds": 30},
            timeout=5,
        )
        self.assertEqual(r_sd.status_code, 200)
        self.assertTrue(r_sd.json()["power"]["scheduled"])
        self.assertEqual(r_sd.json()["power"]["action"], "shutdown")

        r_cancel = sess.post(f"{self.base_url}/api/remote/power", json={"action": "cancel"}, timeout=5)
        self.assertEqual(r_cancel.status_code, 200)
        self.assertFalse(r_cancel.json()["power"]["scheduled"])

        # Disabling remote control on Desktop blocks mobile remote actions with 403
        requests.post(
            f"{self.base_url}/api/desktop/settings",
            headers=self.desktop_headers,
            json={"remote_control_enabled": False},
            timeout=5,
        )
        r_blocked = sess.post(f"{self.base_url}/api/remote/power", json={"action": "lock"}, timeout=5)
        self.assertEqual(r_blocked.status_code, 403)

        # Verify mobile camera capture & remote tab markup + desktop screenshot paste button
        mob_html = requests.get(f"{self.base_url}/", timeout=5).text
        self.assertIn('capture="environment"', mob_html)
        self.assertIn("m-view-remote", mob_html)
        self.assertIn("trackpad-surface", mob_html)
        self.assertIn("btn-screen-live", mob_html)
        desk_html = requests.get(self.server.state.get_desktop_url(), timeout=5).text
        self.assertIn("btn-paste-shot", desk_html)
        self.assertIn("power-banner", desk_html)

    def test_08_telemetry_sinks_brightness_restart_and_live_mjpeg(self) -> None:
        sess = self._auth_mobile_session()

        # Telemetry endpoint
        r_tel = sess.get(f"{self.base_url}/api/remote/telemetry", timeout=5)
        self.assertEqual(r_tel.status_code, 200)
        self.assertTrue(r_tel.json()["telemetry"]["available"])
        self.assertIn("ram_percent", r_tel.json()["telemetry"])

        # Audio sink switching + validation
        r_stat = sess.get(f"{self.base_url}/api/remote/status", timeout=5).json()
        self.assertEqual(len(r_stat["sinks"]), 2)
        r_sink = sess.post(f"{self.base_url}/api/remote/sink", json={"sink": "bluez_output.airpods"}, timeout=5)
        self.assertEqual(r_sink.status_code, 200)
        r_bad_sink = sess.post(f"{self.base_url}/api/remote/sink", json={"sink": "nonexistent;rm"}, timeout=5)
        self.assertEqual(r_bad_sink.status_code, 400)

        # Brightness & Display Off
        r_br = sess.post(f"{self.base_url}/api/remote/brightness", json={"percent": 60}, timeout=5)
        self.assertEqual(r_br.status_code, 200)
        r_doff = sess.post(f"{self.base_url}/api/remote/display-off", json={}, timeout=5)
        self.assertEqual(r_doff.status_code, 200)

        # Restart schedule + cancel
        r_rst = sess.post(
            f"{self.base_url}/api/remote/power",
            json={"action": "restart", "delay_seconds": 25},
            timeout=5,
        )
        self.assertEqual(r_rst.status_code, 200)
        self.assertEqual(r_rst.json()["power"]["action"], "restart")
        sess.post(f"{self.base_url}/api/remote/power", json={"action": "cancel"}, timeout=5)

        # Live Screen Snapshot & MJPEG Stream (mock capture for headless sandbox)
        buf = io.BytesIO()
        Image.new("RGB", (160, 90), color=(14, 17, 23)).save(buf, format="JPEG")
        fake_jpeg = buf.getvalue()
        self.server.state.remote.capture_screen_jpeg = lambda max_width=960, quality=62: fake_jpeg  # type: ignore

        r_snap = sess.get(f"{self.base_url}/api/remote/screen/snapshot", timeout=5)
        self.assertEqual(r_snap.status_code, 200)
        self.assertEqual(r_snap.headers["Content-Type"], "image/jpeg")
        self.assertEqual(r_snap.content, fake_jpeg)

        with sess.get(f"{self.base_url}/api/remote/screen/stream?fps=10", stream=True, timeout=5) as r_mjpeg:
            self.assertEqual(r_mjpeg.status_code, 200)
            self.assertIn("multipart/x-mixed-replace", r_mjpeg.headers["Content-Type"])
            chunk = next(r_mjpeg.iter_content(chunk_size=4096))
            self.assertIn(b"--lanshareframe", chunk)
            self.assertIn(b"image/jpeg", chunk)

    def test_09_dual_mode_offline_vs_anywhere_tailscale_security(self) -> None:
        ts_headers = {"X-Test-Client-IP": "100.88.12.99"}
        pub_headers = {"X-Test-Client-IP": "8.8.8.8"}

        # 1. Default mode is OFFLINE (LAN Only): Tailscale and Public IPs MUST be blocked (403)
        self.assertEqual(requests.get(f"{self.base_url}/api/info", headers=ts_headers, timeout=5).status_code, 403)
        self.assertEqual(requests.get(f"{self.base_url}/", headers=ts_headers, timeout=5).status_code, 403)
        self.assertEqual(requests.get(f"{self.base_url}/api/info", headers=pub_headers, timeout=5).status_code, 403)

        # 2. Switch to ANYWHERE mode from Desktop
        r_mode = requests.post(
            f"{self.base_url}/api/desktop/mode",
            headers=self.desktop_headers,
            json={"mode": "anywhere"},
            timeout=5,
        )
        self.assertEqual(r_mode.status_code, 200)
        self.assertEqual(r_mode.json()["access_mode"], "anywhere")
        self.assertEqual(r_mode.json()["anywhere_url"], f"http://100.88.12.34:{self.server.port}")

        # Public raw internet IP STILL blocked even in Anywhere mode!
        self.assertEqual(requests.get(f"{self.base_url}/api/info", headers=pub_headers, timeout=5).status_code, 403)

        # Tailscale VPN iPhone is now allowed and authenticates with 6-digit PIN
        ts_sess = requests.Session()
        ts_sess.headers.update(ts_headers)
        r_auth = ts_sess.post(
            f"{self.base_url}/api/auth/verify",
            json={"pin": self.server.state.security.pin},
            timeout=5,
        )
        self.assertEqual(r_auth.status_code, 200)
        self.assertEqual(r_auth.json()["device"]["network_zone"], "tailscale")

        # Verify state reflects client_zone = tailscale
        st = ts_sess.get(f"{self.base_url}/api/state", timeout=5).json()
        self.assertEqual(st["client_zone"], "tailscale")

        # Disable remote power via VPN -> power blocked (403), volume still works (200)
        requests.post(
            f"{self.base_url}/api/desktop/settings",
            headers=self.desktop_headers,
            json={"anywhere_allow_power": False},
            timeout=5,
        )
        self.assertEqual(
            ts_sess.post(f"{self.base_url}/api/remote/power", json={"action": "shutdown"}, timeout=5).status_code,
            403,
        )
        self.assertEqual(
            ts_sess.post(f"{self.base_url}/api/remote/volume", json={"percent": 55}, timeout=5).status_code,
            200,
        )

        # 3. Switch back to OFFLINE -> Tailscale session immediately revoked & blocked (403)
        requests.post(
            f"{self.base_url}/api/desktop/mode",
            headers=self.desktop_headers,
            json={"mode": "offline"},
            timeout=5,
        )
        self.assertEqual(ts_sess.get(f"{self.base_url}/api/state", timeout=5).status_code, 403)
        self.assertEqual(len(self.server.state.security.list_devices()), 0)

    def test_10_trusted_device_survives_server_restart(self) -> None:
        # 1. Pair iPhone with PIN on server instance #1
        sess = self._auth_mobile_session()
        dev_id = self.server.state.security.list_devices()[0]["id"]

        # 2. Desktop marks device as Trusted
        r_trust = requests.post(
            f"{self.base_url}/api/desktop/devices/{dev_id}/trust",
            headers=self.desktop_headers,
            json={},
            timeout=5,
        )
        self.assertEqual(r_trust.status_code, 200)
        trust_id = r_trust.json()["trusted_device"]["trust_id"]

        # 3. Mobile claims persistent HttpOnly trust cookie
        r_claim = sess.post(f"{self.base_url}/api/auth/claim-trust", timeout=5)
        self.assertEqual(r_claim.status_code, 200)
        self.assertTrue(r_claim.json()["claimed"])
        self.assertIn("lanshare_trust", sess.cookies.get_dict())

        # Verify file on disk stores ONLY hashed secret, never plaintext cookie secret
        raw_trust_cookie = sess.cookies.get("lanshare_trust")
        assert raw_trust_cookie is not None
        raw_secret = raw_trust_cookie.split(".", 1)[1]
        saved_json = self.trusted_file.read_text(encoding="utf-8")
        self.assertNotIn(raw_secret, saved_json)

        # 4. Simulate full laptop / LANShare restart (new server, new signing key, new 6-digit PIN!)
        sess.close()
        self.server.stop()
        server2 = LANShareServer(config=self.cfg, trusted_file=self.trusted_file)
        server2.start()
        try:
            url2 = f"http://127.0.0.1:{server2.port}"
            # Fresh Safari session after reboot carrying ONLY the persistent lanshare_trust cookie
            # Calling /api/state DIRECTLY (as Safari does when resuming an already-open tab!) must auto-login!
            sess2 = requests.Session()
            sess2.cookies.set("lanshare_trust", raw_trust_cookie)
            r_direct_state = sess2.get(f"{url2}/api/state", timeout=5)
            self.assertEqual(r_direct_state.status_code, 200)
            self.assertTrue(r_direct_state.json()["trusted"])

            # Also verify X-Trust-Token localStorage backup header works without any cookies!
            r_hdr_state = requests.get(f"{url2}/api/state", headers={"X-Trust-Token": raw_trust_cookie}, timeout=5)
            self.assertEqual(r_hdr_state.status_code, 200)

            r_info = sess2.get(f"{url2}/api/info", timeout=5)
            self.assertEqual(r_info.status_code, 200)
            info = r_info.json()
            self.assertTrue(info["authenticated"])
            self.assertTrue(info["trusted"])

            # 5. Revoke trusted device from Desktop -> both cookie & X-Trust-Token rejected (401)!
            h2 = {"X-Desktop-Token": server2.state.security.desktop_token}
            r_rev = requests.post(f"{url2}/api/desktop/trusted/{trust_id}/revoke", headers=h2, json={}, timeout=5)
            self.assertEqual(r_rev.status_code, 200)
            self.assertEqual(sess2.get(f"{url2}/api/state", timeout=5).status_code, 401)
            self.assertEqual(
                requests.get(f"{url2}/api/state", headers={"X-Trust-Token": raw_trust_cookie}, timeout=5).status_code,
                401,
            )
            self.assertFalse(sess2.get(f"{url2}/api/info", timeout=5).json()["authenticated"])
            sess2.close()
        finally:
            server2.stop()

    def test_11_app_launcher_webcam_and_laptop_gallery(self) -> None:
        sess = self._auth_mobile_session()

        # 1. App Launcher lists installed apps & rejects unknown app IDs (anti-injection)
        r_stat = sess.get(f"{self.base_url}/api/remote/status", timeout=5).json()
        self.assertIsInstance(r_stat["apps"], list)
        self.assertGreater(len(r_stat["apps"]), 0)
        r_bad_app = sess.post(f"{self.base_url}/api/remote/apps/launch", json={"app_id": "rm -rf /"}, timeout=5)
        self.assertEqual(r_bad_app.status_code, 400)

        # 2. Webcam snapshot endpoint (mocked capture for headless test)
        buf = io.BytesIO()
        Image.new("RGB", (320, 240), color=(52, 211, 153)).save(buf, format="JPEG")
        cam_jpg = buf.getvalue()
        self.server.state.remote.capture_webcam_jpeg = lambda quality=82: cam_jpg  # type: ignore
        r_cam = sess.get(f"{self.base_url}/api/remote/webcam/snapshot?download=1", timeout=5)
        self.assertEqual(r_cam.status_code, 200)
        self.assertEqual(r_cam.headers["Content-Type"], "image/jpeg")
        self.assertEqual(r_cam.content, cam_jpg)

        # 3. Laptop Gallery Browser scans receive_dir images, serves thumb & download
        sample_img = self.receive_dir / "laptop_photo.png"
        Image.new("RGB", (400, 300), color=(34, 211, 238)).save(sample_img, format="PNG")
        r_gal = sess.get(f"{self.base_url}/api/gallery?folder=LANShare", timeout=5)
        self.assertEqual(r_gal.status_code, 200)
        imgs = r_gal.json()["images"]
        match = [i for i in imgs if i["name"] == "laptop_photo.png"]
        self.assertEqual(len(match), 1)
        gid = match[0]["id"]
        self.assertEqual(sess.get(f"{self.base_url}/api/gallery/{gid}/thumb", timeout=5).status_code, 200)
        r_dl = sess.get(f"{self.base_url}/api/gallery/{gid}/download", timeout=5)
        self.assertEqual(r_dl.status_code, 200)
        self.assertEqual(r_dl.content, sample_img.read_bytes())

        # 4. Responsive web UI markup check
        mob_html = requests.get(f"{self.base_url}/", timeout=5).text
        self.assertIn("m-view-gallery", mob_html)
        self.assertIn("r-apps-grid", mob_html)
        self.assertIn("btn-cap-webcam", mob_html)

    def test_12_bilingual_i18n_and_new_app_icon(self) -> None:
        # Language switches cleanly via desktop settings & propagates to /api/info
        r_set = requests.post(
            f"{self.base_url}/api/desktop/settings",
            headers=self.desktop_headers,
            json={"language": "en"},
            timeout=5,
        )
        self.assertEqual(r_set.status_code, 200)
        info = requests.get(f"{self.base_url}/api/info", timeout=5).json()
        self.assertEqual(info["language"], "en")

        # Verify i18n.js & new glass-bolt app icon are served cleanly
        r_i18n = requests.get(f"{self.base_url}/static/i18n.js", timeout=5)
        self.assertEqual(r_i18n.status_code, 200)
        self.assertIn("VDI18n", r_i18n.text)

        r_icon = requests.get(f"{self.base_url}/static/app-icon.png", timeout=5)
        self.assertEqual(r_icon.status_code, 200)
        self.assertEqual(r_icon.headers["Content-Type"], "image/png")

    def test_13_file_explorer_task_monitor_and_privacy_mode(self) -> None:
        sess = self._auth_mobile_session()

        # 1. File Explorer lists files inside sandboxed LANShare root & streams downloads
        sub = self.receive_dir / "docs_sub"
        sub.mkdir(parents=True, exist_ok=True)
        pdf_file = sub / "tugas_akhir.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 sample voltdeck content")

        r_exp = sess.get(f"{self.base_url}/api/explorer?root=LANShare&path=docs_sub", timeout=5)
        self.assertEqual(r_exp.status_code, 200)
        names = [e["name"] for e in r_exp.json()["entries"]]
        self.assertIn("tugas_akhir.pdf", names)

        r_dl = sess.get(f"{self.base_url}/api/explorer/download?root=LANShare&path=docs_sub/tugas_akhir.pdf", timeout=5)
        self.assertEqual(r_dl.status_code, 200)
        self.assertEqual(r_dl.content, b"%PDF-1.4 sample voltdeck content")

        # Path traversal & dotfile escape strictly blocked (404)
        r_trav = sess.get(f"{self.base_url}/api/explorer/download?root=LANShare&path=../../etc/passwd", timeout=5)
        self.assertEqual(r_trav.status_code, 404)

        # 2. On-demand Task Monitor lists processes & blocks killing PID 1 / self PID
        r_procs = sess.get(f"{self.base_url}/api/remote/processes", timeout=5)
        self.assertEqual(r_procs.status_code, 200)
        self.assertIsInstance(r_procs.json()["processes"], list)
        self.assertEqual(
            sess.post(f"{self.base_url}/api/remote/processes/kill", json={"pid": 1}, timeout=5).status_code,
            400,
        )
        self.assertEqual(
            sess.post(f"{self.base_url}/api/remote/processes/kill", json={"pid": os.getpid()}, timeout=5).status_code,
            400,
        )

        # 3. 1-Tap Privacy Mode executes mute + lock + display off cleanly
        r_priv = sess.post(f"{self.base_url}/api/remote/privacy-mode", json={}, timeout=5)
        self.assertEqual(r_priv.status_code, 200)
        self.assertTrue(r_priv.json()["ok"])
        self.assertIn("locked", r_priv.json()["actions"])
        self.assertIn("display_off", r_priv.json()["actions"])

    def test_14_sticky_mpris_player_and_realtime_remote_live_sync(self) -> None:
        sess = self._auth_mobile_session()
        remote = self.server.state.remote

        # Simulate Chrome appearing BEFORE Spotify in D-Bus order (the exact real-world bug!)
        fake_players = {
            "org.mpris.MediaPlayer2.chromium.instance123": {
                "available": True,
                "bus_name": "org.mpris.MediaPlayer2.chromium.instance123",
                "player": "Chrome",
                "status": "Paused",
                "title": "YouTube Tab",
                "artist": "Channel",
            },
            "org.mpris.MediaPlayer2.spotify": {
                "available": True,
                "bus_name": "org.mpris.MediaPlayer2.spotify",
                "player": "Spotify",
                "status": "Playing",
                "title": "Favorite Song",
                "artist": "Artist",
            },
        }
        invoked: list[tuple[str, str]] = []

        remote._read_all_mpris_players = lambda: [dict(v) for v in fake_players.values()]  # type: ignore

        def fake_invoke(target_bus: str, action: str, seek_seconds: int = 0) -> None:
            invoked.append((target_bus, action))
            if action == "play_pause":
                cur = fake_players[target_bus]["status"]
                fake_players[target_bus]["status"] = "Paused" if cur == "Playing" else "Playing"

        remote._invoke_mpris_command = fake_invoke  # type: ignore

        # 1. Live sync endpoint returns telemetry + media + volume + brightness & detects Spotify playing
        live = sess.get(f"{self.base_url}/api/remote/telemetry", timeout=5).json()
        self.assertTrue(live["ok"])
        self.assertIn("telemetry", live)
        self.assertIn("volume", live)
        self.assertIn("brightness", live)
        self.assertEqual(live["media"]["player"], "Spotify")
        self.assertEqual(live["media"]["status"], "Playing")
        self.assertEqual(len(live["media"]["players"]), 2)

        # 2. Pause Spotify -> MUST stay locked on Spotify (NOT jump to Chrome!)
        r_pause = sess.post(f"{self.base_url}/api/remote/media", json={"action": "play_pause"}, timeout=5).json()
        self.assertTrue(r_pause["ok"])
        self.assertEqual(r_pause["media"]["player"], "Spotify")
        self.assertEqual(r_pause["media"]["status"], "Paused")

        # 3. Subsequent live poll while paused STILL stays on Spotify!
        live2 = sess.get(f"{self.base_url}/api/remote/telemetry", timeout=5).json()
        self.assertEqual(live2["media"]["player"], "Spotify")
        self.assertEqual(live2["media"]["status"], "Paused")

        # 4. Play again -> MUST resume Spotify (NOT Chrome!)
        r_play = sess.post(f"{self.base_url}/api/remote/media", json={"action": "play_pause"}, timeout=5).json()
        self.assertTrue(r_play["ok"])
        self.assertEqual(r_play["media"]["player"], "Spotify")
        self.assertEqual(r_play["media"]["status"], "Playing")
        self.assertEqual(invoked, [
            ("org.mpris.MediaPlayer2.spotify", "play_pause"),
            ("org.mpris.MediaPlayer2.spotify", "play_pause"),
        ])

        # 5. User taps Chrome chip -> cleanly switches target player to Chrome
        r_sel = sess.post(
            f"{self.base_url}/api/remote/media",
            json={"action": "select", "bus_name": "org.mpris.MediaPlayer2.chromium.instance123"},
            timeout=5,
        ).json()
        self.assertTrue(r_sel["ok"])
        self.assertEqual(r_sel["media"]["player"], "Chrome")

    def test_15_custom_persistent_pin_and_light_dark_theme(self) -> None:
        # 1. Fresh instance without custom_pin reports pin_configured = False
        st0 = requests.get(f"{self.base_url}/api/desktop/state", headers=self.desktop_headers, timeout=5).json()
        self.assertFalse(st0["pin_configured"])

        # 2. Invalid PIN (non-6-digit) is rejected (400)
        r_bad = requests.post(
            f"{self.base_url}/api/desktop/pin/set",
            headers=self.desktop_headers,
            json={"pin": "123"},
            timeout=5,
        )
        self.assertEqual(r_bad.status_code, 400)

        # 3. Weak PIN (123456) is allowed per user choice, with weak_warning=True
        r_weak = requests.post(
            f"{self.base_url}/api/desktop/pin/set",
            headers=self.desktop_headers,
            json={"pin": "123456"},
            timeout=5,
        )
        self.assertEqual(r_weak.status_code, 200)
        self.assertTrue(r_weak.json()["weak_warning"])
        self.assertTrue(r_weak.json()["pin_configured"])

        # 4. Strong custom PIN (482915) sets weak_warning=False & persists across server restart!
        r_set = requests.post(
            f"{self.base_url}/api/desktop/pin/set",
            headers=self.desktop_headers,
            json={"pin": "482915"},
            timeout=5,
        )
        self.assertEqual(r_set.status_code, 200)
        self.assertFalse(r_set.json()["weak_warning"])
        self.assertEqual(self.cfg.custom_pin, "482915")

        # 5. Light/Dark Theme persists in config & exposes on /api/info
        r_theme = requests.post(
            f"{self.base_url}/api/desktop/settings",
            headers=self.desktop_headers,
            json={"theme": "light"},
            timeout=5,
        )
        self.assertEqual(r_theme.status_code, 200)
        info = requests.get(f"{self.base_url}/api/info", timeout=5).json()
        self.assertEqual(info["theme"], "light")

        # 6. Simulate server restart using same AppConfig -> custom PIN 482915 still works!
        self.server.stop()
        server2 = LANShareServer(config=self.cfg, trusted_file=self.trusted_file)
        server2.start()
        try:
            url2 = f"http://127.0.0.1:{server2.port}"
            h2 = {"X-Desktop-Token": server2.state.security.desktop_token}
            st2 = requests.get(f"{url2}/api/desktop/state", headers=h2, timeout=5).json()
            self.assertTrue(st2["pin_configured"])
            self.assertEqual(st2["pin"], "482915")
            r_login = requests.post(f"{url2}/api/auth/verify", json={"pin": "482915"}, timeout=5)
            self.assertEqual(r_login.status_code, 200)
        finally:
            server2.stop()


if __name__ == "__main__":
    unittest.main()
