import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

test('content 仅报告生命周期、布局布尔计数和用户 CSS 条件', async () => {
  const messages = [];
  const handlers = {};
  let mutation;
  let timer;
  let count = 3;
  let shown = true;
  let disconnected = false;
  const node = { getClientRects: () => shown ? [{}] : [] };
  const context = {
    location: { protocol: 'https:', origin: 'https://example.org', pathname: '/latest' },
    document: { body: node, readyState: 'complete', getElementsByTagName: () => ({ length: count }),
      querySelector(selector) { assert.equal(selector, 'main.ready'); return node; } },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    chrome: { runtime: { sendMessage(message) { messages.push(message); return Promise.resolve(); } } },
    addEventListener(type, fn) { handlers[type] = fn; },
    removeEventListener(type) { delete handlers[type]; },
    setTimeout(fn) { timer = fn; return 1; }, clearTimeout() {},
    MutationObserver: class { constructor(fn) { mutation = fn; } observe() {} disconnect() { disconnected = true; } }
  };
  vm.createContext(context);
  const code = await readFile(new URL('./content.js', import.meta.url), 'utf8');
  vm.runInContext(code, context);
  assert.equal(messages.length, 0);
  const config = { navigationEpoch: 1, bindingId: 'a'.repeat(32), authorizedOrigin: 'https://example.org', selector: '' };
  context.__cuaObserver(config);
  assert.deepEqual(JSON.parse(JSON.stringify(messages[0])), { action: 'content',
    url: 'https://example.org/latest', navigationEpoch: 1, bindingId: 'a'.repeat(32), lifecycle: 'content',
    readiness: { bodyPresent: true, bodyVisible: true, elementCount: 3,
      readyState: 'complete', conditionMatched: null } });
  context.__cuaObserver({ ...config, selector: 'main.ready' });
  assert.equal(messages.at(-1).readiness.conditionMatched, true);
  shown = false; count = 0;
  mutation(); timer();
  assert.equal(messages.at(-1).readiness.conditionMatched, false);
  assert.equal(messages.at(-1).readiness.elementCount, 0);
  handlers.pageshow({ type: 'pageshow' });
  assert.equal(messages.at(-1).lifecycle, 'pageshow');
  const previous = messages.length;
  vm.runInContext(code, context);
  assert.equal(messages.length, previous);
  context.location.origin = 'https://other.example';
  handlers.load({ type: 'load' });
  assert.equal(messages.length, previous);
  context.__cuaObserverStop();
  assert.equal(disconnected, true);
  assert.equal(context.__cuaObserver, undefined);
});

test('content 动作在未绑定、绑定不符或引用失效时明确失败', async () => {
  const context = {
    location: { protocol: 'https:', origin: 'https://example.org', pathname: '/latest' },
    document: { body: { getClientRects: () => [{}] }, readyState: 'complete',
      getElementsByTagName: () => ({ length: 1 }), querySelector: () => null,
      scrollingElement: null },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    chrome: { runtime: { sendMessage() { return Promise.resolve(); } } },
    addEventListener() {}, removeEventListener() {}, setTimeout() { return 1; }, clearTimeout() {},
    MutationObserver: class { observe() {} disconnect() {} }
  };
  vm.createContext(context);
  vm.runInContext(await readFile(new URL('./content.js', import.meta.url), 'utf8'), context);
  const config = { navigationEpoch: 1, bindingId: 'a'.repeat(32), authorizedOrigin: 'https://example.org' };
  assert.equal(context.__cuaAct, undefined, 'DOM 脚本不提供直接点击入口');
  assert.equal(context.__cuaPrepare(config, { ref: 1 }).ok, false, '未绑定不得准备输入');
  context.__cuaObserver({ ...config, selector: '' });
  assert.equal(context.__cuaPrepare({ ...config, bindingId: 'b'.repeat(32) }, { ref: 1 }).ok,
    false, '绑定不符不得执行');
  assert.equal(context.__cuaPrepare(config, { ref: 999 }).reason, 'stale_ref');
  assert.equal(context.__cuaVerifyFill(config, { ref: 999, value: 'x' }), false);

});

test('content uses viewport geometry, rechecks moved targets and drops references absent from the latest snapshot', async () => {
  let rect = { left: 10, top: 20, right: 110, bottom: 60, width: 100, height: 40 };
  let included = true, scrolled = false;
  const node = { tagName: 'BUTTON', textContent: 'Open', isConnected: true,
    getClientRects: () => [{}], getBoundingClientRect: () => rect, getAttribute: () => null,
    contains: hit => hit === node, scrollIntoView() { scrolled = true; } };
  const context = {
    location: { protocol: 'https:', origin: 'https://example.org', pathname: '/latest' },
    document: { body: node, readyState: 'complete', getElementsByTagName: () => ({ length: 1 }),
      querySelectorAll: selector => selector === 'iframe,frame' ? [] : included ? [node] : [], elementFromPoint: () => node },
    innerWidth: 1280, innerHeight: 800,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    chrome: { runtime: { sendMessage: async () => {} } },
    addEventListener() {}, removeEventListener() {}, setTimeout() { return 1; }, clearTimeout() {},
    MutationObserver: class { observe() {} disconnect() {} }
  };
  node.ownerDocument = context.document;
  context.document.defaultView = context;
  vm.createContext(context);
  vm.runInContext(await readFile(new URL('./content.js', import.meta.url), 'utf8'), context);
  const config = { navigationEpoch: 1, bindingId: 'a'.repeat(32), authorizedOrigin: 'https://example.org', selector: '' };
  context.__cuaObserver(config);
  const first = context.__cuaSnapshot(config), action = { ref: first.elements[0].ref };
  const bound = { ...config, snapshotId: first.snapshotId };
  const point = context.__cuaPrepare(bound, action);
  assert.equal(point.x, 60); assert.equal(point.y, 40); assert.equal(scrolled, true);
  rect = { ...rect, left: 200, right: 300 };
  assert.equal(context.__cuaPointMatches(bound, action, point), false);
  included = false;
  const second = context.__cuaSnapshot(config);
  assert.equal(context.__cuaPrepare({ ...config, snapshotId: second.snapshotId }, action).ok, false);
  assert.equal(context.__cuaPrepare(bound, action).ok, false);
});

test('content 采集页面错误与视口，且在未绑定时拒绝诊断', async () => {
  const handlers = {};
  const context = {
    location: { protocol: 'https:', origin: 'https://example.org', pathname: '/latest' },
    document: { body: { getClientRects: () => [{}] }, readyState: 'complete',
      getElementsByTagName: () => ({ length: 1 }), querySelector: () => null,
      scrollingElement: null },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    chrome: { runtime: { sendMessage() { return Promise.resolve(); } } },
    addEventListener(type, fn) { handlers[type] = fn; },
    removeEventListener(type) { delete handlers[type]; },
    setTimeout() { return 1; }, clearTimeout() {},
    innerWidth: 1280, innerHeight: 800, scrollX: 0, scrollY: 240,
    MutationObserver: class { observe() {} disconnect() {} }
  };
  vm.createContext(context);
  vm.runInContext(await readFile(new URL('./content.js', import.meta.url), 'utf8'), context);
  const config = { navigationEpoch: 1, bindingId: 'a'.repeat(32), authorizedOrigin: 'https://example.org' };
  assert.equal(context.__cuaDiagnostics(config), null, '未绑定不得读取诊断');
  context.__cuaObserver({ ...config, selector: '' });
  handlers.error({ message: ' TypeError: boom ' });
  handlers.error({ message: 'TypeError: boom' });
  handlers.unhandledrejection({ reason: { message: 'Rejected' } });
  const report = JSON.parse(JSON.stringify(context.__cuaDiagnostics(config)));
  assert.equal(report.errors.length, 2, '重复错误只登记一次');
  assert.equal(report.errors[0].message, 'TypeError: boom');
  assert.equal(report.errors[1].message, 'Rejected');
  assert.deepEqual(report.viewport, { width: 1280, height: 800 });
  assert.deepEqual(report.scroll, { x: 0, y: 240 });
  context.__cuaObserverStop();
  assert.equal(handlers.error, undefined, '停止后不应保留错误监听');
});

test('document scrolling works repeatedly while hidden and rejects stale snapshots', async () => {
  let reset = false;
  const root = { isConnected: true, scrollTop: 0, scrollHeight: 1800, clientHeight: 800, getClientRects: () => [{}],
    scrollIntoView() { reset = true; this.scrollTop = 0; },
    scrollBy({ top }) { this.scrollTop = Math.max(0, Math.min(1000, this.scrollTop + top)); } };
  const context = {
    location: { protocol: 'https:', origin: 'https://example.org', pathname: '/' },
    document: { visibilityState: 'hidden', scrollingElement: root, body: root, readyState: 'complete',
      getElementsByTagName: () => ({ length: 1 }), querySelectorAll: () => [] },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    chrome: { runtime: { sendMessage: async () => {} } },
    addEventListener() {}, removeEventListener() {}, setTimeout() { return 1; }, clearTimeout() {},
    MutationObserver: class { observe() {} disconnect() {} }
  };
  root.ownerDocument = context.document;
  context.document.defaultView = context;
  vm.createContext(context);
  vm.runInContext(await readFile(new URL('./content.js', import.meta.url), 'utf8'), context);
  const config = { bindingId: 'a'.repeat(32), navigationEpoch: 1, authorizedOrigin: 'https://example.org', selector: '' };
  context.__cuaObserver(config);
  const snapshot = context.__cuaSnapshot(config);
  const bound = { ...config, snapshotId: snapshot.snapshotId };
  const boundary = context.__cuaScroll(bound, { deltaY: 6 });
  assert.equal(boundary.verified, false);
  assert.equal(boundary.observed.direction, 'up');
  assert.equal(boundary.observed.outcome, 'at_boundary');
  assert.equal(boundary.observed.maxY, 1000);
  assert.equal(context.__cuaScroll(bound, { deltaY: -6 }).observed.afterY, 240);
  assert.equal(context.__cuaScroll(bound, { deltaY: -6 }).observed.afterY, 480);
  assert.equal(context.__cuaScroll(bound, { deltaY: 6 }).observed.afterY, 240);
  assert.equal(reset, false, 'root scrolling must not reset the previous position');
  assert.equal(context.__cuaScroll({ ...bound, snapshotId: 'old' }, { deltaY: -6 }).performed, false);
  assert.equal(root.scrollTop, 240);
  const scrollBy = root.scrollBy;
  root.scrollBy = () => {};
  const blocked = context.__cuaScroll(bound, { deltaY: -6 });
  assert.equal(blocked.verified, false);
  assert.equal(blocked.observed.atBoundary, false);
  assert.equal(blocked.observed.outcome, 'no_movement');
  assert.equal(blocked.observed.expectedY, 480);
  root.scrollBy = scrollBy;
  context.__cuaObserverStop();
  assert.equal(context.__cuaScroll, undefined);
});

test('popup 只由按钮手势一次请求全站权限，无用户任务配对', async () => {
  const messages = [];
  const permissions = [];
  let granted = true;
  const elements = Object.fromEntries(['permission', 'connection', 'assignment', 'state', 'error', 'extid', 'enable', 'disable', 'refresh',
    'heartbeat', 'assignment-detail', 'status-label', 'status-badge', 'overview-title', 'status-description',
    'current-task', 'task-site', 'task-id', 'stop-task', 'activity', 'clear-activity', 'downloads',
    'capture-review', 'review-panel', 'review-canvas', 'review-note', 'save-review', 'export-review', 'review-notes']
    .map(id => [id, { value: '', checked: false, textContent: '', children: [],
      replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
      addEventListener(type, fn) { this.click = fn; } }]));
  let globalPermission = 'permission_missing';
  let lastError = '';
  const writes = [];
  let finishWrite;
  const context = { URL, Date, crypto: { randomUUID: () => 'ui-nonce' },
    document: { visibilityState: 'visible', getElementById(id) { return elements[id]; }, createElement() { return { textContent: '' }; } },
    // 浏览器一定有定时器；测试里给空实现，避免轮询在 node 里真的跑起来。
    setInterval() { return 1; }, clearInterval() {},
    chrome: { storage: { local: { set(value) { writes.push(value); return new Promise(resolve => { finishWrite = resolve; }); } } },
      permissions: { request(options) { assert.equal(writes.length, permissions.length + 1); permissions.push(options); return Promise.resolve(granted); } },
      runtime: { id: 'kbnonjkmdohkdjmfmafhjakkadkjjlno', async sendMessage(message) {
        messages.push(message);
        return { ok: true, globalPermission, enabled: true,
          nativeConnection: { status: lastError ? 'host_unavailable' : 'connected', lastHeartbeatAt: null, lastError }, taskAssignment: 'no_assignment',
          state: message.action === 'status' ? { mappingVerified: false } : null };
      } } } };
  vm.createContext(context);
  vm.runInContext(await readFile(new URL('./popup.js', import.meta.url), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(permissions.length, 0);
  const click = elements.enable.click();
  assert.equal(permissions.length, 1, '存储尚未完成也必须在同一事件立即发起权限请求');
  finishWrite();
  await click;
  assert.deepEqual(JSON.parse(JSON.stringify(permissions[0])), { origins: ['http://*/*', 'https://*/*'] });
  assert.ok(messages.some(m => m.action === 'enable' && m.nonce === writes[0].pendingEnable.nonce));
  assert.match(elements.state.textContent, /mappingVerified/);
  const count = messages.filter(m => m.action === 'enable').length;
  granted = false;
  const rejected = elements.enable.click();
  finishWrite();
  await rejected;
  assert.equal(messages.filter(m => m.action === 'enable').length, count);
  assert.match(elements.error.textContent, /permission_missing/);
  await elements.disable.click();
  assert.ok(messages.some(m => m.action === 'disable'));
  await elements.refresh.click();
  assert.equal(permissions.length, 2);
  assert.ok(messages.some(m => m.action === 'cancel_enable' && m.nonce === 'ui-nonce'));
  globalPermission = 'granted';
  lastError = '本地 host 离线';
  await elements.refresh.click();
  await elements.enable.click();
  assert.equal(permissions.length, 2, '已有授权一次点击直达 enable，不再请求');
  assert.equal(messages.filter(m => m.action === 'enable').length, count + 1);
  assert.equal(elements.permission.textContent, '已授权');
  assert.equal(elements.error.textContent, lastError);
  assert.equal(elements.extid.textContent, 'kbnonjkmdohkdjmfmafhjakkadkjjlno');
  // forbidden 必须点明是注册 origin 不含当前扩展 ID。
  lastError = 'Access to the specified native messaging host is forbidden.';
  await elements.refresh.click();
  assert.match(elements.error.textContent, /allowed_origins/);
  assert.match(elements.error.textContent, /kbnonjkmdohkdjmfmafhjakkadkjjlno/);
});

test('MV3 无静态站点授权、网页通信入口和自动内容注入', async () => {
  const m = JSON.parse(await readFile(new URL('./manifest.json', import.meta.url), 'utf8'));
  assert.equal(m.name, 'Workbuddy CUA Browser Extension');
  const popup = await readFile(new URL('./popup.html', import.meta.url), 'utf8');
  assert.match(popup, /<title>Workbuddy CUA Browser Extension<\/title>/);
  assert.match(popup, /class="brand-name">Workbuddy CUA<\/div>/);
  assert.equal(m.manifest_version, 3);
  assert.deepEqual(m.optional_host_permissions, ['http://*/*', 'https://*/*']);
  for (const key of ['host_permissions', 'externally_connectable', 'content_scripts']) assert.equal(key in m, false);
  assert.deepEqual(m.permissions, ['webNavigation', 'storage', 'scripting', 'nativeMessaging', 'alarms', 'debugger']);
  // key 固定扩展 ID，复制/移动目录重新加载都不会改变 ID 与原生注册。
  assert.equal(typeof m.key, 'string');
  assert.ok(m.key.length > 100, 'manifest 必须携带稳定 ID 的公钥');
});
