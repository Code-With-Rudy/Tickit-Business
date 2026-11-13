'use strict';

if (!requireSession('customer')) throw new Error('Not signed in');

const $ = id => document.getElementById(id);
let allTickets = [];
let timer = null;

function flash(el, message, cls, ms = 5000) {
  el.textContent = message;
  el.className = cls;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, ms);
}

// ---------------------------------------------------------------- find a service
function goToService(serviceId) { window.location.href = `book.html?s=${encodeURIComponent(serviceId)}`; }

function openService() {
  const id = parseServiceId($('serviceInput').value);
  if (!id) return flash($('findError'), 'Enter a valid service ID, like PRK-7K3QM2.', 'error-message');
  goToService(id);
}

function scannerError(msg) { $('scanStatus').textContent = msg; }

function openScanner() {
  $('scanModal').classList.add('show');
  $('scanStatus').textContent = 'Point your camera at the business QR code.';
  Scanner.start('qr-reader', onScanned, scannerError);
}
async function closeScanner() { await Scanner.stop(); $('scanModal').classList.remove('show'); }

async function onScanned(text) {
  const id = parseServiceId(text);
  if (!id) {
    $('scanStatus').textContent = "That isn't a Tick.IT QR code. Try again.";
    return Scanner.start('qr-reader', onScanned, scannerError);
  }
  await closeScanner();
  goToService(id);
}

// ---------------------------------------------------------------- tickets
async function fetchTickets() {
  const { ok, data } = await api(`/get-user-tickets/${encodeURIComponent(Session.userId)}`);
  if (!ok) throw new Error(data.error || 'Failed to load tickets');
  return data;
}

function activeTicketHtml(t) {
  const payload = JSON.stringify({ type: 'TICKIT_TICKET', ticketId: t.ticketId, customerId: Session.userId });
  return `
    <div class="ticket-card valid active-ticket-card">
      <div class="ticket-header">
        <div><span class="ticket-id">${escapeHtml(t.serviceName)}</span><div class="tc-biz">${escapeHtml(t.businessName)}</div></div>
        <span class="ticket-status valid">VALID</span>
      </div>
      <div class="ticket-details">
        <p><strong>Admits:</strong> ${escapeHtml(t.quantity)} ${t.quantity === 1 ? 'person' : 'people'}</p>
        <p><strong>Paid:</strong> ₹${escapeHtml(t.farePaid)}</p>
        <p><strong>Valid until:</strong> ${escapeHtml(new Date(t.expiresAt).toLocaleString())}</p>
      </div>
      <div class="time-remaining countdown" data-expiry="${escapeHtml(t.expiresAt)}"></div>
      <div class="qr-ticket">
        <div class="qr-box">${window.qrcode ? qrSvg(payload) : ''}</div>
        <p class="qr-hint">Show this QR at the entrance.</p>
        <p class="qr-hint">Backup code: <span class="mono">${escapeHtml(t.code || '')}</span></p>
      </div>
    </div>`;
}

function renderActive(tickets) {
  const valid = tickets.filter(t => t.state === 'VALID');
  $('activeTickets').innerHTML = valid.length
    ? `<div class="active-grid">${valid.map(activeTicketHtml).join('')}</div>`
    : '<div class="empty-state"><h4>No active tickets</h4><p>Scan a QR code or enter a service ID to buy one.</p></div>';
  startCountdowns();
}

function startCountdowns() {
  if (timer) clearInterval(timer);
  const tick = () => {
    let expired = false;
    document.querySelectorAll('.countdown').forEach(el => {
      const left = new Date(el.dataset.expiry).getTime() - Date.now();
      if (left <= 0) { el.textContent = 'Expired'; el.classList.add('expired'); expired = true; return; }
      const d = Math.floor(left / 86400000), h = Math.floor((left % 86400000) / 3600000);
      const m = Math.floor((left % 3600000) / 60000), s = Math.floor((left % 60000) / 1000);
      el.textContent = (d ? `${d}d ` : '') + (d || h ? `${h}h ` : '') + `${m}m ${s}s remaining`;
    });
    if (expired) { clearInterval(timer); refreshAll(); }
  };
  tick();
  timer = setInterval(tick, 1000);
}

function renderHistory(tickets) {
  const el = $('ticketHistory');
  if (!tickets.length) {
    el.innerHTML = '<div class="empty-state"><h4>No tickets found</h4><p>Your ticket history will appear here</p></div>';
    return;
  }
  el.innerHTML = tickets.map(t => {
    const cls = t.state.toLowerCase();
    const fmt = v => (v ? new Date(v).toLocaleString() : '-');
    return `
      <div class="history-item ${cls}">
        <div class="history-header">
          <span class="ticket-id">${escapeHtml(t.serviceName)} <span class="mono">#${escapeHtml(t.code || '')}</span></span>
          <span class="status ${cls}">${escapeHtml(t.state)}</span>
        </div>
        <div class="history-details">
          <p><strong>${escapeHtml(t.businessName)}</strong> · ${escapeHtml(t.quantity)} × ₹${escapeHtml(t.unitPrice)} = ₹${escapeHtml(t.farePaid)}</p>
          <p><strong>Purchased:</strong> ${escapeHtml(fmt(t.issuedAt))}</p>
          <p><strong>Expires:</strong> ${escapeHtml(fmt(t.expiresAt))}</p>
          ${t.validatedAt ? `<p><strong>Used at gate:</strong> ${escapeHtml(fmt(t.validatedAt))}</p>` : ''}
        </div>
      </div>`;
  }).join('');
}

function filterTickets(filter, event) {
  document.querySelectorAll('.filter-buttons button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  const want = { valid: 'VALID', used: 'USED', expired: 'EXPIRED' }[filter];
  renderHistory(want ? allTickets.filter(t => t.state === want) : allTickets);
}

// ---------------------------------------------------------------- wallet + profile
async function addFunds() {
  const amount = parseInt($('fundAmount').value, 10);
  const msg = $('walletMessage');
  if (!amount || amount < 1) return flash(msg, 'Please enter a valid amount', 'error-message');
  const { ok, data } = await api('/add-funds', { method: 'POST', body: { amount } });
  if (ok && data.success) {
    flash(msg, `₹${amount} added successfully!`, 'success-message');
    $('fundAmount').value = '';
    loadUserData();
  } else {
    flash(msg, data.error || 'Failed to add funds', 'error-message');
  }
}

async function loadUserData() {
  const { ok, data } = await api(`/get-user-details/${encodeURIComponent(Session.userId)}`);
  if (!ok) { $('userName').textContent = 'Error loading user'; return; }
  $('userName').textContent = data.fullName || 'Customer';
  $('walletBalance').textContent = data.walletBalance || 0;
  $('currentBalance').textContent = data.walletBalance || 0;
}

async function refreshAll() {
  loadUserData();
  try {
    allTickets = await fetchTickets();
    renderActive(allTickets);
    renderHistory(allTickets);
  } catch (e) {
    console.error(e);
    $('activeTickets').innerHTML = '<div class="error-message">Failed to load tickets</div>';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  refreshAll();
  setInterval(refreshAll, 30000);   // picks up gate scans that mark a ticket USED
  $('serviceInput').addEventListener('keydown', e => { if (e.key === 'Enter') openService(); });
});
window.addEventListener('beforeunload', () => Scanner.stop());

Object.assign(window, { openService, openScanner, closeScanner, addFunds, filterTickets });
