import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { initialState, reduce, safeUrl, originOf, pause } from './state.mjs';
import { BackgroundBrowser } from './background-browser.mjs';

const listener = () => ({ addListener(fn) { this.fn = fn; } });
const code = (await readFile(new URL('./service-worker.js', import.meta.url), 'utf8')).replace(/^import .*;\n/gm, '');
test('真实 worker 入口：握手、默认无任务、内部派发、隔离、跨站、撤权、重连与重启', async () => {
  let stored = null;
  let on = false;
  const local = {};
  let permission = true;
  let available = true;
  let assignment = null;
  let requestCandidates = false;
  let blockSync = null;
  let frame = { documentId: 'doc-1', url: 'https://example.org/latest?secret=x' };
  const calls = [], scripts = [], timers = new Map();
  const extraSession = {};
  let timerId = 0;
  let context;
  let chrome;
  function boot() {
    const nav = Object.fromEntries(['onBeforeNavigate', 'onCommitted', 'onDOMContentLoaded',
      'onCompleted', 'onErrorOccurred', 'onHistoryStateUpdated'].map(k => [k, listener()]));
    chrome = {
      storage: {
        onChanged: listener(),
        session: { async get() { return { ...extraSession, observer: structuredClone(stored) }; },
          async set(values) { if ('observer' in values) stored = structuredClone(values.observer);
            for (const [key, value] of Object.entries(values)) if (key !== 'observer') extraSession[key] = value; } },
        local: { async get() { return { ...local, enabled: on }; }, async set(v) {
          Object.assign(local, structuredClone(v));
          if ('enabled' in v) on = v.enabled;
        } }
      },
      permissions: { async contains(o) { assert.deepEqual(Array.from(o.origins), ['http://*/*', 'https://*/*']); return permission; },
        onRemoved: listener(), onAdded: listener() },
      runtime: { id: 'extension', getURL: p => `chrome-extension://extension/${p}`, onMessage: listener(),
        onInstalled: listener(),
        async sendNativeMessage(host, m) {
          assert.equal(host, 'com.cua.browser_observer');
          calls.push(structuredClone(m));
          if (!available) throw new Error('离线');
          if (m.action === 'sync') {
            if (blockSync) { const wait = blockSync; blockSync = null; await wait; }
            if (!m.enabled || !m.permission) assignment = null;
            if (m.candidates) requestCandidates = false;
            return { ok: true, protocolVersion: 2, heartbeatAt: Date.now() / 1000, requestCandidates,
              taskAssignment: assignment ? 'assigned' : 'no_assignment', assignment: structuredClone(assignment) };
          }
          if (m.action === 'event') {
            assert.equal(m.taskId, assignment.taskId);
            assert.equal(m.token, assignment.token);
            assignment.event = structuredClone(m.event);
          }
          if (m.action === 'unbind') assignment = null;
          return { ok: true };
        } },
      alarms: { create() {}, onAlarm: listener() },
      webNavigation: { ...nav, async getFrame() { return frame; } },
      tabs: { onRemoved: listener(), opened: [],
        async create(o) { this.opened.push(o); return { id: 99, ...o }; },
        async get(id) { return { id, windowId: 2, url: frame.url }; },
        async query(q) { assert.equal(Object.keys(q).length, 0); return [
          { id: 7, windowId: 2, url: frame.url }, { id: 8, windowId: 3, url: 'chrome://settings' }]; } },
      scripting: { async executeScript(o) { scripts.push(o); } }
    };
    context = vm.createContext({ chrome, BackgroundBrowser, initialState, reduce, safeUrl, originOf, pause, URL, Date,
      setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
      clearTimeout(id) { timers.delete(id); }, setInterval() { return -1; }, clearInterval() {} });
    vm.runInContext(code, context);
  }
  const popup = { id: 'extension', url: 'chrome-extension://extension/popup.html' };
  const send = async (m, sender = popup) => {
    if (m.action === 'status') await vm.runInContext('queue', context);
    const result = await new Promise(resolve => chrome.runtime.onMessage.fn(m, sender, resolve));
    if (['enable', 'disable'].includes(m.action)) await vm.runInContext('queue', context);
    return result;
  };
  const poll = async () => { chrome.alarms.onAlarm.fn({ name: 'observer-sync' }); return send({ action: 'status' }); };
  const makeAssignment = (version = 1) => ({ taskId: 'task', token: 'b'.repeat(64), bindingId: String(version).repeat(32),
    assignmentVersion: version, globalOrigins: true, selector: '', tabId: 7, browserWindowId: 2, mappingVerified: false });
  boot();
  let status = await send({ action: 'status' });
  assert.equal(status.nativeConnection.status, 'connected');
  assert.equal(status.state, null);
  assert.equal(on, false);
  assert.equal((await send({ action: 'bind', taskId: 'user' })).ok, false);
  await send({ action: 'enable' });
  assert.equal(on, true);
  assert.equal(stored, null);
  assert.equal(calls.some(m => m.candidates), false);
  requestCandidates = true;
  await poll();
  const candidates = calls.find(m => m.candidates).candidates;
  assert.equal(candidates.length, 1);
  assert.deepEqual(Object.keys(candidates[0]).sort(), ['browserWindowId', 'tabId', 'url']);
  assert.equal(candidates[0].url, 'https://example.org/latest');
  assignment = makeAssignment();
  await poll();
  assert.equal(stored.documentId, 'doc-1');
  assert.equal(stored.mappingVerified, false);
  status = await send({ action: 'status' });
  assert.equal('token' in status.state, false);
  const sender = { id: 'extension', tab: { id: 7 }, frameId: 0, documentId: 'doc-1', url: frame.url };
  const report = { action: 'content', navigationEpoch: stored.navigationEpoch, bindingId: stored.bindingId,
    url: safeUrl(frame.url), readiness: { readyState: 'complete', bodyPresent: true,
      bodyVisible: true, elementCount: 5, conditionMatched: null } };
  await send(report, sender);
  assert.equal(stored.elementCount, 5);
  const original = structuredClone(stored);
  await send({ ...report, bindingId: 'old' }, sender);
  await send(report, { ...sender, documentId: 'old' });
  await send(report, { ...sender, tab: { id: 8 } });
  assert.deepEqual(stored, original);
  const nextUrl = 'http://other.example:8080/path?secret=x';
  chrome.webNavigation.onBeforeNavigate.fn({ tabId: 7, frameId: 0, url: nextUrl, timeStamp: Date.now() });
  await send({ action: 'status' });
  assert.equal(stored.paused, false);
  assert.equal(stored.elementCount, 0);
  frame = { documentId: 'doc-2', url: nextUrl };
  chrome.webNavigation.onCommitted.fn({ ...frame, tabId: 7, frameId: 0, timeStamp: Date.now() });
  await send({ action: 'status' });
  assert.equal(stored.url, 'http://other.example:8080/path');
  assert.equal(stored.documentId, 'doc-2');
  const sequence = stored.sequence;
  stored = null;
  boot();
  await send({ action: 'status' });
  assert.ok(stored.sequence > sequence);
  assert.equal(stored.documentId, 'doc-2');
  available = false;
  status = await poll();
  assert.equal(status.nativeConnection.status, 'host_unavailable');
  assert.equal(stored, null);
  available = true;
  await poll();
  assert.equal(stored.documentId, 'doc-2');
  assignment = makeAssignment(2);
  await poll();
  const replaced = structuredClone(stored);
  await send(report, sender);
  assert.deepEqual(stored, replaced);
  let release;
  blockSync = new Promise(resolve => { release = resolve; });
  const blockedPoll = poll();
  await new Promise(resolve => setImmediate(resolve));
  permission = false;
  chrome.permissions.onRemoved.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stored, null, 'host 请求未结束时撤权仍立即清空');
  assert.equal(on, false);
  release();
  await blockedPoll;
  status = await send({ action: 'status' });
  assert.equal(stored, null);
  assert.equal(on, false);
  assert.equal(status.globalPermission, 'permission_missing');
  assert.equal(assignment, null);
  permission = true;
  await send({ action: 'enable' });
  assignment = makeAssignment(3);
  await poll();
  chrome.webNavigation.onBeforeNavigate.fn({ tabId: 7, frameId: 0, url: 'chrome://settings', timeStamp: Date.now() });
  await send({ action: 'status' });
  assert.equal(stored.paused, true);
  assert.equal(stored.url, null);
  await send({ action: 'disable' });
  assert.equal(stored, null);
  assert.ok([...timers.values()].every(t => t.ms === 5000));
  // 网页无法触发 chrome.permissions.request；能自动化的只有「把授权页送到用户眼前」。
  chrome.tabs.opened.length = 0;
  permission = false;
  chrome.runtime.onInstalled.fn({ reason: 'install' });
  await vm.runInContext('controls', context);
  assert.deepEqual(chrome.tabs.opened.map(o => o.url), ['chrome-extension://extension/popup.html']);
  assert.equal(chrome.tabs.opened[0].active, false);
  // 已授权时不得打扰日常重载。
  chrome.tabs.opened.length = 0;
  permission = true;
  chrome.runtime.onInstalled.fn({ reason: 'update' });
  await vm.runInContext('controls', context);
  assert.equal(chrome.tabs.opened.length, 0);
  // 同一个 popup.html 被当成标签页打开时必须拿到与弹窗相同的状态，
  // 不能被当成内容脚本丢消息（两处显示不一致的根因）。
  const asTab = { id: 'extension', url: 'chrome-extension://extension/popup.html', tab: { id: 42 }, frameId: 0 };
  const fromTab = await new Promise(resolve => chrome.runtime.onMessage.fn({ action: 'status' }, asTab, resolve));
  await vm.runInContext('controls', context);
  assert.equal(typeof fromTab.globalPermission, 'string');
  assert.equal(fromTab.nativeConnection.status, 'connected');
  assert.equal(fromTab.taskAssignment, 'no_assignment');
});

async function lifecycleHarness() {
  const local = { enabled: false };
  let permission = false;
  let state = null;
  const session = {};
  const ports = [];
  let nativeHandler = async () => ({ ok: true, protocolVersion: 2, heartbeatAt: 1, taskAssignment: 'no_assignment' });
  const calls = [];
  const nav = Object.fromEntries(['onBeforeNavigate', 'onCommitted', 'onDOMContentLoaded',
    'onCompleted', 'onErrorOccurred', 'onHistoryStateUpdated'].map(k => [k, listener()]));
  const chrome = {
    storage: { onChanged: listener(), local: {
      async get() { return structuredClone(local); },
      async set(values) { Object.assign(local, structuredClone(values)); }
    }, session: { async get(key) {
      if (key === 'observer') return { observer: structuredClone(state) };
      if (typeof key === 'string') return { [key]: structuredClone(session[key]) };
      return structuredClone(session);
    }, async set(values) {
      if ('observer' in values) state = structuredClone(values.observer);
      for (const [key, value] of Object.entries(values)) {
        if (key !== 'observer') session[key] = structuredClone(value);
      }
    } } },
    permissions: { async contains() { return permission; }, onAdded: listener(), onRemoved: listener() },
    runtime: { id: 'extension', getURL: p => `chrome-extension://extension/${p}`, onMessage: listener(),
      onInstalled: listener(),
      connectNative(host) {
        ports.push(host);
        return { postMessage() {}, disconnect() {},
          onMessage: listener(), onDisconnect: listener() };
      },
      async sendNativeMessage(host, message) {
        calls.push(structuredClone(message));
        const reply = await nativeHandler(message);
        if (reply.command && reply.assignment) {
          const a = reply.assignment;
          reply.command = { bindingId: a.bindingId, assignmentVersion: a.assignmentVersion,
            tabId: a.tabId, browserWindowId: a.browserWindowId, documentId: 'doc-1',
            navigationEpoch: state?.navigationEpoch ?? 1, expiresAt: Date.now() / 1000 + 30, ...reply.command };
        }
        return reply;
      } },
    webNavigation: { ...nav, async getFrame() {
      return { documentId: 'doc-1', frameId: 0, url: 'https://example.org/path' };
    } },
    tabs: { onRemoved: listener(),
      async get(id) { return { id, windowId: 2, url: 'https://example.org/path' }; },
      async update(id, props) { return { id, windowId: 2, url: props.url }; } },
    debugger: { onDetach: listener(), async attach() {}, async detach() {}, async sendCommand() { return { identifier: 'script-1' }; } },
    scripting: { async executeScript() {} },
    alarms: { create() {}, onAlarm: listener() }
  };
  let context;
  const boot = () => {
    context = vm.createContext({ chrome, BackgroundBrowser, initialState, reduce, safeUrl, originOf, pause, URL, Date,
      setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 2; }, clearInterval() {} });
    vm.runInContext(code, context);
  };
  boot();
  const flush = async () => {
    await vm.runInContext('controls', context);
    await vm.runInContext('queue', context);
    await vm.runInContext('controls', context);
  };
  await flush();
  return { local, chrome, calls, flush, async restart() { boot(); await flush(); },
    get state() { return state; },
    get ports() { return ports; },
    set permission(value) { permission = value; },
    set native(value) { nativeHandler = value; },
    intent(nonce = 'first', issuedAt = Date.now()) {
      local.pendingEnable = { nonce, issuedAt, expiresAt: issuedAt + 60000 };
      return local.pendingEnable;
    },
    send(message) { return new Promise(resolve => chrome.runtime.onMessage.fn(message,
      { id: 'extension', url: 'chrome-extension://extension/popup.html' }, resolve)); }
  };
}

test('popup 销毁后仅持久用户意图与完整授权共同触发启用并消费 nonce', async () => {
  const h = await lifecycleHarness();
  h.intent();
  h.permission = true;
  h.chrome.permissions.onAdded.fn({ origins: ['http://*/*', 'https://*/*'] });
  await h.flush();
  assert.equal(h.local.enabled, true);
  assert.equal(h.local.pendingEnable, null);
  await h.send({ action: 'disable' });
  await h.flush();
  h.chrome.permissions.onAdded.fn({ origins: ['https://example.org/*'] });
  await h.flush();
  assert.equal(h.local.enabled, false, '任意权限添加不能重新启用');
});

test('onAdded 早于意图写入时由 storage 事件补查，不丢失首次启用', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  h.chrome.permissions.onAdded.fn({});
  await h.flush();
  assert.equal(h.local.enabled, false);
  const intent = h.intent();
  h.chrome.storage.onChanged.fn({ pendingEnable: { newValue: intent } }, 'local');
  await h.flush();
  assert.equal(h.local.enabled, true);
});

test('拒绝及不完整授权不启用，取消仅清除匹配 nonce', async () => {
  const h = await lifecycleHarness();
  h.intent();
  h.chrome.permissions.onAdded.fn({ origins: ['https://example.org/*'] });
  await h.flush();
  assert.equal(h.local.enabled, false);
  await h.send({ action: 'cancel_enable', nonce: 'old' });
  assert.equal(h.local.pendingEnable.nonce, 'first');
  await h.send({ action: 'cancel_enable', nonce: 'first' });
  assert.equal(h.local.pendingEnable, null);
  h.permission = true;
  h.chrome.permissions.onAdded.fn({});
  await h.flush();
  assert.equal(h.local.enabled, false);
});

test('显式停用优先于 onAdded 和迟到的旧 enable，旧意图写入不能复活', async () => {
  const h = await lifecycleHarness();
  const old = structuredClone(h.intent());
  h.permission = true;
  h.chrome.permissions.onAdded.fn({});
  const disabling = h.send({ action: 'disable' });
  await disabling;
  await h.flush();
  assert.equal(h.local.enabled, false);
  await h.send({ action: 'enable', nonce: old.nonce });
  assert.equal(h.local.enabled, false);
  h.local.pendingEnable = old;
  h.chrome.storage.onChanged.fn({ pendingEnable: { newValue: old } }, 'local');
  await h.flush();
  assert.equal(h.local.enabled, false);
  assert.equal(h.local.pendingEnable, null);
});

test('过期与不匹配意图不能启用；已有授权一次 enable 直达', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  h.local.pendingEnable = { issuedAt: Date.now(), expiresAt: Date.now() + 60000 };
  h.chrome.permissions.onAdded.fn({});
  await h.flush();
  assert.equal(h.local.enabled, false, '缺少 nonce 的意图不可自动启用');
  h.intent('expired', Date.now() - 61000);
  h.chrome.permissions.onAdded.fn({});
  await h.flush();
  assert.equal(h.local.enabled, false);
  h.intent('new');
  await h.send({ action: 'enable', nonce: 'old' });
  assert.equal(h.local.enabled, false);
  await h.send({ action: 'enable', nonce: 'new' });
  assert.equal(h.local.enabled, true);
  await h.flush();
  await h.send({ action: 'disable' });
  await h.flush();
  h.intent('stale', Date.now() - 61000);
  const result = await h.send({ action: 'enable' });
  assert.equal(result.enabled, true, '已有授权的新点击不受旧意图阻塞');
  await h.flush();
});

test('native 阻塞不阻塞 UI 启用状态，失败仍 enabled 且展示 lastError，成功清错', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  h.native = async () => { await blocked; throw new Error('离线测试'); };
  const result = await h.send({ action: 'enable' });
  assert.equal(result.enabled, true);
  assert.equal(result.nativeConnection.status, 'syncing');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await h.send({ action: 'status' })).enabled, true);
  release();
  await h.flush();
  const failed = await h.send({ action: 'status' });
  assert.equal(failed.enabled, true);
  assert.equal(failed.nativeConnection.status, 'host_unavailable');
  assert.equal(failed.nativeConnection.lastError, '离线测试');
  h.native = async () => ({ ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'no_assignment' });
  h.chrome.alarms.onAlarm.fn({ name: 'observer-sync' });
  await h.flush();
  const recovered = await h.send({ action: 'status' });
  assert.equal(recovered.nativeConnection.status, 'connected');
  assert.equal(recovered.nativeConnection.lastError, undefined);
});

test('内部创建请求只执行一次，创建证据只回传一次', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  const created = [], navigated = [], protection = [];
  h.chrome.tabs.get = async id => ({ id, windowId: 41, url: 'about:blank' });
  h.chrome.tabs.update = async (id, options) => { navigated.push({ id, options }); return { id, windowId: 41, ...options }; };
  h.chrome.debugger.sendCommand = async (target, method) => { protection.push({ target, method }); return { identifier: 'script-1' }; };
  h.chrome.windows = { async create(options) {
    created.push(structuredClone(options));
    return { id: 41, tabs: [{ id: 42, windowId: 41 }] };
  } };
  const requestId = 'a'.repeat(32);
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    if (!created.length) {
      return { ok: true, protocolVersion: 2, heartbeatAt: 1, taskAssignment: 'no_assignment',
        createRequest: { requestId, taskId: 'task', url: 'https://example.org/' } };
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'no_assignment' };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  assert.equal(created.length, 1);
  assert.equal(created[0].url, 'about:blank');
  assert.equal(navigated[0].id, 42);
  assert.equal(navigated[0].options.url, 'https://example.org/');
  assert.equal(protection[0].method, 'Page.addScriptToEvaluateOnNewDocument');
  assert.equal(created[0].focused, false, '创建专用窗口不得抢占前台');
  const reports = syncs.filter(m => m.created);
  assert.equal(reports.length, 1);
  assert.deepEqual(reports[0].created, { requestId, browserWindowId: 41, tabId: 42 });
  h.chrome.alarms.onAlarm.fn({ name: 'observer-sync' });
  await h.flush();
  assert.equal(created.length, 1, '同一请求不得重复建窗');
  assert.equal(syncs.filter(m => m.created).length, 1, '证据不得重复回传');
});

test('创建失败保留待回传状态且下一次请求不被静默吞掉', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  let attempts = 0;
  h.chrome.windows = { async create() {
    if (++attempts === 1) throw new Error('建窗被拒');
    return { id: 51, tabs: [{ id: 52, windowId: 51 }] };
  } };
  const requestId = 'b'.repeat(32);
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    if (!message.created) {
      return { ok: true, protocolVersion: 2, heartbeatAt: 1, taskAssignment: 'no_assignment',
        createRequest: { requestId, taskId: 'task', url: null } };
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'no_assignment' };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  assert.equal(attempts, 1);
  assert.equal(h.calls.some(m => m.created), false, '失败不得伪造创建证据');
  assert.equal((await h.send({ action: 'status' })).nativeConnection.status, 'connected');
  h.chrome.alarms.onAlarm.fn({ name: 'observer-sync' });
  await h.flush();
  assert.equal(attempts, 1, '相同请求只轮询，不得立即同步空转或重放');
  await h.restart();
  assert.equal(attempts, 1, 'worker 重启不能重放结果未知的建窗');
  assert.match((await h.send({ action: 'status' })).nativeConnection.lastError, /结果未知/);
});

test('建窗证据回传失败后 worker 重启只恢复回传，不再创建窗口', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  let attempts = 0, acknowledged = false, rejectReport = true;
  const reports = [];
  const requestId = 'c'.repeat(32);
  h.chrome.windows = { async create() {
    attempts++;
    return { id: 61, tabs: [{ id: 62, windowId: 61 }] };
  } };
  h.native = async message => {
    if (message.created) {
      reports.push(structuredClone(message.created));
      if (rejectReport) throw new Error('确认响应丢失');
      acknowledged = true;
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 1, taskAssignment: 'no_assignment',
      createRequest: acknowledged ? null : { requestId, taskId: 'task', url: null } };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  assert.equal(attempts, 1);
  assert.equal(reports.length, 1);
  rejectReport = false;
  await h.restart();
  assert.equal(attempts, 1);
  assert.equal(reports.length, 2);
  assert.deepEqual(reports[1], reports[0]);
  assert.equal(acknowledged, true);
});

test('快照命令绑定已核验文档执行，结果只回传一次', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  const snapshot = { url: 'https://example.org/path', readyState: 'complete', elementCount: 1,
    elements: [{ ref: 1, tag: 'a', role: 'link', name: 'Docs', disabled: false,
      x: 0, y: 0, width: 10, height: 10 }] };
  const injections = [];
  h.chrome.scripting.executeScript = async options => {
    // 只记录快照命令注入；观察器注入走同一 API，不能用总数断言。
    if (typeof options.func === 'function' && options.func.toString().includes('__cuaSnapshot')) {
      injections.push(options);
    }
    return [{ result: snapshot }];
  };
  const commandId = 'd'.repeat(32);
  const assignment = { taskId: 'task', token: 'b'.repeat(64), bindingId: 'c'.repeat(32),
    assignmentVersion: 1, globalOrigins: true, selector: '', tabId: 7, browserWindowId: 2,
    mappingVerified: false };
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    if (message.commandResult) {
      return { ok: true, protocolVersion: 2, heartbeatAt: 3, taskAssignment: 'assigned', assignment };
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'assigned', assignment,
      command: { requestId: commandId, taskId: 'task', operation: 'snapshot' } };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  assert.equal(injections.length, 1, '命令必须执行一次');
  // vm 内创建的对象原型不同，按字段断言而非 deepEqual。
  assert.equal(injections[0].target.tabId, 7);
  assert.equal(injections[0].target.documentIds.length, 1);
  assert.equal(injections[0].target.documentIds[0], 'doc-1');
  assert.equal(injections[0].args[0].bindingId, 'c'.repeat(32));
  assert.equal(injections[0].args[0].navigationEpoch, h.state.navigationEpoch);
  const reports = syncs.filter(m => m.commandResult);
  assert.equal(reports.length, 1, '结果必须只回传一次');
  assert.equal(reports[0].commandResult.status, 'ok');
  assert.deepEqual(reports[0].commandResult.result, snapshot);
  h.chrome.alarms.onAlarm.fn({ name: 'observer-sync' });
  await h.flush();
  assert.equal(injections.length, 1, '同一命令不得重复执行');
});

test('缺少已核验文档时命令返回错误而不是伪装成功', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  const commandId = 'e'.repeat(32);
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    return { ok: true, protocolVersion: 2, heartbeatAt: 1, taskAssignment: 'no_assignment',
      ...(message.commandResult ? {} : { command: { requestId: commandId, taskId: 'task', operation: 'snapshot' } }) };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  const reports = syncs.filter(m => m.commandResult);
  assert.equal(reports.length, 1);
  assert.equal(reports[0].commandResult.status, 'error');
  assert.equal('result' in reports[0].commandResult, false);
});

test('动作命令通过 CDP 发给固定标签，DOM 不再直接 click', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  const inputs = [];
  h.chrome.debugger.sendCommand = async (target, method, params) => {
    inputs.push({ target, method, params }); return { identifier: 'script-1' };
  };
  h.chrome.scripting.executeScript = async o => {
    if (o.func?.toString().includes('__cuaPrepare')) return [{ result: { ok: true, x: 10, y: 20 } }];
    return [{ result: o.args?.[1] === 'drain' ? [] : true }];
  };
  const assignment = { taskId: 'task', token: 'b'.repeat(64), bindingId: 'c'.repeat(32),
    assignmentVersion: 1, globalOrigins: true, selector: '', tabId: 7, browserWindowId: 2, mappingVerified: false };
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'assigned', assignment,
      ...message.commandResult ? {} : { command: { requestId: 'e'.repeat(32), taskId: 'task', operation: 'click',
        params: { ref: 3, snapshotId: 'snapshot-1' } } } };
  };
  await h.send({ action: 'enable' }); await h.flush();
  const pointer = inputs.filter(i => i.method === 'Input.dispatchMouseEvent');
  assert.deepEqual(pointer.map(i => i.params.type), ['mouseMoved', 'mousePressed', 'mouseReleased']);
  assert.ok(pointer.every(i => i.target.tabId === 7 && i.params.x === 10 && i.params.y === 20));
  const report = syncs.find(m => m.commandResult).commandResult;
  assert.equal(report.status, 'ok'); assert.equal(report.result.verified, false);
});

test('导航命令走 tabs.update，且不声称已验证', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  const updates = [];
  h.chrome.tabs.update = async (id, props) => { updates.push({ id, props }); return { id, url: props.url }; };
  h.chrome.scripting.executeScript = async o => [{ result: o.args?.[1] === 'drain' ? [] : true }];
  const commandId = 'f1'.repeat(16);
  const assignment = { taskId: 'task', token: 'b'.repeat(64), bindingId: 'c'.repeat(32),
    assignmentVersion: 1, globalOrigins: true, selector: '', tabId: 7, browserWindowId: 2,
    mappingVerified: false };
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    if (message.commandResult) {
      return { ok: true, protocolVersion: 2, heartbeatAt: 3, taskAssignment: 'assigned', assignment };
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'assigned', assignment,
      command: { requestId: commandId, taskId: 'task', operation: 'navigate',
        params: { url: 'https://example.org/next' } } };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  assert.equal(updates.length, 1);
  assert.equal(updates[0].id, 7);
  assert.equal(updates[0].props.url, 'https://example.org/next');
  const report = syncs.find(m => m.commandResult);
  assert.equal(report.commandResult.result.performed, true);
  assert.equal(report.commandResult.result.verified, false, '导航完成由后续快照证明');
});

test('worker 重启后不得重复执行同一命令', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  let executed = 0;
  h.chrome.scripting.executeScript = async options => {
    // 观察器注入走同一 API，只统计动作命令的注入。
    if (typeof options.func === 'function' && options.func.toString().includes('__cuaAct')) executed++;
    return [{ result: null }];
  };
  h.chrome.storage.session.set({ lastCommand: { requestId: 'a2'.repeat(16), at: Date.now() } });
  const assignment = { taskId: 'task', token: 'b'.repeat(64), bindingId: 'c'.repeat(32),
    assignmentVersion: 1, globalOrigins: true, selector: '', tabId: 7, browserWindowId: 2,
    mappingVerified: false };
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    if (message.commandResult) {
      return { ok: true, protocolVersion: 2, heartbeatAt: 3, taskAssignment: 'assigned', assignment };
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'assigned', assignment,
      command: { requestId: 'a2'.repeat(16), taskId: 'task', operation: 'click', params: { ref: 1 } } };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  assert.equal(executed, 0, '已下发过的命令不得再次执行');
  const report = syncs.find(m => m.commandResult);
  assert.equal(report.commandResult.status, 'error');
  assert.match(report.commandResult.error, /结果未知/);
});

test('诊断命令读取页面错误与视口，不声称已复核动作', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  const diagnostics = { url: 'https://example.org/path', readyState: 'complete',
    errors: [{ message: 'TypeError: x is undefined', at: 1761900000000 }],
    viewport: { width: 1280, height: 800 }, scroll: { x: 0, y: 240 } };
  const injections = [];
  h.chrome.scripting.executeScript = async options => {
    if (typeof options.func === 'function' && options.func.toString().includes('__cuaDiagnostics')) {
      injections.push(options);
      return [{ result: diagnostics }];
    }
    return [{ result: null }];
  };
  const commandId = 'c1'.repeat(16);
  const assignment = { taskId: 'task', token: 'b'.repeat(64), bindingId: 'c'.repeat(32),
    assignmentVersion: 1, globalOrigins: true, selector: '', tabId: 7, browserWindowId: 2,
    mappingVerified: false };
  const syncs = [];
  h.native = async message => {
    syncs.push(structuredClone(message));
    if (message.action !== 'sync') return { ok: true };
    if (message.commandResult) {
      return { ok: true, protocolVersion: 2, heartbeatAt: 3, taskAssignment: 'assigned', assignment };
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'assigned', assignment,
      command: { requestId: commandId, taskId: 'task', operation: 'diagnostics', params: {} } };
  };
  await h.send({ action: 'enable' });
  await h.flush();
  assert.equal(injections.length, 1);
  const report = syncs.find(m => m.commandResult);
  assert.equal(report.commandResult.result.errors[0].message, 'TypeError: x is undefined');
  assert.equal(report.commandResult.result.scroll.y, 240);
});

test('启用后打开长期端口保活，停用后不再保持', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  await h.send({ action: 'enable' });
  await h.flush();
  assert.deepEqual(h.ports, ['com.cua.browser_observer'], '启用后应建立持续连接');
  await h.send({ action: 'disable' });
  await h.flush();
  assert.equal(h.ports.length, 1, '停用不得反复新建端口');
});

test('观察状态丢失后仍按持久绑定记录解绑，避免留下死目标', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  await h.send({ action: 'enable' });
  await h.flush();
  await h.chrome.storage.local.set({ boundTarget: { taskId: 't1', token: 'a'.repeat(64), tabId: 9 } });
  h.chrome.tabs.onRemoved.fn(9);
  await h.flush();
  const unbind = h.calls.find(m => m.action === 'unbind');
  assert.ok(unbind, '必须发起解绑');
  assert.equal(unbind.taskId, 't1');
  assert.equal(h.chrome.storage.local && true, true);
});

test('native 旧响应跨停用再启用窗口不得接收旧分配或覆盖 syncing', async () => {
  const h = await lifecycleHarness();
  h.permission = true;
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  let count = 0;
  h.native = async () => {
    if (++count === 1) {
      await blocked;
      return { ok: true, protocolVersion: 2, heartbeatAt: 2, taskAssignment: 'assigned',
        assignment: { taskId: 'old', token: 'b'.repeat(64), bindingId: 'c'.repeat(32), assignmentVersion: 1,
          tabId: 7, browserWindowId: 2, mappingVerified: false, globalOrigins: true, selector: '' } };
    }
    return { ok: true, protocolVersion: 2, heartbeatAt: 3, taskAssignment: 'no_assignment' };
  };
  await h.send({ action: 'enable' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await h.send({ action: 'disable' })).enabled, false);
  assert.equal((await h.send({ action: 'enable' })).enabled, true);
  release();
  await h.flush();
  const status = await h.send({ action: 'status' });
  assert.equal(status.state, null);
  assert.equal(status.taskAssignment, 'no_assignment');
  assert.equal(status.nativeConnection.lastError, undefined, '旧分配不得进入 tabs 或注入路径');
});
