<div align="center">

<img src="assets/voltdeck-banner.svg" alt="VoltDeck Banner Logo" width="100%" />

<br />

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3B82F6?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![GTK3 + WebKit2](https://img.shields.io/badge/Desktop-GTK3%20%2B%20WebKit2-22C55E?style=flat-square)](https://webkitgtk.org/)
[![Platform](https://img.shields.io/badge/OS-Linux%20Mint%20%2F%20Ubuntu-60A5FA?style=flat-square&logo=linuxmint&logoColor=white)](https://linuxmint.com/)
[![Zero Mobile App](https://img.shields.io/badge/Client-Safari%20%2F%20Any%20Browser-0EA5E9?style=flat-square&logo=safari&logoColor=white)](#)

**English** · [Bahasa Indonesia](README.id.md)

*Two-way streaming file transfer, real-time shared clipboard, laptop photo gallery & webcam capture, live MJPEG screen view, and full remote desktop control right from iPhone Safari, iPad, or any browser — with **Offline Mode (LAN Only)** and **Anywhere Mode (Tailscale Private VPN)**.*

</div>

---

## ✨ Features

### 🔄 Two-Way Streaming File Transfer
- **iOS / Web → Linux Mint:** Chunked binary streaming (`256 KB` chunks) directly to `~/Downloads/LANShare` with constant low RAM usage, live progress bar, transfer speed (MB/s), and cancel support.
- **Linux Mint → iOS / Web:** Zero-copy file sharing with HTTP `Range` (`206 Partial Content`) support, image/video lightbox preview, automatic thumbnails, and on-the-fly streaming `.zip` batch downloads.
- **Native Linux Mint Integration:**
  - Drag-and-drop directly from **Nemo** onto the VoltDeck window.
  - Nemo right-click context action: **`Kirim ke iPhone (VoltDeck)`**.
  - **Paste Screenshot (`Ctrl+V`)** directly from the Linux Mint clipboard straight to iOS Safari.
  - Dedicated **Camera** button on iPhone Safari to snap documents/notes straight to your laptop.

### 📋 Two-Way Shared Clipboard + Open Link on Laptop
- Automatic Linux Mint `Gtk.Clipboard` sync to Safari iOS (with HTTP-compatible one-tap copy fallback).
- Paste any WhatsApp message or link on iPhone → tap **`Buka di Laptop`** to open safe `http(s)://` URLs directly in your default Linux Mint browser.

### 🖼️ Laptop Gallery & Instant Capture
- Browse and pull images directly from laptop folders (`Pictures`, `Downloads`, `Desktop`, `Documents`, `LANShare`) in a responsive thumbnail grid.
- One-tap **Laptop Webcam Capture** (`/dev/video0` via GStreamer / `fswebcam` / `ffmpeg`) and **Desktop Screenshot** straight to your phone.

### 🎛️ Complete Remote Control Deck
- **Real-Time Telemetry:** Monitor laptop **Battery (+ charging status)**, **CPU %**, **RAM %**, and **Temperature (°C)**.
- **Media & Audio:** Play/Pause, Next/Previous, and `±10s` Seek via Session D-Bus **MPRIS2** (YouTube, Spotify, VLC, Celluloid), plus volume slider and **Audio Output Sink Switcher** (Speakers ↔ Headset/Bluetooth via `pactl`).
- **Display & Safe Power Controls:** Brightness slider, **Display Off** (`DPMS`), **Lock Screen**, **Sleep**, **Restart**, and **Shutdown** protected by 2-step confirmation, selectable timer (`15s / 15m / 30m / 60m`), and a live cancel button on both phone and desktop.
- **App Launcher:** Launch installed Linux Mint desktop apps (Chrome, Firefox, Nemo, Terminal, VS Code, Spotify, LibreOffice, Settings, etc.) via a strict binary allowlist.
- **Live Screen View (MJPEG):** Stream your Linux Mint screen live in Safari (`2 / 5 / 8 FPS`) with optional **Tap-to-Click** cursor interaction.
- **Touchpad & Presentation Clicker:** Touch/mouse trackpad via X11 `XTest`, presentation keys (`Prev / Next / F5 / Esc`), and remote keyboard typing.

### 🌐 Dual-Mode Access & Security
- **`Offline · LAN` (Default):** Strictly allows local Wi-Fi/LAN clients (`192.168.x.x`, `10.x.x.x`, `172.16–31.x.x`, `.local`) and rejects external/VPN traffic (`403 Forbidden`).
- **`Anywhere · VPN` (Tailscale):** Control and transfer from anywhere (4G/5G) over your private end-to-end encrypted Tailscale mesh — raw public internet IPs remain strictly blocked.
- **6-Digit Security PIN:** Cryptographically random PIN (`secrets`), constant-time verification, brute-force lockout (60s after 5 failed attempts), instant PIN rotation, and session revocation.
- **Trusted Devices:** Approve your iPhone as a **Trusted Device** (stored as SHA-256 hash only in `~/.config/lanshare/trusted_devices.json`, `0600`) for seamless auto-login across laptop restarts.
- **GTK3 Single-Instance & Low-RAM Tray Standby:** `--tray` standby mode stays ready in the Linux Mint system tray without initializing WebKit2 until the window is opened.
- **All-Device Responsive Web UI:** Midnight Sapphire UI that adapts seamlessly across phones (bottom nav bar), tablets (2–3 column grids), and desktop browsers (left sidebar + multi-column dashboard).

---

## 🛠️ System Requirements (Linux Mint / Ubuntu / Debian)

VoltDeck runs on standard Python 3 and native Linux Mint system packages (no extra `pip` packages required when these system libraries are installed):

```bash
sudo apt update
sudo apt install -y \
  python3 python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
  gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7 \
  python3-pil python3-qrcode python3-netifaces python3-psutil \
  python3-xlib python3-dbus pulseaudio-utils x11-xserver-utils
```

*(Optional for **Anywhere Mode**: install [Tailscale](https://tailscale.com/download/linux) and sign in once with `sudo tailscale up`.)*

---

## 🚀 Installation & Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/<username>/VoltDeck.git
cd VoltDeck

# 2. Install icons, App Menu & Desktop shortcuts, Nemo right-click action, & Tray Autostart
bash install_shortcut.sh

# 3. Launch VoltDeck desktop app
python3 lanshare.py
```

### CLI Options (Single-Instance)

```bash
# Start silently in the system tray (low-RAM standby)
python3 lanshare.py --tray

# Run on a custom port
python3 lanshare.py --port 9000

# Queue local files directly into the running VoltDeck instance
python3 lanshare.py ~/Documents/report.pdf ~/Downloads/photo.jpg

# Headless server mode (no GTK3 window)
python3 lanshare.py --headless
```

---

## 🔥 Firewall Setup (UFW)

If UFW is enabled on Linux Mint, allow default port `8765`:

```bash
# For Offline Mode (local Wi-Fi / LAN)
sudo ufw allow 8765/tcp comment 'VoltDeck LAN'

# Optional: for Anywhere Mode (Tailscale private VPN interface)
sudo ufw allow in on tailscale0 to any port 8765 proto tcp comment 'VoltDeck Tailscale'
```

---

## 📁 Project Structure

```text
.
├── lanshare.py                   # Main entrypoint (GTK3 Desktop / --tray / --headless)
├── install_shortcut.sh           # Installs icons, Menu/Desktop shortcuts, Nemo Action & Autostart
├── lanshare.desktop.in           # Linux Mint .desktop launcher template
├── lanshare.nemo_action.in       # Nemo file manager right-click action template
├── assets/
│   ├── generate_icons.py         # Lightning SVG & multi-res PNG icon generator (24px – 512px)
│   ├── lanshare.svg              # Primary VoltDeck vector icon
│   ├── hero_character.png        # Full transparent desktop hero artwork
│   └── hero_character_mobile.png # Compact portrait artwork for mobile screens
├── lanshare/
│   ├── __init__.py               # Application metadata (VoltDeck)
│   ├── config.py                 # Config persistence & XDG Autostart helpers
│   ├── network.py                # LAN interface discovery, mDNS (.local), Tailscale & QR SVG
│   ├── security.py               # 6-digit PIN, rate limiting, HMAC sessions & Trusted Devices
│   ├── storage.py                # Streaming file registry, thumbnails, ZIP stream & gallery scanner
│   ├── clipboard_store.py        # Shared clipboard history & anti-loop hash deduplication
│   ├── remote_control.py         # Telemetry, MPRIS2, pactl, brightness, MJPEG, XTest & power
│   ├── server.py                 # Multi-threaded streaming HTTP + SSE + Dual-Mode network gate
│   ├── desktop_app.py            # GTK3 + WebKit2 Single-Instance app & Ayatana System Tray
│   └── web/                      # Desktop & All-Device Responsive Web UI (HTML/CSS/JS)
└── tests/
    └── test_core.py              # End-to-end automated test suite (11 test cases)
```

---

## 🧪 Running Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers 6-digit PIN security & brute-force lockout, streaming upload & filename sanitization, streaming download (`Range 206`, thumbnails, streaming ZIP), two-way shared clipboard & SSE events, safe URL opening, remote controls & MJPEG streaming, Dual-Mode *Offline vs Anywhere* enforcement, Trusted Device persistence across restarts, App Launcher allowlist, Webcam snapshot, and Laptop Gallery browser.

---

<div align="center">
  <sub>© 2026 <strong>VoltDeck</strong> · Stay Connected Anywhere</sub>
</div>
