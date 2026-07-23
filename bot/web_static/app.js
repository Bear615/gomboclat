const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ui = {
  status: null,
  formHydrated: false,
  formDirty: false,
  configRevision: null,
  savedForm: null,
  lastActivityId: 0,
  lastMaintenanceId: 0,
  refreshRunning: false,
  refreshTimer: null,
  toastTimer: null,
  eventSource: null,
};

const PAGE_COPY = {
  dashboard: ['OVERVIEW', 'System overview'],
  configure: ['CONFIGURATION', 'Model and guardrails'],
  maintenance: ['MAINTENANCE', 'Repository and runtime'],
};

const STATE_LABELS = {
  stopped: 'Bot stopped',
  starting: 'Bot starting',
  running: 'Bot operational',
  stopping: 'Bot stopping',
  restarting: 'Bot restarting',
  error: 'Bot error',
};

const KIND_CLASSES = new Set([
  'system', 'lifecycle', 'error', 'discord', 'request', 'audit',
  'configuration', 'operation', 'line', 'success',
]);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[character]));
}

function safeKind(kind) {
  return KIND_CLASSES.has(kind) ? kind : 'system';
}

function timeLabel(value, includeSeconds = true) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return '—';
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false,
  }).format(date);
}

function endpointLabel(url) {
  try {
    const parsed = new URL(url);
    return `${parsed.host}${parsed.pathname.replace(/\/$/, '')}`;
  } catch (_) {
    return url || 'OpenAI-compatible endpoint';
  }
}

function toast(message, type = 'success') {
  const element = $('#toast');
  element.textContent = message;
  element.className = `show ${type}`;
  clearTimeout(ui.toastTimer);
  ui.toastTimer = setTimeout(() => { element.className = ''; }, 4200);
}

function setStreamState(mode, text) {
  const element = $('#streamState');
  element.className = `stream-state ${mode}`;
  element.lastChild.textContent = ` ${text}`;
}

function navigate(page) {
  if (!PAGE_COPY[page]) page = 'dashboard';
  $$('.nav-item').forEach((button) => {
    const active = button.dataset.page === page;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  });
  $$('.page').forEach((section) => section.classList.toggle('active', section.id === page));
  $('#crumb').textContent = PAGE_COPY[page][0];
  $('#pageTitle').textContent = PAGE_COPY[page][1];
  history.replaceState(null, '', `#${page}`);
  window.scrollTo({ top: 0, behavior: 'auto' });
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function renderActivity(items) {
  const target = $('#activity');
  ui.lastActivityId = Math.max(0, ...items.map((item) => Number(item.id) || 0));
  target.innerHTML = items.slice().reverse().map(activityMarkup).join('') || (
    '<div class="empty-state">No runtime messages yet.</div>'
  );
}

function activityMarkup(item) {
  const kind = safeKind(item.kind);
  return `
    <div class="console-line ${kind}" data-event-id="${Number(item.id) || 0}">
      <time>${escapeHtml(timeLabel(item.time))}</time>
      <span class="line-kind">${escapeHtml(kind)}</span>
      <p>${escapeHtml(item.message)}</p>
    </div>`;
}

function appendActivity(item) {
  const id = Number(item.id) || 0;
  if (id <= ui.lastActivityId) return;
  ui.lastActivityId = id;
  const target = $('#activity');
  $('.empty-state', target)?.remove();
  target.insertAdjacentHTML('afterbegin', activityMarkup(item));
  while (target.children.length > 100) target.lastElementChild.remove();
}

function renderAudit(items) {
  $('#audit').innerHTML = items.map((item) => `
    <article class="audit-event ${item.allowed ? 'allowed' : 'refused'}">
      <span class="verdict-mark">${item.allowed ? 'OK' : 'NO'}</span>
      <div class="audit-copy">
        <strong>${escapeHtml(item.action)}</strong>
        <span>${escapeHtml(item.requester_name)} <small>${escapeHtml(item.requester_id)}</small></span>
        <p>${escapeHtml(item.outcome)}</p>
        <em>${escapeHtml(item.guild_name || 'Direct message')}</em>
      </div>
      <time>${escapeHtml(timeLabel(item.timestamp, false))}</time>
    </article>`).join('') || '<div class="empty-state">No moderation decisions recorded yet.</div>';
}

function maintenanceMarkup(item) {
  const kind = safeKind(item.kind);
  return `
    <div class="terminal-line ${kind}" data-event-id="${Number(item.id) || 0}">
      <time>${escapeHtml(timeLabel(item.time))}</time>
      <span>${escapeHtml(item.message)}</span>
    </div>`;
}

function renderMaintenanceOutput(items) {
  const target = $('#maintOutput');
  ui.lastMaintenanceId = Math.max(0, ...items.map((item) => Number(item.id) || 0));
  target.innerHTML = items.map(maintenanceMarkup).join('') || (
    '<p class="empty-state">Select an operation to begin.</p>'
  );
  target.scrollTop = target.scrollHeight;
}

function appendMaintenance(item) {
  const id = Number(item.id) || 0;
  if (id <= ui.lastMaintenanceId) return;
  ui.lastMaintenanceId = id;
  const target = $('#maintOutput');
  $('.empty-state', target)?.remove();
  target.insertAdjacentHTML('beforeend', maintenanceMarkup(item));
  while (target.children.length > 300) target.firstElementChild.remove();
  target.scrollTop = target.scrollHeight;
}

function renderRepository(repository) {
  $('#repoBranch').textContent = repository.branch || '—';
  $('#repoCommit').textContent = repository.commit || '—';
  $('#repoRemote').textContent = repository.remote || '—';
  $('#repoState').textContent = repository.state || 'Not checked';
  $('#repoBadge').textContent = `${repository.branch || 'Repository'} · ${repository.state || 'Not checked'}`;
  $('#repoBadge').classList.toggle('attention', Number(repository.behind) > 0);
}

function renderMaintenanceState(maintenance) {
  const busy = Boolean(maintenance.busy);
  const state = $('#maintState');
  state.textContent = busy ? `${maintenance.action || 'operation'} running` : (
    maintenance.ok === true ? 'COMPLETE' : maintenance.ok === false ? 'FAILED' : 'IDLE'
  );
  state.className = `operation-state ${busy ? 'working' : maintenance.ok === false ? 'failed' : maintenance.ok === true ? 'complete' : ''}`;
  $('#maintenanceTitle').textContent = busy
    ? `${maintenance.action || 'Maintenance'} in progress`
    : 'Maintenance log';
  $$('[data-maint]').forEach((button) => { button.disabled = busy; });
}

function renderSecrets(secrets) {
  const values = [
    ['#discordSecretState', Boolean(secrets.discord_token)],
    ['#apiSecretState', Boolean(secrets.api_key)],
  ];
  values.forEach(([selector, saved]) => {
    const element = $(selector);
    element.textContent = saved ? 'Saved' : 'Missing';
    element.className = `secret-state ${saved ? 'saved' : 'missing'}`;
  });
}

function hydrateForm(config, secrets, revision) {
  const form = $('#configForm');
  Object.entries(config).forEach(([name, value]) => {
    const input = form.elements.namedItem(name);
    if (!input) return;
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = value ?? '';
  });
  form.elements.namedItem('discord_token').value = '';
  form.elements.namedItem('api_key').value = '';
  renderSecrets(secrets);
  selectMatchingProvider(config.api_base_url);
  ui.savedForm = { ...config };
  ui.configRevision = revision;
  ui.formHydrated = true;
  ui.formDirty = false;
  $('#externalConfig').classList.add('hidden');
  setSaveState(false);
}

function selectMatchingProvider(url) {
  const select = $('#providerPreset');
  const match = [...select.options].find((option) => option.value === url);
  select.value = match ? match.value : 'custom';
}

function setSaveState(dirty) {
  $('#saveState').textContent = dirty ? 'Unsaved changes' : 'Saved values loaded';
  $('#saveState').classList.toggle('dirty', dirty);
  $$('[data-save]').forEach((button) => { button.classList.toggle('has-changes', dirty); });
}

function reconcileForm(data) {
  renderSecrets(data.secrets);
  if (!ui.formHydrated) {
    hydrateForm(data.config, data.secrets, data.config_revision);
    return;
  }
  if (data.config_revision !== ui.configRevision) {
    if (ui.formDirty) {
      $('#externalConfig').classList.remove('hidden');
    } else {
      hydrateForm(data.config, data.secrets, data.config_revision);
    }
  }
}

function renderStatus(data) {
  ui.status = data;
  const stateName = data.state || 'stopped';
  const live = data.connected && stateName === 'running';
  const transitional = ['starting', 'stopping', 'restarting'].includes(stateName);
  const stateClass = live ? 'online' : transitional ? 'pending' : stateName === 'error' ? 'failed' : '';

  $('#sideState').textContent = stateName;
  $('#sideDot').className = `status-light ${stateClass}`;
  $('#heroDot').className = `status-light large ${stateClass}`;
  $('#heroState').textContent = STATE_LABELS[stateName] || `Bot ${stateName}`;
  $('#identity').textContent = live
    ? `Connected to Discord as ${data.user}.`
    : transitional
      ? 'A lifecycle operation is in progress.'
      : 'The control hub is online and waiting.';

  $('#account').textContent = data.user === '—' ? 'Offline' : data.user;
  $('#connectionText').textContent = data.connected ? 'Discord gateway connected' : 'Not connected';
  $('#guildCount').textContent = data.guilds.length;
  $('#guildNames').textContent = data.guilds.join(', ') || 'No servers connected';
  $('#model').textContent = data.config.model || 'Not configured';
  $('#endpoint').textContent = endpointLabel(data.config.api_base_url);
  $('#rate').textContent = `${data.config.rate_limit_max} / ${data.config.rate_limit_window}s`;
  $('#safety').textContent = data.config.enable_punitive ? 'Enabled' : 'Disabled';
  $('#updates').textContent = data.config.auto_update ? 'Enabled' : 'Disabled';
  $('#updatesDetail').textContent = data.config.auto_update
    ? `Every ${data.config.auto_update_interval} min${data.config.auto_restart ? ' · restart enabled' : ''}`
    : 'Manual maintenance only';

  const setupBanner = $('#setupBanner');
  setupBanner.classList.toggle('hidden', data.missing.length === 0);
  $('#setupText').textContent = data.missing.length
    ? `Missing ${data.missing.join(' and ')}. Saved secret values remain hidden by design.`
    : '';

  $('#runtimeError').classList.toggle('hidden', !data.error);
  $('#runtimeErrorText').textContent = data.error || '';

  const active = ['starting', 'running', 'stopping', 'restarting'].includes(stateName);
  $('[data-bot="start"]').disabled = active || data.missing.length > 0;
  $('[data-bot="stop"]').disabled = !active;
  $('[data-bot="restart"]').disabled = data.missing.length > 0 || transitional;

  renderActivity(data.activity || []);
  renderAudit(data.audit || []);
  renderRepository(data.repository || {});
  renderMaintenanceState(data.maintenance || {});
  renderMaintenanceOutput(data.maintenance?.output || []);
  reconcileForm(data);
}

async function refresh() {
  if (ui.refreshRunning) return;
  ui.refreshRunning = true;
  try {
    const data = await api('/api/status');
    renderStatus(data);
  } catch (error) {
    $('#identity').textContent = 'The browser API is unavailable. Retrying automatically.';
    setStreamState('failed', 'Disconnected');
  } finally {
    ui.refreshRunning = false;
  }
}

function scheduleRefresh(delay = 120) {
  clearTimeout(ui.refreshTimer);
  ui.refreshTimer = setTimeout(refresh, delay);
}

async function botAction(action) {
  const buttons = $$('[data-bot]');
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const result = await api(`/api/bot/${action}`, { method: 'POST' });
    toast(`Bot ${result.state}.`);
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    await refresh();
    if (ui.status) renderStatus(ui.status);
    else buttons.forEach((button) => { button.disabled = false; });
  }
}

function collectConfig(restart) {
  const form = $('#configForm');
  if (!form.reportValidity()) return null;
  const data = { restart };
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === 'checkbox') data[input.name] = input.checked;
    else if (input.type === 'number') data[input.name] = Number(input.value);
    else data[input.name] = input.value.trim();
  }
  return data;
}

async function saveConfig(restart) {
  if (!$('#externalConfig').classList.contains('hidden')) {
    toast('Reload the externally changed values before saving.', 'warning');
    return;
  }
  const data = collectConfig(restart);
  if (!data) return;

  const buttons = $$('[data-save]');
  buttons.forEach((button) => { button.disabled = true; });
  $('#saveState').textContent = 'Saving…';
  try {
    const result = await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    $('#configForm').elements.namedItem('discord_token').value = '';
    $('#configForm').elements.namedItem('api_key').value = '';
    ui.formDirty = false;
    ui.formHydrated = false;
    ui.configRevision = result.config_revision;
    renderSecrets(result.secrets);
    toast(result.warning || (result.restarted ? 'Configuration saved and bot restarted.' : 'Configuration saved to .env.'), result.warning ? 'warning' : 'success');
    $('#saveHint').textContent = result.warning || (
      result.restart_required
        ? 'Saved. Restart the running bot to apply every change.'
        : 'The shared .env file is up to date.'
    );
    await refresh();
  } catch (error) {
    $('#saveState').textContent = 'Save failed';
    toast(error.message, 'error');
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function maintain(action) {
  const state = $('#maintState');
  state.textContent = `${action} running`;
  state.className = 'operation-state working';
  $$('[data-maint]').forEach((button) => { button.disabled = true; });
  navigate('maintenance');
  try {
    const result = await api(`/api/maintenance/${action}`, { method: 'POST' });
    toast(result.ok ? 'Maintenance operation complete.' : 'Operation completed with errors.', result.ok ? 'success' : 'error');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    await refresh();
    if (ui.status) renderMaintenanceState(ui.status.maintenance || {});
    else $$('[data-maint]').forEach((button) => { button.disabled = false; });
  }
}

async function clearActivity() {
  try {
    await api('/api/activity/clear', { method: 'POST' });
    await refresh();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function connectEvents() {
  ui.eventSource?.close();
  const source = new EventSource('/api/events');
  ui.eventSource = source;

  source.addEventListener('open', () => setStreamState('online', 'Live'));
  source.addEventListener('error', () => setStreamState('pending', 'Reconnecting'));
  source.addEventListener('activity', (event) => {
    try { appendActivity(JSON.parse(event.data)); } catch (_) { scheduleRefresh(); }
  });
  source.addEventListener('maintenance', (event) => {
    try { appendMaintenance(JSON.parse(event.data)); } catch (_) { scheduleRefresh(); }
  });
  ['status', 'audit', 'config', 'repository', 'maintenance_state'].forEach((name) => {
    source.addEventListener(name, () => scheduleRefresh(name === 'audit' ? 220 : 100));
  });
}

function markFormDirty() {
  if (!ui.formHydrated) return;
  ui.formDirty = true;
  setSaveState(true);
}

function reloadSavedForm() {
  ui.formDirty = false;
  ui.formHydrated = false;
  $('#externalConfig').classList.add('hidden');
  refresh();
}

function resetForm() {
  if (!ui.status) return;
  hydrateForm(ui.status.config, ui.status.secrets, ui.status.config_revision);
  toast('Unsaved form changes discarded.', 'warning');
}

function tickClock() {
  $('#clock').textContent = new Intl.DateTimeFormat(undefined, {
    weekday: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date());
}

$$('.nav-item').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.page)));
$('.brand').addEventListener('click', (event) => { event.preventDefault(); navigate('dashboard'); });
$$('[data-go]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.go)));
$$('[data-bot]').forEach((button) => button.addEventListener('click', () => botAction(button.dataset.bot)));
$$('[data-save]').forEach((button) => button.addEventListener('click', () => saveConfig(button.dataset.save === 'true')));
$$('[data-maint]').forEach((button) => button.addEventListener('click', () => maintain(button.dataset.maint)));

$('#clearActivity').addEventListener('click', clearActivity);
$('#reloadConfig').addEventListener('click', reloadSavedForm);
$('#resetConfig').addEventListener('click', resetForm);
$('#configForm').addEventListener('input', markFormDirty);
$('#configForm').addEventListener('change', markFormDirty);
$('#providerPreset').addEventListener('change', (event) => {
  if (event.target.value !== 'custom') {
    $('#apiBaseUrl').value = event.target.value;
    markFormDirty();
  }
});
$('#apiBaseUrl').addEventListener('input', (event) => selectMatchingProvider(event.target.value));

document.addEventListener('keydown', (event) => {
  const editing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if (event.ctrlKey && event.key.toLowerCase() === 's') {
    event.preventDefault();
    saveConfig(false);
    return;
  }
  if (event.key === 'Escape' && editing) {
    document.activeElement.blur();
    return;
  }
  if (!editing && !event.ctrlKey && !event.metaKey && ['1', '2', '3'].includes(event.key)) {
    navigate(['dashboard', 'configure', 'maintenance'][Number(event.key) - 1]);
  }
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
window.addEventListener('beforeunload', () => ui.eventSource?.close());

navigate(PAGE_COPY[location.hash.slice(1)] ? location.hash.slice(1) : 'dashboard');
tickClock();
setInterval(tickClock, 1000);
refresh();
connectEvents();
setInterval(refresh, 15000);
