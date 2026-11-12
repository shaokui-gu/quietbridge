import test from 'node:test';
import assert from 'node:assert/strict';
import { channelChrome } from './channels.mjs';

const event = () => ({ listeners: new Set(), addListener(fn) { this.listeners.add(fn); },
  removeListener(fn) { this.listeners.delete(fn); } });
const area = () => ({ values: {}, async get(keys) { return Object.fromEntries(keys.map(k => [k, this.values[k]])); },
  async set(v) { Object.assign(this.values, v); }, async remove(keys) { for (const k of keys) delete this.values[k]; } });
test('three channels keep storage, command identities and debugger ownership separate', async () => {
  const messages = [], detached = [];
  const api = { storage: { local: area(), session: area(), onChanged: event() },
    runtime: { onMessage: event(), onInstalled: event(), getURL: p => 'chrome-extension://test/' + p,
      async sendNativeMessage(_, m) { messages.push(m); return { ok: true }; } },
    permissions: { onRemoved: event(), onAdded: event() },
    tabs: { onRemoved: event() },
    webNavigation: Object.fromEntries(['onBeforeNavigate', 'onCommitted', 'onDOMContentLoaded', 'onCompleted',
      'onErrorOccurred', 'onHistoryStateUpdated'].map(n => [n, event()])),
    debugger: { onEvent: event(), onDetach: event(), async attach() {},
      async detach(t) { detached.push(t.tabId); }, async sendCommand(t) { return t.tabId; } },
    alarms: { create() {}, clear() {}, onAlarm: event() } };
  const entries = ['a', 'b', 'c'].map(c => channelChrome(api, c.repeat(64)));
  for (let i = 0; i < 3; i++) {
    const chrome = entries[i].chrome;
    await chrome.storage.session.set({ observer: { tabId: i + 1 }, lastCommand: { requestId: 'same' } });
    await chrome.runtime.sendNativeMessage('host', { action: 'sync' });
    await chrome.debugger.attach({ tabId: i + 1 }, '1.3');
    assert.equal(await chrome.debugger.sendCommand({ tabId: i + 1 }, 'test'), i + 1);
  }
  assert.equal(new Set(messages.map(m => m.channel)).size, 3);
  for (let i = 0; i < 3; i++) assert.equal((await entries[i].chrome.storage.session.get('observer')).observer.tabId, i + 1);
  await assert.rejects(() => entries[1].chrome.debugger.attach({ tabId: 1 }, '1.3'), /TARGET_IN_USE/);
  entries[0].dispose();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(detached, [1]);
  assert.equal(await entries[1].chrome.debugger.sendCommand({ tabId: 2 }, 'test'), 2);
  assert.equal(await entries[2].chrome.debugger.sendCommand({ tabId: 3 }, 'test'), 3);
  assert.throws(() => entries[0].chrome.debugger.sendCommand({ tabId: 1 }, 'test'), /SESSION_ROUTING_INVALID/);
  entries[1].dispose(); entries[2].dispose();
});

test('channel disposal releases only its recorded page lease and does not wait for a stalled renderer', async () => {
  const cleanup = [], detached = [];
  const api = { storage: { local: area(), session: area(), onChanged: event() },
    runtime: { onMessage: event(), onInstalled: event(), getURL: p => 'chrome-extension://test/' + p },
    permissions: { onRemoved: event(), onAdded: event() }, tabs: { onRemoved: event() },
    webNavigation: Object.fromEntries(['onBeforeNavigate', 'onCommitted', 'onDOMContentLoaded', 'onCompleted',
      'onErrorOccurred', 'onHistoryStateUpdated'].map(n => [n, event()])),
    debugger: { onEvent: event(), onDetach: event(), async attach() {},
      async detach(t) { detached.push(t.tabId); } },
    scripting: { executeScript(options) { cleanup.push(options); return new Promise(() => {}); } },
    alarms: { clear() {}, onAlarm: event() } };
  const first = channelChrome(api, 'd'.repeat(64)), other = channelChrome(api, 'e'.repeat(64));
  await first.chrome.storage.session.set({ observer: { taskId: 'task', bindingId: 'old-binding', tabId: 17 },
    managedTaskId: 'task', managedTabs: [17] });
  await other.chrome.storage.session.set({ observer: { taskId: 'other', bindingId: 'other-binding', tabId: 18 } });
  await first.chrome.debugger.attach({ tabId: 17 }, '1.3');
  await other.chrome.debugger.attach({ tabId: 18 }, '1.3');
  first.dispose(); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(detached, [17]);
  assert.equal(cleanup.length, 1);
  assert.deepEqual(cleanup[0].args.slice(0, 2), ['task', 'old-binding']);
  assert.deepEqual(cleanup[0].target, { tabId: 17, allFrames: true });
  assert.ok(cleanup[0].args[2] > Date.now());
  await assert.rejects(first.chrome.debugger.attach({ tabId: 17 }, '1.3'), /SESSION_ROUTING_INVALID/);
  // The channel-level guard already excludes other active sessions before a
  // page-world replacement can be attempted.
  const fresh = channelChrome(api, 'f'.repeat(64));
  await assert.rejects(fresh.chrome.debugger.attach({ tabId: 18 }, '1.3'), /TARGET_IN_USE/);
  await fresh.chrome.debugger.attach({ tabId: 17 }, '1.3');
  other.dispose(); fresh.dispose();
});
