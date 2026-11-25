/* LANShare Mobile iPhone Safari Controller */
(() => {
  'use strict';

  const state = {
    authenticated: false,
    hostName: 'Linux Mint',
    pinValue: '',
    verifying: false,
    lockoutTimer: null,
    files: [],
    serverTransfers: [],
    localUploads: new Map(), // transferId -> {id, filename, total_bytes, transferred_bytes, percent, speed_bps, xhr, status}
    clipboard: [],
    remoteEnabled: true,
    power: { scheduled: false, action: null, execute_at: 0 },
    powerTick: null,
    selectedDelay: 15,
    shutdownArmed: false,
    shutdownArmTimer: null,
    restartArmed: false,
    restartArmTimer: null,
    remoteSubTab: 'controls',
    telemetryTimer: null,
    activeMediaBus: '',
    draggingVol: false,
    draggingBright: false,
    mediaSyncTimer: null,
    screenLive: false,
    screenFps: 5,
    galleryFolder: 'all',
    galleryImages: [],
    galleryFolders: [],
    gallerySubTab: 'photos',
    expRoot: '',
    expPath: '',
    expParent: null,
    expRoots: [],
    expEntries: [],
    expQuery: '',
    processes: [],
    armedKillPid: null,
    armedKillTimer: null,
    apps: [],
    tab: 'files',
    sse: null,
  };

  const $ = (id) => document.getElementById(id);
  const tr = (k) => (window.VDI18n ? window.VDI18n.t(k) : k);

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
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function fileExtBadge(name, kind) {
    const idx = name.lastIndexOf('.');
    if (idx > 0 && idx < name.length - 1) return name.slice(idx + 1, idx + 5).toUpperCase();
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
    }, 950);
  }

  function randomHex(len = 12) {
    const bytes = new Uint8Array(Math.ceil(len / 2));
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('').slice(0, len);
  }

  function toast(message, type = 'info') {
    const container = $('toasts');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transition = 'opacity 180ms ease';
      setTimeout(() => el.remove(), 200);
    }, 2600);
  }

  // Rock-solid iOS Safari HTTP clipboard copy (works on plain http://192.168.x.x!)
  async function copyTextSafe(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {
        // Fall through to execCommand fallback
      }
    }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.contentEditable = 'true';
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '-9999px';
      ta.style.fontSize = '16px';
      document.body.appendChild(ta);

      const range = document.createRange();
      range.selectNodeContents(ta);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      ta.setSelectionRange(0, text.length);

      const ok = document.execCommand('copy');
      sel.removeAllRanges();
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }

  // --- PIN Gate ---

  function renderPinSlots() {
    const slots = $('pin-slots').querySelectorAll('.pin-slot');
    const chars = state.pinValue.split('');
    slots.forEach((slot, i) => {
      slot.textContent = chars[i] || '';
      slot.classList.toggle('filled', i < chars.length);
      slot.classList.toggle('active', i === chars.length && chars.length < 6);
    });
  }

  function shakePinError(msg) {
    $('pin-error').textContent = msg;
    const el = $('pin-slots');
    el.classList.remove('shake');
    void el.offsetWidth;
    el.classList.add('shake');
  }

  function startLockoutCountdown(seconds) {
    if (state.lockoutTimer) clearInterval(state.lockoutTimer);
    let rem = seconds;
    $('pin-error').textContent = `Akses dikunci sementara (${rem} dtk)`;
    state.lockoutTimer = setInterval(() => {
      rem--;
      if (rem <= 0) {
        clearInterval(state.lockoutTimer);
        state.lockoutTimer = null;
        $('pin-error').textContent = '';
      } else {
        $('pin-error').textContent = `Akses dikunci sementara (${rem} dtk)`;
      }
    }, 1000);
  }

  async function submitPin(pinStr) {
    if (state.verifying || pinStr.length !== 6) return;
    state.verifying = true;
    $('pin-error').textContent = 'Memverifikasi PIN...';
    try {
      const res = await fetch('/api/auth/verify', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: pinStr }),
      });
      const data = await res.json().catch(() => ({ ok: false, error: 'Gagal menghubungi server' }));
      if (!res.ok || !data.ok) {
        state.pinValue = '';
        renderPinSlots();
        if (data.retry_after && data.retry_after > 0) {
          startLockoutCountdown(data.retry_after);
        } else {
          shakePinError(data.error || 'PIN salah');
        }
        return;
      }

      // Clean URL query param if auto-PIN was used
      if (window.location.search.includes('pin=')) {
        window.history.replaceState({}, document.title, '/');
      }
      state.pinValue = '';
      $('pin-error').textContent = '';
      enterApp(data.device_name || state.hostName);
    } catch {
      state.pinValue = '';
      renderPinSlots();
      shakePinError('Koneksi ke Linux Mint terputus');
    } finally {
      state.verifying = false;
    }
  }

  function appendPinDigit(digit) {
    if (state.verifying || state.lockoutTimer || state.pinValue.length >= 6) return;
    if (!/^\d$/.test(digit)) return;
    $('pin-error').textContent = '';
    state.pinValue += digit;
    renderPinSlots();
    if (state.pinValue.length === 6) {
      submitPin(state.pinValue);
    }
  }

  function showPinGate(message = '') {
    state.authenticated = false;
    if (state.sse) {
      try { state.sse.close(); } catch {}
      state.sse = null;
    }
    $('mobile-app').classList.add('hidden');
    $('pin-screen').classList.remove('hidden');
    state.pinValue = '';
    renderPinSlots();
    $('pin-error').textContent = message;
  }

  async function enterApp(hostName) {
    state.authenticated = true;
    state.hostName = hostName || state.hostName;
    $('m-host-name').textContent = state.hostName;
    $('pin-screen').classList.add('hidden');
    $('mobile-app').classList.remove('hidden');
    await refreshState();
    connectSSE();
  }

  // --- Data & SSE ---

  const TRUST_STORAGE_KEY = 'lanshare_trust_token';

  function getStoredTrustToken() {
    try {
      return window.localStorage.getItem(TRUST_STORAGE_KEY) || '';
    } catch {
      return '';
    }
  }

  function setStoredTrustToken(token) {
    try {
      if (token) window.localStorage.setItem(TRUST_STORAGE_KEY, token);
      else window.localStorage.removeItem(TRUST_STORAGE_KEY);
    } catch {}
  }

  function authHeaders(extra = {}) {
    const h = { ...extra };
    const tk = getStoredTrustToken();
    if (tk) h['X-Trust-Token'] = tk;
    return h;
  }

  async function claimPendingTrust(showToastMsg = true) {
    try {
      const res = await fetch('/api/auth/claim-trust', {
        method: 'POST',
        credentials: 'same-origin',
        headers: authHeaders(),
      });
      const d = await res.json();
      if (d.ok && d.claimed) {
        if (d.trust_token) setStoredTrustToken(d.trust_token);
        if (showToastMsg) {
          toast('iPhone ini dipercaya — auto-login aktif walau laptop restart!', 'success');
        }
      }
    } catch {}
  }

  async function refreshState() {
    const res = await fetch('/api/state', {
      credentials: 'same-origin',
      headers: authHeaders(),
    });
    if (res.status === 401) {
      setStoredTrustToken('');
      showPinGate('Sesi berakhir. Masukkan PIN kembali.');
      return;
    }
    const data = await res.json();
    if (data.has_pending_trust) {
      claimPendingTrust(true);
    }
    if (data.device_name) $('m-host-name').textContent = data.device_name;
    const badge = $('m-zone-badge');
    if (data.client_zone === 'tailscale') {
      badge.textContent = 'ANYWHERE · VPN';
      badge.className = 'tag tag-out';
    } else {
      badge.textContent = data.access_mode === 'anywhere' ? 'LAN · DUAL' : 'LAN · OFFLINE';
      badge.className = 'tag tag-in';
    }
    state.files = data.files || [];
    state.serverTransfers = data.transfers || [];
    state.clipboard = data.clipboard || [];
    state.remoteEnabled = data.remote_enabled !== false;
    renderFiles();
    renderTransfers();
    renderClipboard();
    renderPowerState(data.power);
    updateRemoteAvailability();
  }

  function updateRemoteAvailability() {
    $('m-remote-disabled').classList.toggle('hidden', state.remoteEnabled);
    $('m-remote-body').classList.toggle('hidden', !state.remoteEnabled);
  }

  async function refreshRemoteStatus() {
    if (!state.authenticated) return;
    try {
      const res = await fetch('/api/remote/status', { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      state.remoteEnabled = data.enabled !== false;
      updateRemoteAvailability();
      if (data.telemetry) renderTelemetry(data.telemetry);
      if (data.volume) renderVolume(data.volume);
      if (data.sinks) renderSinks(data.sinks);
      if (data.brightness) renderBrightness(data.brightness);
      if (data.media) renderMedia(data.media);
      if (data.apps) renderApps(data.apps);
      if (data.power) renderPowerState(data.power);
    } catch {}
  }

  function renderApps(apps) {
    state.apps = apps || [];
    $('apps-count-tag').textContent = `${state.apps.length} APP`;
    const grid = $('r-apps-grid');
    if (state.apps.length === 0) {
      grid.innerHTML = '<div class="m-empty" style="grid-column:1/-1">Tidak ada aplikasi desktop terdeteksi.</div>';
      return;
    }
    grid.innerHTML = state.apps
      .map(
        (app) => `
      <button class="app-tile" data-launch-app="${escHtml(app.id)}" type="button">
        <span class="app-tile-cat">${escHtml(app.category)}</span>
        <span class="app-tile-name">${escHtml(app.name)}</span>
      </button>`
      )
      .join('');
  }

  async function refreshGallery(folder = state.galleryFolder) {
    if (!state.authenticated) return;
    state.galleryFolder = folder || 'all';
    try {
      const res = await fetch(`/api/gallery?folder=${encodeURIComponent(state.galleryFolder)}`, {
        credentials: 'same-origin',
        headers: authHeaders(),
      });
      if (!res.ok) return;
      const data = await res.json();
      state.galleryFolders = data.folders || [];
      state.galleryImages = data.images || [];
      renderGallery();
    } catch {}
  }

  function renderGallery() {
    const fWrap = $('gallery-folders');
    const allChips = ['all', ...state.galleryFolders];
    fWrap.innerHTML = allChips
      .map((f) => {
        const label = f === 'all' ? 'Semua Folder' : f;
        const active = f.toLowerCase() === state.galleryFolder.toLowerCase() ? 'active' : '';
        return `<button class="folder-chip ${active}" data-gal-folder="${escHtml(f)}" type="button">${escHtml(label)}</button>`;
      })
      .join('');

    const grid = $('gallery-grid');
    if (state.galleryImages.length === 0) {
      grid.innerHTML = '<div class="m-empty" style="grid-column:1/-1">Belum ada gambar di folder ini.</div>';
      return;
    }
    grid.innerHTML = state.galleryImages
      .map(
        (img) => `
      <div class="gal-card" data-gal-preview="${escHtml(img.id)}">
        <div class="gal-thumb">
          <img src="/api/gallery/${escHtml(img.id)}/thumb" alt="${escHtml(img.name)}" loading="lazy" />
        </div>
        <div class="gal-meta">
          <span class="gal-name" title="${escHtml(img.name)}">${escHtml(img.name)}</span>
          <span class="gal-folder">${escHtml(img.folder)}</span>
        </div>
      </div>`
      )
      .join('');
  }

  function openImageLightbox(title, previewUrl, downloadUrl) {
    $('m-lightbox-title').textContent = title;
    $('m-lightbox-dl').href = downloadUrl;
    const body = $('m-lightbox-body');
    body.innerHTML = '';
    const img = document.createElement('img');
    img.src = previewUrl;
    img.alt = title;
    body.appendChild(img);
    $('m-lightbox').classList.remove('hidden');
  }

  // --- Sandboxed Laptop File Explorer ---

  async function refreshExplorer(root = state.expRoot, relPath = state.expPath) {
    if (!state.authenticated) return;
    try {
      const url = `/api/explorer?root=${encodeURIComponent(root || '')}&path=${encodeURIComponent(relPath || '')}`;
      const res = await fetch(url, { credentials: 'same-origin', headers: authHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      state.expRoots = data.roots || [];
      state.expRoot = data.root || '';
      state.expPath = data.path || '';
      state.expParent = data.parent;
      state.expEntries = data.entries || [];
      renderExplorer();
    } catch {}
  }

  function renderExplorer() {
    $('exp-roots').innerHTML = state.expRoots
      .map((r) => {
        const active = r === state.expRoot ? 'active' : '';
        return `<button class="folder-chip ${active}" data-exp-root="${escHtml(r)}" type="button">${escHtml(r)}</button>`;
      })
      .join('');

    $('exp-path').textContent = state.expPath ? `${state.expRoot}/${state.expPath}` : `${state.expRoot}/`;
    $('btn-exp-up').classList.toggle('hidden', state.expParent === null || state.expParent === undefined);

    const q = (state.expQuery || '').trim().toLowerCase();
    const list = state.expEntries.filter((e) => !q || e.name.toLowerCase().includes(q));
    const el = $('exp-list');
    if (list.length === 0) {
      el.innerHTML = `<div class="m-empty" style="grid-column:1/-1">${escHtml(tr('explorer_empty'))}</div>`;
      return;
    }

    el.innerHTML = list
      .map((item) => {
        if (item.is_dir) {
          return `
          <div class="m-file-card" data-exp-dir="${escHtml(item.rel_path)}" style="cursor:pointer">
            <div class="m-thumb"><span class="file-ext" style="color:var(--accent-bright)">DIR</span></div>
            <div class="m-file-info">
              <div class="m-file-name">${escHtml(item.name)}</div>
              <div class="m-file-meta"><span>Folder</span></div>
            </div>
            <span class="tag">›</span>
          </div>`;
        }
        const dlUrl = `/api/explorer/download?root=${encodeURIComponent(state.expRoot)}&path=${encodeURIComponent(item.rel_path)}`;
        return `
        <div class="m-file-card">
          <div class="m-thumb"><span class="file-ext">${escHtml(fileExtBadge(item.name, item.kind))}</span></div>
          <div class="m-file-info">
            <div class="m-file-name">${escHtml(item.name)}</div>
            <div class="m-file-meta"><span>${formatBytes(item.size)}</span></div>
          </div>
          <a class="btn btn-primary m-dl-btn" href="${dlUrl}">${escHtml(tr('download'))}</a>
        </div>`;
      })
      .join('');
  }

  // --- On-Demand RAM Task Monitor ---

  async function refreshProcesses() {
    if (!state.authenticated || !state.remoteEnabled) return;
    try {
      const res = await fetch('/api/remote/processes', { credentials: 'same-origin', headers: authHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      state.processes = data.processes || [];
      renderProcesses();
    } catch {}
  }

  function renderProcesses() {
    const el = $('r-proc-list');
    if (!state.processes || state.processes.length === 0) {
      el.innerHTML = `<div class="m-empty">${escHtml(tr('task_mon_empty'))}</div>`;
      return;
    }
    el.innerHTML = state.processes
      .map((p) => {
        const armed = state.armedKillPid === p.pid;
        const actionBtn = p.protected
          ? '<span class="tag">CORE</span>'
          : `<button class="btn btn-sm btn-danger" data-kill-pid="${p.pid}" type="button">${armed ? 'Confirm?' : escHtml(tr('task_close'))}</button>`;
        return `
        <div class="proc-row">
          <div class="proc-info">
            <div class="proc-name">${escHtml(p.name)}</div>
            <div class="proc-meta">${p.ram_mb} MB RAM · PID ${p.pid}</div>
          </div>
          ${actionBtn}
        </div>`;
      })
      .join('');
  }

  function renderTelemetry(t) {
    if (!t || !t.available) return;
    if (t.battery_percent !== null && t.battery_percent !== undefined) {
      $('t-bat').textContent = `${t.battery_percent}%${t.battery_plugged ? '⚡' : ''}`;
    } else {
      $('t-bat').textContent = 'AC';
    }
    $('t-cpu').textContent = `${Math.round(t.cpu_percent || 0)}%`;
    $('t-ram').textContent = `${Math.round(t.ram_percent || 0)}%`;
    $('t-temp').textContent = t.temp_c ? `${t.temp_c}°C` : '—';
  }

  function renderSinks(sinks) {
    const sel = $('r-sink-select');
    if (!sinks || sinks.length === 0) {
      sel.classList.add('hidden');
      return;
    }
    sel.classList.remove('hidden');
    sel.innerHTML = '';
    sinks.forEach((s) => {
      const opt = document.createElement('option');
      opt.value = s.name;
      opt.textContent = `Output: ${s.description}`;
      if (s.active) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function renderBrightness(br) {
    if (!br || !br.available) {
      $('r-bright-val').textContent = 'N/A';
      return;
    }
    if (state.draggingBright) return;
    $('r-bright-val').textContent = `${br.percent}%`;
    $('r-bright-slider').value = String(Math.max(10, Math.min(100, br.percent)));
  }

  async function syncRemoteLive() {
    if (!state.authenticated || state.tab !== 'remote' || document.visibilityState !== 'visible') return;
    try {
      const res = await fetch('/api/remote/telemetry', { credentials: 'same-origin', headers: authHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      if (data.telemetry) renderTelemetry(data.telemetry);
      if (data.media) renderMedia(data.media);
      if (data.volume) renderVolume(data.volume);
      if (data.brightness) renderBrightness(data.brightness);
    } catch {}
  }

  function startTelemetryPolling() {
    stopTelemetryPolling();
    state.telemetryTimer = setInterval(syncRemoteLive, 1500);
  }

  function stopTelemetryPolling() {
    if (state.telemetryTimer) {
      clearInterval(state.telemetryTimer);
      state.telemetryTimer = null;
    }
  }

  function startScreenLive() {
    state.screenLive = true;
    const img = $('screen-img');
    $('screen-placeholder').classList.add('hidden');
    img.classList.remove('hidden');
    img.src = `/api/remote/screen/stream?fps=${state.screenFps}&t=${Date.now()}`;
    $('btn-screen-live').textContent = 'Stop Live';
    $('btn-screen-live').classList.remove('btn-primary');
    $('btn-screen-live').classList.add('btn-danger');
    $('screen-status-tag').textContent = `LIVE · ${state.screenFps} FPS`;
    $('screen-status-tag').classList.add('tag-in');
  }

  function stopScreenLive() {
    if (!state.screenLive) return;
    state.screenLive = false;
    const img = $('screen-img');
    img.src = '';
    $('btn-screen-live').textContent = 'Mulai Live';
    $('btn-screen-live').classList.remove('btn-danger');
    $('btn-screen-live').classList.add('btn-primary');
    $('screen-status-tag').textContent = 'STANDBY';
    $('screen-status-tag').classList.remove('tag-in');
  }

  function renderVolume(vol) {
    if (state.draggingVol) return;
    const pct = vol.available ? vol.percent : 0;
    $('r-vol-val').textContent = vol.muted ? 'MUTE' : `${pct}%`;
    $('r-vol-slider').value = String(Math.min(100, pct));
    $('btn-vol-mute').textContent = vol.muted ? 'Unmute' : 'Mute';
  }

  function renderMedia(media) {
    const playersWrap = $('r-media-players');
    const ppBtn = $('btn-media-playpause');
    if (!media || !media.available) {
      state.activeMediaBus = '';
      $('r-media-player').textContent = 'STANDBY';
      $('r-media-title').textContent = tr('media_none');
      $('r-media-artist').textContent = tr('media_hint');
      if (playersWrap) {
        playersWrap.classList.add('hidden');
        playersWrap.innerHTML = '';
      }
      if (ppBtn) {
        ppBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg><span>Play / Pause</span>`;
      }
      return;
    }
    state.activeMediaBus = media.bus_name || '';
    const isPlaying = String(media.status || '').toLowerCase() === 'playing';
    $('r-media-player').textContent = `${media.player.toUpperCase()} · ${media.status.toUpperCase()}`;
    $('r-media-title').textContent = media.title || media.player;
    $('r-media-artist').textContent = media.artist || 'MPRIS2';

    if (ppBtn) {
      ppBtn.innerHTML = isPlaying
        ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg><span>Pause</span>`
        : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg><span>Play</span>`;
    }

    if (playersWrap) {
      const list = Array.isArray(media.players) ? media.players : [];
      if (list.length > 1) {
        playersWrap.classList.remove('hidden');
        playersWrap.innerHTML = list
          .map((p) => {
            const dotCls = String(p.status || '').toLowerCase() === 'playing' ? 'playing' : '';
            const actCls = p.active ? 'active' : '';
            return `<button class="media-chip ${actCls}" data-media-bus="${escHtml(p.bus_name)}" type="button"><span class="media-chip-dot ${dotCls}"></span>${escHtml(p.player)}</button>`;
          })
          .join('');
      } else {
        playersWrap.classList.add('hidden');
        playersWrap.innerHTML = '';
      }
    }
  }

  function renderPowerState(power) {
    state.power = power || { scheduled: false };
    if (state.powerTick) {
      clearInterval(state.powerTick);
      state.powerTick = null;
    }
    const alertBox = $('m-power-alert');
    if (!state.power.scheduled) {
      alertBox.classList.add('hidden');
      return;
    }
    alertBox.classList.remove('hidden');
    const titleMap = {
      shutdown: 'Laptop Akan Dimatikan',
      restart: 'Laptop Akan Di-Restart',
      sleep: 'Laptop Akan Masuk Sleep',
    };
    $('m-power-alert-title').textContent = titleMap[state.power.action] || 'Jadwal Daya Aktif';
    const tick = () => {
      const rem = Math.max(0, Math.ceil(state.power.execute_at - Date.now() / 1000));
      const m = Math.floor(rem / 60);
      const s = rem % 60;
      $('m-power-countdown').textContent = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
      if (rem <= 0 && state.powerTick) {
        clearInterval(state.powerTick);
        state.powerTick = null;
      }
    };
    tick();
    state.powerTick = setInterval(tick, 500);
  }

  function connectSSE() {
    if (state.sse) {
      try { state.sse.close(); } catch {}
    }
    const es = new EventSource('/api/events');
    state.sse = es;

    es.onopen = () => {
      $('m-live-dot').style.background = '';
    };
    es.addEventListener('files_changed', (e) => {
      state.files = JSON.parse(e.data).files || [];
      renderFiles();
    });
    es.addEventListener('transfer_progress', (e) => {
      state.serverTransfers = JSON.parse(e.data).transfers || [];
      renderTransfers();
    });
    es.addEventListener('clipboard_updated', (e) => {
      state.clipboard = JSON.parse(e.data).clipboard || [];
      renderClipboard();
    });
    es.addEventListener('power_changed', (e) => {
      renderPowerState(JSON.parse(e.data).power);
    });
    es.addEventListener('remote_config_changed', (e) => {
      const cfg = JSON.parse(e.data);
      state.remoteEnabled = cfg.remote_enabled !== false;
      if (cfg.language && window.VDI18n && !window.localStorage.getItem('voltdeck_lang_override')) {
        window.VDI18n.setLang(cfg.language, false);
      }
      updateRemoteAvailability();
    });
    es.addEventListener('trust_granted', () => {
      claimPendingTrust(true);
    });
    es.addEventListener('session_revoked', () => {
      showPinGate('Sesi diputus dari Linux Mint.');
    });
    es.onerror = () => {
      $('m-live-dot').style.background = '#f59e0b';
      setTimeout(() => {
        if (state.authenticated && (!state.sse || state.sse.readyState === EventSource.CLOSED)) {
          refreshState().then(() => {
            if (state.authenticated) connectSSE();
          });
        }
      }, 2500);
    };
  }

  // --- Streaming Multi-File Upload (iPhone Safari -> Linux Mint) ---

  function enqueueUploads(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;
    files.forEach((file) => startStreamingUpload(file));
  }

  function startStreamingUpload(file) {
    const txId = randomHex(12);
    const xhr = new XMLHttpRequest();
    const startedAt = Date.now();

    const record = {
      id: txId,
      filename: file.name || `photo_${Date.now()}`,
      direction: 'inbound',
      total_bytes: file.size,
      transferred_bytes: 0,
      percent: 0,
      speed_bps: 0,
      status: 'active',
      xhr,
    };
    state.localUploads.set(txId, record);
    renderTransfers();

    xhr.open('POST', '/api/upload', true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-Filename', encodeURIComponent(record.filename));
    xhr.setRequestHeader('X-Transfer-Id', txId);

    xhr.upload.onprogress = (evt) => {
      if (!evt.lengthComputable) return;
      const elapsed = Math.max(0.08, (Date.now() - startedAt) / 1000);
      record.transferred_bytes = evt.loaded;
      record.total_bytes = evt.total;
      record.percent = Math.min(100, Math.round((evt.loaded / evt.total) * 1000) / 10);
      record.speed_bps = evt.loaded / elapsed;
      renderTransfers();
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        record.status = 'completed';
        record.percent = 100;
        toast(`Terkirim: ${record.filename}`, 'success');
      } else if (xhr.status === 401) {
        showPinGate('Sesi berakhir.');
      } else {
        record.status = 'error';
        toast(`Gagal mengirim ${record.filename}`, 'error');
      }
      setTimeout(() => {
        state.localUploads.delete(txId);
        renderTransfers();
      }, 1500);
    };

    xhr.onabort = () => {
      state.localUploads.delete(txId);
      renderTransfers();
      toast(`Dibatalkan: ${record.filename}`, 'info');
    };

    xhr.onerror = () => {
      record.status = 'error';
      renderTransfers();
      toast(`Koneksi gagal: ${record.filename}`, 'error');
      setTimeout(() => {
        state.localUploads.delete(txId);
        renderTransfers();
      }, 2500);
    };

    // Raw binary streaming directly from iOS File handle — zero RAM base64 bloat!
    xhr.send(file);
  }

  // --- Rendering ---

  function renderTransfers() {
    const wrap = $('m-transfers-wrap');
    const list = $('m-transfers-list');

    // Merge local active XHR uploads with server-side downloads
    const merged = new Map();
    state.serverTransfers
      .filter((t) => t.status === 'active')
      .forEach((t) => merged.set(t.id, t));
    state.localUploads.forEach((u, id) => {
      if (u.status === 'active') merged.set(id, u);
    });

    const items = Array.from(merged.values());
    if (items.length === 0) {
      wrap.classList.add('hidden');
      list.innerHTML = '';
      return;
    }

    wrap.classList.remove('hidden');
    list.innerHTML = items
      .map((t) => {
        const label = t.direction === 'inbound' ? '↑ KIRIM' : '↓ UNDUH';
        const tagClass = t.direction === 'inbound' ? 'tag-in' : 'tag-out';
        return `
        <div class="transfer-item">
          <div class="transfer-meta">
            <span class="tag ${tagClass}">${label}</span>
            <span class="transfer-name">${escHtml(t.filename)}</span>
          </div>
          <button class="btn btn-sm btn-danger" data-cancel-tx="${escHtml(t.id)}" type="button">Batal</button>
          <div class="transfer-stats" style="grid-column:1/-1">
            ${t.percent}% · ${formatBytes(t.transferred_bytes)} / ${formatBytes(t.total_bytes)} · ${formatSpeed(t.speed_bps)}
          </div>
          <div class="progress-track">
            <div class="progress-fill" style="width:${t.percent}%"></div>
          </div>
        </div>`;
      })
      .join('');
  }

  function renderFiles() {
    const outbound = state.files.filter((f) => f.direction === 'outbound' && f.exists);
    const inbound = state.files.filter((f) => f.direction === 'inbound');

    const zipBtn = $('btn-download-zip');
    zipBtn.classList.toggle('hidden', outbound.length < 2);

    const outEl = $('m-outbound-list');
    if (outbound.length === 0) {
      outEl.innerHTML = '<div class="m-empty">Belum ada file yang dibagikan dari Linux Mint.</div>';
    } else {
      outEl.innerHTML = outbound
        .map((f) => {
          const thumb = f.has_thumb
            ? `<img src="/api/files/${escHtml(f.id)}/thumb" alt="" loading="lazy" />`
            : `<span class="file-ext">${escHtml(fileExtBadge(f.name, f.kind))}</span>`;
          const canPrev = f.kind === 'image' || f.kind === 'video';
          return `
          <div class="m-file-card">
            <div class="m-thumb" ${canPrev ? `data-preview="${escHtml(f.id)}"` : ''}>
              ${thumb}
            </div>
            <div class="m-file-info" ${canPrev ? `data-preview="${escHtml(f.id)}"` : ''}>
              <div class="m-file-name">${escHtml(f.name)}</div>
              <div class="m-file-meta">
                <span>${formatBytes(f.size)}</span>
                <span>·</span>
                <span>${formatTime(f.created_at)}</span>
              </div>
            </div>
            <a class="btn btn-primary m-dl-btn" href="/api/files/${escHtml(f.id)}/download">Unduh</a>
          </div>`;
        })
        .join('');
    }

    const inWrap = $('m-inbound-wrap');
    const inEl = $('m-inbound-list');
    if (inbound.length === 0) {
      inWrap.classList.add('hidden');
      inEl.innerHTML = '';
    } else {
      inWrap.classList.remove('hidden');
      inEl.innerHTML = inbound
        .map((f) => {
          const thumb = f.has_thumb
            ? `<img src="/api/files/${escHtml(f.id)}/thumb" alt="" loading="lazy" />`
            : `<span class="file-ext">${escHtml(fileExtBadge(f.name, f.kind))}</span>`;
          return `
          <div class="m-file-card">
            <div class="m-thumb">${thumb}</div>
            <div class="m-file-info">
              <div class="m-file-name">${escHtml(f.name)}</div>
              <div class="m-file-meta">
                <span>${formatBytes(f.size)}</span>
                <span>·</span>
                <span>Tersimpan di Linux</span>
              </div>
            </div>
            <span class="tag tag-in">OK</span>
          </div>`;
        })
        .join('');
    }
  }

  function renderClipboard() {
    const latestCard = $('m-clip-latest');
    const historyEl = $('m-clip-history');

    if (state.clipboard.length === 0) {
      latestCard.classList.add('hidden');
      historyEl.innerHTML = '<div class="m-empty">Belum ada teks di Shared Clipboard.</div>';
      return;
    }

    const latest = state.clipboard[0];
    latestCard.classList.remove('hidden');
    $('m-clip-latest-src').textContent = latest.source;
    $('m-clip-latest-text').textContent = latest.text;

    historyEl.innerHTML = state.clipboard
      .map((item) => {
        const cls = item.source_kind === 'desktop' ? 'tag-out' : 'tag-in';
        const detectedUrl = extractHttpUrl(item.text);
        const openBtn = detectedUrl
          ? `<button class="btn btn-sm btn-primary" data-open-laptop="${escHtml(detectedUrl)}" type="button">Buka di Laptop</button>`
          : '';
        return `
        <div class="m-clip-item">
          <div class="m-clip-item-head">
            <div style="display:flex;align-items:center;gap:6px">
              <span class="tag ${cls}">${escHtml(item.source)}</span>
              <span class="mono" style="font-size:11px;color:var(--text-muted)">${formatTime(item.created_at)}</span>
            </div>
            <div style="display:flex;gap:6px">
              ${openBtn}
              <button class="btn btn-sm" data-copy-clip="${escHtml(item.id)}" type="button">Salin</button>
            </div>
          </div>
          <pre class="m-clip-preview" style="max-height:110px">${escHtml(item.text)}</pre>
        </div>`;
      })
      .join('');
  }

  function openLightbox(file) {
    $('m-lightbox-title').textContent = file.name;
    $('m-lightbox-dl').href = `/api/files/${file.id}/download`;
    const body = $('m-lightbox-body');
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
      vid.playsInline = true;
      body.appendChild(vid);
    }
    $('m-lightbox').classList.remove('hidden');
  }

  // --- Bind UI Events ---

  function bindEvents() {
    // Language switchers (ID / EN) on PIN gate & main header
    const applyMobileLang = (lang) => {
      if (!window.VDI18n) return;
      window.VDI18n.setLang(lang, true);
      try {
        window.localStorage.setItem('voltdeck_lang_override', '1');
      } catch {}
      renderFiles();
      renderClipboard();
      renderGallery();
      toast(lang === 'en' ? 'Language: English' : 'Bahasa: Indonesia', 'info');
    };
    document.querySelectorAll('[data-lang-btn]').forEach((btn) => {
      btn.addEventListener('click', () => applyMobileLang(btn.getAttribute('data-lang-btn')));
    });
    if ($('btn-lang-cycle')) {
      $('btn-lang-cycle').addEventListener('click', () => {
        const next = window.VDI18n && window.VDI18n.getLang() === 'id' ? 'en' : 'id';
        applyMobileLang(next);
      });
    }

    // Keypad buttons
    $('keypad').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-key]');
      if (!btn) return;
      const key = btn.dataset.key;
      if (key === 'clear') {
        state.pinValue = '';
        renderPinSlots();
      } else if (key === 'back') {
        state.pinValue = state.pinValue.slice(0, -1);
        renderPinSlots();
      } else {
        appendPinDigit(key);
      }
    });

    // Keyboard & paste support on PIN screen
    $('pin-slots').addEventListener('click', () => $('pin-native-input').focus());
    $('pin-native-input').addEventListener('input', (e) => {
      const cleaned = e.target.value.replace(/\D/g, '').slice(0, 6);
      e.target.value = '';
      if (cleaned) {
        state.pinValue = cleaned;
        renderPinSlots();
        if (state.pinValue.length === 6) submitPin(state.pinValue);
      }
    });
    document.addEventListener('keydown', (e) => {
      if (state.authenticated) return;
      if (e.key >= '0' && e.key <= '9') {
        appendPinDigit(e.key);
      } else if (e.key === 'Backspace') {
        state.pinValue = state.pinValue.slice(0, -1);
        renderPinSlots();
      }
    });

    // Bottom/Sidebar Navigation Tabs
    document.querySelectorAll('.m-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        state.tab = tab.dataset.mtab;
        document.querySelectorAll('.m-tab').forEach((t) => t.classList.toggle('active', t === tab));
        $('m-view-files').classList.toggle('hidden', state.tab !== 'files');
        $('m-view-gallery').classList.toggle('hidden', state.tab !== 'gallery');
        $('m-view-clipboard').classList.toggle('hidden', state.tab !== 'clipboard');
        $('m-view-remote').classList.toggle('hidden', state.tab !== 'remote');
        if (state.tab === 'gallery') {
          refreshGallery(state.galleryFolder);
        }
        if (state.tab === 'remote') {
          refreshRemoteStatus();
          startTelemetryPolling();
        } else {
          stopTelemetryPolling();
          stopScreenLive();
        }
      });
    });

    // Gallery Sub-Tabs (Photos & Camera | File Explorer)
    document.querySelectorAll('[data-gsub]').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.gallerySubTab = btn.dataset.gsub;
        document.querySelectorAll('[data-gsub]').forEach((b) => b.classList.toggle('active', b === btn));
        $('g-panel-photos').classList.toggle('hidden', state.gallerySubTab !== 'photos');
        $('g-panel-explorer').classList.toggle('hidden', state.gallerySubTab !== 'explorer');
        if (state.gallerySubTab === 'explorer') refreshExplorer(state.expRoot, state.expPath);
      });
    });

    $('btn-exp-refresh').addEventListener('click', () => refreshExplorer(state.expRoot, state.expPath));
    $('btn-exp-up').addEventListener('click', () => {
      if (state.expParent !== null && state.expParent !== undefined) {
        refreshExplorer(state.expRoot, state.expParent);
      }
    });
    $('exp-search').addEventListener('input', (e) => {
      state.expQuery = e.target.value;
      renderExplorer();
    });

    // Gallery Controls (Webcam Capture, Screen Capture, Folder Filter)
    $('btn-gallery-refresh').addEventListener('click', () => {
      refreshGallery(state.galleryFolder);
      toast('Galeri laptop diperbarui', 'info');
    });

    $('btn-cap-webcam').addEventListener('click', async () => {
      toast('Mengambil foto dari webcam laptop...', 'info');
      const ts = Date.now();
      const testRes = await fetch(`/api/remote/webcam/snapshot?t=${ts}`, {
        credentials: 'same-origin',
        headers: authHeaders(),
      });
      if (!testRes.ok) {
        const err = await testRes.json().catch(() => ({ error: 'Webcam tidak tersedia' }));
        toast(err.error || 'Webcam laptop tidak tersedia', 'error');
        return;
      }
      const blob = await testRes.blob();
      const objUrl = URL.createObjectURL(blob);
      openImageLightbox('Foto Webcam Laptop', objUrl, `/api/remote/webcam/snapshot?download=1&t=${ts}`);
    });

    $('btn-cap-screen').addEventListener('click', () => {
      const ts = Date.now();
      openImageLightbox(
        'Screenshot Layar Laptop',
        `/api/remote/screen/snapshot?t=${ts}`,
        `/api/remote/screen/snapshot?download=1&t=${ts}`
      );
    });

    // Remote Sub-Tabs (Kontrol | Aplikasi | Layar Live | Touchpad)
    document.querySelectorAll('.r-subtab').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.remoteSubTab = btn.dataset.rsub;
        document.querySelectorAll('.r-subtab').forEach((b) => b.classList.toggle('active', b === btn));
        $('r-panel-controls').classList.toggle('hidden', state.remoteSubTab !== 'controls');
        $('r-panel-apps').classList.toggle('hidden', state.remoteSubTab !== 'apps');
        $('r-panel-screen').classList.toggle('hidden', state.remoteSubTab !== 'screen');
        $('r-panel-touchpad').classList.toggle('hidden', state.remoteSubTab !== 'touchpad');
        if (state.remoteSubTab === 'apps') {
          refreshProcesses();
        }
        if (state.remoteSubTab !== 'screen') {
          stopScreenLive();
        }
      });
    });

    $('btn-proc-refresh').addEventListener('click', () => refreshProcesses());

    $('btn-privacy-mode').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/privacy-mode', {});
      if (data) toast('Mode Privasi aktif (Pause · Mute · Lock · Screen Off)', 'success');
    });

    // File & Camera Pickers
    $('btn-pick-media').addEventListener('click', () => $('input-media').click());
    $('btn-pick-camera').addEventListener('click', () => $('input-camera').click());
    $('btn-pick-files').addEventListener('click', () => $('input-files').click());
    $('input-media').addEventListener('change', (e) => {
      enqueueUploads(e.target.files);
      e.target.value = '';
    });
    $('input-camera').addEventListener('change', (e) => {
      enqueueUploads(e.target.files);
      e.target.value = '';
    });
    $('input-files').addEventListener('change', (e) => {
      enqueueUploads(e.target.files);
      e.target.value = '';
    });

    // Remote Control Actions
    const postRemote = async (url, payload) => {
      const res = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.status === 401) {
        showPinGate('Sesi berakhir.');
        return null;
      }
      const data = await res.json().catch(() => ({ ok: false, error: 'Gagal menghubungi laptop' }));
      if (!res.ok || data.ok === false) {
        toast(data.error || 'Aksi remote gagal', 'error');
        return null;
      }
      return data;
    };

    document.querySelectorAll('[data-media]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const payload = { action: btn.dataset.media };
        if (state.activeMediaBus) payload.bus_name = state.activeMediaBus;
        const data = await postRemote('/api/remote/media', payload);
        if (data && data.media) renderMedia(data.media);
        if (state.mediaSyncTimer) clearTimeout(state.mediaSyncTimer);
        state.mediaSyncTimer = setTimeout(syncRemoteLive, 350);
      });
    });

    document.querySelectorAll('[data-seek]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const sec = Number(btn.dataset.seek);
        const payload = { action: 'seek', seek_seconds: sec };
        if (state.activeMediaBus) payload.bus_name = state.activeMediaBus;
        const data = await postRemote('/api/remote/media', payload);
        if (data && data.media) renderMedia(data.media);
      });
    });

    $('r-media-players').addEventListener('click', async (e) => {
      const chip = e.target.closest('[data-media-bus]');
      if (!chip) return;
      const bus = chip.dataset.mediaBus;
      const data = await postRemote('/api/remote/media', { action: 'select', bus_name: bus });
      if (data && data.media) renderMedia(data.media);
    });

    $('r-sink-select').addEventListener('change', async (e) => {
      const data = await postRemote('/api/remote/sink', { sink: e.target.value });
      if (data) {
        if (data.sinks) renderSinks(data.sinks);
        if (data.volume) renderVolume(data.volume);
        toast('Output audio dipindahkan', 'success');
      }
    });

    let brightDebounce = null;
    const brightEl = $('r-bright-slider');
    brightEl.addEventListener('pointerdown', () => { state.draggingBright = true; });
    brightEl.addEventListener('pointerup', () => { state.draggingBright = false; });
    brightEl.addEventListener('pointercancel', () => { state.draggingBright = false; });
    brightEl.addEventListener('change', () => { state.draggingBright = false; });
    brightEl.addEventListener('input', (e) => {
      const val = Number(e.target.value);
      $('r-bright-val').textContent = `${val}%`;
      if (brightDebounce) clearTimeout(brightDebounce);
      brightDebounce = setTimeout(async () => {
        const data = await postRemote('/api/remote/brightness', { percent: val });
        if (data && data.brightness) {
          state.draggingBright = false;
          renderBrightness(data.brightness);
        }
      }, 90);
    });

    $('btn-display-off').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/display-off', {});
      if (data) toast('Layar laptop dimatikan', 'info');
    });

    let volDebounce = null;
    const volEl = $('r-vol-slider');
    volEl.addEventListener('pointerdown', () => { state.draggingVol = true; });
    volEl.addEventListener('pointerup', () => { state.draggingVol = false; });
    volEl.addEventListener('pointercancel', () => { state.draggingVol = false; });
    volEl.addEventListener('change', () => { state.draggingVol = false; });
    volEl.addEventListener('input', (e) => {
      const val = Number(e.target.value);
      $('r-vol-val').textContent = `${val}%`;
      if (volDebounce) clearTimeout(volDebounce);
      volDebounce = setTimeout(async () => {
        const data = await postRemote('/api/remote/volume', { percent: val });
        if (data && data.volume) {
          state.draggingVol = false;
          renderVolume(data.volume);
        }
      }, 90);
    });
    $('btn-vol-down').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/volume', { delta: -5 });
      if (data && data.volume) renderVolume(data.volume);
    });
    $('btn-vol-up').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/volume', { delta: 5 });
      if (data && data.volume) renderVolume(data.volume);
    });
    $('btn-vol-mute').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/volume', { mute_toggle: true });
      if (data && data.volume) renderVolume(data.volume);
    });

    $('btn-power-lock').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/power', { action: 'lock' });
      if (data) toast('Layar laptop dikunci', 'success');
    });

    $('btn-power-sleep').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/power', { action: 'sleep', delay_seconds: 10 });
      if (data) {
        renderPowerState(data.power);
        toast('Sleep dijadwalkan (10 dtk, bisa dibatalkan)', 'info');
      }
    });

    $('r-delay-chips').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-delay]');
      if (!chip) return;
      state.selectedDelay = Number(chip.dataset.delay);
      document.querySelectorAll('.delay-chip').forEach((c) => c.classList.toggle('active', c === chip));
    });

    const resetShutdownArm = () => {
      state.shutdownArmed = false;
      if (state.shutdownArmTimer) {
        clearTimeout(state.shutdownArmTimer);
        state.shutdownArmTimer = null;
      }
      $('btn-shutdown-label').textContent = 'Matikan';
    };

    const resetRestartArm = () => {
      state.restartArmed = false;
      if (state.restartArmTimer) {
        clearTimeout(state.restartArmTimer);
        state.restartArmTimer = null;
      }
      $('btn-restart-label').textContent = 'Restart';
    };

    $('btn-power-restart').addEventListener('click', async () => {
      if (!state.restartArmed) {
        state.restartArmed = true;
        const label = state.selectedDelay < 120 ? `${state.selectedDelay}d` : `${state.selectedDelay / 60}m`;
        $('btn-restart-label').textContent = `Konfirmasi (${label})`;
        state.restartArmTimer = setTimeout(resetRestartArm, 5000);
        return;
      }
      resetRestartArm();
      const data = await postRemote('/api/remote/power', {
        action: 'restart',
        delay_seconds: state.selectedDelay,
      });
      if (data) {
        renderPowerState(data.power);
        toast('Restart dijadwalkan — bisa dibatalkan', 'info');
      }
    });

    $('btn-power-shutdown').addEventListener('click', async () => {
      if (!state.shutdownArmed) {
        state.shutdownArmed = true;
        const label = state.selectedDelay < 120 ? `${state.selectedDelay}d` : `${state.selectedDelay / 60}m`;
        $('btn-shutdown-label').textContent = `Konfirmasi (${label})`;
        state.shutdownArmTimer = setTimeout(resetShutdownArm, 5000);
        return;
      }
      resetShutdownArm();
      const data = await postRemote('/api/remote/power', {
        action: 'shutdown',
        delay_seconds: state.selectedDelay,
      });
      if (data) {
        renderPowerState(data.power);
        toast('Shutdown dijadwalkan — bisa dibatalkan', 'info');
      }
    });

    $('btn-m-cancel-power').addEventListener('click', async () => {
      const data = await postRemote('/api/remote/power', { action: 'cancel' });
      if (data) {
        renderPowerState(data.power);
        toast('Jadwal daya dibatalkan!', 'success');
      }
    });

    // Live Screen MJPEG & Snapshot Controls
    $('btn-screen-live').addEventListener('click', () => {
      if (state.screenLive) stopScreenLive();
      else startScreenLive();
    });

    $('btn-screen-snap').addEventListener('click', () => {
      stopScreenLive();
      const img = $('screen-img');
      $('screen-placeholder').classList.add('hidden');
      img.classList.remove('hidden');
      img.src = `/api/remote/screen/snapshot?t=${Date.now()}`;
      $('screen-status-tag').textContent = 'SNAPSHOT';
    });

    $('r-fps-chips').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-fps]');
      if (!chip) return;
      state.screenFps = Number(chip.dataset.fps);
      $('r-fps-chips').querySelectorAll('.delay-chip').forEach((c) => c.classList.toggle('active', c === chip));
      if (state.screenLive) startScreenLive();
    });

    $('screen-img').addEventListener('click', async (e) => {
      if (!$('toggle-screen-tap').checked) return;
      const rect = e.target.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const x = (e.clientX - rect.left) / rect.width;
      const y = (e.clientY - rect.top) / rect.height;
      await postRemote('/api/remote/input', { type: 'click_at', x, y, button: 'left' });
    });

    // Presentation Keys & Remote Keyboard
    document.querySelectorAll('[data-rkey]').forEach((btn) => {
      btn.addEventListener('click', () => {
        postRemote('/api/remote/input', { type: 'key', key: btn.dataset.rkey });
      });
    });

    const sendRemoteText = async () => {
      const inp = $('r-type-input');
      const val = inp.value;
      if (!val) return;
      const data = await postRemote('/api/remote/input', { type: 'text', text: val });
      if (data) {
        inp.value = '';
      }
    };
    $('btn-type-send').addEventListener('click', sendRemoteText);
    $('r-type-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        sendRemoteText();
      }
    });

    // Precision Trackpad Surface (Touch Drag + Tap-to-Click)
    const pad = $('trackpad-surface');
    let lastTouch = null;
    let touchMoved = false;
    let accumDx = 0;
    let accumDy = 0;
    let moveInFlight = false;
    let touchStartCount = 1;

    const flushMouseMove = () => {
      if (moveInFlight || (accumDx === 0 && accumDy === 0)) return;
      const dx = Math.round(accumDx);
      const dy = Math.round(accumDy);
      accumDx = 0;
      accumDy = 0;
      if (dx === 0 && dy === 0) return;
      moveInFlight = true;
      fetch('/api/remote/input', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: 'move', dx, dy }),
      })
        .catch(() => {})
        .finally(() => {
          moveInFlight = false;
          if (accumDx !== 0 || accumDy !== 0) flushMouseMove();
        });
    };

    pad.addEventListener(
      'touchstart',
      (e) => {
        e.preventDefault();
        touchStartCount = e.touches.length;
        touchMoved = false;
        const t = e.touches[0];
        lastTouch = { x: t.clientX, y: t.clientY };
      },
      { passive: false }
    );

    pad.addEventListener(
      'touchmove',
      (e) => {
        e.preventDefault();
        if (!lastTouch || e.touches.length === 0) return;
        const t = e.touches[0];
        const rawDx = t.clientX - lastTouch.x;
        const rawDy = t.clientY - lastTouch.y;
        lastTouch = { x: t.clientX, y: t.clientY };
        if (Math.abs(rawDx) > 1.2 || Math.abs(rawDy) > 1.2) touchMoved = true;
        const speed = Math.hypot(rawDx, rawDy);
        const gain = speed > 10 ? 2.4 : 1.5;
        accumDx += rawDx * gain;
        accumDy += rawDy * gain;
        flushMouseMove();
      },
      { passive: false }
    );

    pad.addEventListener(
      'touchend',
      (e) => {
        e.preventDefault();
        if (!touchMoved) {
          postRemote('/api/remote/input', {
            type: 'click',
            button: touchStartCount >= 2 ? 'right' : 'left',
          });
        }
        lastTouch = null;
      },
      { passive: false }
    );

    // Also support mouse drag on trackpad when opened from another laptop/tablet mouse!
    let mouseDown = false;
    pad.addEventListener('mousedown', (e) => {
      mouseDown = true;
      touchMoved = false;
      lastTouch = { x: e.clientX, y: e.clientY };
    });
    window.addEventListener('mousemove', (e) => {
      if (!mouseDown || !lastTouch) return;
      const rawDx = e.clientX - lastTouch.x;
      const rawDy = e.clientY - lastTouch.y;
      lastTouch = { x: e.clientX, y: e.clientY };
      if (Math.abs(rawDx) > 1 || Math.abs(rawDy) > 1) touchMoved = true;
      accumDx += rawDx * 1.6;
      accumDy += rawDy * 1.6;
      flushMouseMove();
    });
    window.addEventListener('mouseup', () => {
      if (!mouseDown) return;
      mouseDown = false;
      if (!touchMoved) postRemote('/api/remote/input', { type: 'click', button: 'left' });
      lastTouch = null;
    });

    $('btn-mouse-left').addEventListener('click', () => postRemote('/api/remote/input', { type: 'click', button: 'left' }));
    $('btn-mouse-right').addEventListener('click', () => postRemote('/api/remote/input', { type: 'click', button: 'right' }));
    $('btn-scroll-up').addEventListener('click', () => postRemote('/api/remote/input', { type: 'scroll', delta_y: 3 }));
    $('btn-scroll-down').addEventListener('click', () => postRemote('/api/remote/input', { type: 'scroll', delta_y: -3 }));

    // Clipboard actions
    $('btn-copy-latest').addEventListener('click', async () => {
      const latest = state.clipboard[0];
      if (!latest) return;
      const ok = await copyTextSafe(latest.text);
      toast(ok ? 'Disalin ke clipboard iPhone!' : 'Gagal menyalin otomatis — tahan teks untuk salin', ok ? 'success' : 'error');
    });

    // Auto-detect URL in compose box to show 1-tap "Buka di Laptop" button
    const syncUrlButton = () => {
      const ta = $('m-clip-input');
      const hasUrl = Boolean(extractHttpUrl(ta.value));
      $('btn-m-open-link').classList.toggle('hidden', !hasUrl);
      $('btn-m-send-clip').classList.toggle('btn-primary', !hasUrl);
    };
    $('m-clip-input').addEventListener('input', syncUrlButton);

    const pushMobileClipboard = async (openOnDesktop) => {
      const ta = $('m-clip-input');
      const text = ta.value;
      if (!text.trim()) return;
      try {
        const res = await fetch('/api/clipboard', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, open_on_desktop: openOnDesktop }),
        });
        if (res.status === 401) {
          showPinGate('Sesi berakhir.');
          return;
        }
        const data = await res.json();
        if (data.ok) {
          ta.value = '';
          syncUrlButton();
          toast(
            openOnDesktop && data.opened_url
              ? 'Link langsung dibuka di browser laptop!'
              : 'Terkirim ke clipboard Linux Mint!',
            'success'
          );
        }
      } catch {
        toast('Gagal mengirim ke laptop', 'error');
      }
    };

    $('btn-m-send-clip').addEventListener('click', () => pushMobileClipboard(false));
    $('btn-m-open-link').addEventListener('click', () => pushMobileClipboard(true));

    // Logout
    $('btn-logout').addEventListener('click', async () => {
      setStoredTrustToken('');
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
      showPinGate('');
    });

    // Delegated clicks (Cancel upload/download, preview, gallery, app launcher, copy history)
    document.body.addEventListener('click', async (e) => {
      const appBtn = e.target.closest('[data-launch-app]');
      if (appBtn) {
        const appId = appBtn.dataset.launchApp;
        const data = await postRemote('/api/remote/apps/launch', { app_id: appId });
        if (data && data.app) {
          toast(`Membuka ${data.app.name} di laptop`, 'success');
        }
        return;
      }

      const quickUrlBtn = e.target.closest('[data-quick-url]');
      if (quickUrlBtn) {
        const targetUrl = quickUrlBtn.dataset.quickUrl;
        const data = await postRemote('/api/open-url', { url: targetUrl });
        if (data) toast(`Membuka ${quickUrlBtn.textContent} di laptop`, 'success');
        return;
      }

      const killBtn = e.target.closest('[data-kill-pid]');
      if (killBtn) {
        const pid = Number(killBtn.dataset.killPid);
        if (state.armedKillPid !== pid) {
          state.armedKillPid = pid;
          if (state.armedKillTimer) clearTimeout(state.armedKillTimer);
          state.armedKillTimer = setTimeout(() => {
            state.armedKillPid = null;
            renderProcesses();
          }, 4000);
          renderProcesses();
          return;
        }
        state.armedKillPid = null;
        if (state.armedKillTimer) clearTimeout(state.armedKillTimer);
        const data = await postRemote('/api/remote/processes/kill', { pid });
        if (data) {
          toast(`Ditutup: ${data.name}`, 'success');
          setTimeout(refreshProcesses, 400);
        }
        return;
      }

      const expRootBtn = e.target.closest('[data-exp-root]');
      if (expRootBtn) {
        state.expQuery = '';
        $('exp-search').value = '';
        refreshExplorer(expRootBtn.dataset.expRoot, '');
        return;
      }

      const expDirBtn = e.target.closest('[data-exp-dir]');
      if (expDirBtn) {
        state.expQuery = '';
        $('exp-search').value = '';
        refreshExplorer(state.expRoot, expDirBtn.dataset.expDir);
        return;
      }

      const galFolder = e.target.closest('[data-gal-folder]');
      if (galFolder) {
        refreshGallery(galFolder.dataset.galFolder);
        return;
      }

      const galPrev = e.target.closest('[data-gal-preview]');
      if (galPrev) {
        const gid = galPrev.dataset.galPreview;
        const item = state.galleryImages.find((g) => g.id === gid);
        if (item) {
          openImageLightbox(
            item.name,
            `/api/gallery/${gid}/preview`,
            `/api/gallery/${gid}/download`
          );
        }
        return;
      }

      const cancelBtn = e.target.closest('[data-cancel-tx]');
      if (cancelBtn) {
        const tid = cancelBtn.dataset.cancelTx;
        const local = state.localUploads.get(tid);
        if (local && local.xhr) {
          local.xhr.abort();
        }
        fetch(`/api/transfers/${tid}/cancel`, { method: 'POST', credentials: 'same-origin' }).catch(() => {});
        return;
      }

      const prevEl = e.target.closest('[data-preview]');
      if (prevEl) {
        const file = state.files.find((f) => f.id === prevEl.dataset.preview);
        if (file) openLightbox(file);
        return;
      }

      const openLaptopBtn = e.target.closest('[data-open-laptop]');
      if (openLaptopBtn) {
        const targetUrl = openLaptopBtn.dataset.openLaptop;
        try {
          const res = await fetch('/api/open-url', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: targetUrl }),
          });
          const data = await res.json();
          if (res.ok && data.ok) {
            toast('Link dibuka di browser laptop!', 'success');
          } else {
            toast(data.error || 'Gagal membuka link', 'error');
          }
        } catch {
          toast('Koneksi ke laptop gagal', 'error');
        }
        return;
      }

      const copyBtn = e.target.closest('[data-copy-clip]');
      if (copyBtn) {
        const item = state.clipboard.find((c) => c.id === copyBtn.dataset.copyClip);
        if (item) {
          const ok = await copyTextSafe(item.text);
          toast(ok ? 'Disalin ke clipboard iPhone!' : 'Tahan teks untuk menyalin manual', ok ? 'success' : 'error');
        }
      }
    });

    // Lightbox Close (button OR tap on dark area outside photo)
    const closeLightbox = () => {
      $('m-lightbox').classList.add('hidden');
      $('m-lightbox-body').innerHTML = '';
    };
    $('m-lightbox-close').addEventListener('click', closeLightbox);
    $('m-lightbox-body').addEventListener('click', (e) => {
      if (e.target === $('m-lightbox-body')) closeLightbox();
    });

    // iOS Safari resume/pause: stop live stream when backgrounded, reconnect SSE on resume
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') {
        stopScreenLive();
        return;
      }
      if (document.visibilityState === 'visible' && state.authenticated) {
        refreshState();
        if (state.tab === 'remote') refreshRemoteStatus();
        if (!state.sse || state.sse.readyState === EventSource.CLOSED) {
          connectSSE();
        }
      }
    });
  }

  // --- Bootstrap ---

  async function init() {
    dismissBootSplash();
    try {
      if (window.VDI18n) window.VDI18n.applyDomTranslations();
      bindEvents();
      renderPinSlots();
    } catch (err) {
      console.error('VoltDeck init UI error:', err);
    }
    try {
      const res = await fetch('/api/info', {
        credentials: 'same-origin',
        headers: authHeaders(),
      });
      const info = await res.json();
      if (info.language && window.VDI18n && !window.localStorage.getItem('voltdeck_lang_override')) {
        window.VDI18n.setLang(info.language, false);
      }
      state.hostName = info.device_name || 'Linux Mint';
      $('pin-host').textContent = state.hostName;
      if (info.authenticated) {
        await enterApp(state.hostName);
        return;
      }
      // Check optional ?pin=XXXXXX (only present if user explicitly enabled Auto-PIN QR)
      const params = new URLSearchParams(window.location.search);
      const qPin = (params.get('pin') || '').replace(/\D/g, '');
      if (qPin.length === 6) {
        state.pinValue = qPin;
        renderPinSlots();
        await submitPin(qPin);
      }
    } catch {
      $('pin-error').textContent = 'Tidak dapat terhubung ke server VoltDeck.';
    }
  }

  init();
})();
