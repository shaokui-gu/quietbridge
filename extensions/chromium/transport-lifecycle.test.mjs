import test from 'node:test';
import assert from 'node:assert/strict';
import { startChannel } from './channel-worker.mjs';
import { channelChrome } from './channels.mjs';

const event = () => ({ listeners: new Set(),
  addListener(fn) { this.listeners.add(fn); }, removeListener(fn) { this.listeners.delete(fn); },
  emit(...args) { for (const fn of this.listeners) fn(...args); } });
const area = values => ({ values,
  async get(keys) {
    const names = typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys || values);
    return Object.fromEntries(names.filter(k => k in values).map(k => [k, structuredClone(values[k])]));
  },
  async set(next) { Object.assign(values, structuredClone(next)); },
  async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) delete values[key]; }
});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, timeout = 4500) {
  const end = Date.now() + timeout;
  while (!predicate() && Date.now() < end) await delay(20);
  assert.ok(predicate(), 'lifecycle did not make progress before its bounded deadline');
}

function fixture(t, { stale = false, stall = 'initialize' } = {}) {
  const key = 'a'.repeat(64), prefix = 'qb:' + key + ':';
  const calls = [], inputs = [], scripts = [], pendingScripts = [], entries = [];
  const assignments = new Map(), refresh = new Set();
  const rootTimers = new Set();
  let closed = false;
  const assignment = { taskId: 'fixture-task', token: 'b'.repeat(64), bindingId: 'c'.repeat(32),
    assignmentVersion: 1, globalOrigins: true, selector: '', tabId: 7, browserWindowId: 2, mappingVerified: false };
  assignments.set(key, stale ? null : assignment);
  const stored = stale ? {
    [prefix + 'observer']: { ...assignment, taskId: 'old-fixture-task', documentId: 'old-doc',
      navigationEpoch: 1, sequence: 1, url: 'https://example.org/fixture', documentUrl: 'https://example.org/fixture' },
    [prefix + 'managedTabs']: [7], [prefix + 'managedTaskId']: 'old-fixture-task'
  } : {};
  const api = {
    storage: { local: area({ enabled: true }), session: area(stored), onChanged: event() },
    permissions: { async contains() { return true; }, onAdded: event(), onRemoved: event() },
    runtime: { id: 'fixture-extension', getURL: path => 'chrome-extension://fixture-extension/' + path,
      onMessage: event(), onInstalled: event(),
      connectNative() { return { onMessage: event(), onDisconnect: event(), postMessage() {}, disconnect() {} }; },
      async sendNativeMessage(_host, message) {
        if (closed) return { ok: false, code: 'APP_NOT_RUNNING' };
        calls.push(structuredClone(message));
        const base = { ok: true, protocolVersion: 2, heartbeatAt: Date.now() / 1000, applicationSession: 'fixture-app-session' };
        if (message.action !== 'sync') return base;
        if (!message.channel) return { ...base, taskAssignment: 'no_assignment', assignment: null };
        if (message.candidates) refresh.delete(message.channel);
        const selected = assignments.get(message.channel);
        return { ...base, panelBridge: true, taskAssignment: selected ? 'assigned' : 'needs_target_selection',
          requestCandidates: refresh.has(message.channel),
          activeTaskId: selected?.taskId || 'new-unassigned-task', assignment: structuredClone(selected) };
      }
    },
    tabs: { onRemoved: event(),
      async get(id) { return { id, windowId: 2, title: 'Fixture', url: 'https://example.org/fixture' }; },
      async query(query) {
        if (stall === 'candidates' && !query.active) return new Promise(() => {});
        if (stall === 'metadata' && query.active) return new Promise(() => {});
        return [{ id: 7, windowId: 2, title: 'Fixture', url: 'https://example.org/fixture' }];
      },
      async update(...args) { inputs.push(args); assert.fail('passive lifecycle must not navigate or activate'); }
    },
    webNavigation: { ...Object.fromEntries(['onBeforeNavigate', 'onCommitted', 'onDOMContentLoaded', 'onCompleted',
      'onErrorOccurred', 'onHistoryStateUpdated'].map(name => [name, event()])),
      async getFrame() { return { documentId: 'fixture-doc', frameId: 0, url: 'https://example.org/fixture' }; }
    },
    scripting: { async executeScript(options) {
      scripts.push(options);
      if (options.target.tabId === 7 && ((stall === 'initialize' && options.files) ||
          (stall === 'configure' && options.func?.toString().includes('__quietbridgeObserver?')) ||
          (stall === 'cleanup' && !options.files))) return new Promise(resolve => pendingScripts.push({ options, resolve }));
      return [{ result: true }];
    } },
    debugger: { onEvent: event(), onDetach: event(), async attach() { assert.fail('passive lifecycle must not attach'); },
      async detach() {}, async sendCommand() { assert.fail('passive lifecycle must not issue CDP'); } },
    alarms: { create() {}, clear() {}, onAlarm: event() },
    channelTimers: {
      setTimeout(fn, ms) { if (closed) return; const id = setTimeout(() => { rootTimers.delete(id); fn(); }, ms); rootTimers.add(id); return id; },
      clearTimeout(id) { rootTimers.delete(id); clearTimeout(id); },
      setInterval(fn, ms) { if (closed) return; const id = setInterval(fn, ms); rootTimers.add(id); return id; },
      clearInterval(id) { rootTimers.delete(id); clearInterval(id); }
    }
  };
  startChannel(api);
  const entry = channelChrome(api, key);
  entries.push(entry);
  startChannel(entry.chrome);
  t.after(() => {
    closed = true; for (const active of entries) active.dispose();
    for (const timer of rootTimers) { clearTimeout(timer); clearInterval(timer); }
  });
  return { api, entry, calls, scripts, inputs, key, prefix, assignment, assignments, refresh, pendingScripts,
    addChannel(letter, tabId) {
      const other = letter.repeat(64);
      assignments.set(other, { ...assignment, taskId: 'task-' + letter, bindingId: letter.repeat(32), tabId });
      const active = channelChrome(api, other); entries.push(active); startChannel(active.chrome);
      return { key: other, entry: active };
    },
    syncs: channel => calls.filter(c => c.action === 'sync' && (c.channel || 'root') === channel) };
}

test('a stalled observer initialization cannot stop session heartbeats while root stays connected', async t => {
  const h = fixture(t);
  await until(() => h.scripts.some(s => s.files));
  await until(() => h.syncs('root').length >= 3 && h.syncs(h.key).length >= 2);
  assert.equal(h.inputs.length, 0);
  assert.ok(h.syncs(h.key).some(s => s.assignmentTitle === 'Fixture'), 'title evidence must not wait indefinitely for page scripts');
  const report = h.syncs(h.key).find(s => s.observerStatus?.state === 'failed');
  assert.equal(report?.observerStatus.code, 'BROWSER_LIFECYCLE_TIMEOUT');
  assert.equal(report?.observerStatus.stage, 'observer_load');
});

test('a new unassigned task keeps syncing even when previous-task page cleanup never replies', async t => {
  const h = fixture(t, { stale: true, stall: 'cleanup' });
  await until(() => h.scripts.length > 0);
  await until(() => h.syncs('root').length >= 3 && h.syncs(h.key).length >= 2, 6000);
  assert.equal(h.inputs.length, 0);
  assert.equal(h.calls.some(c => c.commandResult), false, 'no page command is needed to exercise this lifecycle');
  assert.ok(h.syncs(h.key).some(s => s.lifecycleError?.code === 'BROWSER_LIFECYCLE_TIMEOUT'));
  assert.equal(h.api.storage.session.values[h.prefix + 'observer'], null);
});

test('observer configuration retries are bounded; late replies cannot restore an old binding', async t => {
  const h = fixture(t, { stall: 'configure' });
  await until(() => h.syncs(h.key).some(s => s.observerStatus?.attempt === 2 && s.observerStatus.state === 'failed'), 11000);
  assert.equal(h.pendingScripts.length, 2, 'only passive initialization has a finite retry');
  const replacement = { ...h.assignment, bindingId: 'd'.repeat(32), assignmentVersion: 2, tabId: 8 };
  h.assignments.set(h.key, replacement);
  await until(() => h.syncs(h.key).some(s => s.observerStatus?.bindingId === replacement.bindingId && s.observerStatus.state === 'ready'));
  const since = h.syncs(h.key).length;
  for (const pending of h.pendingScripts) pending.resolve([{ result: true }]);
  await until(() => h.syncs(h.key).length > since);
  assert.equal(h.syncs(h.key).at(-1).observerStatus.bindingId, replacement.bindingId);
  assert.equal(h.inputs.length, 0);
});

test('candidate enumeration can time out without disconnecting an unassigned session', async t => {
  const h = fixture(t, { stale: true, stall: 'candidates' });
  h.refresh.add(h.key);
  await until(() => h.syncs(h.key).some(s => s.lifecycleError?.stage === 'candidate_lookup'), 6000);
  assert.ok(h.syncs(h.key).length >= 2);
  assert.equal(h.calls.some(c => c.commandResult), false);
  assert.equal(h.inputs.length, 0);
});

test('active-tab metadata can time out while the actual host exchange continues', async t => {
  const h = fixture(t, { stall: 'metadata' });
  await until(() => h.syncs(h.key).some(s => s.lifecycleError?.stage === 'window_evidence'));
  assert.equal(h.syncs(h.key).at(-1).windowEvidence, null);
  assert.equal(h.syncs(h.key).at(-1).observerStatus.state, 'ready');
  assert.equal(h.inputs.length, 0);
});

test('three session workers stay independent when one observer stalls and is then stopped', async t => {
  const h = fixture(t);
  const second = h.addChannel('e', 8), third = h.addChannel('f', 9);
  await until(() => [second.key, third.key].every(k => h.syncs(k).some(s => s.observerStatus?.state === 'ready')));
  h.refresh.add(second.key); h.refresh.add(third.key);
  h.entry.dispose();
  const stopped = h.syncs(h.key).length;
  const counts = [second.key, third.key].map(k => h.syncs(k).length);
  await until(() => [second.key, third.key].every((k, i) => h.syncs(k).length >= counts[i] + 2));
  assert.equal(h.syncs(h.key).length, stopped);
  assert.ok([second.key, third.key].every(k => h.calls.some(c => c.channel === k && c.candidates?.length === 1)));
  assert.equal(h.inputs.length, 0);
});
