'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let status = null;
let currentTab = 'overview';
let scanId = null;
let scanHasCode = false;

function toast(msg, ms = 3000) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), ms);
}

async function api(path, opts = {}) {
  const init = {method: opts.method || 'GET', headers: {}};
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { location.href = '/login'; throw new Error('401'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

async function act(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try {
    const res = await fn();
    if (okMsg) toast(okMsg);
    refresh();
    return res;
  } catch (e) {
    toast(e.message, 6000);
    return null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmtTs = (ts) => ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '—';
const fmtMs = (ms) => ms ? new Date(ms).toLocaleString('zh-CN') : '—';
const STATE_TEXT = {RUNNING: '运行中', STARTING: '启动中', STOPPED: '已停止', STOPPING: '停止中', BACKOFF: '反复重启', FATAL: '启动失败', EXITED: '已退出', UNKNOWN: '未知'};

function stepsHtml(steps) {
  return `<ul class="steps">${(steps || []).map((s) => `
    <li class="${s.ok ? 'done' : ''}"><span class="n">${s.ok ? '✓' : '✗'}</span>
      <div><div class="t">${esc(s.name)}</div><div class="d">${esc(s.detail)}</div></div></li>`).join('')}</ul>`;
}

function table(rows, cols) {
  if (!rows || !rows.length) return '<p class="muted">暂无数据</p>';
  return `<div style="overflow:auto"><table class="tbl"><thead><tr>${cols.map((c) => `<th>${esc(c[1])}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${c[2] ? c[2](r) : esc(r[c[0]])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

// ------------------------------------------------------------- 标签页
$$('#nav button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
function showTab(name) {
  currentTab = name;
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.id === `tab-${name}`));
  if (name === 'logs') loadLogs();
  if (name === 'device') fillDevice();
  syncVnc();
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
}
$('#logoutBtn').addEventListener('click', async () => { await api('/api/logout', {method: 'POST'}); location.href = '/login'; });

// ------------------------------------------------------------- VNC
const VNC_URL = '/novnc/vnc.html?autoconnect=1&resize=scale&reconnect=1&reconnect_delay=2000&show_dot=1&path=websockify';
function syncVnc(force) {
  const box = $('#vnc');
  const running = status && status.android.state === 'RUNNING';
  const want = running && currentTab === 'android';
  const has = box.querySelector('iframe');
  if (want && (!has || force)) box.innerHTML = `<iframe src="${VNC_URL}" allow="clipboard-read; clipboard-write"></iframe>`;
  else if (!want && (has || !box.innerHTML)) box.innerHTML = `<div class="vnc-empty">${running ? '' : '模拟器未运行'}</div>`;
}
$('[data-reconnect]').addEventListener('click', (e) => { e.preventDefault(); syncVnc(true); });

// ------------------------------------------------------------- 状态
async function refresh() {
  try { status = await api('/api/status'); } catch (e) { return; }
  renderOverview();
  renderAndroid();
  renderWatch();
  syncVnc();
}

function stat(label, value, cls, sub) {
  return `<div class="card stat"><div class="label">${esc(label)}</div>
    <div class="value"><span class="dot ${cls}"></span>${esc(value)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>`;
}

function renderOverview() {
  const s = status, a = s.android, p = a.props || {}, w = s.wechat || {}, d = s.device;
  const arm = (p.abilist || '').includes('arm64');
  const prof = d.DEVICE_PROFILE === 'tablet' ? '平板（Pad 模式）' : '手机';
  $('#stats').innerHTML = [
    stat('KVM', s.kvm ? '可用' : '不可用', s.kvm ? 'ok' : 'bad', s.kvm ? '' : '需要 --device /dev/kvm'),
    stat('安卓模拟器', a.booted ? '已开机' : (STATE_TEXT[a.state] || a.state), a.booted ? 'ok' : (a.state === 'RUNNING' ? 'warn' : 'bad'),
      `${prof} · ${p.wm_size || '?'} @${p.wm_density || '?'}dpi · Android ${p.release || '?'}`),
    stat('root', a.rooted ? '已获取' : '未获取', a.rooted ? 'ok' : 'warn', a.root_error || p.build_type || ''),
    stat('ARM 转译', a.booted ? (arm ? '支持' : '不支持') : '—', a.booted ? (arm ? 'ok' : 'bad') : '',
      p.abilist ? `${p.abilist} · ${p.native_bridge || '无 native bridge'}` : ''),
    stat('微信', w.installed ? `已安装 ${w.version}` : '未安装', w.installed ? (w.running ? 'ok' : 'warn') : 'bad',
      w.installed ? `ABI ${w.abi || '?'} · ${w.running ? '运行中' : '未运行'}` : ''),
    stat('微信登录', w.logged_in ? '已登录' : '未登录', w.logged_in ? 'ok' : 'warn', (w.uins || []).join(', ')),
    stat('数据库密钥', s.wx.key ? s.wx.key : '未解密', s.wx.key ? 'ok' : 'warn', s.wx.key ? `IMEI ${s.wx.imei} · uin ${s.wx.uin}` : ''),
    stat('稳定性', `微信重启 ${s.metrics.wechat_restarts} 次`, s.metrics.wechat_restarts ? 'warn' : 'ok',
      `模拟器启动 ${s.metrics.emulator_starts} 次 · 开机 ${s.metrics.boots} 次 · 统计自 ${fmtTs(s.metrics.since)}`),
  ].join('');
  renderChecklist();
}

// ------------------------------------------------------------- P0 清单
const CHECKS = [
  ['kvm', 'KVM 可用、模拟器能开机', () => status.kvm && status.android.booted],
  ['arm', '系统镜像带 ARM 转译（abilist 含 arm64-v8a）', () => (status.android.props?.abilist || '').includes('arm64')],
  ['install', '微信安装成功，ABI 为 arm64-v8a', () => status.wechat?.installed && (status.wechat.abi || '').includes('arm64')],
  ['login', '微信登录成功（按所选机型）', () => !!status.wechat?.logged_in],
  ['coexist', '平板模式：真手机没有被挤下线（选手机模式可跳过）', null],
  ['stable', '连续运行 24 小时，微信不崩溃、不掉线（看「稳定性」统计）', null],
  ['decrypt', '数据库解密成功', () => !!status.wx?.key],
  ['realtime', '实时监听读到新消息，延迟可接受', () => (status.watch?.snapshots || 0) > 1 && lastFeedCount > 0],
  ['send', '界面自动化发送成功', () => localStorage.getItem('p0.send') === '1'],
  ['scanAlbum', '相册识别扫码成功', null],
  ['scanCam', '虚拟摄像头扫码成功（可选）', null],
];
function checkState(key) {
  try { return localStorage.getItem(`p0.check.${key}`) === '1'; } catch (e) { return false; }
}
function renderChecklist() {
  $('#checklist').innerHTML = CHECKS.map(([key, label, auto]) => {
    let done = checkState(key);
    let autoNote = '';
    if (auto) {
      try { if (auto()) { done = true; autoNote = '（自动检测）'; } } catch (e) { /* ignore */ }
    }
    return `<li class="${done ? 'done' : ''}"><span class="n">${done ? '✓' : ''}</span>
      <div><div class="t">${esc(label)}</div><div class="d">${autoNote}</div></div>
      <label class="act row"><input type="checkbox" data-check="${key}" ${checkState(key) ? 'checked' : ''}> 手动确认</label></li>`;
  }).join('');
}
$('#checklist').addEventListener('change', (e) => {
  const k = e.target.dataset.check;
  if (!k) return;
  try { localStorage.setItem(`p0.check.${k}`, e.target.checked ? '1' : '0'); } catch (err) { /* ignore */ }
  renderChecklist();
});

// ------------------------------------------------------------- 安卓
function renderAndroid() {
  const a = status.android;
  $('#androidState').textContent = `· ${a.booted ? '已开机' : (STATE_TEXT[a.state] || a.state)}${a.desired ? '' : '（已手动关机，看门狗不会自动启动）'}`;
  $('#kvmWarn').innerHTML = status.kvm ? '' : '<div class="notice bad">容器内没有 /dev/kvm，模拟器无法运行。Linux 宿主机请加 --device /dev/kvm；Windows/WSL2 需开启嵌套虚拟化。</div>';
  const w = status.wechat || {};
  $('#wxState').textContent = !a.booted ? '模拟器未开机' : (w.installed
    ? `已安装 ${w.version}（ABI ${w.abi || '?'}），${w.running ? '运行中' : '未运行'}，${w.logged_in ? '已登录' : '未登录'}`
    : '未安装');
  const j = status.job;
  $('#jobState').innerHTML = j ? `<span class="dot ${j.status === 'done' ? 'ok' : j.status === 'error' ? 'bad' : 'warn'}"></span>${esc(j.name)}：${esc(j.message)}` : '';
  const m = status.metrics;
  $('#metrics').innerHTML = `模拟器启动 ${m.emulator_starts} 次，开机 ${m.boots} 次（最近 ${fmtTs(m.last_boot)}）<br>
    微信被看门狗重新拉起 ${m.wechat_restarts} 次（最近 ${fmtTs(m.last_wechat_restart)}）<br>统计开始于 ${fmtTs(m.since)}`;
  if (document.activeElement !== $('#wxWatchdog')) $('#wxWatchdog').checked = !!status.wechat_watchdog;
}
$$('[data-android]').forEach((b) => b.addEventListener('click', () => {
  const action = b.dataset.android;
  if (action === 'stop' && !confirm('关机后看门狗不会再自动启动模拟器，微信也会停止接收消息。确定关机？')) return;
  act(b, () => api(`/api/android/${action}`, {method: 'POST'}), {start: '正在启动，首次开机约 1–3 分钟', restart: '正在重启', stop: '已关机', root: '已执行'}[action]);
}));
$$('[data-key]').forEach((b) => b.addEventListener('click', () => act(b, () => api(`/api/key/${b.dataset.key}`, {method: 'POST'}))));
$('#textSend').addEventListener('click', (e) => act(e.target, async () => {
  await api('/api/text', {method: 'POST', body: {text: $('#textInput').value}});
  $('#textInput').value = '';
}, '已输入'));
$('#wxWatchdog').addEventListener('change', (e) => act(null, () => api('/api/settings', {method: 'PUT', body: {wechat_watchdog: e.target.checked}}), '已保存'));

// ------------------------------------------------------------- 设备配置
async function fillDevice() {
  const d = await api('/api/device');
  const f = $('#deviceForm');
  for (const [k, v] of Object.entries(d)) if (f.elements[k]) f.elements[k].value = v;
}
function deviceBody() {
  const f = $('#deviceForm');
  const out = {};
  for (const el of f.elements) if (el.name) out[el.name] = el.value;
  return out;
}
$('#deviceSave').addEventListener('click', (e) => act(e.target, () => api('/api/device', {method: 'PUT', body: deviceBody()}), '已保存，重启模拟器后生效'));
$('#deviceSaveRestart').addEventListener('click', (e) => {
  if (!confirm('保存并重启模拟器？')) return;
  act(e.target, () => api('/api/device', {method: 'PUT', body: {...deviceBody(), restart: true}}), '已保存，正在重启模拟器');
});
$('#apkFile').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  toast('正在上传…');
  act(null, () => api('/api/wechat/upload-apk', {method: 'POST', body: fd}), '上传完成，正在安装');
  e.target.value = '';
});
$('#apkDownload').addEventListener('click', (e) => act(e.target, () => api('/api/wechat/download', {method: 'POST', body: {url: $('#apkUrl').value}}), '开始下载'));

// ------------------------------------------------------------- 数据库
$('#probeBtn').addEventListener('click', async (e) => {
  $('#probeResult').innerHTML = '<p class="muted">正在验证，数据库较大时拷贝需要一些时间…</p>';
  const r = await act(e.target, () => api('/api/p0/db/probe', {method: 'POST', body: {imei: $('#imei').value}}));
  if (!r) { $('#probeResult').innerHTML = ''; return; }
  $('#probeResult').innerHTML = (r.ok ? `<div class="notice info">解密成功：密钥 <code>${esc(r.key)}</code>（IMEI ${esc(r.imei)}，uin ${esc(r.uin)}）</div>`
    : '<div class="notice bad">未通过，请看下面失败的步骤</div>') + stepsHtml(r.steps);
});

const MSG_COLS = [
  ['createTime', '时间', (r) => esc(fmtMs(r.createTime))],
  ['chat_name', '会话', (r) => `${esc(r.chat_name)}<div class="muted" style="font-size:11px">${esc(r.talker)}</div>`],
  ['isSend', '方向', (r) => r.isSend ? '发出' : '收到'],
  ['type_name', '类型'],
  ['content', '内容', (r) => `${r.sender ? `<b>${esc(r.sender)}</b>: ` : ''}${esc(String(r.content || '').slice(0, 160))}${r.imgPath ? `<div class="muted" style="font-size:11px">${esc(r.imgPath)}</div>` : ''}`],
];
$('#loadMsgs').addEventListener('click', async (e) => {
  const r = await act(e.target, () => api('/api/p0/db/messages?limit=30'));
  if (r) $('#msgs').innerHTML = table(r.items, MSG_COLS);
});
$('#loadConvs').addEventListener('click', async (e) => {
  const r = await act(e.target, () => api('/api/p0/db/conversations?limit=40'));
  if (r) $('#convs').innerHTML = table(r.items, [
    ['username', '会话 ID', (x) => `<a href="#" data-talker="${esc(x.username)}">${esc(x.username)}</a>`],
    ['nickname', '名称', (x) => esc(x.conRemark || x.nickname || '')],
    ['unReadCount', '未读'],
    ['digest', '摘要'],
  ]);
});
$('#convs').addEventListener('click', (e) => {
  const a = e.target.closest('[data-talker]');
  if (!a) return;
  e.preventDefault();
  $('#talker').value = a.dataset.talker;
  showTab('send');
});

let lastFeedCount = 0;
let feedTimer = null;
function renderWatch() {
  const w = status.watch || {};
  $('#watchState').textContent = w.on
    ? `· 监听中，检查 ${w.polls} 次，拷贝 ${w.snapshots} 次（上次 ${w.last_snapshot_cost}s）${w.error ? '，错误：' + w.error : ''}`
    : '· 未开启';
  if (w.on && !feedTimer) feedTimer = setInterval(loadFeed, 2000);
  if (!w.on && feedTimer) { clearInterval(feedTimer); feedTimer = null; }
}
async function loadFeed() {
  if (currentTab !== 'db') return;
  const w = await api('/api/p0/db/watch').catch(() => null);
  if (!w) return;
  lastFeedCount = (w.feed || []).length;
  $('#feed').innerHTML = table(w.feed, [['delay', '延迟(秒)'], ...MSG_COLS]);
}
$('#watchOn').addEventListener('click', (e) => act(e.target, () => api('/api/p0/db/watch', {method: 'POST', body: {on: true, interval: 2}}), '已开始监听'));
$('#watchOff').addEventListener('click', (e) => act(e.target, () => api('/api/p0/db/watch', {method: 'POST', body: {on: false}}), '已停止'));

// ------------------------------------------------------------- 发送
$('#sendBtn').addEventListener('click', async (e) => {
  $('#sendResult').innerHTML = '<p class="muted">正在操作模拟器…</p>';
  const r = await act(e.target, () => api('/api/p0/send', {method: 'POST', body: {talker: $('#talker').value, text: $('#sendText').value}}));
  if (!r) { $('#sendResult').innerHTML = ''; return; }
  if (r.ok) try { localStorage.setItem('p0.send', '1'); } catch (err) { /* ignore */ }
  $('#sendResult').innerHTML = `<div class="notice ${r.ok ? 'info' : 'bad'}">${r.ok ? `已点击发送（耗时 ${r.elapsed}s），请在截图或手机上确认` : '发送未完成'}</div>`
    + stepsHtml(r.steps) + (r.screenshot ? `<img src="${r.screenshot}" class="shot">` : '');
});

// ------------------------------------------------------------- 扫码
const drop = $('#drop');
drop.addEventListener('click', () => $('#scanFile').click());
drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.style.borderColor = 'var(--accent)'; });
drop.addEventListener('dragleave', () => { drop.style.borderColor = ''; });
drop.addEventListener('drop', (e) => { e.preventDefault(); drop.style.borderColor = ''; if (e.dataTransfer.files[0]) uploadScan(e.dataTransfer.files[0]); });
$('#scanFile').addEventListener('change', (e) => { if (e.target.files[0]) uploadScan(e.target.files[0]); e.target.value = ''; });
document.addEventListener('paste', (e) => {
  if (currentTab !== 'scan') return;
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith('image/'));
  if (item) uploadScan(item.getAsFile());
});
async function uploadScan(file) {
  const fd = new FormData();
  fd.append('file', file, file.name || 'paste.png');
  const r = await act(null, () => api('/api/scan/upload', {method: 'POST', body: fd}));
  if (!r) return;
  scanId = r.id;
  scanHasCode = r.codes.length > 0;
  $('#useRegen').disabled = !scanHasCode;
  $('#useRegen').checked = false;
  $('#pushGallery').disabled = false;
  $('#setPoster').disabled = false;
  $('#scanInfo').innerHTML = `<img src="/api/scan/image/${r.id}" class="shot" style="max-height:260px">`
    + (scanHasCode
      ? `<div class="notice info">识别到 ${r.codes.length} 个码：${r.codes.map((c) => `<div><b>${esc(c.format)}</b> <code>${esc(c.text)}</code></div>`).join('')}</div>`
      : '<div class="notice">没有解码出二维码，可以照样推送原图试试，或换一张更清晰的图片。</div>');
}
$('#pushGallery').addEventListener('click', async (e) => {
  $('#scanResult').innerHTML = '<p class="muted">正在推送…</p>';
  const r = await act(e.target, () => api('/api/scan/push', {method: 'POST', body: {id: scanId, regenerated: $('#useRegen').checked, open_scanner: true, auto_album: true}}));
  if (!r) { $('#scanResult').innerHTML = ''; return; }
  $('#scanResult').innerHTML = `<div class="notice ${r.indexed ? 'info' : ''}">已推送到 <code>${esc(r.remote)}</code>，${r.indexed ? '已进入相册' : '暂未在媒体库中查到（相册里可能要稍等或手动刷新）'}。请在「安卓画面」里选中最新的图片。</div>`
    + (r.scanner ? stepsHtml(r.scanner.steps) + (r.scanner.screenshot ? `<img src="${r.scanner.screenshot}" class="shot">` : '') : '');
});
$('#openScanner').addEventListener('click', async (e) => {
  const r = await act(e.target, () => api('/api/scan/open-scanner', {method: 'POST', body: {auto_album: false}}));
  if (r) $('#scanResult').innerHTML = stepsHtml(r.steps) + (r.screenshot ? `<img src="${r.screenshot}" class="shot">` : '');
});
$('#setPoster').addEventListener('click', async (e) => {
  if (!confirm('会把摄像头切到虚拟场景并重启模拟器（约 1–3 分钟），确定？')) return;
  const r = await act(e.target, () => api('/api/scan/camera', {method: 'POST', body: {id: scanId, regenerated: $('#useRegen').checked, restart: true}}));
  if (r) $('#scanResult').innerHTML = '<div class="notice info">已设为虚拟摄像头画面，模拟器正在重启。开机后打开微信「扫一扫」，按住 Alt 并移动鼠标或用 WASD 转动视角，对准墙上的二维码。</div>';
});

// ------------------------------------------------------------- 日志
async function loadLogs() {
  const r = await api(`/api/logs/${$('#logName').value}?lines=400`).catch((e) => ({text: e.message}));
  const pre = $('#logText');
  pre.textContent = r.text || '（空）';
  pre.scrollTop = pre.scrollHeight;
}
$('#logName').addEventListener('change', loadLogs);
$('#logRefresh').addEventListener('click', loadLogs);

// ------------------------------------------------------------- 启动
showTab((location.hash || '#overview').slice(1).replace(/[^a-z]/g, '') || 'overview');
refresh();
setInterval(refresh, 5000);
