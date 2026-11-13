import { initialState, reduce, safeUrl, originOf, pause } from './state.mjs';
import { BackgroundBrowser } from './background-browser.mjs';


export function startChannel(chrome) {
const { setTimeout, clearTimeout, setInterval, clearInterval } = chrome.channelTimers || globalThis;
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
let observerStatus = null;
let lifecycleError = null;
function noteLifecycleError(error) {
  if (!error?.stage || !['BROWSER_LIFECYCLE_TIMEOUT', 'BROWSER_LIFECYCLE_FAILED'].includes(error.code)) return;
  lifecycleError = { stage: error.stage, code: error.code, at: Date.now() / 1000 };
}
let commandReport = null;
let panelReport = null;
let panelBridge = false;
let applicationSession = null;
let lastCommandId = null;
let keepAlivePort = null;
let keepAliveTimer;
let activityQueue = Promise.resolve();
function recordActivity(state, operation, outcome) {
  if (!state?.taskId) return Promise.resolve();
  const job = activityQueue.then(async () => {
    const { activity = [] } = await chrome.storage.session.get('activity');
    const entry = { taskId: state.taskId, operation, outcome, site: originOf(state.url), at: Date.now() };
    await chrome.storage.session.set({ activity: [entry, ...activity].slice(0, 100) });
  });
  activityQueue = job.catch(() => {});
  return job;
}
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
      keepAlivePort.onMessage.addListener(result => {
        if (result?.code === 'APP_NOT_RUNNING' || (applicationSession &&
            result?.applicationSession && result.applicationSession !== applicationSession)) applicationStopped();
      });
      keepAlivePort.onDisconnect.addListener(() => { keepAlivePort = null; });
    } catch { keepAlivePort = null; }
  }
  // 周期性 ping 重置 worker 空闲计时，避免命令排队等到下一次 alarm。
  keepAliveTimer = setInterval(() => {
    try { keepAlivePort?.postMessage({ action: 'ping' }); } catch { keepAlivePort = null; }
  }, 1000);
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
    if (observerStatus?.state === 'failed' && observerStatus.bindingId === state?.bindingId &&
        observerStatus.documentId === state?.documentId) {
      throw new Error('BROWSER_OBSERVER_NOT_READY：原标签观察初始化失败，请先只读查询状态并通知用户；未执行命令');
    }
    const result = await background.run(command, state);
    if (command.operation === 'navigate' && result?.performed) {
      // Publish a navigation barrier before acknowledging dispatch. The next
      // command must never be queued against the outgoing document simply
      // because webNavigation callbacks are still behind this sync in queue.
      await navigation('before', { tabId: state.tabId, frameId: 0,
        url: command.params.url, timeStamp: state.lastTime });
      await reconcileObservation();
    }
    if (result?.children?.length) {
      const { managedTabs = [] } = await chrome.storage.session.get('managedTabs');
      await chrome.storage.session.set({ managedTabs: [...new Set([...managedTabs, ...result.children.map(t => t.tabId)])],
        managedTaskId: state.taskId });
    }
    if (!result) throw new Error('页面未返回结果');
    commandReport = { requestId: command.requestId, status: 'ok', result };
    connection = { ...connection, backgroundStatus: 'ready', lastActionError: null };
    await recordActivity(state, command.operation, result.performed === false ? 'not_performed' : 'completed').catch(() => {});
  } catch (error) {
    commandReport = { requestId: command.requestId, status: 'error',
      error: String(error?.message || '命令执行失败').slice(0, 200) };
    if (error?.debuggerFailure) commandReport.debuggerFailure = error.debuggerFailure;
    connection = { ...connection, backgroundStatus: 'waiting', lastActionError: commandReport.error };
    await recordActivity(state, command.operation, 'failed').catch(() => {});
  }
}
const serial = fn => {
  const job = queue.then(fn);
  queue = job.catch(noteLifecycleError);
  return job;
};
const read = async () => (await chrome.storage.session.get('observer')).observer || null;
const save = observer => chrome.storage.session.set({ observer });
const permitted = async () => !permissionRevoked && await chrome.permissions.contains({ origins: ORIGINS });
const enabled = async () => (await chrome.storage.local.get('enabled')).enabled === true;
let downloadQueue = Promise.resolve();
function recordDownload(source, method, params) {
  downloadQueue = downloadQueue.then(async () => {
    if (!Number.isInteger(source?.tabId) || !params || typeof params.guid !== 'string' || params.guid.length > 128) return;
    const state = await read();
    if (!state || state.paused || state.tabId !== source.tabId || !await enabled() || !await permitted()) return;
    const { downloads = [] } = await chrome.storage.session.get('downloads');
    if (method === 'Page.downloadWillBegin') {
      if (typeof params.frameId !== 'string' || typeof params.suggestedFilename !== 'string') return;
      const [frame, tree] = await Promise.all([
        chrome.webNavigation.getFrame({ tabId: state.tabId, frameId: 0 }),
        chrome.debugger.sendCommand({ tabId: state.tabId }, 'Page.getFrameTree', {})
      ]);
      const current = await read();
      if (frame?.documentId !== state.documentId || current?.bindingId !== state.bindingId ||
          current?.documentId !== state.documentId) return;
      const contains = node => node?.frame?.id === params.frameId || (node?.childFrames || []).some(contains);
      if (!contains(tree?.frameTree)) return;
      const item = { taskId: state.taskId, guid: params.guid,
        filename: params.suggestedFilename.slice(0, 255), state: 'started', at: Date.now() };
      await chrome.storage.session.set({ downloads: [item, ...downloads.filter(d => d.guid !== item.guid)].slice(0, 50) });
      await recordActivity(state, 'download', 'started').catch(() => {});
    } else if (['inProgress', 'completed', 'canceled'].includes(params.state)) {
      const item = downloads.find(d => d.guid === params.guid && d.taskId === state.taskId);
      if (!item) return;
      item.state = params.state;
      await chrome.storage.session.set({ downloads });
    }
  }).catch(() => {});
}
const background = new BackgroundBrowser(chrome, async s => {
  const current = await read();
  return await enabled() && await permitted() && !current?.paused && current?.bindingId === s?.bindingId &&
    current?.documentId === s?.documentId && await runtimeAvailable();
}, recordDownload, noteLifecycleError);
function applicationStopped() {
  if (applicationSession) enableRevision++;
  applicationSession = null;
  connection = { ...connection, status: 'application_stopped', lastError: 'QuietBridge 已退出，请先打开应用' };
  assignmentStatus = 'no_assignment';
  createdReport = commandReport = panelReport = null;
  panelBridge = false;
  keepAlive(false);
  control(() => clearObservation());
}
async function runtimeAvailable() {
  const session = applicationSession;
  if (!session) return false;
  try {
    if ((await native({ action: 'ping' })).applicationSession === session && applicationSession === session) return true;
  } catch {}
  applicationStopped();
  return false;
}
async function native(message) {
  let timeout;
  try {
    const result = await Promise.race([
      chrome.runtime.sendNativeMessage(HOST, message),
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('本地 host 超时')), 10000); })
    ]);
    if (!result?.ok) {
      const error = new Error(result?.error || '本地请求被拒绝');
      error.code = result?.code;
      error.nativeRejected = true;
      if (error.code === 'APP_NOT_RUNNING') applicationStopped();
      throw error;
    }
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
  const session = applicationSession;
  if (!session || !await permitted() || !await enabled()) { await clearObservation(); return; }
  await save(s);
  try {
    await native({ action: 'event', taskId: s.taskId, token: s.token, event: wire(s) });
    s.nativeError = null;
  } catch {
    s.nativeError = '未同步：本地 host 不可用或内部任务分配已失效';
  }
  if (applicationSession === session && await permitted() && await enabled()) await save(s);
  else await clearObservation();
}
async function stop(s) {
  if (s?.documentId) await background.lifecycle('observer_stop', deadline => chrome.scripting.executeScript({ target: {
    tabId: s.tabId, documentIds: [s.documentId] }, injectImmediately: true,
    func: (bindingId, deadline) => Date.now() < deadline && globalThis.__quietbridgeObserverStop?.(bindingId),
    args: [s.bindingId, deadline] }));
}
async function clearObservation(keepTask = false) {
  const s = await read();
  assignmentTitle = null;
  observerStatus = null;
  await save(null);
  const cleanup = [stop(s)];
  if (!keepTask) {
    const { managedTabs = [], managedTaskId } = await chrome.storage.session.get(['managedTabs', 'managedTaskId']);
    await chrome.storage.session.set({ managedTabs: [], managedTaskId: null });
    for (const tabId of new Set([...managedTabs, ...background.attached.keys(), ...background.guards.keys()])) {
      cleanup.push(background.release(tabId, managedTaskId || s?.taskId));
    }
  }
  for (const result of await Promise.allSettled(cleanup)) if (result.status === 'rejected') noteLifecycleError(result.reason);
}
async function suspend(s) {
  await stop(s).catch(noteLifecycleError);
  await publish(pause(s, Date.now()));
}
async function inject(s) {
  if (!s?.url || !s.documentId || s.pending || s.paused || !await enabled() || !await permitted() || !await runtimeAvailable()) return;
  const same = observerStatus?.bindingId === s.bindingId && observerStatus?.documentId === s.documentId &&
    observerStatus?.navigationEpoch === s.navigationEpoch;
  if (same && (observerStatus.state === 'ready' || observerStatus.attempt >= 2 ||
      Date.now() / 1000 - observerStatus.at < 1)) return;
  const attempt = { state: 'initializing', bindingId: s.bindingId, documentId: s.documentId,
    navigationEpoch: s.navigationEpoch, attempt: same ? observerStatus.attempt + 1 : 1,
    stage: 'observer_load', code: null, at: Date.now() / 1000 };
  observerStatus = attempt;
  const current = async () => {
    const now = await read();
    return observerStatus === attempt && now?.bindingId === s.bindingId && now?.documentId === s.documentId &&
      now?.navigationEpoch === s.navigationEpoch && !now?.paused && await enabled() && await permitted();
  };
  try {
    await background.lifecycle('observer_load', () => chrome.scripting.executeScript({ target: { tabId: s.tabId,
      documentIds: [s.documentId] }, files: ['content.js'], injectImmediately: true }), 3000);
    if (!await current() || !await runtimeAvailable()) return;
    attempt.stage = 'observer_configure';
    const replies = await background.lifecycle('observer_configure', deadline => chrome.scripting.executeScript({ target: { tabId: s.tabId,
      documentIds: [s.documentId] }, injectImmediately: true, func: config => globalThis.__quietbridgeObserver?.(config),
      args: [{ navigationEpoch: s.navigationEpoch, bindingId: s.bindingId, deadline,
        authorizedOrigin: originOf(s.url), selector: s.selector }] }), 3000);
    if (!await current()) return;
    if (!replies?.some(reply => reply.result === true && (!reply.documentId || reply.documentId === s.documentId))) {
      const error = new Error('观察脚本未确认初始化');
      error.code = 'BROWSER_LIFECYCLE_FAILED'; error.stage = 'observer_configure';
      throw error;
    }
    attempt.state = 'ready';
    attempt.at = Date.now() / 1000;
  } catch (error) {
    if (await current()) {
      noteLifecycleError(error);
      attempt.state = 'failed'; attempt.code = error.code; attempt.stage = error.stage;
      attempt.at = Date.now() / 1000;
    }
  }
}
async function navigation(type, d) {
  if (d.frameId !== 0) return;
  const old = await read();
  if (!old || old.tabId !== d.tabId || old.paused) return;
  if (!await permitted() || !await enabled()) { await clearObservation(); return; }
  if (type === 'committed') {
    const frame = await background.lifecycle('document_lookup', () => chrome.webNavigation.getFrame({ tabId: d.tabId, frameId: 0 }));
    if (!frame || frame.documentId !== d.documentId || safeUrl(frame.url) !== safeUrl(d.url)) return;
  }
  const next = reduce(old, { ...d, type });
  if (next !== old && type === 'before') {
    next.previousDocumentId = old.documentId || old.previousDocumentId;
    next.previousDocumentUrl = old.pending ? old.previousDocumentUrl : old.documentUrl;
  }
  if (next !== old && type === 'committed') {
    delete next.previousDocumentId;
    delete next.previousDocumentUrl;
  }
  if (next !== old) next.documentUrl = d.url;
  if (next === old) return;
  if (next.paused) await stop(old).catch(noteLifecycleError);
  await publish(next);
  if (['committed', 'dom', 'completed', 'history'].includes(type)) await inject(next);
}
async function reconcileObservation() {
  const s = await read();
  if (!s || s.paused || !await enabled() || !await permitted() || !await runtimeAvailable()) return;
  const [tab, frame] = await background.lifecycle('document_lookup', () => Promise.all([chrome.tabs.get(s.tabId),
    chrome.webNavigation.getFrame({ tabId: s.tabId, frameId: 0 })]));
  const current = await read();
  if (current?.bindingId !== s.bindingId || current?.paused) return;
  if (tab.windowId !== s.browserWindowId || !safeUrl(tab.url) || !safeUrl(frame?.url)) {
    await suspend(s);
    return;
  }
  // Reconcile only Chrome's current, fixed top-level document. Never migrate
  // an already queued command, follow a moved tab or adopt a pending URL.
  if (!frame?.documentId || tab.url !== frame.url || (tab.pendingUrl && tab.pendingUrl !== frame.url)) return;
  const timeStamp = Date.now();
  if (s.pending && frame.documentId === s.previousDocumentId && frame.url === s.previousDocumentUrl) return;
  if (s.documentId !== frame.documentId) {
    if (!s.pending) await navigation('before', { tabId: s.tabId, frameId: 0, url: frame.url, timeStamp });
    await navigation('committed', { ...frame, tabId: s.tabId, frameId: 0, timeStamp });
  } else if (s.documentUrl !== frame.url) {
    await navigation('history', { ...frame, tabId: s.tabId, frameId: 0, timeStamp });
  } else if (s.nativeError) {
    await publish({ ...s, sequence: s.sequence + 1 });
  }
  await inject(await read());
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
    await reconcileObservation();
    return;
  }
  await clearObservation(old?.taskId === a.taskId);
  const tab = await background.lifecycle('assignment_lookup', () => chrome.tabs.get(a.tabId));
  if (tab.windowId !== a.browserWindowId || !safeUrl(tab.url)) {
    assignmentStatus = 'needs_target_selection';
    return;
  }
  if (a.paused) { assignmentStatus = 'paused'; return; }
  const s = initialState({ taskId: a.taskId, token: a.token, bindingId: a.bindingId,
    assignmentVersion: a.assignmentVersion, globalOrigins: true,
    authorizedOrigin: originOf(tab.url), selector: '', tabId: a.tabId, browserWindowId: a.browserWindowId,
    ownedTabCount: (a.ownedTabs || []).length || 1 });
  assignmentTitle = typeof tab.title === 'string' ? tab.title.replace(/[\u0000-\u001f]/gu, ' ').trim().slice(0, 200) : null;
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
  const frame = await background.lifecycle('document_lookup', () => chrome.webNavigation.getFrame({ tabId: a.tabId, frameId: 0 }));
  if (frame?.documentId) await navigation('committed', { ...frame, frameId: 0,
    tabId: a.tabId, timeStamp: Date.now() });
  else await publish(s);
}
function schedule() {
  clearTimeout(timer);
  timer = setTimeout(() => { serial(sync); }, 1000);
}
async function finishCreatedTarget(request, result) {
  const { lastCreatedTarget, managedTabs = [] } = await chrome.storage.session.get(['lastCreatedTarget', 'managedTabs']);
  if (result.navigationState) return result;
  const complete = async (navigationState, error) => {
    const report = { ...result, navigationState };
    if (error) report.navigationError = String(error.message || error).slice(0, 200);
    const latest = (await chrome.storage.session.get('lastCreatedTarget')).lastCreatedTarget;
    await chrome.storage.session.set({ lastCreatedTarget: { ...latest, result: report } });
    return report;
  };
  if (!request.url) return complete('not_requested');
  if (lastCreatedTarget?.navigationIssued) return complete('unknown', '导航已下发但结果未知，未重放');
  const session = applicationSession;
  const check = async () => {
    if (!session || applicationSession !== session || !await enabled() || !await permitted() ||
        !await runtimeAvailable() || applicationSession !== session) throw new Error('应用已退出或授权已撤销，新窗口保持空白');
  };
  let issued = false;
  try {
    await chrome.storage.session.set({ managedTabs: [...new Set([...managedTabs, result.tabId])], managedTaskId: request.taskId });
    await check();
    await background.protectCreatedTab(result.tabId, result.browserWindowId, request.taskId, check);
    await check();
    // Persist before dispatch: an unknown navigation result must never be replayed.
    await chrome.storage.session.set({ lastCreatedTarget: { ...lastCreatedTarget, navigationIssued: true } });
    issued = true;
    await check();
    await chrome.tabs.update(result.tabId, { url: request.url });
    return await complete('issued');
  } catch (error) {
    await background.release(result.tabId, request.taskId).catch(noteLifecycleError);
    return complete(issued ? 'unknown' : 'failed', error);
  }
}
async function createWindow(request) {
  if (!request || typeof request.requestId !== 'string' || !request.requestId ||
      lastCreateRequestId === request.requestId) return false;
  const { lastCreatedTarget } = await chrome.storage.session.get('lastCreatedTarget');
  lastCreateRequestId = request.requestId;
  if (lastCreatedTarget?.requestId === request.requestId) {
    if (lastCreatedTarget.result) {
      createdReport = await finishCreatedTarget(request, lastCreatedTarget.result);
      return true;
    }
    throw new Error('窗口创建已下发但结果未知，未重复建窗');
  }
  // 先登记再建窗：worker 重启或回传失败不能产生第二个窗口。
  await chrome.storage.session.set({ lastCreatedTarget: {
    requestId: request.requestId, at: Date.now()
  } });
  const options = { focused: false, url: 'about:blank' };
  if (!await enabled() || !await permitted() || !await runtimeAvailable()) throw new Error('应用已退出或授权已撤销，未创建窗口');
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
  createdReport = await finishCreatedTarget(request, result);
  return true;
}
async function windowEvidence() {
  const s = await read();
  if (!s || s.paused) return null;
  // Chrome's native window title follows its active tab, even when our task
  // deliberately owns a different, inactive tab. Reading metadata never
  // selects a tab or changes the user's focused window.
  try {
    const [assigned, tabs] = await background.lifecycle('window_evidence', () => Promise.all([
      chrome.tabs.get(s.tabId), chrome.tabs.query({ windowId: s.browserWindowId, active: true })]));
    if (assigned.windowId !== s.browserWindowId || !safeUrl(assigned.url)) return null;
    const tab = tabs.length === 1 ? tabs[0] : null;
    if (!tab || tab.windowId !== s.browserWindowId || !Number.isInteger(tab.id) ||
        !safeUrl(tab.url) || typeof tab.title !== 'string' || !tab.title.trim() ||
        /[\u0000-\u001f]/u.test(tab.title)) return null;
    const current = await read();
    if (current?.bindingId !== s.bindingId || current?.paused) return null;
    const evidence = { bindingId: s.bindingId, browserWindowId: s.browserWindowId,
      tabId: tab.id, title: tab.title.slice(0, 200) };
    // Geometry is already available as metadata; it disambiguates equal page
    // titles without navigating, retitling, moving or activating any window.
    if (chrome.windows?.get) {
      try {
        const win = await background.lifecycle('window_evidence', () => chrome.windows.get(s.browserWindowId));
        const bounds = { x: win.left, y: win.top, width: win.width, height: win.height };
        if (win.id === s.browserWindowId && Object.values(bounds).every(Number.isFinite) &&
            bounds.width > 0 && bounds.height > 0) evidence.bounds = bounds;
      } catch {} // Title evidence remains usable when geometry is unavailable.
    }
    const final = await read();
    return final?.bindingId === s.bindingId && !final.paused ? evidence : null;
  } catch (error) { noteLifecycleError(error); return null; }
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
    const payload = { action: 'sync', enabled: on, permission, applicationSession, clientVersion: '0.4.14', capabilities: ['cdp_input', 'document_binding', 'background_popups', 'paged_read', 'render_probe'],
      windowEvidence: on && permission ? await windowEvidence() : null,
      observerStatus, lifecycleError };
    if (panelBridge) {
      // A command screenshot and a review image can each be large; send them on separate turns.
      if (!commandReport?.result?.data) payload.panelState = await panelSnapshot();
      if (panelReport) payload.panelResult = panelReport;
    }
    if (assignmentTitle) payload.assignmentTitle = assignmentTitle;
    if (createdReport) payload.created = createdReport;
    if (commandReport) payload.commandResult = commandReport;
    let r = await native(payload);
    panelBridge = r.panelBridge === true;
    // host 接受后才清除待回传证据；抛错时保留到下一轮。
    if (payload.created) createdReport = null;
    if (payload.commandResult) commandReport = null;
    if (r.commandAck?.status === 'rejected') {
      connection = { ...connection, backgroundStatus: 'waiting', lastActionError: r.commandAck.code };
    }
    if (payload.panelResult) panelReport = null;
    if (revision !== enableRevision) return;
    if (r.protocolVersion !== 2 || !Number.isFinite(r.heartbeatAt)) throw new Error('host 协议不兼容');
    if (typeof r.applicationSession !== 'string' || !r.applicationSession) throw new Error('请更新 QuietBridge 本地应用');
    if (r.sessionReset || (applicationSession && applicationSession !== r.applicationSession)) {
      createdReport = commandReport = panelReport = null;
      await clearObservation();
    }
    applicationSession = r.applicationSession;
    connection = { ...connection, status: 'connected', lastHeartbeatAt: r.heartbeatAt * 1000 };
    delete connection.lastError;
    delete connection.errorCode;
    if (r.requestCandidates && on && permission) {
      // 仅原生可信端请求时返回候选元数据，不观察候选页面内容，不按焦点筛选。
      const tabs = await background.lifecycle('candidate_lookup', () => chrome.tabs.query({}));
      const candidates = tabs.map(t => ({ tabId: t.id, browserWindowId: t.windowId, url: safeUrl(t.url) }))
        .filter(t => t.url && Number.isInteger(t.tabId) && Number.isInteger(t.browserWindowId)).slice(0, 1000);
      if (!await permitted() || !await enabled()) { await clearObservation(); return; }
      r = await native({ action: 'sync', enabled: on, permission, applicationSession, candidates });
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
    if (r.panelCommand && on && permission && await enabled() && await permitted()) {
      await runPanelCommand(r.panelCommand);
      serial(sync);
    }
  } catch (error) {
    if (error?.stage) {
      noteLifecycleError(error);
      // This is a local lifecycle failure after/before an actual host exchange.
      // Preserve the last transport result and assignment; do not report it as
      // a host disconnect or silently restart/rebind the task.
      return;
    }
    applicationSession = null;
    keepAlive(false);
    connection = { ...connection, status: error?.code === 'APP_NOT_RUNNING' ? 'application_stopped' :
      error?.nativeRejected ? 'request_rejected' : 'host_unavailable',
      errorCode: error?.code || null,
      lastError: String(error?.message || '本地连接失败').slice(0, 500) };
    assignmentStatus = 'no_assignment';
    await clearObservation();
  } finally { schedule(); }
}

async function panelSnapshot() {
  const s = await read();
  const { activity = [], review = null, downloads = [], recentTask = null } =
    await chrome.storage.session.get(['activity', 'review', 'downloads', 'recentTask']);
  const taskSummary = s ? { taskId: s.taskId, url: s.url, canStop: true,
    assignmentVersion: s.assignmentVersion, documentId: s.documentId || null } :
    assignmentStatus === 'paused' && recentTask ? { ...recentTask, canStop: false } : null;
  return { taskSummary, activity: taskSummary ? activity.filter(item => item.taskId === taskSummary.taskId) : [],
    downloads: taskSummary ? downloads.filter(item => item.taskId === taskSummary.taskId) : [],
    review: taskSummary && review?.taskId === taskSummary.taskId ? review : null };
}

async function runPanelCommand(command) {
  const { lastPanelCommand } = await chrome.storage.session.get('lastPanelCommand');
  if (lastPanelCommand?.requestId === command.requestId) {
    panelReport = { requestId: command.requestId, status: 'error', error: '命令已下发但结果未知，未重复执行' };
    return;
  }
  await chrome.storage.session.set({ lastPanelCommand: { requestId: command.requestId, at: Date.now() } });
  try {
    await control(async () => {
      if (!await runtimeAvailable()) throw new Error('QuietBridge 已退出，请先打开应用');
      const current = await panelSnapshot();
      if (current.taskSummary?.taskId !== command.taskId || command.expiresAt * 1000 <= Date.now()) throw new Error('任务已变化或请求已过期');
      if (command.assignmentVersion != null && (current.taskSummary.assignmentVersion !== command.assignmentVersion ||
          current.taskSummary.documentId !== command.documentId)) throw new Error('任务页面已变化，请刷新后重试');
      await performPanelAction({ ...command.arguments, action: command.operation });
    });
    panelReport = { requestId: command.requestId, status: 'ok', error: '' };
  } catch (error) {
    panelReport = { requestId: command.requestId, status: 'error', error: String(error?.message || '操作失败').slice(0, 200) };
  }
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
async function performPanelAction(message) {
  if (message.action === 'stop_task') {
    const target = await read();
    if (!target?.taskId || !target.token) throw new Error('当前没有可停止的浏览器任务');
    await chrome.storage.session.set({ recentTask: { taskId: target.taskId, url: target.url } });
    await clearObservation();
    try { await native({ action: 'pause', taskId: target.taskId, token: target.token }); }
    catch (error) {
      await chrome.storage.local.set({ enabled: false, pendingEnable: null, disabledAt: Date.now() });
      throw new Error(`未能确认任务暂停，观察已停用：${error.message}`);
    }
    await chrome.storage.local.set({ boundTarget: null });
    assignmentStatus = 'paused';
    syncLater();
  } else if (message.action === 'clear_activity') {
    const clear = activityQueue.then(() => chrome.storage.session.set({ activity: [] }));
    activityQueue = clear.catch(() => {});
    await clear;
  } else if (message.action === 'capture_review') {
    const target = await read();
    if (!target?.taskId || !target.documentId) throw new Error('当前没有可截图的任务页面');
    const command = { operation: 'screenshot', taskId: target.taskId, bindingId: target.bindingId,
      assignmentVersion: target.assignmentVersion, tabId: target.tabId, browserWindowId: target.browserWindowId,
      documentId: target.documentId, navigationEpoch: target.navigationEpoch, expiresAt: Date.now() / 1000 + 8 };
    const image = await background.run(command, target);
    await chrome.storage.session.set({ review: { taskId: target.taskId, documentId: target.documentId,
      image: image.data, capturedAt: Date.now(), annotations: [] } });
    await recordActivity(target, 'screenshot', 'completed').catch(() => {});
  } else if (message.action === 'annotate_review' || message.action === 'remove_review_annotation') {
    let target = await read();
    const { review, recentTask } = await chrome.storage.session.get(['review', 'recentTask']);
    if (!target && assignmentStatus === 'paused') target = recentTask;
    if (!target?.taskId || review?.taskId !== target.taskId) throw new Error('截图所属任务已变化');
    if (message.action === 'annotate_review') {
      const box = message.box;
      if (typeof message.note !== 'string' || !message.note.trim() || message.note.length > 200 || !box ||
          !['x', 'y', 'width', 'height'].every(k => typeof box[k] === 'number' && Number.isFinite(box[k])) ||
          box.x < 0 || box.y < 0 || box.width <= 0 || box.height <= 0 ||
          box.x + box.width > 1.000001 || box.y + box.height > 1.000001) throw new Error('批注内容或选区无效');
      review.annotations = [...review.annotations, { box, note: message.note.trim() }].slice(-20);
    } else {
      if (!Number.isInteger(message.index) || message.index < 0 || message.index >= review.annotations.length) throw new Error('批注不存在');
      review.annotations.splice(message.index, 1);
    }
    await chrome.storage.session.set({ review });
  } else throw new Error('不支持此操作');
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  // popup.html 既可能以弹窗出现（无 tab），也可能被当成普通标签页打开（有 tab）。
  // 后者以前会掉进内容脚本分支，状态消息直接被丢弃，导致两种打开方式显示不一致。
  // 一律按 URL 认定是扩展自己的页面，而不是看有没有 tab。
  const fromPopup = sender.id === chrome.runtime.id && typeof sender.url === 'string' &&
    sender.url.startsWith(chrome.runtime.getURL('popup.html'));
  if (fromPopup && ['disable', 'stop_task'].includes(message?.action)) enableRevision++;
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
    } else if (['stop_task', 'clear_activity', 'capture_review', 'annotate_review', 'remove_review_annotation'].includes(message.action)) {
      await performPanelAction(message);
    } else if (message.action !== 'status') throw new Error('不支持此操作');
    if (!await permitted()) {
      await chrome.storage.local.set({ enabled: false });
      await clearObservation();
    }
    const s = await read();
    const visible = s ? Object.fromEntries(Object.entries(s).filter(([k]) => !['token', 'bindingId', 'documentUrl', 'previousDocumentUrl'].includes(k))) : null;
    return { globalPermission: await permitted() ? 'granted' : 'permission_missing',
      enabled: await enabled(), nativeConnection: { ...connection },
      taskAssignment: assignmentStatus, observerStatus, lifecycleError, state: visible };
  }).then(result => respond({ ok: true, ...result }), error => respond({ ok: false, error: error.message }));
  return true;
});
// Worker 被回收后由 alarm 唤醒；每次请求结束后才安排下一轮，无并发忙轮询。
chrome.alarms.create('observer-sync', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'observer-sync') serial(sync); });
control(resumeEnable);
serial(sync);

}
