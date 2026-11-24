/* LANShare Desktop Frontend Controller */
(() => {
  'use strict';

  const state = {
    pin: '',
    accessMode: 'offline',
    baseUrl: '',
    mdnsUrl: '',
    anywhereUrl: '',
    tailscale: null,
    receiveDir: '',
    files: [],
    transfers: [],
    clipboard: [],
    devices: [],
    power: { scheduled: false, action: null, execute_at: 0, requested_by: '' },
    powerTick: null,
    filter: 'all',
    tab: 'files',
    sse: null,
  };

  const $ = (id) => document.getElementById(id);

  // --- Utilities ---

  function escHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let val = bytes / 1024;
    let idx = 0;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx++;
    }
    return `${val.toFixed(val >= 100 ? 0 : val >= 10 ? 1 : 2)} ${units[idx]}`;
  }

  function formatSpeed(bps) {
    if (!bps || bps <= 0) return '—';
    return `${formatBytes(bps)}/s`;
  }

  function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function fileExtBadge(name, kind) {
    const idx = name.lastIndexOf('.');
    if (idx > 0 && idx < name.length - 1) {
      return name.slice(idx + 1, idx + 5).toUpperCase();
    }
    const map = { image: 'IMG', video: 'VID', audio: 'AUD', archive: 'ZIP', document: 'DOC' };
    return map[kind] || 'FILE';
  }

  function extractHttpUrl(text) {
    const m = String(text || '').match(/https?:\/\/[^\s<>'"`]+/i);
    if (!m) return null;
    return m[0].replace(/[.,;:!?)>\]}]+$/, '');
  }

  function dismissBootSplash() {
    const el = $('boot-splash');
    if (!el) return;
    setTimeout(() => {
      el.classList.add('fade-out');
      setTimeout(() => el.remove(), 350);
    }, 1050);
  }

  function toast(message, type = 'info') {
    const container = $('toasts');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      el.style.transition = 'opacity 180ms ease, transform 180ms ease';
      setTimeout(() => el.remove(), 200);
    }, 2800);
  }

  function hasNativeBridge() {
    return Boolean(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.lanshare);
  }

  function sendNative(action, payload = {}) {
    if (hasNativeBridge()) {
      window.webkit.messageHandlers.lanshare.postMessage(JSON.stringify({ action, ...payload }));
      return true;
    }
    return false;
  }

  async function copyToClipboard(text) {
    if (sendNative('copy_text', { text })) {
      return true;
    }
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    }
  }

  async function api(url, options = {}) {
    const res = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
      },
    });
    const data = await res.json().catch(() => ({ ok: false, error: 'Invalid response' }));
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  // --- Rendering ---

  function renderPin(pin) {
    state.pin = pin || '';
    const digits = state.pin.padEnd(6, '-').slice(0, 6).split('');
    const slots = $('pin-grid').querySelectorAll('.pin-digit');
    slots.forEach((slot, i) => {
      slot.textContent = digits[i] || '-';
    });
  }

  function renderSecurity(sec) {
    if (sec.pin) renderPin(sec.pin);
    if (sec.access_mode) {
      state.accessMode = sec.access_mode;
    }
    if (sec.base_url) {
      state.baseUrl = sec.base_url;
    }
    if ('anywhere_url' in sec) {
      state.anywhereUrl = sec.anywhere_url || '';
    }
    if ('tailscale' in sec) {
      state.tailscale = sec.tailscale;
    }

    const isAnywhere = state.accessMode === 'anywhere';
    const activeTopUrl = isAnywhere && state.anywhereUrl ? state.anywhereUrl : state.baseUrl;
    if (activeTopUrl) {
      $('lan-url').textContent = activeTopUrl;
    }

    // Dual-Mode Switcher & Tailscale Panel
    document.querySelectorAll('[data-mode]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.mode === state.accessMode);
    });
    const badge = $('mode-badge');
    if (badge) {
      badge.textContent = isAnywhere ? 'ANYWHERE · VPN' : 'OFFLINE · LAN';
      badge.className = isAnywhere ? 'tag tag-out' : 'tag tag-in';
    }

    $('wrap-qr-pin').classList.toggle('hidden', isAnywhere);
    $('qr-section-title').textContent = isAnywhere ? 'QR Anywhere (Tailscale)' : 'QR Pairing (LAN)';
    $('qr-caption').textContent =
      isAnywhere && state.anywhereUrl
        ? 'Gunakan Tailscale untuk mengakses server dari mana saja.'
        : 'Pindai dengan Kamera iPhone lalu buka di Safari.';

    const dot = $('ts-dot');
    const statusLabel = $('vd-status-label');
    if (isAnywhere) {
      const ts = state.tailscale || {};
      if (ts.running && state.anywhereUrl) {
        dot.style.background = 'var(--emerald)';
        $('ts-status-text').textContent = 'Tailscale Online';
        $('ts-hint').classList.add('hidden');
        $('ts-meta').classList.remove('hidden');
        $('anywhere-url-text').textContent = state.anywhereUrl.replace(/^http:\/\//, '');
        if (statusLabel) statusLabel.textContent = 'Tailscale VPN · Aman & Stabil';
      } else {
        dot.style.background = 'var(--amber)';
        $('ts-status-text').textContent = ts.installed
          ? `Tailscale: ${ts.backend_state || 'Offline'}`
          : 'Tailscale Belum Terpasang';
        $('ts-hint').classList.remove('hidden');
        $('ts-meta').classList.add('hidden');
        $('anywhere-url-text').textContent = 'Belum aktif';
        if (statusLabel) statusLabel.textContent = 'Menunggu Tailscale';
      }
    } else {
      dot.style.background = 'var(--emerald)';
      $('ts-status-text').textContent = 'Mode Offline · LAN';
      $('ts-hint').classList.add('hidden');
      $('ts-meta').classList.add('hidden');
      $('anywhere-url-text').textContent = (state.baseUrl || '').replace(/^http:\/\//, '') || '—';
      if (statusLabel) statusLabel.textContent = 'Koneksi Aman & Stabil';
    }

    if ('mdns_url' in sec) {
      state.mdnsUrl = sec.mdns_url || '';
      const row = $('mdns-row');
      if (state.mdnsUrl && !isAnywhere) {
        row.classList.remove('hidden');
        $('mdns-url').textContent = state.mdnsUrl.replace(/^http:\/\//, '');
      } else {
        row.classList.add('hidden');
      }
    }
    if (sec.qr_data_uri) {
      $('qr-img').src = sec.qr_data_uri;
    }
    if (sec.receive_dir) {
      state.receiveDir = sec.receive_dir;
      $('receive-dir-text').textContent = sec.receive_dir;
      $('receive-dir-text').title = sec.receive_dir;
    }
    if (sec.config) {
      $('toggle-qr-pin').checked = Boolean(sec.config.qr_include_pin);
      $('toggle-qr-mdns').checked = Boolean(sec.config.use_mdns_qr);
      $('toggle-anywhere-power').checked = sec.config.anywhere_allow_power !== false;
      $('toggle-remote').checked = Boolean(sec.config.remote_control_enabled);
      $('toggle-clip-sync').checked = Boolean(sec.config.clipboard_auto_sync);
      $('toggle-notify').checked = Boolean(sec.config.notifications_enabled);
      $('toggle-min-tray').checked = sec.config.minimize_to_tray !== false;
      if ('autostart_enabled' in sec.config) {
        $('toggle-autostart').checked = Boolean(sec.config.autostart_enabled);
      }
    }
  }

  function formatCountdown(sec) {
    const s = Math.max(0, Math.ceil(sec));
    if (s >= 60) {
      const m = Math.floor(s / 60);
      const r = s % 60;
      return `${m}m ${String(r).padStart(2, '0')}d`;
    }
    return `${s} detik`;
  }

  function renderPowerBanner(power) {
    state.power = power || { scheduled: false };
    if (state.powerTick) {
      clearInterval(state.powerTick);
      state.powerTick = null;
    }
    const banner = $('power-banner');
    if (!state.power.scheduled) {
      banner.classList.add('hidden');
      return;
    }
    banner.classList.remove('hidden');
    const updateText = () => {
      const rem = Math.max(0, Math.ceil(state.power.execute_at - Date.now() / 1000));
      const verbs = { shutdown: 'dimatikan', restart: 'di-restart', sleep: 'masuk mode Sleep' };
      const verb = verbs[state.power.action] || 'berubah status daya';
      $('power-banner-text').textContent = `Laptop akan ${verb} dalam ${formatCountdown(rem)} (dari ${state.power.requested_by || 'iPhone'})`;
      if (rem <= 0 && state.powerTick) {
        clearInterval(state.powerTick);
        state.powerTick = null;
      }
    };
    updateText();
    state.powerTick = setInterval(updateText, 500);
  }

  function renderInterfaces(interfaces, active) {
    const sel = $('iface-select');
    sel.innerHTML = '';
    if (!interfaces || interfaces.length === 0) return;
    interfaces.forEach((iface) => {
      const opt = document.createElement('option');
      opt.value = iface.name;
      opt.textContent = `${iface.name} · ${iface.ip}`;
      if (active && iface.name === active.name) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function renderDevices(devices, trustedDevices) {
    state.devices = devices || [];
    $('devices-count').textContent = String(state.devices.length);
    const list = $('devices-list');
    if (state.devices.length === 0) {
      list.innerHTML = '<div class="empty-inline">MENUNGGU KONEKSI LAN</div>';
    } else {
      list.innerHTML = state.devices
        .map((d) => {
          const isVpn = d.network_zone === 'tailscale';
          const zoneBadge = isVpn
            ? '<span class="tag tag-out" style="height:17px;font-size:9.5px">VPN</span>'
            : '<span class="tag tag-in" style="height:17px;font-size:9.5px">LAN</span>';
          const trustAction = d.trusted
            ? '<span class="tag tag-in" style="height:22px;font-size:9.5px" title="Auto-login aktif walau laptop restart">TRUST</span>'
            : `<button class="btn btn-sm" data-trust="${escHtml(d.id)}" style="height:24px;padding:0 7px;font-size:11px" title="Percayai perangkat ini agar bisa auto-login tanpa PIN walau laptop restart">Percayai</button>`;
          return `
        <div class="device-row">
          <div class="device-info">
            <span class="dot"></span>
            <div class="device-meta">
              <div class="device-name" title="${escHtml(d.device_name)}">${escHtml(d.device_name)}</div>
              <div class="device-ip" style="display:flex;align-items:center;gap:5px">${zoneBadge}<span>${escHtml(d.client_ip)}</span></div>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:4px">
            ${trustAction}
            <button class="btn btn-sm btn-ghost" data-revoke="${escHtml(d.id)}" style="height:24px;padding:0 6px" title="Putuskan sesi">✕</button>
          </div>
        </div>`;
        })
        .join('');
    }

    if (trustedDevices !== undefined) {
      const tList = trustedDevices || [];
      const wrap = $('trusted-wrap');
      $('trusted-count').textContent = String(tList.length);
      if (tList.length === 0) {
        wrap.classList.add('hidden');
        $('trusted-list').innerHTML = '';
      } else {
        wrap.classList.remove('hidden');
        $('trusted-list').innerHTML = tList
          .map(
            (t) => `
          <div class="device-row">
            <div class="device-info">
              <div class="device-meta">
                <div class="device-name" title="${escHtml(t.device_name)}">${escHtml(t.device_name)}</div>
                <div class="device-ip">Auto-login aktif</div>
              </div>
            </div>
            <button class="btn btn-sm btn-danger" data-untrust="${escHtml(t.trust_id)}" style="height:24px;padding:0 7px;font-size:11px">Cabut</button>
          </div>`
          )
          .join('');
      }
    }
  }

  function renderTransfers(transfers) {
    state.transfers = transfers || [];
    const strip = $('transfers-strip');
    const visible = state.transfers.filter((t) => t.status === 'active' || t.status === 'error');
    if (visible.length === 0) {
      strip.classList.add('hidden');
      strip.innerHTML = '';
      return;
    }
    strip.classList.remove('hidden');
    strip.innerHTML = visible
      .map((t) => {
        const dirTag = t.direction === 'outbound' ? '<span class="tag tag-out">↑ KE IPHONE</span>' : '<span class="tag tag-in">↓ DARI IPHONE</span>';
        const stats =
          t.status === 'active'
            ? `${t.percent}% · ${formatBytes(t.transferred_bytes)} / ${formatBytes(t.total_bytes)} · ${formatSpeed(t.speed_bps)}`
            : escHtml(t.error || t.status);
        const fillClass = t.status === 'active' ? '' : t.status;
        return `
        <div class="transfer-item">
          <div class="transfer-meta">
            ${dirTag}
            <span class="transfer-name" title="${escHtml(t.filename)}">${escHtml(t.filename)}</span>
            <span class="transfer-stats">${stats}</span>
          </div>
          ${
            t.status === 'active'
              ? `<button class="btn btn-sm btn-danger" data-cancel-tx="${escHtml(t.id)}">Batal</button>`
              : ''
          }
          <div class="progress-track">
            <div class="progress-fill ${fillClass}" style="width:${t.percent}%"></div>
          </div>
        </div>`;
      })
      .join('');
  }

  function renderFiles(files) {
    state.files = files || [];
    $('files-count').textContent = String(state.files.length);
    const filtered = state.files.filter((f) => state.filter === 'all' || f.direction === state.filter);
    const list = $('files-list');

    if (filtered.length === 0) {
      list.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="17 8 12 3 7 8"></polyline>
              <line x1="12" y1="3" x2="12" y2="15"></line>
            </svg>
          </div>
          <div class="empty-title">Belum ada file di daftar transfer</div>
          <div class="empty-desc">Seret file dari Linux Mint ke sini untuk dikirim ke Safari iPhone, atau kirim foto & file langsung dari iPhone Anda.</div>
        </div>`;
      return;
    }

    list.innerHTML = filtered
      .map((f) => {
        const isOut = f.direction === 'outbound';
        const dirBadge = isOut
          ? '<span class="tag tag-out">↑ LINUX → IPHONE</span>'
          : '<span class="tag tag-in">↓ IPHONE → LINUX</span>';
        const thumbHtml = f.has_thumb
          ? `<img src="/api/files/${escHtml(f.id)}/thumb" alt="" loading="lazy" />`
          : `<span class="file-ext">${escHtml(fileExtBadge(f.name, f.kind))}</span>`;
        const canPreview = f.kind === 'image' || f.kind === 'video';
        const dlInfo = f.downloads > 0 ? ` · ${f.downloads}× diunduh` : '';

        return `
        <div class="file-row">
          <div class="file-thumb" ${canPreview ? `data-preview="${escHtml(f.id)}"` : ''} title="${canPreview ? 'Pratinjau' : ''}">
            ${thumbHtml}
          </div>
          <div class="file-main">
            <div class="file-name" title="${escHtml(f.name)}">${escHtml(f.name)}</div>
            <div class="file-sub">
              ${dirBadge}
              <span>${escHtml(f.sender)}</span>
              <span class="mono">${formatTime(f.created_at)}${dlInfo}</span>
            </div>
          </div>
          <div class="file-size">${formatBytes(f.size)}</div>
          <div class="file-actions">
            <button class="btn btn-sm btn-ghost" data-open-path="${escHtml(f.path)}" title="Buka file">Buka</button>
            <button class="btn btn-sm btn-ghost" data-reveal-path="${escHtml(f.path)}" title="Tampilkan di Nemo">Folder</button>
            <button class="btn btn-sm btn-ghost" data-remove-file="${escHtml(f.id)}" title="Hapus dari daftar">✕</button>
          </div>
        </div>`;
      })
      .join('');
  }

  function renderClipboard(items) {
    state.clipboard = items || [];
    $('clip-count').textContent = String(state.clipboard.length);
    const list = $('clip-list');
    if (state.clipboard.length === 0) {
      list.innerHTML = `
        <div class="empty-state" style="padding:36px 20px">
          <div class="empty-title">Shared Clipboard Kosong</div>
          <div class="empty-desc">Salin teks di Linux Mint (Ctrl+C) atau kirim teks dari Safari iPhone — keduanya tersinkron instan secara lokal.</div>
        </div>`;
      return;
    }

    list.innerHTML = state.clipboard
      .map((item) => {
        const badge =
          item.source_kind === 'desktop'
            ? `<span class="tag tag-out">${escHtml(item.source)}</span>`
            : `<span class="tag tag-in">${escHtml(item.source)}</span>`;
        const detectedUrl = extractHttpUrl(item.text);
        const openBtn = detectedUrl
          ? `<button class="btn btn-sm btn-primary" data-open-url="${escHtml(detectedUrl)}" title="Buka tautan di browser Linux Mint">Buka Link</button>`
          : '';
        return `
        <div class="clip-card">
          <div class="clip-card-head">
            <div class="clip-card-meta">
              ${badge}
              <span class="mono">${formatTime(item.created_at)}</span>
              <span class="mono">${item.char_count} kar</span>
            </div>
            <div style="display:flex;gap:4px">
              ${openBtn}
              <button class="btn btn-sm" data-copy-clip="${escHtml(item.id)}">Salin</button>
              <button class="btn btn-sm btn-ghost" data-del-clip="${escHtml(item.id)}" title="Hapus">✕</button>
            </div>
          </div>
          <pre class="clip-card-text">${escHtml(item.text)}</pre>
        </div>`;
      })
      .join('');
  }

  // --- Initial Load & SSE ---

  async function loadFullState() {
    try {
      const data = await api('/api/desktop/state');
      $('host-name').textContent = data.device_name || 'Linux Mint';
      renderSecurity(data);
      renderInterfaces(data.interfaces, data.active_interface);
      renderDevices(data.devices, data.trusted_devices);
      renderFiles(data.files);
      renderTransfers(data.transfers);
      renderClipboard(data.clipboard);
      renderPowerBanner(data.power);
    } catch (err) {
      toast(`Gagal memuat status: ${err.message}`, 'error');
    }
  }

  function connectSSE() {
    if (state.sse) {
      try { state.sse.close(); } catch {}
    }
    const es = new EventSource('/api/events');
    state.sse = es;

    es.addEventListener('files_changed', (e) => {
      const d = JSON.parse(e.data);
      renderFiles(d.files);
    });
    es.addEventListener('transfer_progress', (e) => {
      const d = JSON.parse(e.data);
      renderTransfers(d.transfers);
    });
    es.addEventListener('clipboard_updated', (e) => {
      const d = JSON.parse(e.data);
      renderClipboard(d.clipboard);
    });
    es.addEventListener('devices_changed', (e) => {
      const d = JSON.parse(e.data);
      renderDevices(d.devices, d.trusted_devices);
    });
    es.addEventListener('security_changed', (e) => {
      const d = JSON.parse(e.data);
      renderSecurity(d);
    });
    es.addEventListener('power_changed', (e) => {
      const d = JSON.parse(e.data);
      renderPowerBanner(d.power);
    });
    es.onerror = () => {
      $('server-dot').style.background = '#f59e0b';
      setTimeout(() => {
        if (es.readyState === EventSource.CLOSED) connectSSE();
      }, 2000);
    };
    es.onopen = () => {
      $('server-dot').style.background = '';
    };
  }

  // --- Browser Drop / File Fallback Upload (for WebKit HTML5 drops or browser preview) ---

  async function uploadBrowserFilesAsOutbound(fileList) {
    const files = Array.from(fileList || []);
    for (const file of files) {
      await new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload');
        xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
        xhr.setRequestHeader('X-Direction', 'outbound');
        xhr.onload = () => resolve();
        xhr.onerror = () => resolve();
        xhr.send(file);
      });
    }
    if (files.length > 0) {
      toast(`${files.length} file dibagikan ke iPhone`, 'success');
    }
  }

  function openLightbox(file) {
    $('lightbox-title').textContent = file.name;
    const body = $('lightbox-body');
    body.innerHTML = '';
    if (file.kind === 'image') {
      const img = document.createElement('img');
      img.src = `/api/files/${file.id}/preview`;
      img.alt = file.name;
      body.appendChild(img);
    } else if (file.kind === 'video') {
      const vid = document.createElement('video');
      vid.src = `/api/files/${file.id}/preview`;
      vid.controls = true;
      vid.autoplay = true;
      body.appendChild(vid);
    }
    $('lightbox').classList.remove('hidden');
  }

  // --- Event Listeners ---

  function bindEvents() {
    // Left Sidebar Navigation (Home | Transfer | Clipboard | Settings)
    document.querySelectorAll('.vd-nav-item').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.tab = btn.dataset.nav;
        document.querySelectorAll('.vd-nav-item').forEach((b) => b.classList.toggle('active', b === btn));
        ['home', 'files', 'clipboard', 'settings'].forEach((v) => {
          const el = $(`view-${v}`);
          if (el) el.classList.toggle('hidden', state.tab !== v);
        });
      });
    });

    // Expandable Perangkat Terhubung drawer on Home
    const devDrawer = $('devices-drawer');
    const devChev = $('devices-chevron');
    if (devDrawer && $('btn-toggle-devices')) {
      $('btn-toggle-devices').addEventListener('click', () => {
        const collapsed = devDrawer.classList.toggle('collapsed');
        if (devChev) devChev.style.transform = collapsed ? 'rotate(0deg)' : 'rotate(90deg)';
      });
      // Default open when devices exist, rotate chevron
      if (devChev) devChev.style.transform = 'rotate(90deg)';
    }

    // Download QR button (saves crisp SVG QR Code)
    if ($('btn-download-qr')) {
      $('btn-download-qr').addEventListener('click', () => {
        const src = $('qr-img').src;
        if (!src) return;
        const a = document.createElement('a');
        a.href = src;
        a.download = `VoltDeck-QR-${state.accessMode}.svg`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        toast('QR Code diunduh', 'success');
      });
    }

    // File Filter Pills
    document.querySelectorAll('.filter-pill').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.filter = btn.dataset.filter;
        document.querySelectorAll('.filter-pill').forEach((b) => b.classList.toggle('active', b === btn));
        renderFiles(state.files);
      });
    });

    // Copy URL & PIN
    $('copy-url-btn').addEventListener('click', async () => {
      const target = state.accessMode === 'anywhere' && state.anywhereUrl ? state.anywhereUrl : state.baseUrl;
      if (target && (await copyToClipboard(target))) {
        toast(`URL disalin: ${target}`, 'success');
      }
    });

    // Dual-Mode Switcher (Offline LAN <-> Anywhere Tailscale VPN)
    $('mode-switch').addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-mode]');
      if (!btn || btn.dataset.mode === state.accessMode) return;
      try {
        const mode = btn.dataset.mode;
        await api('/api/desktop/mode', { method: 'POST', body: JSON.stringify({ mode }) });
        toast(
          mode === 'anywhere'
            ? 'Mode ANYWHERE aktif (LAN + Tailscale VPN)'
            : 'Mode OFFLINE aktif (Khusus Wi-Fi lokal, sesi VPN diputus)',
          'success'
        );
      } catch (err) {
        toast(err.message, 'error');
      }
    });

    $('btn-ts-refresh').addEventListener('click', async () => {
      try {
        const res = await api('/api/desktop/tailscale/refresh', { method: 'POST', body: '{}' });
        toast(res.tailscale && res.tailscale.running ? `Tailscale aktif (${res.tailscale.ip})` : 'Tailscale belum aktif', 'info');
      } catch (err) {
        toast(err.message, 'error');
      }
    });

    $('btn-copy-anywhere').addEventListener('click', async () => {
      const target = state.accessMode === 'anywhere' && state.anywhereUrl ? state.anywhereUrl : state.baseUrl;
      if (target && (await copyToClipboard(target))) {
        toast(`URL disalin: ${target}`, 'success');
      }
    });

    $('toggle-anywhere-power').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ anywhere_allow_power: e.target.checked }),
      });
      toast(
        e.target.checked ? 'Remote Power via VPN diizinkan' : 'Remote Power via VPN dinonaktifkan',
        'info'
      );
    });

    $('btn-copy-pin').addEventListener('click', async () => {
      if (state.pin && (await copyToClipboard(state.pin))) {
        toast(`PIN ${state.pin} disalin`, 'success');
      }
    });

    // Rotate PIN
    $('btn-rotate-pin').addEventListener('click', async () => {
      try {
        await api('/api/desktop/pin/rotate', { method: 'POST', body: JSON.stringify({ revoke_sessions: false }) });
        toast('PIN 6 digit baru dibuat', 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });

    $('btn-rotate-revoke').addEventListener('click', async () => {
      try {
        await api('/api/desktop/pin/rotate', { method: 'POST', body: JSON.stringify({ revoke_sessions: true }) });
        toast('PIN diacak & semua perangkat diputus', 'info');
      } catch (err) {
        toast(err.message, 'error');
      }
    });

    // Settings Toggles
    $('toggle-qr-pin').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ qr_include_pin: e.target.checked }),
      });
      toast(e.target.checked ? 'Auto-PIN di QR aktif' : 'Mode ketat: PIN wajib diketik di iPhone', 'info');
    });

    $('toggle-qr-mdns').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ use_mdns_qr: e.target.checked }),
      });
      toast(e.target.checked ? 'QR menggunakan alamat tetap .local' : 'QR menggunakan alamat IP LAN', 'info');
    });

    $('btn-copy-mdns').addEventListener('click', async () => {
      if (state.mdnsUrl && (await copyToClipboard(state.mdnsUrl))) {
        toast(`Disalin: ${state.mdnsUrl}`, 'success');
      }
    });

    $('toggle-remote').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ remote_control_enabled: e.target.checked }),
      });
      toast(e.target.checked ? 'Remote Control dari iPhone diaktifkan' : 'Remote Control dari iPhone dinonaktifkan', 'info');
    });

    $('btn-cancel-power').addEventListener('click', async () => {
      await api('/api/remote/power', { method: 'POST', body: JSON.stringify({ action: 'cancel' }) });
      toast('Jadwal daya dibatalkan', 'success');
    });

    $('btn-paste-shot').addEventListener('click', () => {
      if (!sendNative('paste_clipboard_image')) {
        toast('Tekan Ctrl+V untuk menempel screenshot dari clipboard', 'info');
      }
    });

    // Global Ctrl+V screenshot paste support
    window.addEventListener('paste', async (e) => {
      const activeTag = document.activeElement ? document.activeElement.tagName : '';
      const items = e.clipboardData && e.clipboardData.items ? Array.from(e.clipboardData.items) : [];
      const imgItem = items.find((it) => it.type && it.type.startsWith('image/'));
      if (imgItem) {
        e.preventDefault();
        const blob = imgItem.getAsFile();
        if (blob) {
          const ext = (imgItem.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
          const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
          const file = new File([blob], `screenshot_${stamp}.${ext}`, { type: imgItem.type });
          await uploadBrowserFilesAsOutbound([file]);
        }
        return;
      }
      // If user pressed Ctrl+V outside text inputs in GTK WebKit2, check native X11 image clipboard
      if (activeTag !== 'TEXTAREA' && activeTag !== 'INPUT' && hasNativeBridge()) {
        sendNative('paste_clipboard_image');
      }
    });

    $('toggle-clip-sync').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ clipboard_auto_sync: e.target.checked }),
      });
      toast(e.target.checked ? 'Auto-Sync clipboard aktif' : 'Auto-Sync clipboard dijeda', 'info');
    });

    $('toggle-notify').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ notifications_enabled: e.target.checked }),
      });
    });

    $('toggle-autostart').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ autostart_enabled: e.target.checked }),
      });
      toast(
        e.target.checked
          ? 'Autostart aktif: LANShare otomatis siaga di tray saat login'
          : 'Autostart dimatikan',
        'success'
      );
    });

    $('toggle-min-tray').addEventListener('change', async (e) => {
      await api('/api/desktop/settings', {
        method: 'POST',
        body: JSON.stringify({ minimize_to_tray: e.target.checked }),
      });
      toast(
        e.target.checked ? 'Tutup jendela (X) akan menyembunyikan ke tray' : 'Tutup jendela (X) akan menutup aplikasi',
        'info'
      );
    });

    // Network Interface Selector
    $('iface-select').addEventListener('change', async (e) => {
      try {
        await api('/api/desktop/interface', {
          method: 'POST',
          body: JSON.stringify({ interface: e.target.value }),
        });
        toast('Interface LAN diperbarui', 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });

    // Add Files (Native GTK dialog or fallback file input)
    const triggerAddFiles = () => {
      if (!sendNative('pick_files')) {
        $('fallback-file-input').click();
      }
    };
    $('btn-add-files').addEventListener('click', triggerAddFiles);
    $('drop-strip').addEventListener('click', triggerAddFiles);
    $('fallback-file-input').addEventListener('change', (e) => {
      uploadBrowserFilesAsOutbound(e.target.files);
      e.target.value = '';
    });

    // Native Folder Actions
    $('btn-open-folder').addEventListener('click', () => {
      if (!sendNative('open_receive_dir')) {
        toast(`Folder penerima: ${state.receiveDir}`, 'info');
      }
    });
    $('btn-change-dir').addEventListener('click', () => {
      if (!sendNative('pick_receive_dir')) {
        toast('Pengubahan folder tersedia pada aplikasi desktop GTK3', 'info');
      }
    });

    // Clear Files
    $('btn-clear-files').addEventListener('click', async () => {
      await api('/api/desktop/files/clear', {
        method: 'POST',
        body: JSON.stringify({ direction: state.filter === 'all' ? null : state.filter }),
      });
      toast('Daftar file dibersihkan', 'info');
    });

    // Send Clipboard Text
    const sendClip = async () => {
      const input = $('clip-input');
      const text = input.value;
      if (!text.trim()) return;
      try {
        await api('/api/clipboard', { method: 'POST', body: JSON.stringify({ text }) });
        input.value = '';
        toast('Teks dikirim ke iPhone', 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    };
    $('btn-send-clip').addEventListener('click', sendClip);
    $('clip-input').addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        sendClip();
      }
    });

    $('btn-clear-clip').addEventListener('click', async () => {
      await api('/api/clipboard/clear', { method: 'POST', body: '{}' });
      toast('Riwayat clipboard dihapus', 'info');
    });

    // Delegated Clicks: Devices, Transfers, Files, Clipboard
    document.body.addEventListener('click', async (e) => {
      const trustBtn = e.target.closest('[data-trust]');
      if (trustBtn) {
        try {
          await api(`/api/desktop/devices/${trustBtn.dataset.trust}/trust`, { method: 'POST', body: '{}' });
          toast('Perangkat dipercaya! Tetap bisa masuk otomatis walau laptop restart.', 'success');
        } catch (err) {
          toast(err.message, 'error');
        }
        return;
      }

      const untrustBtn = e.target.closest('[data-untrust]');
      if (untrustBtn) {
        await api(`/api/desktop/trusted/${untrustBtn.dataset.untrust}/revoke`, { method: 'POST', body: '{}' });
        toast('Kepercayaan perangkat dicabut', 'info');
        return;
      }

      const revBtn = e.target.closest('[data-revoke]');
      if (revBtn) {
        await api(`/api/desktop/devices/${revBtn.dataset.revoke}/revoke`, { method: 'POST', body: '{}' });
        toast('Sesi perangkat diputus', 'info');
        return;
      }

      const cancelTx = e.target.closest('[data-cancel-tx]');
      if (cancelTx) {
        await api(`/api/transfers/${cancelTx.dataset.cancelTx}/cancel`, { method: 'POST', body: '{}' });
        toast('Transfer dibatalkan', 'info');
        return;
      }

      const previewEl = e.target.closest('[data-preview]');
      if (previewEl) {
        const file = state.files.find((f) => f.id === previewEl.dataset.preview);
        if (file) openLightbox(file);
        return;
      }

      const openPath = e.target.closest('[data-open-path]');
      if (openPath) {
        if (!sendNative('open_file', { path: openPath.dataset.openPath })) {
          toast('Buka file tersedia di aplikasi GTK3', 'info');
        }
        return;
      }

      const revealPath = e.target.closest('[data-reveal-path]');
      if (revealPath) {
        if (!sendNative('reveal_file', { path: revealPath.dataset.revealPath })) {
          toast('Tampilkan folder tersedia di aplikasi GTK3', 'info');
        }
        return;
      }

      const rmFile = e.target.closest('[data-remove-file]');
      if (rmFile) {
        await api(`/api/files/${rmFile.dataset.removeFile}`, { method: 'DELETE' });
        return;
      }

      const openUrlBtn = e.target.closest('[data-open-url]');
      if (openUrlBtn) {
        const targetUrl = openUrlBtn.dataset.openUrl;
        if (!sendNative('open_url', { url: targetUrl })) {
          await api('/api/open-url', { method: 'POST', body: JSON.stringify({ url: targetUrl }) }).catch(() => {
            window.open(targetUrl, '_blank', 'noopener,noreferrer');
          });
        }
        toast('Membuka tautan di browser', 'success');
        return;
      }

      const copyClip = e.target.closest('[data-copy-clip]');
      if (copyClip) {
        const item = state.clipboard.find((c) => c.id === copyClip.dataset.copyClip);
        if (item && (await copyToClipboard(item.text))) {
          toast('Disalin ke clipboard Linux Mint', 'success');
        }
        return;
      }

      const delClip = e.target.closest('[data-del-clip]');
      if (delClip) {
        await api(`/api/clipboard/${delClip.dataset.delClip}`, { method: 'DELETE' });
      }
    });

    // Lightbox Close
    $('lightbox-close').addEventListener('click', () => {
      $('lightbox').classList.add('hidden');
      $('lightbox-body').innerHTML = '';
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('lightbox').classList.contains('hidden')) {
        $('lightbox').classList.add('hidden');
        $('lightbox-body').innerHTML = '';
      }
    });

    // Drag & Drop Visual Feedback + Fallback Upload
    let dragCounter = 0;
    const overlay = $('drop-overlay');
    window.addEventListener('dragenter', (e) => {
      e.preventDefault();
      dragCounter++;
      overlay.classList.remove('hidden');
    });
    window.addEventListener('dragover', (e) => {
      e.preventDefault();
    });
    window.addEventListener('dragleave', () => {
      dragCounter = Math.max(0, dragCounter - 1);
      if (dragCounter === 0) overlay.classList.add('hidden');
    });
    window.addEventListener('drop', (e) => {
      e.preventDefault();
      dragCounter = 0;
      overlay.classList.add('hidden');
      // Native GTK DnD intercepts uri-list first when running in WebKit2;
      // if files arrive here via HTML5 DataTransfer, upload smoothly.
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        uploadBrowserFilesAsOutbound(e.dataTransfer.files);
      }
    });
  }

  // Expose tiny hook so Python GTK DnD can show a toast
  window.__lanshareToast = toast;

  bindEvents();
  loadFullState();
  connectSSE();
  dismissBootSplash();
})();
