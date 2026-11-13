// Google Sign-In via Firebase Auth. Firebase is only used to prove who the person is;
// the server verifies the ID token and hands back Tick.IT's own session token.
import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js';
import { getAuth, GoogleAuthProvider, signInWithPopup, signOut }
  from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js';
import { firebaseConfig } from './firebase-config.js';

const configured = !String(firebaseConfig.apiKey).startsWith('YOUR_');
let auth = null;
if (configured) auth = getAuth(initializeApp(firebaseConfig));

// data-arg: customer | owner (log in) | owner-register (sign up, needs business details)
window.googleSignIn = async function (mode, _event, button) {
  const role = mode === 'customer' ? 'customer' : 'owner';
  const formType = { customer: 'userLogin', owner: 'ownerLogin', 'owner-register': 'ownerRegister' }[mode];
  const errEl = document.getElementById(
    (button?.dataset.form || formType) + 'Error');
  const fail = msg => { errEl.textContent = msg; errEl.style.display = 'block'; };
  clearMessages();

  if (!configured) return fail('Google sign-in is not configured yet. Add your Firebase web config to public/js/firebase-config.js.');

  const body = { role };
  if (mode === 'owner-register') {
    body.businessName = document.getElementById('businessName').value.trim();
    body.category = document.getElementById('category').value;
    if (!body.businessName) return fail('Enter your business name above, then continue with Google.');
  }

  button.disabled = true;
  try {
    const result = await signInWithPopup(auth, new GoogleAuthProvider());
    body.idToken = await result.user.getIdToken();
    await signOut(auth); // the app session below replaces the Firebase one

    const { ok, data } = await api('/auth/google', { method: 'POST', body, auth: false });
    if (!ok) return fail(data.error || 'Google sign-in failed');

    Session.save(data.token, data.uid, data.role);
    window.location.href = data.role === 'owner' ? 'owner_dashboard.html' : (safeNext() || 'dashboard.html');
  } catch (err) {
    const code = err && err.code;
    if (code === 'auth/popup-closed-by-user' || code === 'auth/cancelled-popup-request') return;
    fail(code === 'auth/popup-blocked'
      ? 'Your browser blocked the Google pop-up. Allow pop-ups for this site and try again.'
      : 'Google sign-in failed. Please try again.');
    console.error('Google sign-in error:', code || err);
  } finally {
    button.disabled = false;
  }
};
