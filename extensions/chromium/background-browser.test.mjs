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
      if (o.world === 'MAIN') return [{ result: o.func.name === 'installPopupGuard' ? 'ready' : o.args?.[1] === 'drain' ? popupUrls : true }];
      const f = o.func.toString();
      if (f.includes('__quietbridgePrepare')) return [{ result: { ok: true, x: 100, y: 80 } }];
      if (f.includes('__quietbridgeSnapshot')) return [{ result: { snapshotId: 'snap' } }];
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
  assert.deepEqual(inputs.map(c => c[2].type), ['mousePressed', 'mouseReleased']);
  assert.ok(inputs.every(c => c[1].tabId === 7 && c[2].x === 100 && c[2].y === 80));
  assert.equal(result.verified, false);
  assert.equal(h.calls.some(c => c[0] === 'update'), false);
  const refreshes = h.calls.filter(c => c[0] === 'Page.captureScreenshot' && c[2]?.quality === 1);
  assert.equal(refreshes.length, 0, 'ordinary input must not implicitly take a screenshot');
  assert.equal('data' in result, false, 'the refresh image never enters the command result');
});

test('read-only page commands do not emit render-refresh captures', async () => {
  const h = harness();
  await h.browser.run(h.command('read'), h.state);
  assert.equal(h.calls.some(c => c[0] === 'Page.captureScreenshot' && c[2]?.quality === 1), false);
});

test('ordinary input succeeds without invoking screenshot support', async () => {
  const h = harness(), send = h.chrome.debugger.sendCommand;
  h.chrome.debugger.sendCommand = async (...args) => {
    if (args[1] === 'Page.captureScreenshot' && args[2]?.quality === 1) assert.fail('ordinary click must not request an image');
    return send(...args);
  };
  const result = await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
  assert.equal(result.performed, true);
  assert.equal(result.verified, false);
});

test('background scrolling uses the verified document without waiting for wheel acknowledgements', async () => {
  const h = harness();
  const result = await h.browser.run(h.command('scroll', { snapshotId: 'snap', deltaY: -6 }), h.state);
  assert.ok(result);
  assert.ok(h.calls.some(c => c[0] === 'script' && c[1].func?.toString().includes('__quietbridgeScroll')));
  assert.equal(h.calls.some(c => c[0].startsWith('Input.')), false);
  assert.equal(h.calls.some(c => c[0] === 'update'), false);
});

test('paged DOM reading remains read only and never attaches a debugger', async () => {
  const h = harness();
  h.chrome.debugger.attach = async () => { assert.fail('reading must not attach a debugger'); };
  h.chrome.scripting.executeScript = async options => {
    assert.equal(options.target.tabId, h.state.tabId);
    assert.deepEqual(options.target.documentIds, [h.state.documentId]);
    assert.match(options.func.toString(), /__quietbridgeRead/u);
    assert.equal(options.args[1].mode, 'content');
    return [{ documentId: 'doc', result: { readId: 'read-1', items: [] } }];
  };
  const result = await h.browser.run(h.command('read', { mode: 'content' }), h.state);
  assert.equal(result.readId, 'read-1');
  assert.equal(h.calls.some(c => c[0] === 'update' || c[0] === 'create'), false);
});

test('read responses are discarded if their document is replaced during execution', async () => {
  const h = harness();
  h.chrome.scripting.executeScript = async () => { h.navigate(); return [{ result: { readId: 'old' } }]; };
  await assert.rejects(h.browser.run(h.command('read'), h.state), /STALE_DOCUMENT/u);
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

test('mouse timeouts identify their exact stage, stop later events and retain read-only observation', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const stages = ['mousePressed', 'mouseReleased'];
  for (const [index, stage] of stages.entries()) {
    const h = harness(), attempted = [], send = h.chrome.debugger.sendCommand;
    h.chrome.debugger.sendCommand = async (...args) => {
      if (args[1] === 'Input.dispatchMouseEvent') {
        attempted.push(args[2].type);
        if (args[2].type === stage) return new Promise(() => {});
      }
      return send(...args);
    };
    const pending = h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
    const rejected = assert.rejects(pending,
      new RegExp('BACKGROUND_COMMAND_TIMEOUT.*Input.dispatchMouseEvent/' + stage));
    await new Promise(resolve => setImmediate(resolve));
    t.mock.timers.tick(3001);
    await rejected;
    assert.deepEqual(attempted, stages.slice(0, index + 1));
    assert.equal(h.calls.filter(c => c[0] === 'detach').length, 1);
    assert.equal((await h.browser.run(h.command('snapshot'), h.state)).snapshotId, 'snap');
    assert.equal(h.browser.detached.has(h.state.tabId), true);
  }
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

test('a navigation during final hit testing prevents mousedown on a replacement page', async () => {
  const h = harness(), execute = h.chrome.scripting.executeScript;
  h.chrome.scripting.executeScript = async options => {
    const r = await execute(options);
    if (options.func?.toString().includes('__quietbridgePointMatches')) h.navigate();
    return r;
  };
  await assert.rejects(h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state), /STALE_DOCUMENT/);
  assert.equal(h.calls.some(c => c[2]?.type === 'mousePressed'), false);
});

test('click does not wait for an unnecessary mouseMoved acknowledgement in a background tab', async () => {
  const h = harness(), send = h.chrome.debugger.sendCommand;
  h.chrome.debugger.sendCommand = async (...args) => {
    if (args[1] === 'Input.dispatchMouseEvent' && args[2].type === 'mouseMoved') return new Promise(() => {});
    return send(...args);
  };
  const result = await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
  assert.equal(result.performed, true);
  assert.equal(result.verified, false);
  assert.deepEqual(h.calls.filter(c => c[0] === 'Input.dispatchMouseEvent').map(c => c[2].type), ['mousePressed', 'mouseReleased']);
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

test('a stalled renderer cleanup does not delay detach or remove a newer task guard', async () => {
  const h = harness();
  let complete;
  h.chrome.scripting.executeScript = options => {
    h.calls.push(['script', options]);
    return new Promise(resolve => { complete = resolve; });
  };
  h.browser.attached.set(7, 'old-binding');
  h.browser.guards.set(7, { taskId: 'old-task', scriptId: 'old-script' });
  const releasing = h.browser.release(7, 'old-task');
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(h.calls.some(c => c[0] === 'detach'), 'detach is dispatched before renderer cleanup replies');
  await assert.rejects(releasing, /BROWSER_LIFECYCLE_TIMEOUT/);
  const next = { taskId: 'new-task', scriptId: 'new-script' };
  h.browser.guards.set(7, next);
  const cleanup = h.calls.find(c => c[0] === 'script')[1];
  const context = { calls: 0, __quietbridgePopupGuard() { this.calls++; }, args: cleanup.args };
  vm.createContext(context);
  vm.runInContext('(' + cleanup.func.toString() + ')(...args)', context);
  assert.equal(context.calls, 0, 'expired cleanup does not execute against a later page guard');
  complete([{ result: true }]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.browser.guards.get(7), next);
});

test('an unacknowledged detach prevents reset until the original detach completes', async () => {
  const h = harness();
  let complete;
  h.chrome.debugger.detach = () => new Promise(resolve => { complete = resolve; });
  h.browser.attached.set(7, 'old-binding');
  await assert.rejects(h.browser.release(7), /BROWSER_LIFECYCLE_TIMEOUT/);
  await assert.rejects(h.browser.reset(h.state), /BROWSER_LIFECYCLE_TIMEOUT/);
  complete();
  await new Promise(resolve => setImmediate(resolve));
  await h.browser.reset(h.state);
  assert.equal(h.browser.releasing.size, 0);
});

test('a secondary cleanup failure preserves the original operation failure', async () => {
  const h = harness();
  h.chrome.debugger.attach = async () => h.navigate();
  h.browser.release = async () => { throw new Error('BROWSER_LIFECYCLE_TIMEOUT: cleanup'); };
  await assert.rejects(h.browser.attach(h.state, h.command('screenshot')), /STALE_DOCUMENT/);
});

test('persisted task ownership permits hook cleanup after a worker restart', async () => {
  const h = harness();
  await h.browser.release(7, 'task');
  const cleanup = h.calls.find(c => c[0] === 'script');
  assert.equal(cleanup[1].args[0], 'task');
  assert.ok(cleanup[1].args[1] > Date.now(), 'cleanup has an execution deadline');
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

test('foreign extension frames are reported as an actionable Chrome restriction without fallback or replay', async () => {
  const message = 'Cannot access a chrome-extension:// URL of different extension';
  for (const stage of ['attach', 'Page.enable', 'Page.addScriptToEvaluateOnNewDocument', 'Input.insertText']) {
    const h = harness();
    let attempts = 0;
    if (stage === 'attach') h.chrome.debugger.attach = async () => { attempts++; throw new Error(message); };
    else {
      const send = h.chrome.debugger.sendCommand;
      h.chrome.debugger.sendCommand = async (...args) => {
        if (args[1] === stage) { attempts++; throw new Error(message); }
        return send(...args);
      };
    }
    await assert.rejects(h.browser.run(h.command('fill', { ref: 2, snapshotId: 'snap', value: 'once' }), h.state),
      error => error.message.startsWith('BROWSER_DEBUGGER_RESTRICTED') && error.message.includes(stage));
    assert.equal(attempts, 1, stage);
    assert.equal(h.calls.some(c => c[0] === 'update' || c[0] === 'create'), false);
    assert.equal(h.calls.some(c => c[0] === 'script' && /\.click\(/u.test(c[1].func?.toString() || '')), false);
  }
});

test('foreign-frame rejection preserves Chrome wording and reads only bounded iframe extension IDs', async () => {
  const h = harness(), id = 'iahnhfdhidomcpggpaimmmahffihkfnj';
  const message = 'Cannot access a chrome-extension:// URL of different extension';
  let attempts = 0;
  h.chrome.debugger.attach = async () => { attempts++; throw new Error(message); };
  h.chrome.scripting.executeScript = async options => {
    h.calls.push(['script', options]);
    assert.deepEqual(options.target, { tabId: 7, documentIds: ['doc'] });
    assert.equal(options.world, undefined);
    const frames = ['chrome-extension://' + id + '/JSON-handle/JSON-handle.html?private=secret',
      'https://example.org/frame?private=secret', 'chrome-extension://' + id + '/duplicate'];
    const context = vm.createContext({ URL, document: { baseURI: h.state.url,
      querySelectorAll: () => frames.map(src => ({ getAttribute: () => src })) } });
    return [{ documentId: 'doc', result: vm.runInContext('(' + options.func.toString() + ')()', context) }];
  };
  await assert.rejects(h.browser.run(h.command('navigate', { url: 'https://example.org/next' }), h.state), error => {
    assert.match(error.message, /Chrome 原文：Cannot access/);
    assert.deepEqual(error.debuggerFailure, { reason: 'foreign_extension_page', stage: 'attach',
      chromeMessage: message, foreignFrameExtensionIds: [id], frameEvidence: 'top_document_iframe_src', inputDispatched: false });
    assert.equal(JSON.stringify(error.debuggerFailure).includes('private'), false);
    return true;
  });
  assert.equal(attempts, 1);
  assert.equal(h.calls.some(c => ['update', 'detach', 'Page.captureScreenshot'].includes(c[0])), false);
});

test('actual debugger occupation has a different error and never tries frame reads or takeover', async () => {
  const h = harness();
  const message = 'Another debugger is already attached to the tab with id: 7.';
  h.chrome.debugger.attach = async () => { throw new Error(message); };
  await assert.rejects(h.browser.run(h.command('navigate', { url: 'https://example.org/next' }), h.state), error => {
    assert.match(error.message, /^BROWSER_DEBUGGER_IN_USE/);
    assert.equal(error.debuggerFailure.reason, 'debugger_in_use');
    assert.equal(error.debuggerFailure.chromeMessage, message);
    return true;
  });
  assert.equal(h.calls.some(c => ['update', 'detach', 'script'].includes(c[0])), false);
});

test('child attach failure after a click never claims the original input was undispatched', async () => {
  const h = harness(); h.popup('https://example.org/child');
  let attaches = 0;
  h.chrome.debugger.attach = async () => {
    if (++attaches === 2) throw new Error('Cannot access a chrome-extension:// URL of different extension');
  };
  await assert.rejects(h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state), error => {
    assert.equal(error.debuggerFailure.stage, 'attach');
    assert.equal('inputDispatched' in error.debuggerFailure, false);
    return true;
  });
  assert.equal(h.calls.filter(c => c[0] === 'Input.dispatchMouseEvent').length, 2);
});

test('unavailable or replaced frame evidence cannot hide Chrome rejection or report another document', async () => {
  for (const mode of ['unavailable', 'wrong_document', 'navigation']) {
    const h = harness(), message = 'Cannot access a chrome-extension:// URL of different extension';
    h.chrome.debugger.attach = async () => { throw new Error(message); };
    h.chrome.scripting.executeScript = async () => {
      if (mode === 'unavailable') throw new Error('scripting unavailable');
      if (mode === 'navigation') h.navigate();
      return [{ documentId: mode === 'wrong_document' ? 'other' : 'doc', result: ['a'.repeat(32)] }];
    };
    await assert.rejects(h.browser.run(h.command('navigate', { url: 'https://example.org/next' }), h.state), error => {
      assert.match(error.message, /^BROWSER_DEBUGGER_RESTRICTED/);
      assert.equal(error.debuggerFailure.frameEvidence, 'not_checked');
      assert.deepEqual(error.debuggerFailure.foreignFrameExtensionIds, []);
      return true;
    });
    assert.equal(h.calls.some(c => c[0] === 'update'), false);
  }
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
  context.__quietbridgePopupGuard('task', 'arm');
  context.window.open('/topic'); context.window.open('javascript:alert(1)');
  assert.deepEqual(Array.from(context.__quietbridgePopupGuard('task', 'drain')), ['https://example.org/topic']);
  context.__quietbridgePopupGuard('task', 'release');
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
  context.__quietbridgePopupGuard('task', 'release');
});

test('a document script that never replies cannot block later observations; late responses are discarded', async () => {
  for (const operation of ['snapshot', 'read', 'diagnostics']) {
    const h = harness();
    let finish;
    h.chrome.scripting.executeScript = async () => new Promise(resolve => { finish = resolve; });
    const command = h.command(operation);
    command.expiresAt = Date.now() / 1000 + 0.04;
    await assert.rejects(h.browser.run(command, h.state), /BROWSER_OBSERVATION_TIMEOUT/);
    h.chrome.scripting.executeScript = async () => [{ documentId: 'doc', result: { current: true } }];
    assert.deepEqual(await h.browser.run(h.command('diagnostics'), h.state), { current: true });
    finish([{ documentId: 'doc', result: { late: true } }]);
    assert.equal(h.calls.some(c => c[0] === 'Input.dispatchMouseEvent'), false);
  }
});

const probeActions = h => h.calls.filter(c => !['get'].includes(c[0])).map(c => c[0]);
const probe = (h, stage) => h.browser.run(h.command('render_probe', { stage }), h.state);

test('render probe stages perform only their explicit operation and never export pixels', async () => {
  const h = harness();
  let r = await probe(h, 'inspect');
  assert.equal(r.attachedBefore, false); assert.equal(r.performed, false);
  assert.deepEqual(probeActions(h), []);
  h.calls.length = 0;
  r = await probe(h, 'attach');
  assert.equal(r.performed, true); assert.equal(r.pageEnabledAfter, false);
  assert.deepEqual(probeActions(h), ['attach']);
  h.calls.length = 0;
  r = await probe(h, 'attach');
  assert.equal(r.attachedBefore, true); assert.equal(r.performed, false);
  assert.deepEqual(probeActions(h), []);
  r = await probe(h, 'enable');
  assert.equal(r.pageEnabledAfter, true);
  assert.deepEqual(probeActions(h), ['Page.enable']);
  h.calls.length = 0;
  await probe(h, 'enable');
  assert.deepEqual(probeActions(h), []);
  r = await probe(h, 'layout');
  assert.deepEqual(probeActions(h), ['Page.getLayoutMetrics']);
  assert.equal(r.viewportWidth, 1600);
  h.calls.length = 0;
  r = await probe(h, 'capture');
  assert.deepEqual(probeActions(h), ['Page.captureScreenshot']);
  assert.equal(r.captureDataLength, 8);
  assert.deepEqual(Object.keys(r).sort(), ['stage', 'startedAt', 'completedAt', 'performed',
    'attachedBefore', 'attachedAfter', 'pageEnabledBefore', 'pageEnabledAfter', 'captureDataLength'].sort());
  assert.ok(r.completedAt >= r.startedAt);
  await assert.rejects(probe(h, 'capture'), /STAGE_REQUIRED/);
  assert.deepEqual(probeActions(h), ['Page.captureScreenshot']);
});

test('probe never auto-attaches/enables/captures out of order or accepts arbitrary CDP', async () => {
  const h = harness();
  for (const stage of ['enable', 'layout', 'capture', 'Runtime.evaluate'])
    await assert.rejects(probe(h, stage));
  assert.deepEqual(probeActions(h), []);
  await probe(h, 'attach'); h.calls.length = 0;
  for (const stage of ['layout', 'capture']) await assert.rejects(probe(h, stage), /STAGE_REQUIRED/);
  await assert.rejects(h.browser.run(h.command('render_probe', { stage: 'inspect', method: 'Page.enable' }), h.state));
  assert.deepEqual(probeActions(h), []);
});

test('normal screenshot after attach-only still enables Page domain', async () => {
  const h = harness(); await probe(h, 'attach'); h.calls.length = 0;
  await h.browser.run(h.command('screenshot'), h.state);
  assert.deepEqual(probeActions(h), ['Page.enable', 'Page.getLayoutMetrics', 'Page.captureScreenshot']);
});

test('probe checks permission/document identity and invalidates layout on detach/release', async () => {
  for (const change of ['navigate', 'move', 'revoke']) {
    const h = harness(); await probe(h, 'attach'); await probe(h, 'enable'); await probe(h, 'layout');
    h.calls.length = 0; h[change]();
    await assert.rejects(probe(h, 'capture'), /STALE/);
    assert.equal(h.calls.some(c => c[0] === 'Page.captureScreenshot'), false);
  }
  for (const stop of ['detach', 'release']) {
    const h = harness(); await probe(h, 'attach'); await probe(h, 'enable'); await probe(h, 'layout');
    if (stop === 'detach') h.chrome.debugger.onDetach.listener({ tabId: 7 });
    else await h.browser.release(7, 'task');
    assert.equal(h.browser.probeLayouts.size, 0); assert.equal(h.browser.pageEnabled.size, 0);
  }
});

test('probe consumes stale or failed capture without automatic retry', async () => {
  const h = harness(); await probe(h, 'attach'); await probe(h, 'enable'); await probe(h, 'layout');
  h.browser.probeLayouts.get(7).at -= 31000;
  await assert.rejects(probe(h, 'capture'), /STAGE_REQUIRED/);
  await probe(h, 'layout');
  let attempts = 0;
  h.chrome.debugger.sendCommand = async () => { attempts++; throw new Error('synthetic capture failure'); };
  await assert.rejects(probe(h, 'capture'), /synthetic/);
  await assert.rejects(probe(h, 'capture'), /STAGE_REQUIRED/);
  assert.equal(attempts, 1);
});

test('probe attach timeout cleans up late attachment without replay', async () => {
  const h = harness(); let finish; let count = 0;
  h.chrome.debugger.attach = () => { count++; return new Promise(resolve => { finish = resolve; }); };
  h.chrome.channelTimers = { setTimeout: fn => setTimeout(fn, 10), clearTimeout };
  await assert.rejects(probe(h, 'attach'), /BROWSER_LIFECYCLE_TIMEOUT/);
  await assert.rejects(probe(h, 'attach'), /BACKGROUND_UNAVAILABLE/);
  finish(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(count, 1); assert.deepEqual(probeActions(h), ['detach']);
  assert.equal(h.browser.attached.size, 0); assert.equal(h.browser.releasing.size, 0);
});

test('probe preserves Chrome restriction code and cleans up when authorization changes during attach', async () => {
  const h = harness();
  h.chrome.debugger.attach = async () => { throw new Error('Cannot access a chrome-extension:// URL of different extension'); };
  await assert.rejects(probe(h, 'attach'), /BROWSER_DEBUGGER_RESTRICTED/);
  const other = harness(); other.chrome.debugger.attach = async () => other.revoke();
  await assert.rejects(probe(other, 'attach'), /STALE/);
  assert.equal(other.browser.attached.size, 0);
  assert.ok(other.calls.some(c => c[0] === 'detach'));
});

function guardPage() {
  const handlers = new Map(); let now = 1000;
  const original = function() { throw new Error('native popup must not open'); };
  const context = vm.createContext({ URL, Date: { now: () => now },
    location: { href: 'https://example.org/' }, window: { open: original },
    document: { querySelector() { return null; } }, HTMLAnchorElement: class {},
    addEventListener(type, fn) { const set = handlers.get(type) || new Set(); set.add(fn); handlers.set(type, set); },
    removeEventListener(type, fn) { handlers.get(type)?.delete(fn); } });
  return { context, original, handlers, tick() { now += 100; },
    install(task, lease, claim = true) {
      context.args = [task, lease, claim ? { issuedAt: now, deadline: now + 1200 } : null];
      return vm.runInContext(`(${installPopupGuard.toString()})(...args)`, context);
    } };
}

test('new verified lease replaces leftover guard without stacking hooks or leaking queued URLs', () => {
  const p = guardPage();
  assert.equal(p.install('old-task', 'old-binding'), 'ready');
  const old = p.context.__quietbridgePopupGuard;
  old('old-task', 'arm', 'old-binding'); p.context.window.open('/old-private');
  p.tick(); assert.equal(p.install('new-task', 'new-binding'), 'ready');
  const current = p.context.__quietbridgePopupGuard;
  assert.notEqual(current, old);
  assert.deepEqual(Array.from(current('new-task', 'drain', 'new-binding')), []);
  assert.equal(current('old-task', 'release', 'old-binding'), null);
  assert.equal(p.install('old-task', 'old-binding', false), 'conflict', 'old preload cannot retake a document');
  assert.equal(p.context.__quietbridgePopupGuard, current);
  assert.equal(p.handlers.get('click').size, 1); assert.equal(p.handlers.get('submit').size, 1);
  current('new-task', 'arm', 'new-binding'); p.context.window.open('/new');
  assert.deepEqual(Array.from(current('new-task', 'drain', 'new-binding')), ['https://example.org/new']);
  current('new-task', 'release', 'new-binding');
  assert.equal(p.context.window.open, p.original); assert.equal(p.handlers.get('click').size, 0);
});

test('same task rebind rejects delayed cleanup, expired claims and older in-flight installs', () => {
  const p = guardPage(); p.install('task', 'old'); p.tick();
  p.install('task', 'new'); const current = p.context.__quietbridgePopupGuard;
  assert.equal(current('task', 'release', 'old'), null);
  assert.equal(current('task', 'release'), null, 'unscoped cleanup must not release a binding lease');
  p.context.args = ['task', 'old', { issuedAt: 1000, deadline: 2000 }];
  assert.equal(vm.runInContext(`(${installPopupGuard.toString()})(...args)`, p.context), 'conflict');
  p.context.args = ['task', 'old', { issuedAt: 1000, deadline: 1050 }];
  assert.equal(vm.runInContext(`(${installPopupGuard.toString()})(...args)`, p.context), 'expired');
  assert.equal(p.context.__quietbridgePopupGuard, current);
});

test('legacy guard is reported without guessing owner or stacking a new wrapper', () => {
  const p = guardPage(); let calls = 0;
  p.context.__quietbridgePopupGuard = () => { calls++; return null; };
  const previous = p.context.__quietbridgePopupGuard;
  assert.equal(p.install('new', 'binding'), 'legacy');
  assert.equal(calls, 0); assert.equal(p.context.window.open, p.original);
  assert.equal(p.context.__quietbridgePopupGuard, previous);
});

test('input refuses an unconfirmed guard without dispatching navigation or pointer events', async () => {
  for (const status of ['legacy', 'conflict', 'expired', null]) {
    const h = harness(); const script = h.chrome.scripting.executeScript;
    h.chrome.scripting.executeScript = async o => o.func.name === 'installPopupGuard' ? [{ result: status }] : script(o);
    await assert.rejects(h.browser.run(h.command('navigate', { url: 'https://example.org/next' }), h.state),
      status === 'legacy' ? /POPUP_GUARD_LEGACY/ : /POPUP_GUARD_CONFLICT/);
    assert.equal(h.calls.some(c => c[0] === 'update' || c[0].startsWith('Input.')), false);
  }
});

test('same document accepts a new task after an earlier guard cleanup was missed', async () => {
  const h = harness(); const p = guardPage(); const script = h.chrome.scripting.executeScript;
  // Use the real page-world function rather than a mock success: reproduce the
  // old document closure surviving loss of worker-local state.
  p.context.Date = Date;
  h.chrome.scripting.executeScript = async o => {
    if (o.world !== 'MAIN') return script(o);
    p.context.args = o.args;
    return [{ documentId: 'doc', result: vm.runInContext(`(${o.func.toString()})(...args)`, p.context) }];
  };
  await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
  await new Promise(resolve => setTimeout(resolve, 2));
  const next = new BackgroundBrowser(h.chrome);
  h.state.taskId = 'new-task'; h.state.bindingId = 'new-binding';
  await next.run(h.command('navigate', { url: 'https://example.org/next' }), h.state);
  assert.equal(p.context.__quietbridgePopupGuard.owner, 'new-task');
  assert.equal(h.calls.filter(c => c[0] === 'update').length, 1);
});

test('rebinding replaces the old preload and scopes current cleanup to the new binding', async () => {
  const h = harness();
  await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
  h.state.bindingId = 'next-binding';
  await h.browser.run(h.command('click', { ref: 1, snapshotId: 'snap' }), h.state);
  assert.equal(h.calls.filter(c => c[0] === 'Page.removeScriptToEvaluateOnNewDocument').length, 1);
  await h.browser.release(7, 'task');
  const cleanup = h.calls.findLast(c => c[0] === 'script' && c[1].target.allFrames);
  assert.equal(cleanup[1].args[2], 'next-binding');
});

test('preview captures a bounded viewport from the fixed tab without input or focus', async () => {
  const h = harness();
  const lease = { ...h.command('preview'), leaseId: 'lease' };
  const frame = await h.browser.previewFrame(lease, h.state);
  assert.equal(frame.leaseId, 'lease');
  assert.equal(frame.identity.documentId, 'doc');
  const captures = h.calls.filter(c => c[0] === 'Page.captureScreenshot');
  assert.equal(captures.length, 1);
  assert.equal(captures[0][2].clip.scale, 0.625);
  assert.equal(captures[0][2].captureBeyondViewport, false);
  assert.equal(h.calls.some(c => ['update', 'create', 'script'].includes(c[0]) || c[0].startsWith('Input.')), false);
});

test('preview discards navigation during capture and oversized data without retry', async () => {
  for (const navigate of [true, false]) {
    const h = harness();
    const send = h.chrome.debugger.sendCommand;
    h.chrome.debugger.sendCommand = async (...args) => {
      if (args[1] === 'Page.captureScreenshot') {
        if (navigate) h.navigate();
        return { data: 'a'.repeat(180001) };
      }
      return send(...args);
    };
    await assert.rejects(h.browser.previewFrame({ ...h.command('preview'), leaseId: 'lease' }, h.state),
      navigate ? /STALE_DOCUMENT/ : /PREVIEW_FRAME_TOO_LARGE/);
  }
});

test('expired or revoked preview lease cannot attach or capture', async () => {
  for (const revoke of [true, false]) {
    const h = harness();
    const lease = { ...h.command('preview'), leaseId: 'lease' };
    if (revoke) h.revoke(); else lease.expiresAt = Date.now() / 1000 - 1;
    await assert.rejects(h.browser.previewFrame(lease, h.state), /STALE_TARGET/);
    assert.equal(h.calls.length, 0);
  }
});
