(() => {
  if (!['http:', 'https:'].includes(location.protocol)) return;
  if (globalThis.__cuaObserver) return;
  let timer;
  let previous = '';
  let binding = null;
  function visible(node) {
    if (!node) return false;
    const style = getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      style.visibility !== 'collapse' && Number(style.opacity) !== 0 &&
      node.getClientRects().length > 0;
  }
  function report(force = false, lifecycle = 'content') {
    if (!binding || location.origin !== binding.authorizedOrigin) return;
    const readiness = { readyState: document.readyState, bodyPresent: !!document.body,
      bodyVisible: visible(document.body), elementCount: Math.min(document.getElementsByTagName('*').length, 10000),
      conditionMatched: binding.selector ? visible(document.querySelector(binding.selector)) : null };
    const url = `${location.origin}${location.pathname}`;
    const signature = JSON.stringify({ url, readiness });
    if (!force && signature === previous) return;
    previous = signature;
    chrome.runtime.sendMessage({ action: 'content', url, readiness, lifecycle,
      navigationEpoch: binding.navigationEpoch, bindingId: binding.bindingId }).catch(() => {});
  }
  function schedule() {
    if (timer || !binding) return;
    timer = setTimeout(() => { timer = null; report(); }, 250);
  }
  globalThis.__cuaObserver = config => {
    if (!config || !Number.isSafeInteger(config.navigationEpoch) ||
        typeof config.bindingId !== 'string' || config.authorizedOrigin !== location.origin ||
        typeof config.selector !== 'string' || config.selector.length > 128 ||
        (config.selector && !/^(?:[a-zA-Z][a-zA-Z0-9-]*)?(?:[.#][a-zA-Z_][a-zA-Z0-9_-]*)*$/.test(config.selector))) return;
    binding = config;
    report(true);
  };
  const onLifecycle = event => report(true, event.type === 'pageshow' ? 'pageshow' : 'content');
  const types = ['DOMContentLoaded', 'load', 'pageshow', 'popstate', 'resize'];
  for (const type of types) addEventListener(type, onLifecycle);
  const observer = new MutationObserver(schedule);
  observer.observe(document, { childList: true, subtree: true,
    attributes: true, attributeFilter: ['class', 'style', 'hidden'] });
  const refs = new Map();
  const ids = new WeakMap();
  let refSeq = 0;
  let snapshotSeq = 0;
  let snapshotId = null;
  const SELECTOR = 'a[href],button,input,select,textarea,[role],[contenteditable="true"]';
  function refFor(node) {
    let id = ids.get(node);
    if (!id || !refs.has(id)) {
      if (refs.size >= 5000) refs.clear();
      id = ++refSeq;
      ids.set(node, id);
      refs.set(id, node);
    }
    return id;
  }
  function defaultRole(node) {
    const tag = node.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (node.getAttribute('type') || 'text').toLowerCase();
      return ({ checkbox: 'checkbox', radio: 'radio', submit: 'button',
        button: 'button', search: 'searchbox', range: 'slider' })[type] || 'textbox';
    }
    return (node.getAttribute('role') || '').slice(0, 32);
  }
  function snapshot(limit) {
    const elements = [];
    for (const node of document.querySelectorAll(SELECTOR)) {
      if (elements.length >= limit) break;
      if (!visible(node)) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      const name = (node.getAttribute('aria-label') || node.getAttribute('placeholder') ||
        node.getAttribute('alt') || node.textContent || '').replace(/\s+/g, ' ').trim();
      elements.push({ ref: refFor(node), tag: node.tagName.toLowerCase().slice(0, 32),
        role: defaultRole(node).slice(0, 32), name: name.slice(0, 80),
        disabled: node.disabled === true || node.getAttribute('aria-disabled') === 'true',
        x: Math.round(rect.left), y: Math.round(rect.top),
        width: Math.round(rect.width), height: Math.round(rect.height) });
    }
    return elements;
  }
  globalThis.__cuaSnapshot = config => {
    if (!binding || !config || config.bindingId !== binding.bindingId ||
        config.navigationEpoch !== binding.navigationEpoch ||
        location.origin !== binding.authorizedOrigin) return null;
    refs.clear();
    snapshotId = `${binding.bindingId}:${binding.navigationEpoch}:${++snapshotSeq}`;
    return { snapshotId, url: `${location.origin}${location.pathname}`, readyState: document.readyState,
      elementCount: Math.min(document.getElementsByTagName('*').length, 10000),
      elements: snapshot(300) };
  };
  function bound(config) {
    return !!(binding && config && config.bindingId === binding.bindingId &&
      config.navigationEpoch === binding.navigationEpoch &&
      location.origin === binding.authorizedOrigin);
  }
  const fail = reason => ({ performed: false, verified: false,
    observed: { reason: String(reason).slice(0, 100) } });
  function writable(node) {
    return node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement;
  }
  function targetFor(config, action) {
    if (!bound(config) || config.snapshotId !== snapshotId) return null;
    return action.ref === undefined ? document.scrollingElement : refs.get(action.ref);
  }
  globalThis.__cuaPrepare = (config, action) => {
    const target = targetFor(config, action);
    if (!target?.isConnected) return { ok: false, reason: 'stale_ref' };
    if (!visible(target) || target.disabled || target.getAttribute('aria-disabled') === 'true') {
      return { ok: false, reason: 'not_interactable' };
    }
    target.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
    const r = target.getBoundingClientRect();
    const left = Math.max(0, r.left), top = Math.max(0, r.top);
    const right = Math.min(innerWidth, r.right), bottom = Math.min(innerHeight, r.bottom);
    if (right <= left || bottom <= top) return { ok: false, reason: 'outside_viewport' };
    const x = (left + right) / 2, y = (top + bottom) / 2;
    const hit = document.elementFromPoint(x, y);
    if (hit !== target && !target.contains(hit)) return { ok: false, reason: 'obscured' };
    return { ok: true, x, y };
  };
  globalThis.__cuaFocusInput = (config, action) => {
    const target = targetFor(config, action);
    if (!target?.isConnected || (!writable(target) && !target.isContentEditable) ||
        target.disabled || target.readOnly) return false;
    // This is DOM focus inside the bound document, never application activation.
    target.focus({ preventScroll: true });
    if (document.activeElement !== target) return false;
    if (writable(target)) {
      try { target.setSelectionRange(0, target.value.length); } catch { return false; }
    } else {
      const range = document.createRange();
      range.selectNodeContents(target);
      const selection = getSelection();
      selection.removeAllRanges(); selection.addRange(range);
    }
    return true;
  };
  globalThis.__cuaInputReady = (config, action) => {
    const target = targetFor(config, action);
    return !!target?.isConnected && document.activeElement === target && !target.disabled && !target.readOnly;
  };
  globalThis.__cuaPointMatches = (config, action, point) => {
    const target = targetFor(config, action);
    if (!target?.isConnected || !visible(target) || target.disabled) return false;
    const r = target.getBoundingClientRect();
    if (point.x < r.left || point.x >= r.right || point.y < r.top || point.y >= r.bottom) return false;
    const hit = document.elementFromPoint(point.x, point.y);
    return hit === target || target.contains(hit);
  };
  globalThis.__cuaVerifyFill = (config, action) => {
    const target = targetFor(config, action);
    return !!target?.isConnected && (target.value ?? target.textContent) === (action.value ?? '');
  };
  globalThis.__cuaSelect = (config, action) => {
    const target = targetFor(config, action);
    if (!(target instanceof HTMLSelectElement) || target.disabled) return fail('not_a_select');
    target.value = action.value ?? '';
    target.dispatchEvent(new Event('input', { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    return { performed: true, verified: target.value === (action.value ?? ''),
      observed: { route: 'dom', length: target.value.length } };
  };
  const errors = [];
  function recordError(value) {
    const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, 200);
    if (!text || errors.some(item => item.message === text)) return;
    errors.push({ message: text, at: Date.now() });
    if (errors.length > 20) errors.shift();
  }
  const onError = event => recordError(event && event.message);
  const onRejection = event => recordError(event && event.reason && event.reason.message
    ? event.reason.message : event.reason);
  addEventListener('error', onError);
  addEventListener('unhandledrejection', onRejection);
  globalThis.__cuaDiagnostics = config => {
    if (!bound(config)) return null;
    return { url: `${location.origin}${location.pathname}`, readyState: document.readyState,
      errors: errors.slice(),
      viewport: { width: Math.round(innerWidth), height: Math.round(innerHeight) },
      scroll: { x: Math.round(scrollX), y: Math.round(scrollY) } };
  };
  globalThis.__cuaObserverStop = () => {
    binding = null;
    observer.disconnect();
    clearTimeout(timer);
    for (const type of types) removeEventListener(type, onLifecycle);
    removeEventListener('error', onError);
    removeEventListener('unhandledrejection', onRejection);
    delete globalThis.__cuaObserver;
    delete globalThis.__cuaSnapshot;
    for (const key of ['__cuaPrepare', '__cuaFocusInput', '__cuaInputReady', '__cuaPointMatches', '__cuaVerifyFill', '__cuaSelect']) delete globalThis[key];
    delete globalThis.__cuaDiagnostics;
    delete globalThis.__cuaObserverStop;
  };
})();
