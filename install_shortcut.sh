#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="${HOME}/.local/share/applications"
ICONS_BASE="${HOME}/.local/share/icons/hicolor"
DESKTOP_DIR="${HOME}/Desktop"

echo "==> Membuat ikon VoltDeck PETIR..."
python3 "${APP_DIR}/assets/generate_icons.py"

echo "==> Memasang ikon ke tema hicolor (${ICONS_BASE})..."
for sz in 48 64 128 256 512; do
  mkdir -p "${ICONS_BASE}/${sz}x${sz}/apps"
  cp -f "${APP_DIR}/assets/icons/lanshare-${sz}.png" "${ICONS_BASE}/${sz}x${sz}/apps/voltdeck.png"
  cp -f "${APP_DIR}/assets/icons/lanshare-${sz}.png" "${ICONS_BASE}/${sz}x${sz}/apps/lanshare.png"
done
mkdir -p "${ICONS_BASE}/scalable/apps"
cp -f "${APP_DIR}/assets/lanshare.svg" "${ICONS_BASE}/scalable/apps/voltdeck.svg"
cp -f "${APP_DIR}/assets/lanshare.svg" "${ICONS_BASE}/scalable/apps/lanshare.svg"

echo "==> Membuat shortcut menu aplikasi Linux Mint (VoltDeck)..."
mkdir -p "${APPS_DIR}"
chmod +x "${APP_DIR}/lanshare.py"
rm -f "${APPS_DIR}/lanshare.desktop"
sed "s|@APP_DIR@|${APP_DIR}|g" "${APP_DIR}/lanshare.desktop.in" > "${APPS_DIR}/voltdeck.desktop"
chmod +x "${APPS_DIR}/voltdeck.desktop"

if [ -d "${DESKTOP_DIR}" ]; then
  echo "==> Membuat shortcut di Desktop (${DESKTOP_DIR}/VoltDeck.desktop)..."
  rm -f "${DESKTOP_DIR}/LANShare.desktop"
  cp -f "${APPS_DIR}/voltdeck.desktop" "${DESKTOP_DIR}/VoltDeck.desktop"
  chmod +x "${DESKTOP_DIR}/VoltDeck.desktop"
  if command -v gio >/dev/null 2>&1; then
    gio set "${DESKTOP_DIR}/VoltDeck.desktop" metadata::trusted true 2>/dev/null || true
  fi
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${ICONS_BASE}" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${APPS_DIR}" 2>/dev/null || true
fi

# Pasang aksi klik kanan Nemo (File Manager Linux Mint)
NEMO_ACTIONS_DIR="${HOME}/.local/share/nemo/actions"
echo "==> Memasang menu klik kanan Nemo (${NEMO_ACTIONS_DIR}/voltdeck.nemo_action)..."
mkdir -p "${NEMO_ACTIONS_DIR}"
rm -f "${NEMO_ACTIONS_DIR}/lanshare.nemo_action"
sed "s|@APP_DIR@|${APP_DIR}|g" "${APP_DIR}/lanshare.nemo_action.in" > "${NEMO_ACTIONS_DIR}/voltdeck.nemo_action"

# Pasang Autostart Standby di System Tray Linux Mint
AUTOSTART_DIR="${HOME}/.config/autostart"
echo "==> Memasang Standby Autostart di Tray (${AUTOSTART_DIR}/lanshare.desktop)..."
mkdir -p "${AUTOSTART_DIR}"
cat > "${AUTOSTART_DIR}/lanshare.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=VoltDeck (Standby)
Comment=VoltDeck standby di system tray untuk transfer & remote dari iOS/Web
Exec=/usr/bin/python3 ${APP_DIR}/lanshare.py --tray
Icon=voltdeck
Terminal=false
Categories=Network;Utility;
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=4
EOF

echo "Selesai! VoltDeck tersedia di Menu Aplikasi, Desktop, Klik Kanan Nemo, dan Standby Autostart."
