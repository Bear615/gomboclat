'use strict';
// Show a friendly message when the server redirected back with ?error=…
const params = new URLSearchParams(window.location.search);
const el = document.getElementById('login-error');
if (params.get('error') === 'bad') {
  el.textContent = 'Wrong password.';
} else if (params.get('error') === 'throttled') {
  el.textContent = 'Too many attempts — try again in ' + (params.get('wait') || '60') + 's.';
}
