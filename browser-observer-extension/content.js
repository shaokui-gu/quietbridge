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
    return { url: `${location.origin}${location.pathname}`, readyState: document.readyState,
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
  function setValue(node, value) {
    if (writable(node)) {
      const proto = node instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(node, value);
      return node.value === value;
    }
    if (node.isContentEditable) {
      node.textContent = value;
      return (node.textContent || '') === value;
    }
    return false;
  }
  globalThis.__cuaAct = (config, action) => {
    if (!bound(config) || !action || typeof action !== 'object') return null;
    const operation = action.operation;
    const target = operation === 'scroll' && action.ref === undefined
      ? document.scrollingElement : refs.get(action.ref);
    if (!target || (target !== document.scrollingElement && !target.isConnected)) {
      return fail('stale_ref');
    }
    if (target !== document.scrollingElement && !visible(target)) return fail('not_visible');
    try {
      if (operation === 'click') {
        target.focus?.();
        target.click();
        return { performed: true, verified: false,
          observed: { tag: target.tagName.toLowerCase().slice(0, 32) } };
      }
      if (operation === 'fill') {
        if (typeof action.value !== 'string' || action.value.length > 1000) return fail('bad_value');
        target.focus?.();
        const applied = setValue(target, action.value);
        target.dispatchEvent(new Event('input', { bubbles: true }));
        target.dispatchEvent(new Event('change', { bubbles: true }));
        // 必须在事件之后读回：受控组件会在事件里改写值。
        const actual = writable(target) || target.isContentEditable ? target.value ?? target.textContent : '';
        return { performed: applied, verified: (actual || '') === action.value,
          observed: { length: String(actual || '').length } };
      }
      if (operation === 'select') {
        if (!(target instanceof HTMLSelectElement)) return fail('not_a_select');
        if (typeof action.value !== 'string') return fail('bad_value');
        target.value = action.value;
        target.dispatchEvent(new Event('input', { bubbles: true }));
        target.dispatchEvent(new Event('change', { bubbles: true }));
        const ok = target.value === action.value;
        return { performed: ok, verified: ok, observed: { length: target.value.length } };
      }
      if (operation === 'scroll') {
        if (!Number.isInteger(action.deltaY) || Math.abs(action.deltaY) > 10) return fail('bad_delta');
        const root = document.scrollingElement || document.documentElement;
        const isRoot = target === document.scrollingElement || target === document.documentElement;
        const read = () => isRoot ? Math.round(window.scrollY || root.scrollTop)
          : Math.round(target.scrollTop);
        const before = read();
        // 与 cua_scroll 一致：deltaY > 0 表示显示上方内容，即 scrollTop 减小。
        const next = before - action.deltaY * 40;
        if (isRoot) {
          root.scrollTop = next;
          window.scrollTo(0, next);
        } else {
          target.scrollTop = next;
        }
        const after = read();
        return { performed: true, verified: after !== before,
          observed: { before, after,
            viewport: Math.round(window.innerHeight || 0),
            scrollHeight: Math.round(root.scrollHeight || 0),
            clientHeight: Math.round(root.clientHeight || 0),
            target: isRoot ? 'root' : target.tagName.toLowerCase().slice(0, 16) } };
      }
      return fail('unknown_operation');
    } catch (error) {
      return fail(error && error.message ? error.message : 'act_failed');
    }
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
    delete globalThis.__cuaAct;
    delete globalThis.__cuaDiagnostics;
    delete globalThis.__cuaObserverStop;
  };
})();
