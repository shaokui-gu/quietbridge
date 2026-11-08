const $ = id => document.getElementById(id);
let hasPermission = false;
let refreshSequence = 0;
$('extid').textContent = chrome.runtime.id || '未知';

async function send(message) {
  const r = await chrome.runtime.sendMessage(message);
  if (!r?.ok) throw new Error(r?.error || '扩展通信失败');
  return r;
}

function showError(message) {
  $('error').textContent = message;
  $('error').hidden = !message;
}

function setValue(id, text, tone = '') {
  $(id).textContent = text;
  $(id).className = `row-value ${tone}`;
}

async function refresh() {
  const sequence = ++refreshSequence;
  const r = await send({ action: 'status' });
  if (sequence !== refreshSequence) return;
  const connection = r.nativeConnection || {};
  const permissionKnown = r.globalPermission === 'granted' || r.globalPermission === 'permission_missing';
  hasPermission = r.globalPermission === 'granted';
  const enabled = Boolean(r.enabled && hasPermission);
  const connected = connection.status === 'connected';
  const syncing = connection.status === 'syncing';
  const assignment = r.taskAssignment;

  $('enable').hidden = enabled;
  $('enable').disabled = !permissionKnown;
  $('enable').textContent = hasPermission ? '启用观察' : '授权并启用';
  $('disable').hidden = !enabled;
  setValue('permission', !permissionKnown ? '未知' : hasPermission ? '已授权' : '未授权', hasPermission ? 'good' : permissionKnown ? 'warning' : '');
  setValue('connection', connected ? '已连接' : syncing ? '连接中' : '未连接', connected ? 'good' : syncing ? '' : 'error-tone');
  const heartbeat = connection.lastHeartbeatAt ? new Date(connection.lastHeartbeatAt) : null;
  $('heartbeat').textContent = heartbeat && !Number.isNaN(heartbeat.getTime())
    ? `最后握手：${heartbeat.toLocaleString()}` : connected ? '连接正常' : syncing ? '正在连接原生端' : '请检查本地应用';

  const assignmentStates = {
    assigned: ['已分配', '任务页面已绑定', 'good'],
    no_assignment: ['等待分配', '由原生端自动分配', ''],
    needs_target_selection: ['等待目标', '等待原生端指定任务页面', 'warning'],
    paused: ['已暂停', '当前任务页面暂停观察', 'warning'],
    permission_missing: ['等待授权', '授权后接收任务', 'warning'],
    disabled: ['已停用', '启用后接收任务', '']
  };
  const task = !permissionKnown ? ['未知', '等待状态更新', '']
    : !hasPermission ? assignmentStates.permission_missing
    : !enabled ? assignmentStates.disabled
    : assignmentStates[assignment] || ['未知', '等待状态更新', ''];
  setValue('assignment', task[0], task[2]);
  $('assignment-detail').textContent = task[1];

  let label, title, description, tone;
  if (!permissionKnown) {
    [label, title, description, tone] = ['状态未知', '无法获取站点权限', '请刷新状态后重试。', 'warning'];
  } else if (!hasPermission) {
    [label, title, description, tone] = ['待授权', '启用浏览器控制', '授权后，QuietBridge 才能接收并执行浏览器任务。', 'warning'];
  } else if (!enabled) {
    [label, title, description, tone] = ['已停用', '观察已停用', '站点权限仍然保留，可随时重新启用。', ''];
  } else if (connection.status === 'application_stopped') {
    [label, title, description, tone] = ['应用未运行', 'QuietBridge 已退出', '请先打开 QuietBridge，再使用浏览器控制。', 'warning'];
  } else if (syncing) {
    [label, title, description, tone] = ['连接中', '正在连接原生端', '正在同步扩展与本地应用的状态。', ''];
  } else if (!connected) {
    [label, title, description, tone] = ['连接异常', '无法连接原生端', '请检查 QuietBridge 是否正在运行，然后刷新状态。', 'error-tone'];
  } else if (assignment === 'paused') {
    [label, title, description, tone] = ['已暂停', '任务观察已暂停', '当前任务页面暂不可观察，请检查任务目标。', 'warning'];
  } else if (assignment === 'needs_target_selection') {
    [label, title, description, tone] = ['等待目标', '等待任务目标', '原生端指定任务页面后将开始观察。', 'warning'];
  } else if (assignment === 'assigned') {
    [label, title, description, tone] = ['运行中', '任务已分配', '扩展已启用，正在处理已分配的任务页面。', 'good'];
  } else if (assignment === 'no_assignment') {
    [label, title, description, tone] = ['运行中', '已准备就绪', '扩展已启用，正在等待任务分配。', 'good'];
  } else {
    [label, title, description, tone] = ['状态更新中', '正在同步任务状态', '请稍候，扩展正在等待任务状态更新。', ''];
  }
  $('status-label').textContent = label;
  $('status-badge').className = `badge ${tone}`;
  $('overview-title').textContent = title;
  $('status-description').textContent = description;
  const error = connection.lastError || connection.lastActionError || '';
  showError(/forbidden/i.test(error)
    ? `${error}（注册的 allowed_origins 不含当前扩展 ID ${chrome.runtime.id}）` : error);
  $('state').textContent = r.state ? JSON.stringify(r.state, null, 2) : '无观察状态；等待内部任务和目标分配';
}

async function run(fn) {
  showError('');
  try { await fn(); await refresh(); }
  catch (e) { showError(e?.message || '扩展通信失败'); }
}

$('enable').addEventListener('click', () => {
  if (hasPermission) return run(() => send({ action: 'enable' }));
  const pendingEnable = { nonce: crypto.randomUUID(), issuedAt: Date.now(), expiresAt: Date.now() + 60000 };
  // 权限请求必须与点击处于同一用户手势，不能先等待存储完成。
  const recording = chrome.storage.local.set({ pendingEnable });
  const permission = chrome.permissions.request({ origins: ['http://*/*', 'https://*/*'] });
  return run(async () => {
    const [recorded, granted] = await Promise.allSettled([recording, permission]);
    if (recorded.status === 'rejected' || granted.status === 'rejected' || !granted.value) {
      await send({ action: 'cancel_enable', nonce: pendingEnable.nonce });
      if (recorded.status === 'rejected') throw recorded.reason;
      if (granted.status === 'rejected') throw granted.reason;
      throw new Error('permission_missing：未授予普通 HTTP/HTTPS 全站权限');
    }
    await send({ action: 'enable', nonce: pendingEnable.nonce });
  });
});
$('disable').addEventListener('click', () => run(() => send({ action: 'disable' })));
$('refresh').addEventListener('click', () => run(async () => {}));
setInterval(() => { if (document.visibilityState === 'visible') refresh().catch(() => {}); }, 5000);
run(async () => {});
