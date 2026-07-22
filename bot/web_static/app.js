const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let formLoaded = false;
let toastTimer;

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));

function toast(message, isError = false) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.toggle('error', isError);
  element.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 3200);
}

function navigate(page) {
  $$('nav button, .page').forEach((element) => element.classList.remove('active'));
  $(`nav button[data-page="${page}"]`).classList.add('active');
  $(`#${page}`).classList.add('active');
  $('#crumb').textContent = page === 'dashboard' ? 'OVERVIEW' : page.toUpperCase();
  $('#title').textContent = {
    dashboard: 'Command the calm.', configure: 'Shape the guardrails.', maintenance: 'Keep the system healthy.'
  }[page];
  history.replaceState(null, '', `#${page}`);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function renderStatus(data) {
  const live = data.connected && data.state === 'running';
  const working = ['running', 'starting', 'restarting', 'stopping'].includes(data.state);
  $('#sideState').textContent = data.state;
  $('#sideDot').className = live ? 'online' : working ? 'pending' : '';
  $('#heroDot').className = live ? 'online' : working ? 'pending' : '';
  $('#heroState').textContent = `Bot ${live ? 'operational' : data.state}`;
  $('#identity').textContent = live ? `Connected as ${data.user}` : (data.error || 'Waiting for a Discord connection.');
  $('#guildCount').textContent = data.guilds.length;
  $('#guildNames').textContent = data.guilds.join(', ') || 'No guilds connected';
  $('#model').textContent = data.config.model;
  $('#endpoint').textContent = data.config.api_base_url.replace(/^https?:\/\//, '');
  $('#rate').textContent = `${data.config.rate_limit_max} / ${data.config.rate_limit_window}s`;
  $('#safety').textContent = data.config.enable_punitive ? 'CONFIRM ON' : 'PUNITIVE OFF';
  $('#setupBanner').classList.toggle('hidden', !data.missing.length);
  $('#setupBanner p').textContent = data.missing.length ? `Missing ${data.missing.join(' and ')}. Add them once to begin.` : '';

  $('#activity').innerHTML = data.activity.slice().reverse().map((item) => `
    <div class="console-line ${escapeHtml(item.kind)}"><time>${escapeHtml(item.time.slice(11, 19))}</time><span>${escapeHtml(item.message)}</span></div>`
  ).join('') || '<div class="empty">No runtime messages.</div>';
  $('#audit').innerHTML = data.audit.map((item) => `
    <div class="event"><i class="${item.allowed ? 'allowed' : 'refused'}">${item.allowed ? '✓' : '×'}</i><div><b>${escapeHtml(item.action)} <span>· ${escapeHtml(item.requester_name)}</span></b><p>${escapeHtml(item.outcome)}</p><small>${escapeHtml(item.guild_name)}</small></div><time>${escapeHtml(item.timestamp.slice(11, 16))}</time></div>`
  ).join('') || '<div class="empty">No moderation activity yet.</div>';

  const repo = data.repository;
  $('#repoBranch').textContent = repo.branch;
  $('#repoCommit').textContent = repo.commit;
  $('#repoState').textContent = repo.state;
  $('#repoBadge').textContent = `${repo.branch} · ${repo.state}`;
  $$('[data-maint]').forEach((button) => { button.disabled = data.maintenance_busy; });

  if (!formLoaded) {
    Object.entries(data.config).forEach(([name, value]) => {
      const input = document.querySelector(`[name="${name}"]`);
      if (input) input.type === 'checkbox' ? input.checked = value : input.value = value;
    });
    formLoaded = true;
  }
}

async function refresh() {
  try { renderStatus(await api('/api/status')); }
  catch (error) { $('#identity').textContent = 'Web API unavailable'; }
}

async function bot(action) {
  try {
    await api(`/api/bot/${action}`, { method: 'POST' });
    toast(`Bot ${action} requested.`);
    await refresh();
  } catch (error) { toast(error.message, true); }
}

async function save(restart) {
  const form = $('#configForm');
  if (!form.reportValidity()) return;
  const data = { restart };
  for (const input of form.elements) {
    if (!input.name) continue;
    data[input.name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value.trim();
  }
  try {
    const result = await api('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    form.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; });
    toast(restart ? 'Saved and restart requested.' : 'Configuration saved.');
    $('#saveHint').textContent = result.restart_required ? 'Saved. Restart the running bot to apply these changes.' : 'All changes saved locally.';
    await refresh();
  } catch (error) { toast(error.message, true); }
}

async function maintain(action) {
  $('#maintOutput').textContent = `Running ${action}…`;
  $('#maintState').textContent = 'WORKING';
  $$('[data-maint]').forEach((button) => { button.disabled = true; });
  try {
    const data = await api(`/api/maintenance/${action}`, { method: 'POST' });
    $('#maintOutput').textContent = `${(data.output || []).join('\n')}\n\n${JSON.stringify(data.result, null, 2)}`.trim();
    $('#maintState').textContent = data.ok ? 'COMPLETE' : 'FAILED';
    toast(data.ok ? 'Operation complete.' : 'Operation finished with errors.', !data.ok);
  } catch (error) {
    $('#maintOutput').textContent += `\n\nError: ${error.message}`;
    $('#maintState').textContent = 'FAILED';
    toast(error.message, true);
  } finally { await refresh(); }
}

$$('nav button').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.page)));
$$('[data-go]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.go)));
$$('[data-bot]').forEach((button) => button.addEventListener('click', () => bot(button.dataset.bot)));
$$('[data-save]').forEach((button) => button.addEventListener('click', () => save(button.dataset.save === 'true')));
$$('[data-maint]').forEach((button) => button.addEventListener('click', () => maintain(button.dataset.maint)));
$('#clearActivity').addEventListener('click', async () => { try { await api('/api/activity/clear', { method: 'POST' }); await refresh(); } catch (error) { toast(error.message, true); } });

setInterval(() => { $('#clock').textContent = `${new Date().toISOString().slice(11, 16)} UTC`; }, 1000);
navigate(['dashboard', 'configure', 'maintenance'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'dashboard');
refresh();
setInterval(refresh, 3000);
