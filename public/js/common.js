/* Tick.IT shared client helpers. No inline scripts anywhere (strict CSP). */
(function () {
  'use strict';

  const API_BASE_URL = '/api';

  // Session tokens live in sessionStorage (cleared when the tab closes), not localStorage.
  const Session = {
    get token() { return sessionStorage.getItem('tickit_token'); },
    get userId() { return sessionStorage.getItem('tickit_uid'); },
    get role() { return sessionStorage.getItem('tickit_role'); },
    save(token, uid, role) {
      sessionStorage.setItem('tickit_token', token);
      sessionStorage.setItem('tickit_uid', uid);
      sessionStorage.setItem('tickit_role', role);
    },
    clear() {
      ['tickit_token', 'tickit_uid', 'tickit_role'].forEach(k => sessionStorage.removeItem(k));
      // Remove anything left over by the pre-upgrade version of the app.
      ['userToken', 'userId', 'ownerToken', 'ownerId', 'userType'].forEach(k => localStorage.removeItem(k));
    },
  };

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  /** fetch wrapper: adds the Bearer token, parses JSON, bounces to login on an expired session. */
  async function api(path, { method = 'GET', body, auth = true } = {}) {
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const sentToken = auth && Session.token;
    if (sentToken) headers['Authorization'] = 'Bearer ' + Session.token;

    const res = await fetch(API_BASE_URL + path, {
      method, headers, body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (res.status === 401 && sentToken) {
      Session.clear();
      window.location.href = '/index.html';
    }
    return { ok: res.ok, status: res.status, data };
  }

  function requireSession(role, loginRedirect = '/index.html') {
    if (!Session.token || Session.role !== role) {
      window.location.href = loginRedirect;
      return false;
    }
    return true;
  }

  function logout() {
    Session.clear();
    window.location.href = '/index.html';
  }

  // ---- click delegation: <button data-click="fnName" data-arg="x"> calls window.fnName('x', event) ----
  document.addEventListener('click', function (e) {
    const el = e.target.closest('[data-click]');
    if (!el) return;
    const fn = window[el.dataset.click];
    if (typeof fn === 'function') fn(el.dataset.arg, e, el);
  });

  // ---- QR rendering (qrcode-generator): builds a real vector SVG ----
  function qrModel(text) {
    const qr = window.qrcode(0, 'M');
    qr.addData(text);
    qr.make();
    return qr;
  }

  function qrSvg(text, { margin = 4 } = {}) {
    const qr = qrModel(text);
    const n = qr.getModuleCount();
    let path = '';
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (qr.isDark(r, c)) path += `M${c + margin},${r + margin}h1v1h-1z`;
      }
    }
    const size = n + margin * 2;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges" role="img" aria-label="QR code">` +
      `<rect width="${size}" height="${size}" fill="#fff"/><path d="${path}" fill="#000"/></svg>`;
  }

  function qrPngBlob(text, px = 1024) {
    return new Promise(resolve => {
      const qr = qrModel(text);
      const margin = 4, n = qr.getModuleCount(), cell = Math.floor(px / (n + margin * 2));
      const side = cell * (n + margin * 2);
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = side;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, side, side);
      ctx.fillStyle = '#000';
      for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) {
        if (qr.isDark(r, c)) ctx.fillRect((c + margin) * cell, (r + margin) * cell, cell, cell);
      }
      canvas.toBlob(resolve, 'image/png');
    });
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---- Camera scanner (html5-qrcode) ----
  const Scanner = {
    instance: null,
    async start(elementId, onDecode, onError) {
      await this.stop();
      if (!window.Html5Qrcode) { onError && onError('Scanner library failed to load.'); return; }
      if (!window.isSecureContext) { onError && onError('Camera access needs HTTPS.'); return; }
      let handled = false;
      this.instance = new Html5Qrcode(elementId);
      try {
        await this.instance.start(
          { facingMode: 'environment' },
          { fps: 10, qrbox: (w, h) => { const s = Math.floor(Math.min(w, h) * 0.75); return { width: s, height: s }; } },
          text => { if (handled) return; handled = true; onDecode(text); },
          () => { /* per-frame "no QR found" noise */ }
        );
      } catch (err) {
        this.instance = null;
        const denied = /permission|denied|notallowed/i.test(String(err));
        onError && onError(denied
          ? 'Camera permission was denied. Allow camera access in your browser settings, or enter the ID manually.'
          : 'Could not start the camera. Enter the ID manually instead.');
      }
    },
    async stop() {
      const inst = this.instance;
      this.instance = null;
      if (!inst) return;
      try { await inst.stop(); } catch (_) { /* already stopped */ }
      try { inst.clear(); } catch (_) { /* nothing to clear */ }
    },
  };

  // ---- booking links / ids ----
  const CATEGORY_LABELS = {
    park: 'Park / Attraction', temple: 'Temple / Religious', parking: 'Parking',
    transport: 'Ride / Transport', event: 'Event', other: 'Other business',
  };
  const bookingLink = serviceId => `${location.origin}/book.html?s=${encodeURIComponent(serviceId)}`;
  const businessLink = code => `${location.origin}/book.html?b=${encodeURIComponent(code)}`;

  /** Accepts a booking URL on this site (what our QR codes contain) or a typed ID. Returns an upper-case ID or null. */
  function parseServiceId(text) {
    text = String(text || '').trim();
    let candidate = text;
    try {
      const u = new URL(text);
      if (u.origin !== location.origin) return null;
      candidate = u.searchParams.get('s') || '';
    } catch (_) { /* not a URL - treat as a typed ID */ }
    candidate = candidate.toUpperCase();
    return /^[A-Z]{3}-[A-Z0-9]{6}$/.test(candidate) ? candidate : null;
  }

  /** Only allow returning to our own booking pages after login (prevents open redirects). */
  function safeNext() {
    const n = new URLSearchParams(location.search).get('next');
    return n && /^book\.html\?(s|b)=[A-Za-z0-9-]{3,20}$/.test(n) ? n : null;
  }

  function formatValidity(min) {
    min = Number(min);
    if (min < 60) return `${min} min`;
    if (min < 1440) { const h = +(min / 60).toFixed(1); return `${h} hour${h === 1 ? '' : 's'}`; }
    const d = +(min / 1440).toFixed(1);
    return `${d} day${d === 1 ? '' : 's'}`;
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; }
    catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      let ok = false; try { ok = document.execCommand('copy'); } catch (_) { /* ignore */ }
      ta.remove(); return ok;
    }
  }

  function toast(message) {
    let box = document.getElementById('notification-container');
    if (!box) { box = document.createElement('div'); box.id = 'notification-container'; document.body.appendChild(box); }
    const n = document.createElement('div');
    n.className = 'notification';
    n.textContent = message;
    box.appendChild(n);
    setTimeout(() => n.remove(), 5000);
  }

  Object.assign(window, {
    API_BASE_URL, Session, escapeHtml, api, requireSession, logout,
    qrSvg, qrPngBlob, downloadBlob, Scanner, toast,
    CATEGORY_LABELS, bookingLink, businessLink, parseServiceId, safeNext, formatValidity, copyText,
  });
})();
