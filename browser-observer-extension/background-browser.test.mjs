import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { BackgroundBrowser } from './background-browser.mjs';
import { installPopupGuard } from './popup-guard.mjs';

function harness() {
  const state = { taskId: 'task', bindingId: 'binding', assignmentVersion: 1,
    tabId: 7, browserWindowId: 2, documentId: 'doc', navigationEpoch: 1, url: 'https://example.org/a' };
  let frame = { documentId: 'doc', url: state.url }, windowId = 2, allowed = true, popupUrls = [];
  const calls = [];
  const chrome = {
    tabs: {
      async get(id) { calls.push(['get', id]); return { id, windowId, url: id === 11 ? 'about:blank' : frame.url }; },
      async update(id, options) { calls.push(['update', id, options]); return { id, windowId: 2 }; },
      async create(options) { calls.push(['create', options]); return { id: 11, windowId: options.windowId }; },
      async remove(id) { calls.push(['remove', id]); }
    },
    webNavigation: { async getFrame() { return frame; } },
    debugger: {
      onDetach: { addListener(fn) { this.listener = fn; } },
      async attach(target) { calls.push(['attach', target]); },
      async detach(target) { calls.push(['detach', target]); },
      async sendCommand(target, method, params) {
        calls.push([method, target, params]);
        if (method === 'Page.getFrameTree') return { frameTree: { frame: { id: 'initial-frame', url: 'about:blank' } } };
        if (method === 'Page.getLayoutMetrics') return { cssVisualViewport: { pageX: 0, pageY: 0, clientWidth: 1600, clientHeight: 1000 } };
        if (method === 'Page.captureScreenshot') return { data: 'ZmFrZQ==' };
        return { identifier: 'script' };
      }
    },
    scripting: { async executeScript(o) {
      calls.push(['script', o]);
      if (o.world === 'MAIN') return [{ result: o.args?.[1] === 'drain' ? popupUrls : true }];
      const f = o.func.toString();
      if (f.includes('__cuaPrepare')) return [{ result: { ok: true, x: 100, y: 80 } }];
      if (f.includes('__cuaSnapshot')) return [{ result: { snapshotId: 'snap' } }];
      return [{ result: true }];
    } }
  };
  const browser = new BackgroundBrowser(chrome, async () => allowed);
  const command = (operation, params = {}) => ({ ...state, operation, params, expiresAt: Date.now() / 1000 + 10 });
  return { browser, chrome, state, calls, command,
    navigate() { frame = { documentId: 'new-doc', url: state.url }; },
    move() { windowId = 99; }, revoke() { allowed = false; },
    popup(url) { popupUrls = Array.isArray(url) ? url : [url]; } };
}

test('created-tab protection rejects moved or independently navigating tabs before attachment', async () => {
  for (const tab of [{ windowId: 99, url: 'about:blank' },
    { windowId: 2, url: 'about:blank', pendingUrl: 'https://other.example/' },
    { windowId: 2, url: 'https://other.example/' }]) {
    const h = harness();
    h.chrome.tabs.get = async () => tab;
    await assert.rejects(h.browser.protectCreatedTab(11, 2, 'task'), /身份已变化/);
    assert.equal(h.calls.some(c => c[0] === 'attach'), false);
  }
});

test('created blank tab can be verified when HTTP-only permission hides its URL metadata', async () => {
  const h = harness();
  h.chrome.tabs.get = async id => ({ id, windowId: 2 });
  const send = h.chrome.debugger.sendCommand;
  h.chrome.debugger.sendCommand = async (...args) => args[1] === 'Page.getFrameTree' ?
    { frameTree: { frame: { id: 'initial-frame', url: 'about:blank' } } } : send(...args);
  await h.browser.protectCreatedTab(11, 2, 'task');
  assert.equal(h.browser.guards.get(11)?.taskId, 'task');
  assert.equal(h.calls.some(c => c[0] === 'Page.addScriptToEvaluateOnNewDocument'), true);
});

test('hidden tab metadata never permits overwriting a nonblank CDP document', async () => {
  for (const url of ['https://other.example/', 'chrome://newtab/', 'data:text/plain,changed']) {
    const h = harness();
    h.chrome.tabs.get = async id => ({ id, windowId: 2 });
    const send = h.chrome.debugger.sendCommand;
    h.chrome.debugger.sendCommand = async (...args) => args[1] === 'Page.getFrameTree' ?
      { frameTree: { frame: { id: 'frame', url } } } : send(...args);
    await assert.rejects(h.browser.protectCreatedTab(11, 2, 'task'), /文档已变化/);
    assert.equal(h.calls.some(c => c[0] === 'Page.addScriptToEvaluateOnNewDocument'), false);
    assert.equal(h.calls.some(c => c[0] === 'detach'), true);
  }
});

test('replacing the blank document during preload invalidates creation protection', async () => {
  const h = harness();
  h.chrome.tabs.get = async id => ({ id, windowId: 2 });
  let frameReads = 0;
  const send = h.chrome.debugger.sendCommand;
  h.chrome.debugger.sendCommand = async (...args) => args[1] === 'Page.getFrameTree' ?
    { frameTree: { frame: { id: 'frame', loaderId: ++frameReads === 1 ? 'original' : 'replacement', url: 'about:blank' } } } : send(...args);
  await assert.rejects(h.browser.protectCreatedTab(11, 2, 'task'), /文档已替换/);
  assert.equal(h.browser.guards.size, 0);
});

test('a tab that changes during popup preload is released without being navigated', async () => {
  const h = harness();
  let moved = false;
  h.chrome.tabs.get = async () => ({ windowId: moved ? 99 : 2, url: 'about:blank' });
  const send = h.chrome.debugger.sendCommand;
  h.chrome.debugger.sendCommand = async (...args) => {
    const result = await send(...args);
    if (args[1] === 'Page.addScriptToEvaluateOnNewDocument') moved = true;
    return result;
  };
  await assert.rejects(h.browser.protectCreatedTab(11, 2, 'task'), /身份已变化/);
  assert.equal(h.calls.some(c => c[0] === 'detach'), true);
  assert.equal(h.browser.guards.size, 0);
});

test('background pointer uses the bound tab; it never activates a tab/window or uses screen coordinates', async () => {
  const h = harness();
  const result = await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
  const inputs = h.calls.filter(c => c[0] === 'Input.dispatchMouseEvent');
  assert.deepEqual(inputs.map(c => c[2].type), ['mouseMoved', 'mousePressed', 'mouseReleased']);
  assert.ok(inputs.every(c => c[1].tabId === 7 && c[2].x === 100 && c[2].y === 80));
  assert.equal(result.verified, false);
  assert.equal(h.calls.some(c => c[0] === 'update'), false);
});

test('background scrolling uses the verified document without waiting for wheel acknowledgements', async () => {
  const h = harness();
  const result = await h.browser.run(h.command('scroll', { snapshotId: 'snap', deltaY: -6 }), h.state);
  assert.ok(result);
  assert.ok(h.calls.some(c => c[0] === 'script' && c[1].func?.toString().includes('__cuaScroll')));
  assert.equal(h.calls.some(c => c[0].startsWith('Input.')), false);
  assert.equal(h.calls.some(c => c[0] === 'update'), false);
});

test('an unacknowledged CDP command detaches, suspends and never replays input', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness();
  const send = h.chrome.debugger.sendCommand;
  h.chrome.debugger.sendCommand = async (...args) => args[1] === 'Input.insertText' ? new Promise(() => {}) : send(...args);
  const pending = h.browser.run(h.command('fill', { snapshotId: 'snap', ref: 1, value: 'once' }), h.state);
  const rejected = assert.rejects(pending, /BACKGROUND_COMMAND_TIMEOUT.*Input.insertText/);
  await new Promise(resolve => setImmediate(resolve));
  t.mock.timers.tick(3001);
  await rejected;
  assert.equal(h.calls.filter(c => c[0] === 'detach').length, 1);
  assert.ok(h.browser.detached.has(7));
  await assert.rejects(h.browser.run(h.command('screenshot'), h.state), /BACKGROUND_UNAVAILABLE/);
});

test('same URL with a replacement document, moved tab, expired command and revoked permission stop before input', async () => {
  for (const change of ['navigate', 'move', 'revoke', 'expire', 'wrongLease']) {
    const h = harness(), cmd = h.command('click', { ref: 1, snapshotId: 'snap' });
    if (change === 'expire') cmd.expiresAt = 1;
    else if (change === 'wrongLease') cmd.bindingId = 'other-task';
    else h[change]();
    await assert.rejects(h.browser.run(cmd, h.state), /STALE/);
    assert.equal(h.calls.some(c => c[0].startsWith('Input.')), false, change);
  }
});

test('a navigation during hover prevents the mousedown instead of clicking a new page', async () => {
  const h = harness(), send = h.chrome.debugger.sendCommand;
  h.chrome.debugger.sendCommand = async (...args) => {
    const r = await send(...args);
    if (args[2]?.type === 'mouseMoved') h.navigate();
    return r;
  };
  await assert.rejects(h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state), /STALE_DOCUMENT/);
  assert.equal(h.calls.some(c => c[2]?.type === 'mousePressed'), false);
});

test('debugger detach suspends execution until an explicit reassignment', async () => {
  const h = harness();
  await h.browser.run(h.command('screenshot'), h.state);
  h.chrome.debugger.onDetach.listener({ tabId: 7 });
  const attachments = h.calls.filter(c => c[0] === 'attach').length;
  await assert.rejects(h.browser.run(h.command('screenshot'), h.state), /BACKGROUND_UNAVAILABLE/);
  assert.equal(h.calls.filter(c => c[0] === 'attach').length, attachments);
});

test('detach and preload-removal errors still restore page popup hooks on release', async () => {
  for (const detached of [false, true]) {
    const h = harness();
    await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
    if (detached) h.chrome.debugger.onDetach.listener({ tabId: 7 });
    else {
      const send = h.chrome.debugger.sendCommand;
      h.chrome.debugger.sendCommand = async (...args) => {
        if (args[1] === 'Page.removeScriptToEvaluateOnNewDocument') throw new Error('preload already removed');
        return send(...args);
      };
    }
    const before = h.calls.length;
    await h.browser.release(7);
    const cleanup = h.calls.slice(before).find(c => c[0] === 'script' && c[1].args?.[0] === 'task');
    assert.ok(cleanup, 'page cleanup is independent of debugger availability');
    assert.equal(cleanup[1].target.allFrames, true);
    assert.equal(h.browser.guards.size, 0);
  }
});

test('persisted task ownership permits hook cleanup after a worker restart', async () => {
  const h = harness();
  await h.browser.release(7, 'task');
  const cleanup = h.calls.find(c => c[0] === 'script');
  assert.deepEqual(cleanup[1].args, ['task']);
  assert.equal(h.calls.some(c => c[0] === 'attach'), false);
});

test('debugger permission/attachment failure never falls back to DOM click', async () => {
  const h = harness();
  h.chrome.debugger.attach = async () => { throw new Error('permission denied'); };
  await assert.rejects(h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state), /permission denied/);
  assert.equal(h.calls.some(c => c[0].startsWith('Input.')), false);
});

test('browser screenshot and text input use the same bound CDP target', async () => {
  const h = harness();
  const shot = await h.browser.run(h.command('screenshot'), h.state);
  assert.equal(shot.tabId, 7); assert.equal(shot.documentId, 'doc');
  await h.browser.run(h.command('fill', { ref: 2, snapshotId: 'snap', value: 'example' }), h.state);
  const text = h.calls.find(c => c[0] === 'Input.insertText');
  assert.equal(text[1].tabId, 7); assert.equal(text[2].text, 'example');
});

test('popup children remain inactive in the owning window and are registered in the action result', async () => {
  const h = harness(); h.popup('https://example.org/topic?private=1');
  const result = await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
  const created = h.calls.find(c => c[0] === 'create')[1];
  assert.equal(created.active, false); assert.equal(created.windowId, 2); assert.equal(created.openerTabId, 7);
  assert.deepEqual(result.children, [{ tabId: 11, browserWindowId: 2, url: 'https://example.org/topic' }]);
  assert.ok(h.calls.findIndex(c => c[0] === 'Page.addScriptToEvaluateOnNewDocument' && c[1].tabId === 11) <
    h.calls.findIndex(c => c[0] === 'update' && c[1] === 11));
});

test('a later popup failure cleans up earlier children instead of leaving unregistered tabs', async () => {
  const h = harness(); h.popup(['https://example.org/one', 'https://example.org/two']);
  const create = h.chrome.tabs.create;
  let count = 0;
  h.chrome.tabs.create = async options => {
    if (++count === 2) throw new Error('cannot create another tab');
    return create(options);
  };
  await assert.rejects(h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state), /cannot create/);
  assert.ok(h.calls.some(c => c[0] === 'remove' && c[1] === 11));
  assert.equal(h.browser.guards.has(11), false);
});

test('page popup guard queues only armed HTTP links, blocks native popups, and restores its hooks', () => {
  const handlers = {}, original = () => { throw new Error('foreground popup must never open'); };
  const context = { URL, location: { href: 'https://example.org/' }, window: { open: original },
    document: { querySelector() { return null; } }, HTMLAnchorElement: class {},
    addEventListener(type, fn) { handlers[type] = fn; }, removeEventListener(type) { delete handlers[type]; } };
  vm.createContext(context);
  vm.runInContext(`(${installPopupGuard.toString()})('task')`, context);
  assert.equal(context.window.open('/ignored'), null);
  context.__cuaPopupGuard('task', 'arm');
  context.window.open('/topic'); context.window.open('javascript:alert(1)');
  assert.deepEqual(Array.from(context.__cuaPopupGuard('task', 'drain')), ['https://example.org/topic']);
  context.__cuaPopupGuard('task', 'release');
  assert.equal(context.window.open, original); assert.equal(Object.keys(handlers).length, 0);
});

test('new-window forms inherited from base targets cannot open a foreground window', () => {
  const handlers = {};
  let submitted = 0, prevented = 0;
  class Form { submit() { submitted++; } }
  const context = { URL, location: { href: 'https://example.org/' }, window: { open() {} },
    document: { querySelector() { return { target: '_blank' }; } },
    HTMLAnchorElement: class {}, HTMLFormElement: Form,
    addEventListener(type, fn) { handlers[type] = fn; }, removeEventListener(type) { delete handlers[type]; } };
  vm.createContext(context);
  vm.runInContext(`(${installPopupGuard.toString()})('task')`, context);
  const form = new Form(); form.target = '';
  form.submit();
  handlers.submit({ target: form, preventDefault() { prevented++; } });
  assert.equal(submitted, 0); assert.equal(prevented, 1);
  form.target = '_self'; form.submit();
  assert.equal(submitted, 1);
  context.__cuaPopupGuard('task', 'release');
});
