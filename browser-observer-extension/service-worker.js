import { initialState, reduce, safeUrl, originOf, pause } from './state.mjs';
import { BackgroundBrowser } from './background-browser.mjs';

const HOST = 'com.cua.browser_observer';
const ORIGINS = ['http://*/*', 'https://*/*'];
let queue = Promise.resolve();
let timer;
let controls = Promise.resolve();
const control = fn => {
  const job = controls.then(fn);
  controls = job.catch(() => {});
  return job;
};
const syncLater = () => {
  connection = { ...connection, status: 'syncing' };
  serial(sync);
};
async function completeEnable(nonce, automatic = false) {
  const revision = enableRevision;
  const { pendingEnable: intent, disabledAt = 0 } = await chrome.storage.local.get(['pendingEnable', 'disabledAt']);
  if (automatic || nonce !== undefined) {
    if (!intent || nonce !== intent.nonce) return;
    if (typeof intent.nonce !== 'string' || !intent.nonce || !Number.isFinite(intent.issuedAt) ||
        !Number.isFinite(intent.expiresAt) || intent.issuedAt <= disabledAt || intent.issuedAt > Date.now() ||
        intent.expiresAt <= Date.now() || intent.expiresAt - intent.issuedAt > 60000) {
      await chrome.storage.local.set({ pendingEnable: null });
      return;
    }
  }
  if (!await chrome.permissions.contains({ origins: ORIGINS })) {
    if (automatic) return;
    throw new Error('permission_missing：需要一次 HTTP/HTTPS 全站授权');
  }
  if (revision !== enableRevision) return;
  enableRevision++;
  permissionRevoked = false;
  await chrome.storage.local.set({ enabled: true, pendingEnable: null });
  syncLater();
}
async function resumeEnable() {
  const { pendingEnable } = await chrome.storage.local.get('pendingEnable');
  if (pendingEnable) await completeEnable(pendingEnable.nonce, true);
}
let permissionRevoked = false;
let enableRevision = 0;
let connection = { status: 'host_unavailable', lastHeartbeatAt: null };
let assignmentStatus = 'no_assignment';
let createdReport = null;
let lastCreateRequestId = null;
let assignmentTitle = null;
let commandReport = null;
let lastCommandId = null;
let keepAlivePort = null;
let keepAliveTimer;
function keepAlive(on) {
  clearInterval(keepAliveTimer);
  if (!on) {
    try { keepAlivePort?.disconnect(); } catch {}
    keepAlivePort = null;
    return;
  }
  if (!keepAlivePort) {
    try {
      keepAlivePort = chrome.runtime.connectNative(HOST);
      keepAlivePort.onMessage.addListener(() => {});
      keepAlivePort.onDisconnect.addListener(() => { keepAlivePort = null; });
    } catch { keepAlivePort = null; }
  }
  // 周期性 ping 重置 worker 空闲计时，避免命令排队等到下一次 alarm。
  keepAliveTimer = setInterval(() => {
    try { keepAlivePort?.postMessage({ action: 'ping' }); } catch { keepAlivePort = null; }
  }, 20000);
}
async function runCommand(command, state) {
  if (!command || typeof command.requestId !== 'string' || lastCommandId === command.requestId) return;
  // 先落盘再执行：worker 被回收后重启不得重复执行同一动作。
  const { lastCommand } = await chrome.storage.session.get('lastCommand');
  if (lastCommand && lastCommand.requestId === command.requestId) {
    lastCommandId = command.requestId;
    commandReport = { requestId: command.requestId, status: 'error',
      error: '命令已下发但结果未知，未重复执行' };
    return;
  }
  lastCommandId = command.requestId;
  await chrome.storage.session.set({ lastCommand: { requestId: command.requestId, at: Date.now() } });
  try {
    const result = await background.run(command, state);
    if (result?.children?.length) {
      const { managedTabs = [] } = await chrome.storage.session.get('managedTabs');
      await chrome.storage.session.set({ managedTabs: [...new Set([...managedTabs, ...result.children.map(t => t.tabId)])],
        managedTaskId: state.taskId });
    }
    if (!result) throw new Error('页面未返回结果');
    commandReport = { requestId: command.requestId, status: 'ok', result };
    connection = { ...connection, backgroundStatus: 'ready', lastActionError: null };
  } catch (error) {
    commandReport = { requestId: command.requestId, status: 'error',
      error: String(error?.message || '命令执行失败').slice(0, 200) };
    connection = { ...connection, backgroundStatus: 'waiting', lastActionError: commandReport.error };
  }
}
const serial = fn => {
  const job = queue.then(fn);
  queue = job.catch(() => {});
  return job;
};
const read = async () => (await chrome.storage.session.get('observer')).observer || null;
const save = observer => chrome.storage.session.set({ observer });
const permitted = async () => !permissionRevoked && await chrome.permissions.contains({ origins: ORIGINS });
const enabled = async () => (await chrome.storage.local.get('enabled')).enabled === true;
const background = new BackgroundBrowser(chrome, async s => {
  const current = await read();
  return await enabled() && await permitted() && current?.bindingId === s?.bindingId &&
    current?.documentId === s?.documentId;
});
async function native(message) {
  let timeout;
  try {
    const result = await Promise.race([
      chrome.runtime.sendNativeMessage(HOST, message),
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('本地 host 超时')), 10000); })
    ]);
    if (!result?.ok) throw new Error(result?.error || '本地请求被拒绝');
    return result;
  } finally { clearTimeout(timeout); }
}
function wire(s) {
  const keys = ['browserWindowId', 'tabId', 'documentId', 'navigationEpoch', 'url',
    'phase', 'loaded', 'domReady', 'contentReady', 'bodyPresent', 'bodyVisible',
    'elementCount', 'readyState', 'conditionMatched', 'sequence', 'mappingVerified', 'bindingId'];
  return Object.fromEntries(keys.map(k => [k, s[k]]));
}
async function publish(s) {
  if (!await permitted() || !await enabled()) { await clearObservation(); return; }
  await save(s);
  try {
    await native({ action: 'event', taskId: s.taskId, token: s.token, event: wire(s) });
    s.nativeError = null;
  } catch {
    s.nativeError = '未同步：本地 host 不可用或内部任务分配已失效';
  }
  if (await permitted() && await enabled()) await save(s);
  else await clearObservation();
}
async function stop(s) {
  try {
    if (s?.documentId) await chrome.scripting.executeScript({ target: {
      tabId: s.tabId, documentIds: [s.documentId] }, func: () => globalThis.__cuaObserverStop?.() });
  } catch {}
}
async function clearObservation(keepTask = false) {
  const s = await read();
  assignmentTitle = null;
  await save(null);
  await stop(s);
  if (!keepTask) {
    const { managedTabs = [], managedTaskId } = await chrome.storage.session.get(['managedTabs', 'managedTaskId']);
    for (const tabId of new Set([...managedTabs, ...background.attached.keys(), ...background.guards.keys()])) {
      await background.release(tabId, managedTaskId || s?.taskId);
    }
    await chrome.storage.session.set({ managedTabs: [], managedTaskId: null });
  }
}
async function suspend(s) {
  await stop(s);
  await publish(pause(s, Date.now()));
}
async function inject(s) {
  if (!s?.url || !s.documentId || s.pending || s.paused || !await enabled() || !await permitted()) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId: s.tabId,
      documentIds: [s.documentId] }, files: ['content.js'], injectImmediately: true });
    await chrome.scripting.executeScript({ target: { tabId: s.tabId,
      documentIds: [s.documentId] }, func: config => globalThis.__cuaObserver?.(config),
      args: [{ navigationEpoch: s.navigationEpoch, bindingId: s.bindingId,
        authorizedOrigin: originOf(s.url), selector: s.selector }] });
  } catch { /* 文档切换不回退到其他文档。 */ }
}
async function navigation(type, d) {
  if (d.frameId !== 0) return;
  const old = await read();
  if (!old || old.tabId !== d.tabId || old.paused) return;
  if (!await permitted() || !await enabled()) { await clearObservation(); return; }
  if (type === 'committed') {
    const frame = await chrome.webNavigation.getFrame({ tabId: d.tabId, frameId: 0 });
    if (!frame || frame.documentId !== d.documentId || safeUrl(frame.url) !== safeUrl(d.url)) return;
  }
  const next = reduce(old, { ...d, type });
  if (next !== old) next.documentUrl = d.url;
  if (next === old) return;
  if (next.paused) await stop(old);
  await publish(next);
  if (['committed', 'dom', 'completed', 'history'].includes(type)) await inject(next);
}
async function acceptAssignment(a) {
  if (!a || typeof a.taskId !== 'string' || !/^[0-9a-f]{64}$/.test(a.token) ||
      !/^[0-9a-f]{32}$/.test(a.bindingId) || !Number.isSafeInteger(a.assignmentVersion) ||
      a.assignmentVersion < 1 || !Number.isInteger(a.tabId) || !Number.isInteger(a.browserWindowId) ||
      a.mappingVerified !== false || a.globalOrigins !== true || a.selector !== '') {
    await clearObservation();
    return;
  }
  const old = await read();
  if (old && old.taskId === a.taskId && old.bindingId === a.bindingId &&
      old.assignmentVersion === a.assignmentVersion && old.token === a.token) {
    old.ownedTabCount = (a.ownedTabs || []).length || old.ownedTabCount || 1;
    await save(old);
    return;
  }
  await clearObservation(old?.taskId === a.taskId);
  const tab = await chrome.tabs.get(a.tabId);
  if (tab.windowId !== a.browserWindowId || !safeUrl(tab.url)) {
    assignmentStatus = 'needs_target_selection';
    return;
  }
  if (a.paused) { assignmentStatus = 'paused'; return; }
  const s = initialState({ taskId: a.taskId, token: a.token, bindingId: a.bindingId,
    assignmentVersion: a.assignmentVersion, globalOrigins: true,
    authorizedOrigin: originOf(tab.url), selector: '', tabId: a.tabId, browserWindowId: a.browserWindowId,
    ownedTabCount: (a.ownedTabs || []).length || 1 });
  assignmentTitle = typeof tab.title === 'string' ? tab.title.slice(0, 200) : null;
  // 重启保留 host 已接受的序号与 epoch，但重新核验当前文档。
  if (a.event) {
    s.sequence = a.event.sequence;
    s.navigationEpoch = a.event.navigationEpoch;
  }
  await save(s);
  await background.reset(s);
  await chrome.storage.session.set({ managedTabs: (a.ownedTabs || [{ tabId: a.tabId }]).map(t => t.tabId),
    managedTaskId: a.taskId });
  await chrome.storage.local.set({ boundTarget: { taskId: a.taskId, token: a.token, tabId: a.tabId } });
  const frame = await chrome.webNavigation.getFrame({ tabId: a.tabId, frameId: 0 });
  if (frame?.documentId) await navigation('committed', { ...frame, frameId: 0,
    tabId: a.tabId, timeStamp: Date.now() });
  else await publish(s);
}
function schedule() {
  clearTimeout(timer);
  timer = setTimeout(() => { serial(sync); }, 5000);
}
async function finishCreatedTarget(request, result) {
  const { lastCreatedTarget, managedTabs = [] } = await chrome.storage.session.get(['lastCreatedTarget', 'managedTabs']);
  if (!request.url || lastCreatedTarget?.navigationIssued) return;
  await chrome.storage.session.set({ managedTabs: [...new Set([...managedTabs, result.tabId])], managedTaskId: request.taskId });
  await background.protectCreatedTab(result.tabId, result.browserWindowId, request.taskId);
  if (!await enabled() || !await permitted()) throw new Error('授权已撤销，新窗口保持空白');
  // An unknown navigation result must never be replayed after worker restart.
  await chrome.storage.session.set({ lastCreatedTarget: { ...lastCreatedTarget, navigationIssued: true } });
  await chrome.tabs.update(result.tabId, { url: request.url });
}
async function createWindow(request) {
  if (!request || typeof request.requestId !== 'string' || !request.requestId ||
      lastCreateRequestId === request.requestId) return false;
  const { lastCreatedTarget } = await chrome.storage.session.get('lastCreatedTarget');
  lastCreateRequestId = request.requestId;
  if (lastCreatedTarget?.requestId === request.requestId) {
    if (lastCreatedTarget.result) {
      createdReport = lastCreatedTarget.result;
      await finishCreatedTarget(request, lastCreatedTarget.result);
      return true;
    }
    throw new Error('窗口创建已下发但结果未知，未重复建窗');
  }
  // 先登记再建窗：worker 重启或回传失败不能产生第二个窗口。
  await chrome.storage.session.set({ lastCreatedTarget: {
    requestId: request.requestId, at: Date.now()
  } });
  const options = { focused: false, url: 'about:blank' };
  const win = await chrome.windows.create(options);
  const tab = win && Array.isArray(win.tabs) ? win.tabs[0] : null;
  if (!win || !Number.isInteger(win.id) || !tab || !Number.isInteger(tab.id)) {
    throw new Error('窗口创建结果不完整');
  }
  const result = { requestId: request.requestId, browserWindowId: win.id, tabId: tab.id };
  await chrome.storage.session.set({ lastCreatedTarget: {
    requestId: request.requestId, at: Date.now(), result
  } });
  // Record the created identity before any further await. Failures must never
  // create another window. Only navigate after early popup protection is ready.
  createdReport = result;
  await finishCreatedTarget(request, result);
  return true;
}
async function sync() {
  try {
    const { permission, on, revision } = await control(async () => {
      const permission = await permitted();
      let on = await enabled();
      if (!permission) {
        on = false;
        await chrome.storage.local.set({ enabled: false });
      }
      if (!on) await clearObservation();
      return { permission, on, revision: enableRevision };
    });
    keepAlive(on && permission);
    const payload = { action: 'sync', enabled: on, permission, clientVersion: '0.3.0', capabilities: ['cdp_input', 'document_binding', 'background_popups'] };
    if (assignmentTitle) payload.assignmentTitle = assignmentTitle;
    if (createdReport) payload.created = createdReport;
    if (commandReport) payload.commandResult = commandReport;
    let r = await native(payload);
    // host 接受后才清除待回传证据；抛错时保留到下一轮。
    if (payload.created) createdReport = null;
    if (payload.commandResult) commandReport = null;
    if (revision !== enableRevision) return;
    if (r.protocolVersion !== 2 || !Number.isFinite(r.heartbeatAt)) throw new Error('host 协议不兼容');
    connection = { ...connection, status: 'connected', lastHeartbeatAt: r.heartbeatAt * 1000 };
    delete connection.lastError;
    if (r.requestCandidates && on && permission) {
      // 仅原生可信端请求时返回候选元数据，不观察候选页面内容，不按焦点筛选。
      const tabs = await chrome.tabs.query({});
      const candidates = tabs.map(t => ({ tabId: t.id, browserWindowId: t.windowId, url: safeUrl(t.url) }))
        .filter(t => t.url && Number.isInteger(t.tabId) && Number.isInteger(t.browserWindowId)).slice(0, 1000);
      if (!await permitted() || !await enabled()) { await clearObservation(); return; }
      r = await native({ action: 'sync', enabled: on, permission, candidates });
    }
    if (revision !== enableRevision) return;
    assignmentStatus = r.taskAssignment;
    if (on && permission && await enabled() && await permitted() && r.assignment) await acceptAssignment(r.assignment);
    else await clearObservation(on && permission && r.activeTaskId != null &&
      [...background.guards.values()].some(g => g.taskId === r.activeTaskId));
    if (r.createRequest && on && permission) {
      try {
        if (await createWindow(r.createRequest)) serial(sync);
      } catch (error) {
        connection = { ...connection,
          lastError: String(error?.message || '专用窗口创建失败').slice(0, 500) };
      }
    }
    if (r.command && on && permission && await enabled() && await permitted()) {
      const before = commandReport;
      await runCommand(r.command, await read());
      // 只有本轮真正产生了新结果才立刻回传，避免重复下发导致空转。
      if (commandReport !== before) serial(sync);
    }
  } catch (error) {
    connection = { ...connection, status: 'host_unavailable',
      lastError: String(error?.message || '本地连接失败').slice(0, 500) };
    assignmentStatus = 'no_assignment';
    await clearObservation();
  } finally { schedule(); }
}
for (const [name, type] of Object.entries({ onBeforeNavigate: 'before', onCommitted: 'committed',
  onDOMContentLoaded: 'dom', onCompleted: 'completed', onErrorOccurred: 'error', onHistoryStateUpdated: 'history' })) {
  chrome.webNavigation[name].addListener(d => { serial(() => navigation(type, d)); });
}
chrome.permissions.onRemoved.addListener(() => {
  // 不等待可能仍在进行的 host 请求：立即阻止后续发布并清空本地观察。
  permissionRevoked = true;
  enableRevision++;
  control(async () => {
    await chrome.storage.local.set({ enabled: false, pendingEnable: null, disabledAt: Date.now() });
    await clearObservation();
    syncLater();
  });
});
chrome.permissions.onAdded.addListener(() => { control(resumeEnable); });
// 缺权限时只开后台说明页；授权由用户主动打开扩展面板完成。
chrome.runtime.onInstalled.addListener(() => {
  control(async () => {
    if (await chrome.permissions.contains({ origins: ORIGINS })) return;
    await chrome.tabs.create({ url: chrome.runtime.getURL('popup.html'), active: false });
  });
});
// 权限事件可能早于 popup 的持久写入完成，补查写入后的真实授权。
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.pendingEnable?.newValue) control(resumeEnable);
});
chrome.tabs.onRemoved.addListener(tabId => {
  serial(async () => {
    const s = await read();
    if (s?.tabId === tabId) {
      await clearObservation();
      try { await native({ action: 'unbind', taskId: s.taskId, token: s.token }); } catch {}
      await chrome.storage.local.set({ boundTarget: null });
      return;
    }
    // worker 休眠期间观察状态已丢失，仍要用持久绑定记录解绑，避免留下死目标。
    const { boundTarget } = await chrome.storage.local.get('boundTarget');
    if (boundTarget && boundTarget.tabId === tabId) {
      try { await native({ action: 'unbind', taskId: boundTarget.taskId, token: boundTarget.token }); } catch {}
      await chrome.storage.local.set({ boundTarget: null });
      await clearObservation();
    }
  });
});
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  // popup.html 既可能以弹窗出现（无 tab），也可能被当成普通标签页打开（有 tab）。
  // 后者以前会掉进内容脚本分支，状态消息直接被丢弃，导致两种打开方式显示不一致。
  // 一律按 URL 认定是扩展自己的页面，而不是看有没有 tab。
  const fromPopup = sender.id === chrome.runtime.id && typeof sender.url === 'string' &&
    sender.url.startsWith(chrome.runtime.getURL('popup.html'));
  if (fromPopup && message?.action === 'disable') enableRevision++;
  (sender.tab && !fromPopup ? serial : control)(async () => {
    if (sender.id !== chrome.runtime.id || !message || typeof message !== 'object') throw new Error('消息来源无效');
    if (sender.tab && !fromPopup) {
      if (message.action !== 'content' || sender.frameId !== 0) return;
      const s = await read();
      if (!s || s.paused || sender.tab.id !== s.tabId || sender.documentId !== s.documentId ||
          message.navigationEpoch !== s.navigationEpoch || message.bindingId !== s.bindingId) return;
      if (!await permitted() || !await enabled()) { await clearObservation(); return; }
      const frame = await chrome.webNavigation.getFrame({ tabId: s.tabId, frameId: 0 });
      if (!frame || frame.documentId !== sender.documentId || !safeUrl(frame.url) ||
          originOf(frame.url) !== originOf(sender.url) || safeUrl(frame.url) !== safeUrl(message.url)) return;
      const next = reduce(s, { type: message.lifecycle === 'pageshow' ? 'pageshow' : 'content',
        tabId: s.tabId, frameId: 0, documentId: sender.documentId, url: frame.url,
        timeStamp: Date.now(), readiness: message.readiness });
      if (next !== s) await publish(next);
      return;
    }
    if (sender.url !== chrome.runtime.getURL('popup.html')) throw new Error('只允许 popup 操作');
    if (message.action === 'enable') {
      await completeEnable(message.nonce);
    } else if (message.action === 'cancel_enable') {
      const { pendingEnable } = await chrome.storage.local.get('pendingEnable');
      if (pendingEnable?.nonce === message.nonce) await chrome.storage.local.set({ pendingEnable: null });
    } else if (message.action === 'disable') {
      await chrome.storage.local.set({ enabled: false, pendingEnable: null, disabledAt: Date.now(),
        boundTarget: null });
      await clearObservation();
      syncLater();
    } else if (message.action !== 'status') throw new Error('不支持此操作');
    if (!await permitted()) {
      await chrome.storage.local.set({ enabled: false });
      await clearObservation();
    }
    const s = await read();
    const visible = s ? Object.fromEntries(Object.entries(s).filter(([k]) => !['token', 'bindingId', 'documentUrl'].includes(k))) : null;
    return { globalPermission: await permitted() ? 'granted' : 'permission_missing',
      enabled: await enabled(), nativeConnection: { ...connection },
      taskAssignment: assignmentStatus, state: visible };
  }).then(result => respond({ ok: true, ...result }), error => respond({ ok: false, error: error.message }));
  return true;
});
// Worker 被回收后由 alarm 唤醒；每次请求结束后才安排下一轮，无并发忙轮询。
chrome.alarms.create('observer-sync', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'observer-sync') serial(sync); });
control(resumeEnable);
serial(sync);
