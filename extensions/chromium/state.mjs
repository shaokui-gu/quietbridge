export function safeUrl(value) {
  try {
    const u = new URL(value);
    if (u.hostname === 'chromewebstore.google.com' ||
        (u.hostname === 'chrome.google.com' && u.pathname.startsWith('/webstore'))) return null;
    return ['http:', 'https:'].includes(u.protocol) ? `${u.origin}${u.pathname}` : null;
  } catch { return null; }
}

export function originOf(value) {
  const url = safeUrl(value);
  return url ? new URL(url).origin : null;
}

// 仅允许一个简单复合选择器：标签、ID、class；不允许属性、伪类、组合器或列表。
export function validSelector(value) {
  return typeof value === 'string' && (value === '' || (value.length <= 128 &&
    /^(?:[a-zA-Z][a-zA-Z0-9-]*)?(?:[.#][a-zA-Z_][a-zA-Z0-9_-]*)*$/.test(value)));
}

const cleared = () => ({ loaded: false, domReady: false, contentReady: null,
  readyState: null, bodyPresent: false, bodyVisible: false, elementCount: 0,
  conditionMatched: null });

export function initialState(binding) {
  return { ...binding, mappingVerified: false, documentId: null, navigationEpoch: 0,
    lastTime: 0, pending: false, paused: false, url: null, phase: 'bound',
    ...cleared(), sequence: 0 };
}

export function pause(state, timeStamp) {
  return { ...state, ...cleared(), paused: true, pending: false, documentId: null,
    url: null, phase: 'paused', navigationEpoch: state.navigationEpoch + 1,
    lastTime: Math.max(state.lastTime, timeStamp), sequence: state.sequence + 1 };
}

export function reduce(state, event) {
  if (!state || state.paused || event.tabId !== state.tabId || event.frameId !== 0 ||
      !Number.isFinite(event.timeStamp) || event.timeStamp < state.lastTime) return state;
  if (state.phase === 'error' && event.type !== 'before') return state;
  const url = safeUrl(event.url);
  if (!url || (!state.globalOrigins && originOf(url) !== state.authorizedOrigin)) return pause(state, event.timeStamp);
  const s = { ...state };
  const reset = () => Object.assign(s, cleared());
  if (event.type === 'before') {
    s.navigationEpoch++;
    s.documentId = null;
    s.pending = true;
    reset();
  } else if (event.type === 'committed') {
    if (typeof event.documentId !== 'string' || !event.documentId ||
        (!s.pending && s.documentId !== null)) return state;
    if (!s.pending) s.navigationEpoch++;
    s.documentId = event.documentId;
    s.pending = false;
    reset();
  } else {
    if (s.pending) {
      if (event.type !== 'error') return state;
    } else if (!s.documentId || event.documentId !== s.documentId) return state;
    if (event.type === 'history') {
      s.navigationEpoch++;
      reset();
    } else if (event.type === 'dom') {
      s.domReady = true; s.readyState = s.loaded ? 'complete' : 'interactive';
    }
    else if (event.type === 'completed') {
      s.loaded = true; s.domReady = true; s.readyState = 'complete';
    } else if (event.type === 'content' || event.type === 'pageshow') {
      if (url !== s.url) return state;
      const r = event.readiness;
      if (!r || !['loading', 'interactive', 'complete'].includes(r.readyState) ||
          typeof r.bodyPresent !== 'boolean' || typeof r.bodyVisible !== 'boolean' ||
          !Number.isInteger(r.elementCount) || r.elementCount < 0 || r.elementCount > 10000 ||
          (s.selector ? typeof r.conditionMatched !== 'boolean' : r.conditionMatched !== null)) return state;
      s.readyState = r.readyState;
      s.loaded = r.readyState === 'complete';
      s.domReady = r.readyState !== 'loading';
      s.bodyPresent = r.bodyPresent;
      s.bodyVisible = r.bodyVisible;
      s.elementCount = r.elementCount;
      s.conditionMatched = r.conditionMatched;
    } else if (event.type === 'error') reset();
    else return state;
  }
  s.url = url;
  s.phase = event.type;
  s.lastTime = event.timeStamp;
  s.sequence++;
  return s;
}
