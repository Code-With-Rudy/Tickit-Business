'use strict';
// Public booking page: opened from a QR code, a WhatsApp/social link, or an entered service ID.

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
let service = null;
let quantity = 1;
let balance = null;

function setState(message) {
  const el = $('state');
  el.className = message ? 'error-message' : '';
  el.textContent = message || '';
  el.style.display = message ? 'block' : 'none';
}

function renderNav() {
  const nav = $('navRight');
  if (Session.token) {
    const home = Session.role === 'owner' ? 'owner_dashboard.html' : 'dashboard.html';
    nav.innerHTML = `<a class="btn btn-secondary" href="${home}">${Session.role === 'owner' ? 'Dashboard' : 'My tickets'}</a>`;
  } else {
    nav.innerHTML = '<a class="btn" href="index.html">Log in</a>';
  }
}

async function init() {
  renderNav();
  const serviceId = (params.get('s') || '').toUpperCase();
  const biz = (params.get('b') || '').toUpperCase();
  if (serviceId) return loadService(serviceId);
  if (biz) return loadBusiness(biz);
  setState('This booking link is missing its service ID.');
}

async function loadBusiness(code) {
  const { ok, data } = await api(`/public/business/${encodeURIComponent(code)}`, { auth: false });
  if (!ok) return setState(data.error || 'Business not found');
  setState('');
  $('bizName').textContent = data.businessName;
  $('bizCategory').textContent = CATEGORY_LABELS[data.category] || '';
  $('bizServices').innerHTML = data.services.length ? data.services.map(s => `
    <a class="svc-card" href="book.html?s=${encodeURIComponent(s.serviceId)}">
      <div><strong>${escapeHtml(s.name)}</strong>
        <small>${escapeHtml(s.description || '')}</small>
        <small>Valid ${escapeHtml(formatValidity(s.validityMinutes))}</small></div>
      <span class="svc-price">${s.price ? '₹' + escapeHtml(s.price) : 'Free'}</span>
    </a>`).join('') : '<div class="empty-state"><h4>No tickets on sale</h4><p>Check back soon.</p></div>';
  $('businessView').style.display = 'block';
}

async function loadService(serviceId) {
  const { ok, data } = await api(`/public/service/${encodeURIComponent(serviceId)}`, { auth: false });
  if (!ok) return setState(data.error || 'Service not found');
  service = data;
  setState('');
  document.title = `${data.name} - Tick.IT`;
  $('svcBusiness').textContent = data.businessName || '';
  $('svcName').textContent = data.name;
  $('svcDesc').textContent = data.description || '';
  $('svcPrice').textContent = data.price;
  $('svcValidity').textContent = formatValidity(data.validityMinutes);
  $('svcCategory').textContent = CATEGORY_LABELS[data.category] || '';
  $('svcId').textContent = data.serviceId;
  $('serviceView').style.display = 'block';
  await renderBuyPanel();
}

async function renderBuyPanel() {
  const panel = $('buyPanel');
  if (!service.active) {
    panel.innerHTML = '<div class="warning-message">This service isn\'t selling tickets right now.</div>';
    return;
  }
  if (!Session.token) {
    const next = encodeURIComponent(`book.html?s=${service.serviceId}`);
    panel.innerHTML = `
      <a class="btn btn-lg btn-block" href="index.html?next=${next}">Log in or sign up to buy</a>
      <p class="qr-hint">New customers get a starter wallet balance to try it out.</p>`;
    return;
  }
  if (Session.role !== 'customer') {
    panel.innerHTML = '<div class="warning-message">You are logged in as a business owner. Log out and use a customer account to buy tickets.</div>';
    return;
  }
  const { ok, data } = await api(`/get-user-details/${encodeURIComponent(Session.userId)}`);
  if (!ok) { panel.innerHTML = '<div class="error-message">Could not load your wallet.</div>'; return; }
  balance = data.walletBalance || 0;
  drawBuyForm();
}

function drawBuyForm() {
  const total = service.price * quantity;
  const short = balance < total;
  $('buyPanel').innerHTML = `
    <div class="qty-row">
      <span>Number of tickets</span>
      <div class="stepper">
        <button type="button" class="btn btn-secondary" data-click="changeQty" data-arg="-1" aria-label="Fewer tickets" ${quantity <= 1 ? 'disabled' : ''}>−</button>
        <strong>${quantity}</strong>
        <button type="button" class="btn btn-secondary" data-click="changeQty" data-arg="1" aria-label="More tickets" ${quantity >= 10 ? 'disabled' : ''}>+</button>
      </div>
    </div>
    <div class="total-row"><span>Total</span><strong>₹${total}</strong></div>
    <p class="meta-line">Wallet balance: ₹${balance}</p>
    ${short ? '<div class="warning-message">Not enough balance. <a href="dashboard.html">Add money to your wallet</a> and come back.</div>' : ''}
    <button class="btn btn-lg btn-block" id="payBtn" type="button" data-click="pay" ${short ? 'disabled' : ''}>${total ? `Pay ₹${total}` : 'Get free ticket'}</button>
    <div class="error-message" id="payError" style="display:none"></div>`;
}

function changeQty(delta) {
  quantity = Math.min(10, Math.max(1, quantity + parseInt(delta, 10)));
  drawBuyForm();
}

async function pay() {
  const btn = $('payBtn');
  btn.disabled = true;   // one tap = one purchase
  const { ok, data } = await api('/pay', { method: 'POST', body: { serviceId: service.serviceId, quantity } });
  if (!ok || !data.success) {
    const err = $('payError');
    err.textContent = data.error || 'Payment failed. Please try again.';
    err.style.display = 'block';
    btn.disabled = false;
    return;
  }
  showSuccess(data);
}

function showSuccess(d) {
  $('serviceView').style.display = 'none';
  $('okName').textContent = d.serviceName;
  $('okQty').textContent = `${d.quantity} ${d.quantity === 1 ? 'person' : 'people'}`;
  $('okPaid').textContent = d.farePaid;
  $('okExpires').textContent = new Date(d.expiresAt).toLocaleString();
  $('okCode').textContent = d.code;
  $('okQr').innerHTML = qrSvg(JSON.stringify({ type: 'TICKIT_TICKET', ticketId: d.ticketId, customerId: Session.userId }));
  $('successView').style.display = 'block';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

Object.assign(window, { changeQty, pay });
init().catch(e => { console.error(e); setState('Something went wrong. Please try again.'); });
