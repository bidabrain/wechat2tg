const $ = (s) => document.querySelector(s);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = '/login'; throw new Error('unauth'); }
  return r.json();
};
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString('zh-CN', {hour12: false}) : '—';
const stateBadge = (s) => {
  const ok = s === 'RUNNING';
  return `<span class="badge ${ok ? 'good' : 'bad'}">${s || '—'}</span>`;
};

// ---- 标签切换 ----
let vncLoaded = false;
document.querySelectorAll('#tabs button').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#tabs button').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    const tab = b.dataset.tab;
    document.querySelectorAll('.pane').forEach((p) => { p.hidden = p.dataset.pane !== tab; });
    if (tab === 'screen' && !vncLoaded) {
      $('#vnc').src = '/novnc/vnc.html?path=websockify&autoconnect=1&resize=scale&reconnect=1';
      vncLoaded = true;
    }
    if (tab === 'telegram') loadConfig();
    if (tab === 'messages') loadMessages();
    if (tab === 'logs') loadLog();
  });
});

$('#logout').addEventListener('click', async () => {
  await fetch('/api/logout', {method: 'POST'});
  location.href = '/login';
});

// ---- 概览状态 ----
async function refreshStatus() {
  let s;
  try { s = await api('/api/status'); } catch { return; }
  const yes = (b) => `<span class="badge ${b ? 'good' : 'bad'}">${b ? '是' : '否'}</span>`;
  $('#wx-running').innerHTML = yes(s.wechat.running);
  $('#wx-login').innerHTML = yes(s.wechat.logged_in);
  $('#wx-hint').textContent = s.wechat.logged_in ? '' :
    (s.wechat.running ? '微信在运行但未登录，去「微信画面」扫码。' : '微信进程未运行。');
  $('#svc-bridge').innerHTML = stateBadge(s.services.bridge);
  $('#svc-xvfb').innerHTML = stateBadge(s.services.xvfb);
  $('#svc-x11vnc').innerHTML = stateBadge(s.services.x11vnc);
  $('#svc-openbox').innerHTML = stateBadge(s.services.openbox);
  $('#tg-configured').innerHTML = yes(s.telegram.configured);
  $('#tg-forward').innerHTML = yes(s.telegram.forward_enabled);
  $('#tg-reply').innerHTML = yes(s.telegram.reply_enabled);
}

$('#btn-restart-wechat').addEventListener('click', async () => {
  await api('/api/service/wechat/restart', {method: 'POST'}); refreshStatus();
});
$('#btn-restart-bridge').addEventListener('click', async () => {
  await api('/api/service/bridge/restart', {method: 'POST'}); refreshStatus();
});

// ---- Telegram 配置 ----
async function loadConfig() {
  const c = await api('/api/config');
  $('#cfg-token').value = '';
  $('#cfg-token-mask').textContent = c.tg_bot_token_set ? `已保存：${c.tg_bot_token_mask}` : '未设置';
  $('#cfg-chat').value = c.tg_chat_id || '';
  $('#cfg-forward').checked = c.forward_enabled;
  $('#cfg-reply').checked = c.reply_enabled;
}
$('#btn-save-cfg').addEventListener('click', async () => {
  $('#cfg-msg').textContent = '保存中…';
  await api('/api/config', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      tg_bot_token: $('#cfg-token').value,
      tg_chat_id: $('#cfg-chat').value,
      forward_enabled: $('#cfg-forward').checked,
      reply_enabled: $('#cfg-reply').checked,
    }),
  });
  $('#cfg-msg').textContent = '已保存，桥接已重启。';
  loadConfig(); refreshStatus();
});

// ---- 最近消息 ----
async function loadMessages() {
  $('#msg-hint').textContent = '加载中…';
  const d = await api('/api/messages?limit=30');
  const tb = $('#msg-tbl tbody');
  tb.innerHTML = '';
  (d.messages || []).forEach((m) => {
    const tr = document.createElement('tr');
    const who = m.is_group ? m.sender : '';
    tr.innerHTML = `<td>${fmtTime(m.time)}</td><td>${esc(m.chat)}</td><td>${esc(who)}</td><td>${esc(m.text)}</td>`;
    tb.appendChild(tr);
  });
  $('#msg-hint').textContent = d.error ? ('读取失败：' + d.error)
    : (d.messages && d.messages.length ? '' : '暂无消息（微信是否已登录？）');
}
$('#btn-refresh-msg').addEventListener('click', loadMessages);
const esc = (s) => (s || '').replace(/[&<>]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));

// ---- 日志 ----
let logTimer = null;
async function loadLog() {
  const name = $('#log-name').value;
  const d = await api('/api/logs/' + name);
  $('#log-out').textContent = d.text || '(空)';
  $('#log-out').scrollTop = $('#log-out').scrollHeight;
}
$('#btn-refresh-log').addEventListener('click', loadLog);
$('#log-name').addEventListener('change', loadLog);
$('#log-auto').addEventListener('change', (e) => {
  clearInterval(logTimer);
  if (e.target.checked) logTimer = setInterval(loadLog, 3000);
});

// ---- 轮询 ----
refreshStatus();
setInterval(refreshStatus, 5000);
