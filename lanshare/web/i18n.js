/* VoltDeck Bilingual i18n Engine (Bahasa Indonesia / English) */
(() => {
  'use strict';

  const DICT = {
    id: {
      // Common
      folder: 'Folder',
      send_file: 'Kirim File',
      copy: 'Salin',
      download: 'Unduh',
      close: 'Tutup',
      cancel: 'Batal',
      clear: 'Bersihkan',
      open: 'Buka',
      change_folder: 'Ubah Folder',
      refresh: 'Muat Ulang',
      lock: 'Kunci',

      // Desktop Nav & Header
      nav_home: 'Home',
      nav_transfer: 'Transfer',
      nav_clipboard: 'Clipboard',
      nav_settings: 'Pengaturan',
      access_mode: 'Mode Akses',
      mode_anywhere: 'Anywhere · VPN',
      mode_offline: 'Offline · LAN',
      allow_remote_power_vpn: 'Izinkan Remote Power via VPN',
      qr_pairing_lan: 'QR Pairing (LAN)',
      qr_pairing_anywhere: 'QR Anywhere (Tailscale)',
      scan_qr_title: 'Scan QR Code',
      qr_desc_lan: 'Pindai dengan kamera HP untuk membuka VoltDeck di browser.',
      qr_desc_anywhere: 'Gunakan Tailscale untuk mengakses laptop dari mana saja.',
      download_qr: 'Download QR',
      pin_title: 'PIN Keamanan 6 Digit',
      rotate_pin: 'Acak PIN',
      reset_sessions: 'Reset Sesi',
      connected_devices: 'Perangkat Terhubung',
      trusted_devices: 'Perangkat Terpercaya',
      waiting_connection: 'MENUNGGU KONEKSI PERANGKAT',
      trust_btn: 'Percayai',
      revoke_trust: 'Cabut',
      auto_login_active: 'Auto-login aktif',
      status_stable: 'Koneksi Aman & Stabil',
      status_vpn_stable: 'Tailscale VPN · Aman & Stabil',
      status_ts_wait: 'Menunggu Tailscale',
      ts_online: 'Tailscale Online',
      mode_offline_title: 'Mode Offline · LAN',

      // Desktop Transfer & Clipboard
      filter_all: 'Semua',
      filter_out: 'Linux → HP',
      filter_in: 'HP → Linux',
      paste_screenshot: 'Paste Screenshot',
      drop_hint_strong: 'Seret file dari Nemo ke sini',
      drop_hint_rest: 'atau klik untuk dibagikan langsung ke perangkat Anda',
      empty_files_title: 'Belum ada file di daftar transfer',
      empty_files_desc: 'Seret file dari Linux Mint ke sini untuk dikirim ke HP, atau kirim foto & file langsung dari browser HP.',
      auto_sync_clip: 'Auto-Sync Clipboard Linux Mint (Ctrl+C otomatis ke HP)',
      clear_history: 'Hapus Riwayat',
      clip_placeholder: 'Ketik atau tempel teks, tautan, atau potongan kode untuk dikirim instan ke HP...',
      clip_hint: 'Ctrl + Enter untuk kirim cepat',
      send_to_phone: 'Kirim ke HP',
      empty_clip_title: 'Shared Clipboard Kosong',
      empty_clip_desc: 'Salin teks di Linux Mint (Ctrl+C) atau kirim teks dari HP — keduanya tersinkron instan.',
      open_link: 'Buka Link',
      drop_overlay_title: 'Lepaskan File untuk Dibagikan',
      drop_overlay_sub: 'File akan tersedia instan di browser perangkat Anda via VoltDeck',

      // Desktop Settings
      settings_system: 'Kontrol & Sistem',
      set_language: 'Bahasa Aplikasi',
      set_language_sub: 'Pilih bahasa antarmuka Desktop & Web (Indonesia / English)',
      set_remote: 'Izinkan Remote Control dari HP / Web',
      set_remote_sub: 'Media, volume, kecerahan, aplikasi, live screen, touchpad & daya',
      set_autostart: 'Standby Autostart saat Login',
      set_autostart_sub: 'Otomatis siaga di system tray (hemat RAM tanpa membuka jendela)',
      set_mintray: 'Sembunyi ke Tray saat Ditutup (X)',
      set_mintray_sub: 'Server tetap aktif di latar belakang agar selalu bisa di-remote',
      set_notify: 'Notifikasi Desktop Linux Mint',
      set_notify_sub: 'Tampilkan pemberitahuan saat file masuk atau perangkat terhubung',
      set_receive_dir: 'Folder Penyimpanan File Masuk',
      network_updated: 'Jaringan Wi-Fi berubah — URL & QR diperbarui:',

      // Mobile / Web PIN & Tabs
      pin_prompt_prefix: 'Masukkan PIN 6 digit di',
      pin_clear: 'Hapus',
      tab_files: 'File',
      tab_gallery: 'Galeri',
      tab_clipboard: 'Clipboard',
      tab_remote: 'Remote',

      // Mobile Files & Gallery
      send_to_laptop: 'Kirim ke Laptop',
      btn_gallery: 'Galeri',
      btn_camera: 'Kamera',
      btn_files: 'File',
      active_transfers: 'Transfer Aktif',
      from_laptop: 'Dari Laptop',
      download_all_zip: 'Unduh Semua (.zip)',
      sent_to_laptop: 'Terkirim ke Laptop',
      no_files_from_laptop: 'Belum ada file yang dibagikan dari laptop.',
      saved_on_laptop: 'Tersimpan di Laptop',
      pull_instant_title: 'Ambil Gambar Instan dari Laptop',
      btn_webcam: 'Foto Webcam',
      btn_screenshot: 'Screenshot Layar',
      laptop_gallery_title: 'Galeri Folder Laptop',
      all_folders: 'Semua Folder',
      no_images_folder: 'Belum ada gambar di folder ini.',

      // Mobile Clipboard
      latest_clip: 'Clipboard Terbaru',
      copy_to_device: 'Salin ke Perangkat',
      send_text_title: 'Kirim Teks / Link ke Laptop',
      m_clip_placeholder: 'Tempel link dari WA, kode OTP, atau catatan di sini...',
      send_to_clipboard: 'Kirim ke Clipboard',
      open_on_laptop: 'Buka di Laptop',
      clip_history: 'Riwayat Clipboard',
      clip_empty: 'Belum ada teks di Shared Clipboard.',

      // Mobile Remote
      tele_bat: 'BATERAI',
      tele_cpu: 'CPU',
      tele_ram: 'RAM',
      tele_temp: 'SUHU',
      rsub_controls: 'Kontrol',
      rsub_apps: 'Aplikasi',
      rsub_screen: 'Layar Live',
      rsub_touchpad: 'Touchpad',
      remote_disabled_msg: 'Remote Control sedang dinonaktifkan di aplikasi laptop. Aktifkan toggle Remote di pengaturan desktop.',
      media_title: 'Media Laptop',
      media_none: 'Tidak ada media aktif',
      media_hint: 'Putar YouTube, Spotify, atau VLC di laptop',
      audio_title: 'Audio & Output',
      bright_title: 'Kecerahan & Layar',
      display_off_btn: 'Matikan Layar Saja (Laptop Tetap Nyala)',
      power_title: 'Kontrol Daya Laptop',
      lock_screen: 'Kunci Layar',
      sleep_btn: 'Sleep',
      power_timer_label: 'Timer Matikan / Restart',
      restart_btn: 'Restart',
      shutdown_btn: 'Matikan',
      cancel_now: 'Batalkan Sekarang',
      apps_title: 'Buka Aplikasi di Laptop',
      screen_title: 'Intip Layar Laptop (Live)',
      screen_placeholder: 'Ketuk Mulai Live untuk memantau layar laptop secara langsung, atau ketuk Snapshot.',
      start_live: 'Mulai Live',
      stop_live: 'Stop Live',
      snapshot_btn: 'Snapshot',
      save_jpg: 'Simpan JPG',
      tap_to_click_label: 'Tap pada layar live untuk klik kursor di laptop',
      presentation_title: 'Slide Presentasi & Shortcut',
      slide_prev: '◀ Slide Prev',
      slide_next: 'Slide Next ▶',
      trackpad_title: 'Trackpad Laptop',
      trackpad_hint: 'Geser jari untuk gerak kursor · Ketuk = Klik Kiri',
      left_click: 'Klik Kiri',
      right_click: 'Klik Kanan',
      type_title: 'Ketik ke Laptop',
      type_placeholder: 'Ketik teks lalu kirim...',
      type_btn: 'Ketik',
    },

    en: {
      // Common
      folder: 'Folder',
      send_file: 'Send Files',
      copy: 'Copy',
      download: 'Download',
      close: 'Close',
      cancel: 'Cancel',
      clear: 'Clear',
      open: 'Open',
      change_folder: 'Change Folder',
      refresh: 'Refresh',
      lock: 'Lock',

      // Desktop Nav & Header
      nav_home: 'Home',
      nav_transfer: 'Transfer',
      nav_clipboard: 'Clipboard',
      nav_settings: 'Settings',
      access_mode: 'Access Mode',
      mode_anywhere: 'Anywhere · VPN',
      mode_offline: 'Offline · LAN',
      allow_remote_power_vpn: 'Allow Remote Power via VPN',
      qr_pairing_lan: 'QR Pairing (LAN)',
      qr_pairing_anywhere: 'QR Anywhere (Tailscale)',
      scan_qr_title: 'Scan QR Code',
      qr_desc_lan: 'Scan with your phone camera to open VoltDeck in the browser.',
      qr_desc_anywhere: 'Use Tailscale to access your laptop securely from anywhere.',
      download_qr: 'Download QR',
      pin_title: '6-Digit Security PIN',
      rotate_pin: 'Rotate PIN',
      reset_sessions: 'Reset Sessions',
      connected_devices: 'Connected Devices',
      trusted_devices: 'Trusted Devices',
      waiting_connection: 'WAITING FOR DEVICE CONNECTION',
      trust_btn: 'Trust',
      revoke_trust: 'Revoke',
      auto_login_active: 'Auto-login enabled',
      status_stable: 'Secure & Stable Connection',
      status_vpn_stable: 'Tailscale VPN · Secure & Stable',
      status_ts_wait: 'Waiting for Tailscale',
      ts_online: 'Tailscale Online',
      mode_offline_title: 'Offline Mode · LAN',

      // Desktop Transfer & Clipboard
      filter_all: 'All',
      filter_out: 'Linux → Phone',
      filter_in: 'Phone → Linux',
      paste_screenshot: 'Paste Screenshot',
      drop_hint_strong: 'Drag files from Nemo here',
      drop_hint_rest: 'or click to share instantly with your devices',
      empty_files_title: 'No files in transfer list yet',
      empty_files_desc: 'Drag files from Linux Mint here to send to your phone, or upload photos & files from your mobile browser.',
      auto_sync_clip: 'Auto-Sync Linux Mint Clipboard (Ctrl+C pushes to phone)',
      clear_history: 'Clear History',
      clip_placeholder: 'Type or paste text, links, or code snippets to push instantly to your phone...',
      clip_hint: 'Ctrl + Enter to send quickly',
      send_to_phone: 'Send to Phone',
      empty_clip_title: 'Shared Clipboard is Empty',
      empty_clip_desc: 'Copy text on Linux Mint (Ctrl+C) or send text from your phone — both sync instantaneously.',
      open_link: 'Open Link',
      drop_overlay_title: 'Drop Files to Share',
      drop_overlay_sub: 'Files will be instantly available on your browser via VoltDeck',

      // Desktop Settings
      settings_system: 'Controls & System',
      set_language: 'App Language',
      set_language_sub: 'Choose Desktop & Web interface language (Indonesian / English)',
      set_remote: 'Allow Remote Control from Phone / Web',
      set_remote_sub: 'Media, volume, brightness, apps, live screen, touchpad & power',
      set_autostart: 'Standby Autostart on Login',
      set_autostart_sub: 'Start silently in system tray on login (low RAM)',
      set_mintray: 'Minimize to Tray on Close (X)',
      set_mintray_sub: 'Keep server running in background so remote control stays ready',
      set_notify: 'Linux Mint Desktop Notifications',
      set_notify_sub: 'Show desktop alerts when files arrive or devices connect',
      set_receive_dir: 'Incoming Files Folder',
      network_updated: 'Wi-Fi network changed — URL & QR updated:',

      // Mobile / Web PIN & Tabs
      pin_prompt_prefix: 'Enter the 6-digit PIN on',
      pin_clear: 'Clear',
      tab_files: 'Files',
      tab_gallery: 'Gallery',
      tab_clipboard: 'Clipboard',
      tab_remote: 'Remote',

      // Mobile Files & Gallery
      send_to_laptop: 'Send to Laptop',
      btn_gallery: 'Photos',
      btn_camera: 'Camera',
      btn_files: 'Files',
      active_transfers: 'Active Transfers',
      from_laptop: 'From Laptop',
      download_all_zip: 'Download All (.zip)',
      sent_to_laptop: 'Sent to Laptop',
      no_files_from_laptop: 'No files shared from the laptop yet.',
      saved_on_laptop: 'Saved on Laptop',
      pull_instant_title: 'Instant Capture from Laptop',
      btn_webcam: 'Webcam Photo',
      btn_screenshot: 'Screen Capture',
      laptop_gallery_title: 'Laptop Folders Gallery',
      all_folders: 'All Folders',
      no_images_folder: 'No images found in this folder.',

      // Mobile Clipboard
      latest_clip: 'Latest Clipboard',
      copy_to_device: 'Copy to Device',
      send_text_title: 'Send Text / Link to Laptop',
      m_clip_placeholder: 'Paste a WhatsApp link, OTP code, or note here...',
      send_to_clipboard: 'Send to Clipboard',
      open_on_laptop: 'Open on Laptop',
      clip_history: 'Clipboard History',
      clip_empty: 'No text in Shared Clipboard yet.',

      // Mobile Remote
      tele_bat: 'BATTERY',
      tele_cpu: 'CPU',
      tele_ram: 'RAM',
      tele_temp: 'TEMP',
      rsub_controls: 'Controls',
      rsub_apps: 'Apps',
      rsub_screen: 'Live View',
      rsub_touchpad: 'Touchpad',
      remote_disabled_msg: 'Remote Control is currently disabled on the laptop. Enable the Remote toggle in Desktop Settings.',
      media_title: 'Laptop Media',
      media_none: 'No active media player',
      media_hint: 'Play YouTube, Spotify, or VLC on your laptop',
      audio_title: 'Audio & Output',
      bright_title: 'Brightness & Display',
      display_off_btn: 'Turn Off Display Only (Keep Laptop Awake)',
      power_title: 'Laptop Power Controls',
      lock_screen: 'Lock Screen',
      sleep_btn: 'Sleep',
      power_timer_label: 'Shutdown / Restart Timer',
      restart_btn: 'Restart',
      shutdown_btn: 'Shutdown',
      cancel_now: 'Cancel Now',
      apps_title: 'Launch Laptop Apps',
      screen_title: 'Live Laptop Screen',
      screen_placeholder: 'Tap Start Live to monitor your laptop screen in real time, or tap Snapshot.',
      start_live: 'Start Live',
      stop_live: 'Stop Live',
      snapshot_btn: 'Snapshot',
      save_jpg: 'Save JPG',
      tap_to_click_label: 'Tap live screen to click cursor on laptop',
      presentation_title: 'Presentation & Shortcuts',
      slide_prev: '◀ Prev Slide',
      slide_next: 'Next Slide ▶',
      trackpad_title: 'Laptop Trackpad',
      trackpad_hint: 'Drag finger to move cursor · Tap = Left Click',
      left_click: 'Left Click',
      right_click: 'Right Click',
      type_title: 'Type on Laptop',
      type_placeholder: 'Type text to send...',
      type_btn: 'Type',
    },
  };

  let currentLang = 'id';
  try {
    const saved = window.localStorage.getItem('voltdeck_lang');
    if (saved === 'en' || saved === 'id') currentLang = saved;
  } catch {}

  function t(key) {
    const table = DICT[currentLang] || DICT.id;
    return table[key] !== undefined ? table[key] : DICT.id[key] || key;
  }

  function applyDomTranslations(root = document) {
    document.documentElement.lang = currentLang;
    root.querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.getAttribute('data-i18n');
      if (key) el.textContent = t(key);
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      const key = el.getAttribute('data-i18n-placeholder');
      if (key) el.setAttribute('placeholder', t(key));
    });
    root.querySelectorAll('[data-lang-btn]').forEach((btn) => {
      btn.classList.toggle('active', btn.getAttribute('data-lang-btn') === currentLang);
    });
    const compactBtn = document.getElementById('btn-lang-cycle');
    if (compactBtn) {
      compactBtn.textContent = currentLang.toUpperCase();
    }
  }

  function setLang(lang, persist = true) {
    if (lang !== 'id' && lang !== 'en') return currentLang;
    currentLang = lang;
    if (persist) {
      try {
        window.localStorage.setItem('voltdeck_lang', lang);
      } catch {}
    }
    applyDomTranslations();
    return currentLang;
  }

  function getLang() {
    return currentLang;
  }

  window.VDI18n = { t, setLang, getLang, applyDomTranslations };
})();
