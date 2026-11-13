'use strict';

if (!requireSession('owner')) throw new Error('Not signed in');

const $ = id => document.getElementById(id);
let services = [];
let owner = null;
let editingId = null;       // null = creating
let sharing = null;         // service currently in the share modal
let activeFilter = 'active';
let lastKnownTicketCount = 0;
let verifying = false;

const parseDate = v => (v ? new Date(v) : null);
const fmtDate = v => (parseDate(v) ? parseDate(v).toLocaleString() : null);
const priceLabel = p => (p ? `₹${p}` : 'Free');

function showSettingsMessage(message, type) {
  const box = $('settingsMessage');
  const div = document.createElement('div');
  div.className = type === 'error' ? 'error-message' : 'success-message';
  div.textContent = message;
  box.replaceChildren(div);
  setTimeout(() => box.replaceChildren(), 5000);
}

function openModal(id) { $(id).classList.add('show'); }
function closeModal(id) { $(id).classList.remove('show'); }

// ---------------------------------------------------------------- profile
async function loadOwnerData() {
  const { ok, data } = await api(`/get-owner-details/${encodeURIComponent(Session.userId)}`);
  if (!ok) { $('businessName').textContent = 'Error loading data'; return; }
  owner = data;
  $('businessName').textContent = data.businessName || 'Business';
  $('businessCategory').textContent = CATEGORY_LABELS[data.category] || '';
  if (data.bizCode) {
    const link = businessLink(data.bizCode);
    $('bizLinkText').textContent = link;
    $('bizWhatsapp').href = whatsappUrl(`Buy tickets for ${data.businessName}: ${link}`);
    $('bizLinkRow').style.display = 'flex';
  }
}

function whatsappUrl(text) { return `https://wa.me/?text=${encodeURIComponent(text)}`; }

async function copyBizLink() {
  if (!owner?.bizCode) return;
  toast((await copyText(businessLink(owner.bizCode))) ? 'Link copied' : 'Could not copy - select and copy it manually');
}

// ---------------------------------------------------------------- services
async function loadServices() {
  const { ok, data } = await api('/owner/services');
  if (!ok) { $('servicesList').innerHTML = `<div class="error-message">${escapeHtml(data.error || 'Failed to load services')}</div>`; return; }
  services = data.services;
  renderServices();
}

function renderServices() {
  if (!services.length) {
    $('servicesList').innerHTML = '<div class="empty-state"><h4>No services yet</h4><p>Add your first service to get a QR code and booking link.</p></div>';
    return;
  }
  $('servicesList').innerHTML = services.map(s => `
    <div class="service-row ${s.active ? '' : 'paused'}">
      <div class="service-main">
        <strong>${escapeHtml(s.name)}</strong>
        <small>${escapeHtml(s.description || '')}</small>
        <small><span class="mono">${escapeHtml(s.serviceId)}</span> · ${escapeHtml(priceLabel(s.price))} · valid ${escapeHtml(formatValidity(s.validityMinutes))}
          · <span class="status ${s.active ? 'valid' : 'used'}">${s.active ? 'ON SALE' : 'PAUSED'}</span></small>
      </div>
      <div class="btn-row">
        <button class="btn" type="button" data-click="openShare" data-arg="${escapeHtml(s.serviceId)}">QR &amp; link</button>
        <button class="btn btn-secondary" type="button" data-click="openServiceForm" data-arg="${escapeHtml(s.serviceId)}">Edit</button>
        <button class="btn btn-secondary" type="button" data-click="toggleService" data-arg="${escapeHtml(s.serviceId)}">${s.active ? 'Pause' : 'Resume'}</button>
      </div>
    </div>`).join('');
}

function openServiceForm(serviceId) {
  const s = services.find(x => x.serviceId === serviceId);
  editingId = s ? s.serviceId : null;
  $('serviceModalTitle').textContent = s ? 'Edit service' : 'Add service';
  $('svcName').value = s ? s.name : '';
  $('svcDescription').value = s ? s.description : '';
  $('svcPrice').value = s ? s.price : 50;
  const sel = $('svcValidity');
  const v = String(s ? s.validityMinutes : 60);
  if (![...sel.options].some(o => o.value === v)) sel.add(new Option(formatValidity(v), v));
  sel.value = v;
  $('svcFormError').style.display = 'none';
  openModal('serviceModal');
}

async function saveService() {
  const body = {
    name: $('svcName').value.trim(),
    description: $('svcDescription').value.trim(),
    price: parseInt($('svcPrice').value, 10),
    validityMinutes: parseInt($('svcValidity').value, 10),
  };
  const err = $('svcFormError');
  if (body.name.length < 2) { err.textContent = 'Enter a service name.'; err.style.display = 'block'; return; }
  if (isNaN(body.price) || body.price < 0) { err.textContent = 'Enter a valid price (0 for free).'; err.style.display = 'block'; return; }
  const btn = $('svcSaveBtn');
  btn.disabled = true;
  try {
    const { ok, data } = editingId
      ? await api(`/owner/services/${encodeURIComponent(editingId)}`, { method: 'PATCH', body })
      : await api('/owner/services', { method: 'POST', body });
    if (!ok) { err.textContent = data.error || 'Could not save'; err.style.display = 'block'; return; }
    closeModal('serviceModal');
    await loadServices();
    if (!editingId) openShare(data.service.serviceId);   // new service: go straight to its QR
    toast(editingId ? 'Service updated' : 'Service created');
  } finally {
    btn.disabled = false;
  }
}

async function toggleService(serviceId) {
  const s = services.find(x => x.serviceId === serviceId);
  if (!s) return;
  const { ok, data } = await api(`/owner/services/${encodeURIComponent(serviceId)}`, { method: 'PATCH', body: { active: !s.active } });
  if (!ok) return toast(data.error || 'Could not update');
  await loadServices();
  toast(s.active ? 'Service paused' : 'Service is on sale');
}

// ---------------------------------------------------------------- share / QR / poster
function openShare(serviceId) {
  const s = services.find(x => x.serviceId === serviceId);
  if (!s) return;
  if (!window.qrcode) return toast('QR library failed to load. Check your connection and refresh.');
  sharing = s;
  const link = bookingLink(s.serviceId);
  const svg = qrSvg(link);                       // QR holds the booking URL: any phone camera can open it
  $('shareTitle').textContent = s.name;
  $('shareQr').innerHTML = svg;
  $('shareId').textContent = s.serviceId;
  $('shareLinkText').textContent = link;
  $('shareWhatsapp').href = whatsappUrl(`Get your ${s.name} ticket for ${owner?.businessName || ''}: ${link}`);
  $('nativeShareBtn').style.display = navigator.share ? '' : 'none';
  $('posterQr').innerHTML = svg;
  $('posterBiz').textContent = owner?.businessName || '';
  $('posterService').textContent = s.name;
  $('posterId').textContent = s.serviceId;
  openModal('shareModal');
}

async function copyShareLink() {
  if (sharing) toast((await copyText(bookingLink(sharing.serviceId))) ? 'Link copied' : 'Could not copy - select and copy it manually');
}
function nativeShare() {
  if (sharing) navigator.share({ title: sharing.name, text: `Get your ${sharing.name} ticket`, url: bookingLink(sharing.serviceId) }).catch(() => {});
}
async function downloadQrPng() { if (sharing) downloadBlob(await qrPngBlob(bookingLink(sharing.serviceId)), `tickit-${sharing.serviceId}.png`); }
function downloadQrSvg() { if (sharing) downloadBlob(new Blob([qrSvg(bookingLink(sharing.serviceId))], { type: 'image/svg+xml' }), `tickit-${sharing.serviceId}.svg`); }
function printPoster() { if (sharing) window.print(); }

// ---------------------------------------------------------------- analytics + tickets
async function fetchOwnerTickets() {
  const { ok, data } = await api(`/get-owner-tickets/${encodeURIComponent(Session.userId)}`);
  if (!ok || !Array.isArray(data)) throw new Error(data.error || 'Invalid data received from server');
  return data;
}

async function loadAnalytics() {
  try {
    const all = await fetchOwnerTickets();
    const total = all.reduce((s, t) => s + (parseFloat(t.farePaid) || 0), 0);
    const now = new Date();
    const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const today = all.filter(t => { const d = parseDate(t.issuedAt); return d && d >= startOfDay; })
      .reduce((s, t) => s + (parseFloat(t.farePaid) || 0), 0);
    const unused = all.filter(t => t.state === 'VALID').length;
    const used = all.filter(t => t.state === 'USED').length;
    const customers = new Set(all.map(t => t.customerId)).size;
    $('todayRevenue').textContent = `₹${today.toFixed(0)}`;
    $('activeTicketsCount').textContent = unused;
    $('totalCustomers').textContent = customers;
    $('totalTickets').textContent = all.length;
    $('usedTickets').textContent = used;
    $('expiredTickets').textContent = all.length - unused - used;
    $('totalEarnings').textContent = total.toFixed(0);
    $('revenuePerCustomer').textContent = customers ? (total / customers).toFixed(0) : '0';
  } catch (e) {
    console.error(e);
    showSettingsMessage('Failed to load analytics. ' + e.message, 'error');
  }
}

async function syncRevenue() {
  const { ok, data } = await api(`/sync-owner-earnings/${encodeURIComponent(Session.userId)}`, { method: 'POST' });
  if (!ok || !data.success) return showSettingsMessage(data.error || 'Sync failed', 'error');
  showSettingsMessage(`Revenue synced: ₹${data.totalRevenue} from ${data.ticketCount} tickets`, 'success');
  loadAnalytics();
}

async function showTickets(status, event) {
  if (status) activeFilter = status;
  if (event) {
    document.querySelectorAll('.tab-button').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
  }
  const display = $('ticketsDisplay');
  display.innerHTML = '<div class="loading">Loading tickets...</div>';
  try {
    const all = await fetchOwnerTickets();
    const want = { active: 'VALID', used: 'USED', expired: 'EXPIRED' }[activeFilter];
    const tickets = want ? all.filter(t => t.state === want) : all;
    display.innerHTML = tickets.length
      ? `<div class="tickets-list">${tickets.map(ticketHtml).join('')}</div>`
      : `<div class="empty-state"><h4>No ${escapeHtml(activeFilter)} tickets</h4><p>Tickets appear here as customers buy them.</p></div>`;
  } catch (e) {
    display.innerHTML = `<div class="error-message">Failed to load tickets: ${escapeHtml(e.message)}</div>`;
  }
}

function ticketHtml(t) {
  const label = t.state === 'VALID' ? 'UNUSED' : t.state;
  // Customer-controlled text (name/email) is always escaped before reaching innerHTML.
  return `
    <div class="ticket-item ${t.state === 'VALID' ? 'active-ticket' : 'expired-ticket'}">
      <div class="ticket-info">
        <div class="passenger-info">
          <strong>${escapeHtml(t.customerName || 'Customer')}</strong>
          <small>${escapeHtml(t.customerEmail || '')}</small>
        </div>
        <div class="ticket-details">
          <p><strong>${escapeHtml(t.serviceName)}</strong> × ${escapeHtml(t.quantity)} · <strong>₹${escapeHtml(t.farePaid)}</strong> <span class="mono">#${escapeHtml(t.code || '')}</span></p>
          <p><strong>Purchased:</strong> ${escapeHtml(fmtDate(t.issuedAt) || '-')}</p>
          <p><strong>Expires:</strong> ${escapeHtml(fmtDate(t.expiresAt) || '-')}</p>
          ${t.validatedAt ? `<p><strong>Used at gate:</strong> ${escapeHtml(fmtDate(t.validatedAt))}</p>` : ''}
          <p><span class="status ${t.state.toLowerCase()}">${escapeHtml(label)}</span></p>
        </div>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- validate tickets
function openTicketScanner() {
  openModal('verifyModal');
  resetVerifyView();
  startTicketScan();
}
function startTicketScan() { Scanner.start('ticket-reader', onTicketScanned, msg => { $('verifyStatus').textContent = msg; }); }
function resetVerifyView() {
  $('verifyResult').style.display = 'none';
  $('verifyScanArea').style.display = 'block';
  $('verifyStatus').textContent = "Point your camera at the customer's ticket QR.";
}
async function closeTicketScanner() {
  await Scanner.stop();
  closeModal('verifyModal');
  loadAnalytics();
  showTickets();
}
function scanNext() { resetVerifyView(); startTicketScan(); }

async function submitVerify(body) {
  if (verifying) return;
  verifying = true;
  await Scanner.stop();
  try {
    const { data } = await api('/tickets/verify', { method: 'POST', body });
    const detail = [data.serviceName, data.customerName].filter(Boolean).join(' · ');
    if (data.result === 'VALID') showVerifyResult('VALID', 'VALID - Redeemed', detail, `ADMIT ${data.quantity}`);
    else if (data.result === 'USED') showVerifyResult('USED', 'ALREADY USED', `${detail}${data.validatedAt ? ' · used ' + fmtDate(data.validatedAt) : ''}`, '');
    else if (data.result === 'EXPIRED') showVerifyResult('EXPIRED', 'INVALID / EXPIRED', detail || data.error, '');
    else showVerifyResult('INVALID', 'INVALID', data.error || 'Ticket could not be verified', '');
  } catch (e) {
    console.error(e);
    showVerifyResult('INVALID', 'Network error', 'Could not reach the server. The ticket was NOT redeemed - try again.', '');
  } finally {
    verifying = false;
  }
}

async function onTicketScanned(text) {
  let qr;
  try { qr = JSON.parse(text); } catch (_) { qr = null; }
  if (!qr || qr.type !== 'TICKIT_TICKET' || !qr.ticketId || !qr.customerId) {
    await Scanner.stop();
    return showVerifyResult('INVALID', 'Not a Tick.IT ticket', 'That QR code is not a customer ticket.', '');
  }
  submitVerify({ ticketId: qr.ticketId, customerId: qr.customerId });
}

function redeemCode() {
  const code = $('manualCode').value.trim().toUpperCase();
  if (!/^[A-Z0-9]{8}$/.test(code)) return toast('Enter the 8-character code shown on the ticket');
  openModal('verifyModal');
  $('verifyScanArea').style.display = 'none';
  $('manualCode').value = '';
  submitVerify({ code });
}

function showVerifyResult(kind, title, detail, admit) {
  $('verifyScanArea').style.display = 'none';
  const box = $('verifyResult');
  box.style.display = 'flex';
  box.className = `verify-result ${kind.toLowerCase()}`;
  $('verifyIcon').textContent = kind === 'VALID' ? '✓' : '✕';
  $('verifyTitle').textContent = title;
  $('verifyAdmit').textContent = admit;
  $('verifyDetail').textContent = detail;
}

// ---------------------------------------------------------------- notifications
async function checkForNewTickets() {
  try {
    const tickets = await fetchOwnerTickets();
    if (lastKnownTicketCount > 0 && tickets.length > lastKnownTicketCount && 'Notification' in window && Notification.permission === 'granted') {
      const t = tickets[0];
      new Notification('New ticket sold', { body: `${t.serviceName} × ${t.quantity} - ₹${t.farePaid}` });
      new Audio('/preview.mp3').play().catch(() => {});
    }
    lastKnownTicketCount = tickets.length;
  } catch (e) { console.error(e); }
}

document.addEventListener('DOMContentLoaded', async () => {
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
  $('manualCode').addEventListener('keydown', e => { if (e.key === 'Enter') redeemCode(); });
  await Promise.all([loadOwnerData(), loadServices()]);
  await loadAnalytics();
  await showTickets('active');
  checkForNewTickets();
  setInterval(() => {
    loadAnalytics(); checkForNewTickets();
    if (!document.querySelector('.modal.show')) showTickets();
  }, 30000);
});
window.addEventListener('beforeunload', () => Scanner.stop());

Object.assign(window, {
  openServiceForm, saveService, toggleService, closeModal, openShare, copyShareLink, copyBizLink, nativeShare,
  downloadQrPng, downloadQrSvg, printPoster, syncRevenue, showTickets,
  openTicketScanner, closeTicketScanner, scanNext, redeemCode,
});
