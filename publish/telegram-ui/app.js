/* TG Archiver front-end — served statically from GitHub Pages.
 *
 * The UI lives on the web, but all messages/media are read from the local
 * tg-archiver server on this computer.  Set TG_API_BASE (config.js or
 * localStorage tg_api_base) to that server; the default is port 8799.
 */
'use strict';

const TG_API_BASE = (window.TG_API_BASE
  || localStorage.getItem('tg_api_base')
  || 'http://127.0.0.1:8799').replace(/\/+$/, '');
const TG_LOCAL_ONLY = window.TG_LOCAL_ONLY !== false;

function apiUrl(path) {
  if (!path) return path;
  if (/^(https?:|data:|blob:|tg:)/i.test(path)) return path;
  if (path.charAt(0) === '/') return TG_API_BASE + path;
  return path;
}

function localServerHelp(message) {
  return `${message || 'Local archive server not reachable'}<br>
    Start it on this computer with <code>cd tg-archiver &amp;&amp; ./run.sh 8799</code>,
    then <a href="#" id="retryLocal">retry</a> or
    <a href="#" id="changeTgApi">change the local URL</a>.
    <span class="muted">Now using ${TG_API_BASE}</span>`;
}

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  status: null,
  dialogs: [],
  filter: '',
  chat: null,
  messages: [],       // ascending, oldest first
  oldestId: null,
  hasMore: false,
  loading: false,
  highlightId: null,
  exportTaskId: null,
  tasks: [],
  taskTimer: null,
  dockCollapsed: false,
  finishedSeen: new Set(),
  localMode: false,
  localArchives: [],
  localSlug: null,
};

/* ------------------------------------------------------------------ helpers */
async function api(path, opts = {}) {
  let res;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    res = await fetch(apiUrl(path), {
      headers: { 'Content-Type': 'application/json' },
      signal: ctrl.signal,
      ...opts,
    });
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw new Error('The browser is blocking local network access. '
        + 'Click Connect below (or the lock/tune icon in the address bar), '
        + 'choose Allow for local network access, then retry.');
    }
    throw new Error('Cannot reach the local archive server at ' + TG_API_BASE
      + '. Start tg-archiver with ./run.sh 8799 on this computer.');
  } finally {
    clearTimeout(timer);
  }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : data;
    throw new Error(typeof detail === 'string' ? detail : 'Request failed');
  }
  return data;
}

const esc = (s = '') =>
  String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

function fmtTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function fmtDay(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return 'Today';
  const y = new Date(Date.now() - 86400000);
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { year: 'numeric', month: 'long', day: 'numeric' });
}
function fmtShort(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return fmtTime(iso);
  const y = new Date(Date.now() - 86400000);
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function fmtBytes(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${u[i]}`;
}
function fmtDur(sec) {
  if (!sec) return '';
  sec = Math.round(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
function initials(title = '') {
  const parts = String(title).trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '✈';
  if (parts.length === 1) return parts[0].slice(0, 2);
  return parts[0][0] + parts[1][0];
}
function colorFor(s) {
  let h = 0;
  for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return { g1: `hsl(${h} 42% 34%)`, g2: `hsl(${h} 45% 22%)` };
}
function avatarHtml(key, title, cls = '', imgUrl = null) {
  const c = colorFor(key);
  const localKey = String(key).startsWith('local:');
  const src = imgUrl ? apiUrl(imgUrl)
    : (localKey ? null : apiUrl(`/api/avatar/${encodeURIComponent(key)}`));
  return `<div class="avatar ${cls}" style="--g1:${c.g1};--g2:${c.g2}">
    <span>${esc(initials(title))}</span>
    ${src ? `<img src="${esc(src)}" loading="lazy" alt="" onerror="this.remove()">` : ''}
  </div>`;
}
function setHeaderAvatar(key, title, imgUrl = null) {
  const av = $('#headerAvatar');
  const c = colorFor(key);
  av.style.setProperty('--g1', c.g1);
  av.style.setProperty('--g2', c.g2);
  const localKey = String(key).startsWith('local:');
  const src = imgUrl ? apiUrl(imgUrl)
    : (localKey ? null : apiUrl(`/api/avatar/${encodeURIComponent(key)}`));
  av.innerHTML = `<span>${esc(initials(title))}</span>
    ${src ? `<img src="${esc(src)}" loading="lazy" alt="" onerror="this.remove()">` : ''}`;
}
function archiveTitle(a) {
  return String(a.slug || '').startsWith('Saved Messages') ? 'Saved Messages' : a.title;
}
function renderText(t) {
  let h = esc(t);
  h = h.replace(/(https?:\/\/[^\s<]+)/g, (u) =>
    `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  h = h.replace(/(^|[\s(])(@[A-Za-z0-9_]{3,})/g,
    (m, p1, p2) => `${p1}<a href="https://t.me/${p2.slice(1)}" target="_blank" rel="noopener">${p2}</a>`);
  return h.replace(/\n/g, '<br>');
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 4600);
}

function showLocalServerHelp() {
  hideSetup();
  state.localMode = false;
  const banner = $('#offlineBanner');
  banner.classList.remove('hidden');
  banner.innerHTML = '<span>🗄 <b>Local archive mode</b> — messages stay on this PC.</span>';
  $('#chatList').innerHTML = '<div class="side-loading">Waiting for the local server…</div>';
  $('#headerTitle').textContent = 'Telegram archive';
  $('#headerSub').textContent = 'Local files on your computer';
  $('#messages').innerHTML = `<div class="empty-chat">
    <div class="empty-plane">🗄</div>
    <h2>Local Telegram archives</h2>
    <p class="muted">The messages and media are on your own computer, so the
      browser must be allowed to reach the local archive server.</p>
    <p class="muted">If Chrome asks “Allow local network access?”, choose
      <b>Allow</b>. Otherwise click <b>Connect</b> below.</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:14px 0">
      <button id="connectLocal" class="primary">Connect to local archives</button>
      <a id="openLocalUi" class="button" target="_blank" rel="noopener"
         href="http://127.0.0.1:8799/?local=1">Open local UI ↗</a>
      <a id="changeTgApi" class="button" href="#">Change local URL</a>
    </div>
    <p class="muted" style="font-size:12px">Trying <code>${TG_API_BASE}</code>.
      Start the server with <code>cd tg-archiver &amp;&amp; ./run.sh 8799</code>.</p>
  </div>`;
  const connect = document.getElementById('connectLocal');
  if (connect) connect.onclick = async () => {
    connect.disabled = true;
    connect.textContent = 'Connecting…';
    await boot();
    const btn = document.getElementById('connectLocal');
    if (btn) { btn.disabled = false; btn.textContent = 'Connect to local archives'; }
  };
  const retry = document.getElementById('retryLocal');
  if (retry) retry.onclick = (e) => { e.preventDefault(); boot(); };
  const change = document.getElementById('changeTgApi');
  if (change) change.onclick = (e) => {
    e.preventDefault();
    const next = prompt('Local tg-archiver URL', TG_API_BASE);
    if (next) { localStorage.setItem('tg_api_base', next.trim()); location.reload(); }
  };
}

/* --------------------------------------------------------------------- boot */
async function boot() {
  clearLocalMode();
  state.status = null;
  const localParam = new URLSearchParams(location.search).get('local');
  const forceLocal = localParam === '1' || (TG_LOCAL_ONLY && localParam !== '0');
  if (forceLocal || navigator.onLine === false) {
    $('#chatList').innerHTML =
      '<div class="side-loading">Waiting for the local archive server…</div>';
    $('#headerSub').textContent = 'Allow local network access if Chrome asks';
    if (await loadLocalArchives()) return;
    if (TG_LOCAL_ONLY) {
      showLocalServerHelp();
      return;
    }
  }
  try {
    state.status = await api('/api/status');
  } catch {
    state.status = { configured: false, authorized: false, error: 'offline' };
  }
  if (!state.status.configured || !state.status.authorized) {
    // Telegram unreachable? fall back to downloaded archives
    if (await loadLocalArchives()) return;
    return showSetup(state.status.configured ? 'phone' : 'config');
  }
  hideSetup();
  showMe();
  await loadDialogs();
}

function showMe() {
  const me = state.status && state.status.me;
  if (me) {
    $('#meLine').textContent =
      `Signed in as ${me.name}${me.username ? ' (@' + me.username + ')' : ''}`;
  }
  startTaskPolling();
}

function showSetup(step) {
  $('#setup').classList.remove('hidden');
  setSetupError('');
  $('#setupInfo').classList.add('hidden');
  $('#configForm').classList.toggle('hidden', step !== 'config');
  $('#phoneForm').classList.toggle('hidden', step !== 'phone');
  $('#codeForm').classList.toggle('hidden', step !== 'code');
  if (step === 'phone') setTimeout(() => $('#phone').focus(), 50);
  if (step === 'code') setTimeout(() => $('#code').focus(), 50);
}
function hideSetup() { $('#setup').classList.add('hidden'); }
function setSetupError(msg) {
  const el = $('#setupError');
  el.textContent = msg || '';
  el.classList.toggle('hidden', !msg);
}

/* --------------------------------------------------- offline / local archives */
function clearLocalMode() {
  state.localMode = false;
  state.localSlug = null;
  state.localArchives = [];
  $('#offlineBanner').classList.add('hidden');
  $('#savedExportBtn').classList.remove('hidden');
  $('#logoutBtn').classList.remove('hidden');
  $('#viewerBtn').classList.add('hidden');
}

async function loadLocalArchives() {
  try {
    const r = await api('/api/local/archives');
    state.localArchives = r.archives || [];
  } catch {
    return false;
  }
  if (!state.localArchives.length) return false;
  enterLocalMode();
  return true;
}

async function refreshLocalArchives() {
  try {
    const r = await api('/api/local/archives');
    state.localArchives = r.archives || [];
  } catch { return; }
  renderLocalArchives();
}

function enterLocalMode() {
  hideSetup();
  state.localMode = true;
  state.chat = null;
  state.messages = [];
  state.oldestId = null;
  state.hasMore = false;
  document.body.classList.remove('show-chat');

  const banner = $('#offlineBanner');
  banner.classList.remove('hidden');
  banner.innerHTML =
    '<span>🗄 <b>Local archives</b> — these messages and media are on this PC. No Telegram connection needed.</span>' +
    (TG_LOCAL_ONLY ? '' :
      '<button id="goOnlineBtn" class="ghost sm">Go online / Sign in</button>');
  const goOnline = $('#goOnlineBtn');
  if (goOnline) goOnline.onclick = () => { clearLocalMode(); boot(); };

  $('#savedExportBtn').classList.add('hidden');
  $('#logoutBtn').classList.add('hidden');
  $('#exportBtn').classList.add('hidden');
  $('#pfpBtn').classList.add('hidden');
  $('#viewerBtn').classList.add('hidden');
  $('#meLine').textContent = 'Local archive mode';
  $('#headerTitle').textContent = 'Local archives';
  $('#headerSub').textContent =
    `${state.localArchives.length} downloaded archive${state.localArchives.length === 1 ? '' : 's'}`;

  const av = $('#headerAvatar');
  av.style.setProperty('--g1', '#2b5278');
  av.style.setProperty('--g2', '#1c3a57');
  av.innerHTML = '<span>🗄</span>';

  $('#messages').innerHTML = `<div class="empty-chat">
    <div class="empty-plane">🗄</div>
    <h2>Offline archives</h2>
    <p class="muted">Pick an archive on the left. Media, text and forward links are served from disk.<br>
    Completed archives also have a standalone HTML viewer.</p>
  </div>`;
  renderLocalArchives();
}

function renderLocalArchives() {
  const box = $('#chatList');
  const q = state.filter.trim().toLowerCase();
  const list = state.localArchives.filter(
    (a) => !q || (a.title || '').toLowerCase().includes(q)
  );
  if (!list.length) {
    box.innerHTML = '<div class="side-loading">No local archives found</div>';
    return;
  }
  box.innerHTML = list.map((a) => {
    const active = state.localSlug === a.slug ? ' active' : '';
    const partial = a.status === 'partial' ? ' · partial' : '';
    const title = archiveTitle(a);
    return `<div class="chat-item${active}" data-local-slug="${esc(a.slug)}">
      ${avatarHtml('local:' + a.slug, title, '', apiUrl(a.avatar_url))}
      <div class="chat-body">
        <div class="chat-row">
          <span class="chat-title">${esc(title)}</span>
          <span class="chat-time">${a.count} msgs</span>
        </div>
        <div class="chat-sub"><span>🗄 local · ${a.media_count} files${partial}</span></div>
      </div>
    </div>`;
  }).join('');
}

async function openLocalArchive(a) {
  state.localMode = true;
  state.localSlug = a.slug;
  state.chat = {
    key: 'local:' + a.slug, title: a.title, username: a.username,
    type: a.type || 'channel', local: true, slug: a.slug,
  };
  state.messages = [];
  state.oldestId = null;
  state.hasMore = false;
  state.loading = false;
  state.highlightId = null;
  document.body.classList.add('show-chat');

  $('#headerTitle').textContent = archiveTitle(a);
  $('#headerSub').textContent =
    `Offline archive · ${a.count} messages${a.status === 'partial' ? ' · partial' : ''}`;
  setHeaderAvatar('local:' + a.slug, archiveTitle(a), apiUrl(a.avatar_url));
  $('#exportBtn').classList.add('hidden');
  $('#pfpBtn').classList.add('hidden');
  if (a.viewer_url) {
    $('#viewerBtn').href = apiUrl(a.viewer_url);
    $('#viewerBtn').classList.remove('hidden');
  } else {
    $('#viewerBtn').classList.add('hidden');
  }
  $('#messages').innerHTML = '<div class="side-loading">Loading local messages…</div>';
  renderLocalArchives();

  try {
    const r = await api(
      `/api/local/messages?slug=${encodeURIComponent(a.slug)}&limit=50`
    );
    state.messages = (r.messages || []).slice().reverse();
    state.hasMore = !!r.has_more;
    state.oldestId = state.messages.length ? state.messages[0].id : null;
    renderMessages(true);
  } catch (err) {
    $('#messages').innerHTML = `<div class="side-loading">${esc(err.message)}</div>`;
  } finally {
    updateLoadOlder();
  }
}

/* --------------------------------------------------------------------- auth */
$('#configForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  setSetupError('');
  try {
    const r = await api('/api/config', {
      method: 'POST',
      body: JSON.stringify({
        api_id: Number($('#apiId').value),
        api_hash: $('#apiHash').value.trim(),
      }),
    });
    state.status = r.status;
    if (state.status.authorized) {
      hideSetup();
      showMe();
      await loadDialogs();
    } else {
      showSetup('phone');
    }
  } catch (err) { setSetupError(err.message); }
});

$('#phoneForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  setSetupError('');
  try {
    await api('/api/auth/send_code', {
      method: 'POST',
      body: JSON.stringify({ phone: $('#phone').value.trim() }),
    });
    showSetup('code');
    const info = $('#setupInfo');
    info.textContent = 'Code sent. Check Telegram (or SMS) and enter it below.';
    info.classList.remove('hidden');
  } catch (err) { setSetupError(err.message); }
});

$('#codeForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  setSetupError('');
  try {
    const r = await api('/api/auth/sign_in', {
      method: 'POST',
      body: JSON.stringify({
        code: $('#code').value.trim(),
        password: $('#password').value || null,
      }),
    });
    if (r.needs_password) {
      $('#passwordRow').classList.remove('hidden');
      $('#password').focus();
      setSetupError('Two-step verification is enabled — enter your cloud password.');
      return;
    }
    hideSetup();
    state.status = { ...(state.status || {}), configured: true, authorized: true, me: r.me };
    showMe();
    await loadDialogs();
  } catch (err) { setSetupError(err.message); }
});

$('#resendBtn').addEventListener('click', async () => {
  setSetupError('');
  try {
    await api('/api/auth/send_code', {
      method: 'POST',
      body: JSON.stringify({ phone: $('#phone').value.trim() }),
    });
    toast('A new login code was sent.');
  } catch (err) { setSetupError(err.message); }
});

$('#backToConfig').addEventListener('click', () => showSetup('config'));

$('#logoutBtn').addEventListener('click', async () => {
  if (!confirm('Log out of this Telegram session? The local .session file will be removed by Telegram.')) return;
  try {
    await api('/api/auth/logout', { method: 'POST' });
    state.dialogs = [];
    state.chat = null;
    $('#chatList').innerHTML = '';
    showSetup('phone');
  } catch (err) { toast(err.message, true); }
});

/* ------------------------------------------------------------------ dialogs */
async function loadDialogs() {
  $('#chatList').innerHTML = '<div class="side-loading">Loading chats…</div>';
  try {
    const r = await api('/api/dialogs');
    state.dialogs = r.dialogs || [];
    renderDialogs();
  } catch (err) {
    $('#chatList').innerHTML = `<div class="side-loading">${esc(err.message)}</div>`;
  }
}

function dialogSub(d) {
  if (d.saved) return 'Your personal cloud archive';
  let prefix = d.type === 'channel' ? '📢 ' : d.type === 'group' ? '👥 ' : '';
  if (d.username) prefix += '@' + d.username + ' · ';
  return prefix + (d.last_message || (d.type === 'channel' ? 'Channel' : 'Chat'));
}

function renderDialogs() {
  const box = $('#chatList');
  const q = state.filter.trim().toLowerCase();
  const list = state.dialogs.filter((d) =>
    !q || d.title.toLowerCase().includes(q) || (d.username || '').toLowerCase().includes(q));
  if (!list.length) {
    box.innerHTML = '<div class="side-loading">No chats found</div>';
    return;
  }
  box.innerHTML = list.map((d) => {
    const active = state.chat && state.chat.key === d.key ? ' active' : '';
    const badge = d.unread
      ? `<span class="badge">${d.unread > 99 ? '99+' : d.unread}</span>`
      : d.pinned ? '<span class="badge pin">📌</span>' : '';
    return `<div class="chat-item${active}" data-key="${esc(d.key)}">
      ${avatarHtml(d.key, d.title)}
      <div class="chat-body">
        <div class="chat-row">
          <span class="chat-title">${esc(d.title)}</span>
          <span class="chat-time">${fmtShort(d.last_date)}</span>
        </div>
        <div class="chat-sub"><span>${esc(dialogSub(d))}</span>${badge}</div>
      </div>
    </div>`;
  }).join('');
}

$('#chatList').addEventListener('click', (e) => {
  const item = e.target.closest('.chat-item');
  if (!item) return;
  if (item.dataset.localSlug) {
    const a = state.localArchives.find((x) => x.slug === item.dataset.localSlug);
    if (a) openLocalArchive(a);
    return;
  }
  const d = state.dialogs.find((x) => x.key === item.dataset.key);
  if (d) openChat(d);
});

$('#search').addEventListener('input', (e) => {
  state.filter = e.target.value;
  if (state.localMode) renderLocalArchives();
  else renderDialogs();
});

$('#refreshBtn').addEventListener('click', async () => {
  if (state.localMode) {
    await refreshLocalArchives();
    toast('Local archives refreshed.');
    return;
  }
  try {
    const r = await api('/api/dialogs?refresh=true');
    state.dialogs = r.dialogs || [];
    renderDialogs();
    toast('Chats refreshed.');
  } catch (err) { toast(err.message, true); }
});

$('#localBtn').addEventListener('click', async () => {
  const ok = await loadLocalArchives();
  if (!ok) toast('No local archives yet. Export a chat while online first.', true);
});

$('#backBtn').addEventListener('click', () => document.body.classList.remove('show-chat'));

/* ----------------------------------------------------------------- messages */
async function openChat(d, focusId = null) {
  state.chat = d;
  state.messages = [];
  state.oldestId = null;
  state.hasMore = false;
  state.loading = false;
  state.highlightId = focusId;
  document.body.classList.add('show-chat');

  $('#headerTitle').textContent = d.title || 'Chat';
  $('#headerSub').textContent = d.username
    ? '@' + d.username
    : d.type === 'channel' ? 'Channel' : d.type === 'group' ? 'Group' : 'Private chat';
  api('/api/chat/info?chat=' + encodeURIComponent(d.key)).then((info) => {
    if (!state.chat || state.chat.key !== d.key) return;
    const bits = [];
    if (d.username) bits.push('@' + d.username);
    if (info.total != null) bits.push(`${info.total} messages`);
    if (info.participants) bits.push(`${info.participants} members`);
    if (bits.length) $('#headerSub').textContent = bits.join(' · ');
  }).catch(() => {});
  const av = $('#headerAvatar');
  const c = colorFor(d.key);
  av.style.setProperty('--g1', c.g1);
  av.style.setProperty('--g2', c.g2);
  av.innerHTML = `<span>${esc(initials(d.title))}</span>
    <img src="${apiUrl(`/api/avatar/${encodeURIComponent(d.key)}`)}" loading="lazy" alt="" onerror="this.remove()">`;
  $('#viewerBtn').classList.add('hidden');
  $('#exportBtn').classList.remove('hidden');
  $('#pfpBtn').classList.remove('hidden');
  $('#messages').innerHTML = '<div class="side-loading">Loading messages…</div>';
  renderDialogs();

  try {
    const q = new URLSearchParams({ chat: d.key, limit: '50' });
    if (focusId) q.set('offset_id', String(focusId + 26));
    const r = await api('/api/messages?' + q.toString());
    state.messages = (r.messages || []).slice().reverse();
    state.hasMore = !!r.has_more;
    state.oldestId = state.messages.length ? state.messages[0].id : null;
    renderMessages(!focusId);
  } catch (err) {
    $('#messages').innerHTML = `<div class="side-loading">${esc(err.message)}</div>`;
  } finally {
    updateLoadOlder();
  }
}

function msgHtml(m, key, prev) {
  const out = m.out;
  const isChannel = state.chat && state.chat.type === 'channel';
  let h = `<div class="msg-row${out ? ' out' : ''}" id="msg-${m.id}" data-id="${m.id}">`;

  if (!out && !isChannel && m.sender_name &&
      (!prev || prev.sender_name !== m.sender_name)) {
    h += `<div class="sender">${esc(m.sender_name)}</div>`;
  }

  const media = m.media;
  h += `<div class="bubble${media ? ' has-media' : ''}">`;

  if (m.forward) {
    const f = m.forward;
    const name = f.from_name || 'unknown chat';
    const label = f.author ? `Forwarded from ${esc(f.author)}` : 'Forwarded from';
    const inner = f.url
      ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(name)}</a>`
      : esc(name);
    h += `<div class="fwd"><span class="fwd-label">${label}</span> ${inner}</div>`;
  }

  const isLocal = !!m.local_url;
  const base = apiUrl(m.local_url || `/api/media/${encodeURIComponent(key)}/${m.id}`);
  if (media) {
    if (media.type === 'photo' || media.type === 'sticker') {
      const src = isLocal ? base : base + '?thumb=1';
      h += `<img class="msg-media" loading="lazy" src="${src}" data-full="${base}" data-kind="image" alt="">`;
    } else if (media.type === 'video' || media.type === 'gif') {
      const posterSrc = isLocal
        ? apiUrl(m.thumb_url || '')
        : (media.has_thumb ? base + '?thumb=1' : '');
      const poster = posterSrc ? `<img src="${posterSrc}" loading="lazy" alt="" onerror="this.remove()">` : '';
      const dur = media.duration ? `<span class="dur">${fmtDur(media.duration)}</span>` : '';
      h += `<div class="msg-video" data-full="${base}" data-kind="video">${poster}<div class="play">▶</div>${dur}</div>`;
    } else if (media.type === 'voice' || media.type === 'audio') {
      h += `<audio class="msg-audio" controls preload="none" src="${base}"></audio>`;
    } else if (media.type === 'document') {
      h += `<a class="msg-file" href="${base}" target="_blank" rel="noopener">⤓
        <span>${esc(media.name || 'document')}</span>
        ${media.size ? `<span class="muted">(${fmtBytes(media.size)})</span>` : ''}</a>`;
    }
  }

  if (m.text) h += `<div class="text">${renderText(m.text)}</div>`;

  h += `<div class="meta"><span>${fmtTime(m.date)}</span>`;
  if (m.views) h += `<span title="views">👁 ${m.views}</span>`;
  if (m.link) h += `<a class="permalink" href="${esc(m.link)}" target="_blank" rel="noopener" title="Open in Telegram">🔗</a>`;
  h += `</div></div></div>`;
  return h;
}

function renderMessages(scrollBottom = false) {
  const box = $('#messages');
  if (!state.messages.length) {
    box.innerHTML = '<div class="side-loading">No messages here.</div>';
    return;
  }
  let html = '';
  let prev = null;
  let lastDay = '';
  for (const m of state.messages) {
    const day = m.date ? new Date(m.date).toDateString() : '';
    if (day !== lastDay) {
      html += `<div class="day-sep"><span>${esc(fmtDay(m.date))}</span></div>`;
      lastDay = day;
    }
    html += msgHtml(m, state.chat.key, prev);
    prev = m;
  }
  box.innerHTML = html;

  if (state.highlightId) {
    const el = document.getElementById('msg-' + state.highlightId);
    if (el) {
      el.classList.add('highlight');
      el.scrollIntoView({ block: 'center' });
    }
    state.highlightId = null;
  } else if (scrollBottom) {
    box.scrollTop = box.scrollHeight;
  }
  $('#composerHint').textContent =
    `${state.messages.length} loaded` + (state.hasMore ? ' · scroll up for older' : ' · start of chat');
  updateLoadOlder();
}

function updateLoadOlder() {
  $('#loadOlder').classList.toggle('hidden', !state.hasMore || !state.chat);
}

async function loadOlder() {
  if (!state.chat || state.loading || !state.hasMore || !state.oldestId) return;
  state.loading = true;
  const box = $('#messages');
  const prevHeight = box.scrollHeight;
  const prevTop = box.scrollTop;
  try {
    let r;
    if (state.localMode && state.chat && state.chat.local) {
      const q = new URLSearchParams({
        slug: state.chat.slug, limit: '50', offset_id: String(state.oldestId),
      });
      r = await api('/api/local/messages?' + q.toString());
    } else {
      const q = new URLSearchParams({
        chat: state.chat.key, limit: '50', offset_id: String(state.oldestId),
      });
      r = await api('/api/messages?' + q.toString());
    }
    const older = (r.messages || []).slice().reverse();
    const seen = new Set(state.messages.map((m) => m.id));
    const fresh = older.filter((m) => !seen.has(m.id));
    state.messages = fresh.concat(state.messages);
    state.oldestId = state.messages.length ? state.messages[0].id : state.oldestId;
    state.hasMore = !!r.has_more && fresh.length > 0;
    renderMessages(false);
    box.scrollTop = prevTop + (box.scrollHeight - prevHeight);
  } catch (err) {
    toast(err.message, true);
  } finally {
    state.loading = false;
    updateLoadOlder();
  }
}

$('#loadOlder').addEventListener('click', loadOlder);
$('#messages').addEventListener('scroll', () => {
  if ($('#messages').scrollTop < 90) loadOlder();
});

$('#messages').addEventListener('click', (e) => {
  const media = e.target.closest('[data-full]');
  if (media) {
    openLightbox(media.dataset.full, media.dataset.kind);
  }
});

/* ---------------------------------------------------------------- lightbox */
function openLightbox(url, kind) {
  $('#lbBody').innerHTML = kind === 'video'
    ? `<video controls autoplay playsinline src="${esc(url)}"></video>`
    : `<img src="${esc(url)}" alt="">`;
  $('#lightbox').classList.remove('hidden');
}
function closeLightbox() {
  $('#lightbox').classList.add('hidden');
  $('#lbBody').innerHTML = '';
}
$('#lightbox').addEventListener('click', (e) => {
  if (e.target.id === 'lightbox' || e.target.closest('.lb-close')) closeLightbox();
});

/* ---------------------------------------------------------------- exporting */
$('#exportBtn').addEventListener('click', openExportModal);
$('#savedExportBtn').addEventListener('click', async () => {
  const saved = state.dialogs.find((d) => d.saved) ||
    state.dialogs.find((d) => /^saved messages$/i.test(d.title));
  if (!saved) return toast('Saved Messages was not found in your dialogs.', true);
  await openChat(saved);
  openExportModal();
});

function openExportModal() {
  if (!state.chat) return toast('Open a chat first.', true);
  state.exportTaskId = null;
  $('#exportTitle').textContent = `Export “${state.chat.title}”`;
  $('#exportNote').textContent =
    'Every selected message, its media and its forward link are written to data/exports/, ' +
    'together with a standalone Telegram-style HTML viewer.';
  $('#exportSetup').classList.remove('hidden');
  $('#exportProgress').classList.add('hidden');
  $('#exportError').classList.add('hidden');
  $('#openExport').classList.add('hidden');
  $('#zipExport').classList.add('hidden');
  $('#cancelExport').classList.add('hidden');
  showModal('exportModal');
  api('/api/chat/info?chat=' + encodeURIComponent(state.chat.key)).then((info) => {
    if (!state.chat || info.total == null) return;
    $('#exportNote').textContent =
      `This chat has ${info.total} messages. Media, text and forward links are written to ` +
      `data/exports/, together with a standalone Telegram-style HTML viewer.`;
  }).catch(() => {});
}

$('#startExport').addEventListener('click', async () => {
  if (!state.chat) return;
  const btn = $('#startExport');
  btn.disabled = true;
  $('#exportError').classList.add('hidden');
  try {
    const task = await api('/api/export', {
      method: 'POST',
      body: JSON.stringify({
        chat: state.chat.key,
        limit: Number($('#exportLimit').value),
        media: $('#exportMedia').checked,
        title: state.chat.title,
      }),
    });
    beginExportPolling(task.id);
  } catch (err) {
    $('#exportError').textContent = err.message;
    $('#exportError').classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
});

function beginExportPolling(id) {
  state.exportTaskId = id;
  $('#exportSetup').classList.add('hidden');
  $('#exportProgress').classList.remove('hidden');
  $('#cancelExport').classList.remove('hidden');
  $('#exportBar').style.width = '4%';
  $('#exportBar').style.opacity = '1';
  $('#exportStat').textContent = 'Starting…';
  $('#exportCurrent').textContent = '';
  $('#exportPath').textContent = '';
  startTaskPolling();
  pollTasks();
}

/* ---------------- global live polling: dock + export modal ---------------- */
function startTaskPolling() {
  if (state.taskTimer) return;
  state.taskTimer = setInterval(pollTasks, 1500);
  pollTasks();
}

async function pollTasks() {
  if (!state.status || !state.status.authorized) return;
  try {
    const r = await api('/api/exports');
    state.tasks = r.tasks || [];
  } catch { return; }
  renderDock();
  if (state.exportTaskId) {
    const t = state.tasks.find((x) => x.id === state.exportTaskId);
    if (t) applyExportTask(t);
  }
}

function applyExportTask(t) {
  renderExportProgress(t);
  const terminal = ['done', 'error', 'cancelled'].includes(t.status);
  $('#cancelExport').classList.toggle('hidden', terminal);
  if (terminal && t.status === 'done' && !state.finishedSeen.has(t.id)) {
    state.finishedSeen.add(t.id);
    $('#openExport').href = apiUrl(t.view_url);
    $('#openExport').classList.remove('hidden');
    $('#zipExport').href = apiUrl(t.zip_url);
    $('#zipExport').classList.remove('hidden');
    loadDialogs();
    toast(`Export finished: ${t.chat_title} (${t.done} messages)`);
  }
}

function renderDock() {
  const dock = $('#downloadDock');
  const tasks = state.tasks.filter((t) => !String(t.id).startsWith('disk:'));
  const active = tasks.filter((t) => t.status === 'running' || t.status === 'queued');
  const recent = tasks
    .filter((t) => ['done', 'error', 'cancelled'].includes(t.status))
    .slice(0, 3);
  const show = active.concat(recent);
  if (!show.length) {
    dock.classList.add('hidden');
    return;
  }
  dock.classList.remove('hidden');
  $('#dockTitle').textContent = active.length
    ? `Downloads (${active.length} active)`
    : 'Recent downloads';
  $('#dockSpinner').classList.toggle('idle', !active.length);
  $('#dockList').innerHTML = show.map(dockItemHtml).join('');
}

function dockItemHtml(t) {
  const pct = t.total ? Math.min(100, (t.done / t.total) * 100) : null;
  const active = t.status === 'running' || t.status === 'queued';
  const width = pct != null ? pct : active ? 22 : 100;
  const bits = [`${t.done}${t.total ? '/' + t.total : ''} msgs`];
  if (t.bytes_done_human) bits.push(t.bytes_done_human);
  if (t.file_progress) bits.push(`${Math.round(t.file_progress)}% file`);
  const current = t.error ? 'Error: ' + t.error : (t.current || '');
  const actions = [];
  if (active) actions.push(`<button class="ghost sm" data-cancel="${esc(t.id)}">Cancel</button>`);
  if (t.status === 'done' && t.view_url)
    actions.push(`<a class="button sm" href="${esc(apiUrl(t.view_url))}" target="_blank" rel="noopener">Open</a>`);
  if (t.status === 'done' && t.zip_url)
    actions.push(`<a class="button alt sm" href="${esc(apiUrl(t.zip_url))}">ZIP</a>`);
  return `<div class="dock-item">
    <div class="di-top">
      <span class="status-pill ${esc(t.status)}">${esc(t.status)}</span>
      <span class="di-title" title="${esc(t.output_dir || '')}">${esc(t.chat_title)}</span>
    </div>
    <div class="bar" style="${active ? '' : 'opacity:.7'}"><div style="width:${width}%"></div></div>
    <div class="di-sub">${esc(bits.join(' · '))}${current ? ' · <span class="di-current">' + esc(String(current).slice(0, 70)) + '</span>' : ''}</div>
    ${actions.length ? `<div class="di-actions">${actions.join('')}</div>` : ''}
  </div>`;
}

$('#dockHead').addEventListener('click', (e) => {
  if (e.target.closest('button[data-cancel]')) return;
  state.dockCollapsed = !state.dockCollapsed;
  $('#downloadDock').classList.toggle('collapsed', state.dockCollapsed);
  $('#dockToggle').textContent = state.dockCollapsed ? '▸' : '▾';
});
$('#dockList').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-cancel]');
  if (!btn) return;
  e.stopPropagation();
  try {
    await api(`/api/export/${btn.dataset.cancel}/cancel`, { method: 'POST' });
    toast('Export cancelled.');
    pollTasks();
  } catch (err) { toast(err.message, true); }
});

function renderExportProgress(t) {
  const pct = t.total ? Math.min(100, (t.done / t.total) * 100) : null;
  $('#exportBar').style.width = pct != null ? pct + '%' : '30%';
  $('#exportBar').style.opacity = pct != null ? '1' : '.55';

  let stat = `${t.status} · ${t.done}${t.total ? ' / ' + t.total : ''} messages · ${t.bytes_done_human}`;
  if (t.file_progress) stat += ` · current file ${Math.round(t.file_progress)}%`;
  $('#exportStat').textContent = stat;
  $('#exportCurrent').textContent = t.error ? 'Error: ' + t.error : (t.current || '');
  $('#exportPath').textContent = t.output_dir ? 'Saved to: ' + t.output_dir : '';

  if (t.status === 'error') {
    $('#exportError').textContent = t.error || 'Export failed';
    $('#exportError').classList.remove('hidden');
  }
  if (t.status === 'done') {
    $('#exportBar').style.width = '100%';
    $('#exportStat').textContent =
      `Done · ${t.done} messages · ${t.bytes_done_human}`;
  }
}

$('#cancelExport').addEventListener('click', async () => {
  if (!state.exportTaskId) return;
  try {
    await api(`/api/export/${state.exportTaskId}/cancel`, { method: 'POST' });
    toast('Export cancelled.');
    pollTasks();
  } catch (err) { toast(err.message, true); }
});

/* ------------------------------------------------------------ exports list */
$('#exportsBtn').addEventListener('click', openExports);
async function openExports() {
  showModal('exportsModal');
  const box = $('#exportsList');
  box.innerHTML = '<div class="side-loading">Loading…</div>';
  try {
    const r = await api('/api/exports');
    if (!r.tasks.length) {
      box.innerHTML = '<p class="muted">No exports yet. Open a chat and press “Export all”.</p>';
      return;
    }
    box.innerHTML = r.tasks.map((t) => {
      const when = t.started_at ? new Date(t.started_at).toLocaleString() : '';
      const open = t.status === 'done'
        ? `<a class="button alt" href="${esc(apiUrl(t.view_url))}" target="_blank" rel="noopener">Open</a>`
        : '';
      return `<div class="exports-item">
        <span class="status-pill ${t.status}">${esc(t.status)}</span>
        <div class="grow">
          <div class="title">${esc(t.chat_title)}</div>
          <div class="sub">${t.done}${t.total ? ' / ' + t.total : ''} msgs · ${esc(when)}</div>
        </div>${open}
      </div>`;
    }).join('');
  } catch (err) {
    box.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

/* -------------------------------------------------------------- link resolve */
$('#linkBtn').addEventListener('click', () => {
  $('#linkError').classList.add('hidden');
  showModal('linkModal');
  $('#linkInput').focus();
});
$('#resolveBtn').addEventListener('click', resolveLink);
$('#linkInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') resolveLink();
});
async function resolveLink() {
  const link = $('#linkInput').value.trim();
  if (!link) return;
  const err = $('#linkError');
  err.classList.add('hidden');
  try {
    const r = await api('/api/resolve?link=' + encodeURIComponent(link));
    hideModal('linkModal');
    await loadDialogs();
    const d = state.dialogs.find((x) => x.key === r.key) ||
      { key: r.key, id: r.id, title: r.title, username: r.username, type: 'channel' };
    await openChat(d, r.message_id || null);
    toast(`Opened “${r.title}”.`);
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove('hidden');
  }
}

/* --------------------------------------------------------------- profile pic */
$('#pfpBtn').addEventListener('click', async () => {
  if (!state.chat) return;
  try {
    const r = await api('/api/profile-photo', {
      method: 'POST',
      body: JSON.stringify({ chat: state.chat.key }),
    });
    toast(`Profile photo saved (${fmtBytes(r.size)}) → ${r.path}`);
    window.open(apiUrl(r.url), '_blank', 'noopener');
  } catch (err) { toast(err.message, true); }
});

/* -------------------------------------------------------------------- modals */
function showModal(id) { $('#' + id).classList.remove('hidden'); }
function hideModal(id) { $('#' + id).classList.add('hidden'); }
function closeAllModals() {
  ['exportModal', 'exportsModal', 'linkModal'].forEach(hideModal);
}
$$('[data-close]').forEach((b) => {
  b.addEventListener('click', () => hideModal(b.dataset.close));
});
$$('.modal').forEach((m) => {
  m.addEventListener('click', (e) => { if (e.target === m) hideModal(m.id); });
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeLightbox(); closeAllModals(); }
});

/* ---------------------------------------------------------------------- init */
boot();
