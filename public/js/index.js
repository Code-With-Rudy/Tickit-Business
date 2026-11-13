'use strict';

function openAuthTab(tabName) {
  document.getElementById('auth').scrollIntoView({ behavior: 'smooth' });
  showTab(tabName);
}

function showTab(tabName) {
  document.querySelectorAll('.form-container').forEach(c => c.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(tabName).classList.add('active');
  const tab = document.querySelector(`.tab[data-arg="${tabName}"]`);
  if (tab) tab.classList.add('active');
  clearMessages();
}

function clearMessages() {
  document.querySelectorAll('.error, .success').forEach(m => { m.style.display = 'none'; m.textContent = ''; });
}
function showLoading(f) {
  document.getElementById(f + 'Loading').style.display = 'block';
  document.querySelector(`#${f}Form button[type=submit]`).disabled = true;
}
function hideLoading(f) {
  document.getElementById(f + 'Loading').style.display = 'none';
  document.querySelector(`#${f}Form button[type=submit]`).disabled = false;
}
function showError(f, msg) { const el = document.getElementById(f + 'Error'); el.textContent = msg; el.style.display = 'block'; }
function showSuccess(f, msg) { const el = document.getElementById(f + 'Success'); el.textContent = msg; el.style.display = 'block'; }

function bindForm(formType, handler) {
  document.getElementById(formType + 'Form').addEventListener('submit', async e => {
    e.preventDefault();
    clearMessages();
    showLoading(formType);
    try {
      await handler(Object.fromEntries(new FormData(e.target)), e.target);
    } catch (err) {
      showError(formType, 'Network error. Please try again.');
      console.error(formType + ' error:', err);
    } finally {
      hideLoading(formType);
    }
  });
}

function loginHandler(formType, path, role, target) {
  return async body => {
    const { ok, data } = await api(path, { method: 'POST', body, auth: false });
    if (ok && data.success) {
      Session.save(data.token, data.uid, role);
      showSuccess(formType, 'Login successful! Redirecting...');
      // Customers coming from a shared booking link go straight back to it.
      const back = role === 'customer' ? safeNext() : null;
      setTimeout(() => { window.location.href = back || target; }, 800);
    } else {
      // Includes "N attempt(s) remaining" and the 1-hour lockout message from the server.
      showError(formType, data.error || 'Login failed');
    }
  };
}

bindForm('userLogin', loginHandler('userLogin', '/login/user', 'customer', 'dashboard.html'));
bindForm('ownerLogin', loginHandler('ownerLogin', '/login/owner', 'owner', 'owner_dashboard.html'));

function registerHandler(formType, path, nextTab, transform = x => x) {
  return async (body, form) => {
    if (body.password.length < 8) return showError(formType, 'Password must be at least 8 characters long');
    const { ok, data } = await api(path, { method: 'POST', body: transform(body), auth: false });
    if (ok && data.success) {
      showSuccess(formType, 'Registration successful! You can now log in.');
      form.reset();
      setTimeout(() => showTab(nextTab), 1500);
    } else {
      showError(formType, data.error || 'Registration failed');
    }
  };
}

bindForm('userRegister', registerHandler('userRegister', '/register/user', 'user-login'));
bindForm('ownerRegister', registerHandler('ownerRegister', '/register/owner', 'owner-login'));

window.addEventListener('load', () => {
  if (Session.token && Session.role === 'customer') window.location.href = safeNext() || 'dashboard.html';
  else if (Session.token && Session.role === 'owner') window.location.href = 'owner_dashboard.html';
});

if (safeNext()) window.addEventListener('DOMContentLoaded', () => showTab('user-register'));

Object.assign(window, { openAuthTab, showTab, clearMessages });
