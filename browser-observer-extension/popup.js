const $ = id => document.getElementById(id);
let hasPermission = false;
$('extid').textContent = chrome.runtime.id || '未知';
async function send(message) {
  const r = await chrome.runtime.sendMessage(message);
  if (!r?.ok) throw new Error(r?.error || '扩展通信失败');
  return r;
}
async function refresh() {
  const r = await send({ action: 'status' });
  // 字段缺失时给可读占位，而不是把 undefined 甩到界面上再抛错。
  const connection = r.nativeConnection || { status: '状态不可用', lastHeartbeatAt: null, lastError: '' };
  hasPermission = r.globalPermission === 'granted';
  $('permission').textContent = `${r.globalPermission ?? '未知'} / ${r.enabled ? '已启用' : '已停用'}`;
  $('connection').textContent = `${connection.status} / 最后握手：${connection.lastHeartbeatAt ? new Date(connection.lastHeartbeatAt).toLocaleString() : '尚无'}`;
  const error = connection.lastError || connection.lastActionError || '';
  // Chrome 的 forbidden 只在注册 origin 与当前扩展 ID 不一致时出现，直接点明。
  $('error').textContent = /forbidden/i.test(error)
    ? `${error}（注册的 allowed_origins 不含当前扩展 ID ${chrome.runtime.id}）` : error;
  $('assignment').textContent = r.taskAssignment ?? '未知';
  $('state').textContent = r.state ? JSON.stringify(r.state, null, 2) : '无观察状态；等待内部任务和目标分配';
}
async function run(fn) {
  $('error').textContent = '';
  try { await fn(); await refresh(); }
  catch (e) { $('error').textContent = e.message; }
}
$('enable').addEventListener('click', () => {
  if (hasPermission) return run(() => send({ action: 'enable' }));
  const pendingEnable = { nonce: crypto.randomUUID(), issuedAt: Date.now(), expiresAt: Date.now() + 60000 };
  // 同一手势内先发起持久写入再请求；不能 await 写入，否则会丢失用户手势。
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
$('refresh').addEventListener('click', () => run(refresh));
// 同一份 popup.html 被当成标签页长期打开时不会自己更新，会和弹窗显示不一致；
// 仅为可见状态补一个轻量轮询，切到后台不轮询。
setInterval(() => { if (document.visibilityState === 'visible') refresh().catch(() => {}); }, 5000);
run(refresh);
