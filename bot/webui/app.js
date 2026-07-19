'use strict';
// Client for the web control hub. Polls /api/status + the event buffers and
// drives the same actions the TUI exposes. All POSTs carry the CSRF token the
// server derives from our session cookie.

const $ = (sel) => document.querySelector(sel);

let csrf = null;
let feedAfter = 0;
let maintAfter = 0;

async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    window.location = '/login';
    throw new Error('unauthenticated');
  }
  if (!resp.ok) {
    let msg = resp.statusText;
    try { msg = (await resp.json()).error || msg; } catch (e) { /* keep statusText */ }
    throw new Error(msg);
  }
  return resp.json();
}

function post(path, body = {}) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify(body),
  });
}

const STATE_LABEL = {
  running: '● running',
  starting: '◐ starting…',
  stopping: '◑ stopping…',
  stopped: '○ stopped',
  error: '✖ error',
};

function renderStatus(s) {
  csrf = s.csrf;
  const state = $('#state');
  state.textContent = STATE_LABEL[s.state] || s.state;
  state.className = 'state ' + s.state;
  $('#link').textContent = s.connected ? 'connected' : '—';
  $('#user').textContent = s.user || '—';
  $('#guild-count').textContent = s.guilds.length;
  $('#guilds').textContent = s.guilds.slice(0, 8).map((g) => '• ' + g).join('\n');
  const c = s.config;
  const updates = c.auto_update
    ? 'on · every ' + c.auto_update_interval + 'm' + (c.auto_restart ? ' · auto-restart' : '')
    : 'off';
  $('#cfg-summary').textContent = [
    'Model    : ' + c.model,
    'Rate     : ' + c.rate_limit_max + ' / ' + c.rate_limit_window + 's',
    'Punitive : ' + (c.enable_punitive ? 'on (typed CONFIRM)' : 'off'),
    'Updates  : ' + updates,
  ].join('\n');
  $('#warn').textContent = s.missing_secrets.length ? '⚠ set: ' + s.missing_secrets.join(', ') : '';
  $('#bot-error').textContent = s.error || '';
  $('#git-line').textContent = s.git || '—';
}

function appendEvents(el, events) {
  if (!events.length) return;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  for (const e of events) {
    const div = document.createElement('div');
    div.className = 'evt evt-' + e.kind;
    div.textContent = e.ts + '  ' + e.text;
    el.appendChild(div);
  }
  while (el.childElementCount > 600) el.removeChild(el.firstChild);
  if (nearBottom) el.scrollTop = el.scrollHeight;
}

async function poll() {
  try {
    renderStatus(await api('/api/status'));
    const feed = await api('/api/feed?after=' + feedAfter);
    if (feed.events.length) feedAfter = feed.events[feed.events.length - 1].id;
    appendEvents($('#feed'), feed.events);
    const ml = await api('/api/maintlog?after=' + maintAfter);
    if (ml.events.length) maintAfter = ml.events[ml.events.length - 1].id;
    appendEvents($('#maint-log'), ml.events);
    $('#conn-lost').hidden = true;
  } catch (e) {
    if (e.message !== 'unauthenticated') $('#conn-lost').hidden = false;
  }
}

const CONFIG_TEXT = [
  'discord_token', 'anthropic_api_key', 'anthropic_model', 'max_tokens',
  'max_agent_iterations', 'rate_limit_max', 'rate_limit_window',
  'bulk_confirm_threshold', 'auto_update_interval',
];
const CONFIG_BOOL = ['enable_punitive', 'auto_update', 'auto_restart'];

async function loadConfig() {
  const c = await api('/api/config');
  for (const f of CONFIG_TEXT) $('#cfg-' + f).value = c.values[f] || '';
  for (const f of CONFIG_BOOL) $('#cfg-' + f).checked = !!c.values[f];
  $('#cfg-discord_token').placeholder =
    c.discord_token_set ? '(set — leave blank to keep)' : 'required';
  $('#cfg-anthropic_api_key').placeholder =
    c.anthropic_key_set ? '(set — leave blank to keep)' : 'required';
}

async function saveConfig(restart) {
  const values = {};
  for (const f of CONFIG_TEXT) values[f] = $('#cfg-' + f).value.trim();
  for (const f of CONFIG_BOOL) values[f] = $('#cfg-' + f).checked;
  try {
    await post('/api/config', { values, restart });
    note(restart ? 'Saved — restarting bot…' : 'Saved to .env.');
    await loadConfig();
  } catch (e) {
    note('Save failed: ' + e.message, true);
  }
}

function note(msg, isError) {
  const n = $('#notice');
  n.textContent = msg;
  n.className = isError ? 'notice show error' : 'notice show';
  setTimeout(() => {
    if (n.textContent === msg) { n.textContent = ''; n.className = 'notice'; }
  }, 6000);
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.pane').forEach((p) =>
    p.classList.toggle('active', p.id === 'pane-' + name));
}

function botAction(action) {
  post('/api/bot', { action }).catch((e) => note(e.message, true));
}

function maintAction(action) {
  post('/api/maintenance', { action })
    .then(() => switchTab('maintenance'))
    .catch((e) => note(e.message, true));
}

window.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab').forEach((t) =>
    t.addEventListener('click', () => switchTab(t.dataset.tab)));
  $('#btn-start').onclick = () => botAction('start');
  $('#btn-stop').onclick = () => botAction('stop');
  $('#btn-restart').onclick = () => botAction('restart');
  $('#btn-clear').onclick = () => { $('#feed').innerHTML = ''; };
  $('#btn-save').onclick = () => saveConfig(false);
  $('#btn-save-restart').onclick = () => saveConfig(true);
  $('#btn-install').onclick = () => maintAction('install');
  $('#btn-reinstall').onclick = () => maintAction('reinstall');
  $('#btn-check').onclick = () => maintAction('check');
  $('#btn-update').onclick = () => maintAction('update');
  $('#btn-logout').onclick = async () => {
    try { await post('/logout'); } catch (e) { /* cookie may already be gone */ }
    window.location = '/login';
  };
  loadConfig().catch(() => {});
  poll();
  setInterval(poll, 3000);
});
