const $ = id => document.getElementById(id);
let hasPermission = false;
let refreshSequence = 0;
let reviewKey = '';
let reviewImage = null;
let reviewAnnotations = [];
let reviewSelection = null;
let reviewStart = null;
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

function drawReview() {
  if (!reviewImage) return;
  const canvas = $('review-canvas');
  const ctx = canvas.getContext('2d');
  canvas.width = reviewImage.naturalWidth;
  canvas.height = reviewImage.naturalHeight;
  ctx.drawImage(reviewImage, 0, 0);
  const scale = Math.max(1, canvas.width / 800);
  ctx.lineWidth = 3 * scale;
  ctx.font = `bold ${16 * scale}px sans-serif`;
  const drawBox = (box, index) => {
    const x = box.x * canvas.width, y = box.y * canvas.height;
    const w = box.width * canvas.width, h = box.height * canvas.height;
    ctx.strokeStyle = '#df4936';
    ctx.strokeRect(x, y, w, h);
    if (index != null) {
      ctx.fillStyle = '#df4936';
      const label = String(index + 1);
      const labelWidth = ctx.measureText(label).width + 12 * scale;
      ctx.fillRect(x, y, labelWidth, 24 * scale);
      ctx.fillStyle = '#fff';
      ctx.fillText(label, x + 6 * scale, y + 17 * scale);
    }
  };
  reviewAnnotations.forEach((item, index) => drawBox(item.box, index));
  if (reviewSelection) drawBox(reviewSelection, null);
}

function renderReview(review) {
  $('review-panel').hidden = !review;
  const key = review ? JSON.stringify([review.capturedAt, review.annotations]) : '';
  if (key === reviewKey) return;
  reviewKey = key;
  reviewSelection = null;
  reviewImage = null;
  reviewAnnotations = review?.annotations || [];
  $('review-notes').replaceChildren();
  reviewAnnotations.forEach((item, index) => {
    const row = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = `${index + 1}. ${item.note}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-annotation';
    remove.textContent = '删除';
    remove.addEventListener('click', () => run(() => send({ action: 'remove_review_annotation', index })));
    row.appendChild(label);
    row.appendChild(remove);
    $('review-notes').appendChild(row);
  });
  if (!review) { reviewImage = null; return; }
  const img = new Image();
  img.onload = () => { if (reviewKey === key) { reviewImage = img; drawReview(); } };
  img.src = `data:image/jpeg;base64,${review.image}`;
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
    [label, title, description, tone] = ['待授权', '启用浏览器控制', '授权后，workbuddy-cua 才能接收并执行浏览器任务。', 'warning'];
  } else if (!enabled) {
    [label, title, description, tone] = ['已停用', '观察已停用', '站点权限仍然保留，可随时重新启用。', ''];
  } else if (syncing) {
    [label, title, description, tone] = ['连接中', '正在连接原生端', '正在同步扩展与本地应用的状态。', ''];
  } else if (!connected) {
    [label, title, description, tone] = ['连接异常', '无法连接原生端', '请检查 workbuddy-cua 是否正在运行，然后刷新状态。', 'error-tone'];
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
  $('current-task').hidden = !r.taskSummary;
  $('task-site').textContent = r.taskSummary?.url || '等待页面状态';
  $('task-id').textContent = r.taskSummary?.taskId ? `任务 ID：${r.taskSummary.taskId}` : '';
  $('stop-task').hidden = !r.taskSummary?.canStop;
  $('capture-review').disabled = !r.state?.documentId;
  const names = { snapshot: '读取页面快照', screenshot: '页面截图', diagnostics: '页面诊断',
    click: '点击', fill: '填写', select: '选择', scroll: '滚动', navigate: '导航', keypress: '按键', download: '候选下载' };
  const outcomes = { completed: '已返回', not_performed: '未执行', failed: '失败', started: '已开始' };
  const activity = $('activity');
  activity.replaceChildren();
  for (const item of (r.activity || [])) {
    const row = document.createElement('li');
    const time = new Date(item.at);
    row.textContent = `${Number.isNaN(time.getTime()) ? '时间未知' : time.toLocaleTimeString()} · ${names[item.operation] || '操作'} · ${outcomes[item.outcome] || '未知'}${item.site ? ` · ${item.site}` : ''}`;
    activity.appendChild(row);
  }
  if (!activity.children.length) {
    const row = document.createElement('li');
    row.textContent = '暂无活动记录';
    activity.appendChild(row);
  }
  const downloads = $('downloads');
  downloads.replaceChildren();
  const downloadStates = { started: '已开始', inProgress: '下载中', completed: '已完成', canceled: '已取消' };
  for (const item of (r.downloads || [])) {
    const row = document.createElement('li');
    row.textContent = `${item.filename} · ${downloadStates[item.state] || '状态未知'}`;
    downloads.appendChild(row);
  }
  if (!downloads.children.length) {
    const row = document.createElement('li');
    row.textContent = '暂无候选下载';
    downloads.appendChild(row);
  }
  renderReview(r.review);
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
$('stop-task').addEventListener('click', () => run(() => send({ action: 'stop_task' })));
$('clear-activity').addEventListener('click', () => run(() => send({ action: 'clear_activity' })));
$('capture-review').addEventListener('click', () => run(() => send({ action: 'capture_review' })));
const reviewCanvas = $('review-canvas');
function reviewPoint(event) {
  const rect = reviewCanvas.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) };
}
reviewCanvas.addEventListener('pointerdown', event => {
  if (!reviewImage) return;
  reviewStart = reviewPoint(event);
  reviewSelection = null;
  drawReview();
  reviewCanvas.setPointerCapture(event.pointerId);
});
reviewCanvas.addEventListener('pointermove', event => {
  if (!reviewStart) return;
  const end = reviewPoint(event);
  reviewSelection = { x: Math.min(reviewStart.x, end.x), y: Math.min(reviewStart.y, end.y),
    width: Math.abs(end.x - reviewStart.x), height: Math.abs(end.y - reviewStart.y) };
  drawReview();
});
reviewCanvas.addEventListener('pointerup', () => { reviewStart = null; });
reviewCanvas.addEventListener('pointercancel', () => { reviewStart = null; reviewSelection = null; drawReview(); });
$('save-review').addEventListener('click', () => {
  const note = $('review-note').value.trim();
  if (!reviewSelection || reviewSelection.width < 0.005 || reviewSelection.height < 0.005 || !note) {
    showError('请先选择截图区域并填写批注内容');
    return;
  }
  return run(async () => {
    await send({ action: 'annotate_review', box: reviewSelection, note });
    $('review-note').value = '';
  });
});
$('export-review').addEventListener('click', () => {
  if (!reviewImage) return;
  reviewSelection = null;
  drawReview();
  const output = document.createElement('canvas');
  const scale = Math.max(1, reviewCanvas.width / 800);
  const fontSize = 16 * scale, padding = 16 * scale, lineHeight = 24 * scale;
  const ctx = output.getContext('2d');
  ctx.font = `${fontSize}px sans-serif`;
  const lines = [];
  reviewAnnotations.forEach((item, index) => {
    let line = `${index + 1}. `;
    for (const character of item.note) {
      if (ctx.measureText(line + character).width > reviewCanvas.width - padding * 2 && line.trim()) {
        lines.push(line);
        line = '';
      }
      line += character;
    }
    lines.push(line);
  });
  output.width = reviewCanvas.width;
  output.height = reviewCanvas.height + (lines.length ? padding * 2 + lines.length * lineHeight : 0);
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, output.width, output.height);
  ctx.drawImage(reviewCanvas, 0, 0);
  ctx.font = `${fontSize}px sans-serif`;
  ctx.fillStyle = '#192a23';
  lines.forEach((line, index) => ctx.fillText(line, padding, reviewCanvas.height + padding + fontSize + index * lineHeight));
  const link = document.createElement('a');
  link.download = 'workbuddy-cua-review.png';
  link.href = output.toDataURL('image/png');
  link.click();
});
$('refresh').addEventListener('click', () => run(async () => {}));
setInterval(() => { if (document.visibilityState === 'visible') refresh().catch(() => {}); }, 5000);
run(async () => {});
