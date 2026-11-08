import { startChannel } from './channel-worker.mjs';
import { channelChrome } from './channels.mjs';

const channels = new Map();
let revision = 0;
async function discover() {
  const token = ++revision;
  try {
    const result = await chrome.runtime.sendNativeMessage('com.cua.browser_observer', { action: 'channels' });
    if (token !== revision || !result?.ok || !Array.isArray(result.channels)) return;
    const live = new Set(result.channels.filter(k => /^[a-f0-9]{64}$/.test(k)).slice(0, 3));
    for (const [key, entry] of channels) if (!live.has(key)) {
      entry.dispose(); channels.delete(key);
    }
    for (const key of live) if (!channels.has(key)) {
      const entry = channelChrome(chrome, key);
      channels.set(key, entry);
      startChannel(entry.chrome);
    }
  } catch { /* Preserve active channels through a transient host failure. */ }
}
// Root channel owns enablement and the extension popup. It never selects a
// session based on the active browser tab or most recent task.
startChannel(chrome);
chrome.alarms.create('quietbridge-channels', { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'quietbridge-channels') discover(); });
setInterval(discover, 1000);
discover();
