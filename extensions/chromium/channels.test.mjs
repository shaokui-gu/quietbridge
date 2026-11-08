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
