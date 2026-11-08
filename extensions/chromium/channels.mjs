// Every observer instance owns its storage, command deduplication and CDP guard.
// Global enablement is shared; user actions never pick a task by current focus.
const debuggerOwners = new Map();
export function channelChrome(api, channel) {
  if (!/^[a-f0-9]{64}$/.test(channel)) throw new Error('invalid channel');
  const prefix = `qb:${channel}:`;
  let disposed = false;
  const removers = [];
  const timers = new Set();
  function event(source, filter = () => true) {
    return {
      addListener(fn) {
        const listener = (...args) => { if (!disposed && filter(...args)) return fn(...args); };
        source.addListener(listener);
        removers.push(() => source.removeListener?.(listener));
      },
      removeListener() {},
    };
  }
  function storage(area, scoped) {
    const key = k => scoped || k === 'boundTarget' ? prefix + k : k;
    return {
      async get(keys) {
        const defaults = keys && !Array.isArray(keys) && typeof keys === 'object' ? keys : {};
        const list = typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(defaults);
        const raw = await area.get(list.map(key));
        return Object.fromEntries(list.map(k => [k, raw[key(k)] ?? defaults[k]]));
      },
      async set(values) {
        if (disposed) return;
        return area.set(Object.fromEntries(Object.entries(values).map(([k, v]) => [key(k), v])));
      },
      async remove(keys) { return area.remove((Array.isArray(keys) ? keys : [keys]).map(key)); },
    };
  }
  const runtime = { ...api.runtime };
  runtime.sendNativeMessage = async (host, message) => {
    if (disposed) throw new Error('channel stopped');
    return api.runtime.sendNativeMessage(host, message.action === 'ping' ? message : { ...message, channel });
  };
  runtime.onMessage = event(api.runtime.onMessage, (message, sender) => Boolean(sender.tab) &&
    !sender.url?.startsWith(api.runtime.getURL('popup.html')));
  runtime.onInstalled = { addListener() {} };
  // Channels reuse root's enabled native port; their timers and alarm names are
  // scoped so one completion cannot clear another channel's wakeup.
  runtime.connectNative = () => {
    const port = api.runtime.connectNative('com.cua.browser_observer');
    removers.push(() => { try { port.disconnect(); } catch {} });
    return port;
  };
  const wrapped = { ...api };
  wrapped.runtime = runtime;
  wrapped.storage = { local: storage(api.storage.local, false), session: storage(api.storage.session, true),
    onChanged: event(api.storage.onChanged, (_, area) => area === 'local') };
  wrapped.permissions = { ...api.permissions };
  wrapped.permissions.onRemoved = event(api.permissions.onRemoved);
  wrapped.permissions.onAdded = event(api.permissions.onAdded);
  wrapped.webNavigation = { ...api.webNavigation };
  for (const name of ['onBeforeNavigate', 'onCommitted', 'onDOMContentLoaded', 'onCompleted', 'onErrorOccurred', 'onHistoryStateUpdated'])
    wrapped.webNavigation[name] = event(api.webNavigation[name]);
  wrapped.tabs = { ...api.tabs };
  wrapped.tabs.onRemoved = event(api.tabs.onRemoved);
  wrapped.debugger = { ...api.debugger };
  wrapped.debugger.attach = async (target, version) => {
    const owner = debuggerOwners.get(target.tabId);
    if (owner && owner !== channel) throw new Error('TARGET_IN_USE: target belongs to another session');
    debuggerOwners.set(target.tabId, channel);
    try { return await api.debugger.attach(target, version); }
    catch (error) { if (!owner) debuggerOwners.delete(target.tabId); throw error; }
  };
  wrapped.debugger.detach = async target => {
    if (debuggerOwners.get(target.tabId) !== channel) return;
    debuggerOwners.delete(target.tabId);
    return api.debugger.detach(target);
  };
  wrapped.debugger.sendCommand = (target, ...args) => {
    if (disposed || debuggerOwners.get(target.tabId) !== channel) throw new Error('SESSION_ROUTING_INVALID: debugger not owned');
    return api.debugger.sendCommand(target, ...args);
  };
  for (const name of ['onEvent', 'onDetach']) if (api.debugger[name]) wrapped.debugger[name] = event(api.debugger[name]);
  wrapped.alarms = {
    create: (name, options) => !disposed && api.alarms.create(prefix + name, options),
    onAlarm: event(api.alarms.onAlarm, a => {
      if (!a.name.startsWith(prefix)) return false;
      a.name = a.name.slice(prefix.length);
      return true;
    }),
  };
  // Timer functions are injected so removal cancels every channel keepalive.
  wrapped.channelTimers = {
    setTimeout(fn, ms) { const id = setTimeout(() => { timers.delete(id); if (!disposed) fn(); }, ms); timers.add(id); return id; },
    clearTimeout(id) { timers.delete(id); clearTimeout(id); },
    setInterval(fn, ms) { const id = setInterval(() => { if (!disposed) fn(); }, ms); timers.add(id); return id; },
    clearInterval(id) { timers.delete(id); clearInterval(id); },
  };
  return { chrome: wrapped, dispose() {
    disposed = true;
    for (const remove of removers) remove();
    for (const id of timers) { clearTimeout(id); clearInterval(id); }
    api.alarms.clear(prefix + 'observer-sync');
    // Detach only debuggers proven to belong to this channel's saved targets.
    api.storage.session.get([prefix + 'observer', prefix + 'managedTabs']).then(async raw => {
      const ids = new Set([raw[prefix + 'observer']?.tabId, ...(raw[prefix + 'managedTabs'] || [])]);
      for (const tabId of ids) if (Number.isInteger(tabId)) {
        if (debuggerOwners.get(tabId) !== channel) continue;
        debuggerOwners.delete(tabId);
        try { await api.debugger.detach({ tabId }); } catch {}
      }
    });
  } };
}
