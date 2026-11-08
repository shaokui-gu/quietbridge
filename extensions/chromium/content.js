(() => {
  if (!['http:', 'https:'].includes(location.protocol)) return;
  if (globalThis.__quietbridgeObserver) return;
  globalThis.__cuaObserverStop?.(); // Cleanup previously injected legacy content.
  const legacyNames = ['Observer', 'Snapshot', 'Prepare', 'Scroll', 'FocusInput', 'InputReady', 'PointMatches', 'VerifyFill', 'Select', 'Diagnostics', 'ObserverStop'];
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
  globalThis.__quietbridgeObserver = config => {
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
  // Resolve every frame again when using a reference: a child document can
  // navigate without changing the top document's navigation epoch.
  function frameChain(doc) {
    const chain = [];
    try {
      while (doc !== document) {
        if (chain.length >= 8) return null;
        const frame = doc.defaultView?.frameElement;
        if (!frame?.isConnected || frame.contentDocument !== doc ||
            frame.contentWindow.location.origin !== location.origin || !visible(frame)) return null;
        for (let ancestor = frame.parentElement; ancestor; ancestor = ancestor.parentElement) {
          const style = getComputedStyle(ancestor);
          if ((style.transform && style.transform !== 'none') || (style.zoom && style.zoom !== '1' && style.zoom !== 'normal')) return null;
        }
        const transform = getComputedStyle(frame).transform;
        if (transform && transform !== 'none') {
          const m = new DOMMatrixReadOnly(transform);
          if (!m.is2D || m.b !== 0 || m.c !== 0 || m.a <= 0 || m.d <= 0) return null;
        }
        chain.push(frame);
        doc = frame.ownerDocument;
      }
      return chain;
    } catch { return null; }
  }
  function toParent(frame, x, y) {
    const r = frame.getBoundingClientRect();
    const sx = r.width / frame.offsetWidth, sy = r.height / frame.offsetHeight;
    if (!Number.isFinite(sx) || !Number.isFinite(sy) || sx <= 0 || sy <= 0) return null;
    return { x: r.left + (frame.clientLeft + x) * sx, y: r.top + (frame.clientTop + y) * sy };
  }
  function toTop(doc, x, y) {
    const chain = frameChain(doc);
    if (!chain) return null;
    let point = { x, y };
    for (const frame of chain) {
      point = toParent(frame, point.x, point.y);
      if (!point) return null;
    }
    return point;
  }
  function hitTarget(target, point) {
    const chain = frameChain(target.ownerDocument);
    if (!chain) return false;
    let x = point.x, y = point.y, doc = document;
    for (const frame of chain.slice().reverse()) {
      if (doc.elementFromPoint(x, y) !== frame) return false;
      const r = frame.getBoundingClientRect();
      const sx = r.width / frame.offsetWidth, sy = r.height / frame.offsetHeight;
      if (!Number.isFinite(sx) || !Number.isFinite(sy) || sx <= 0 || sy <= 0) return false;
      x = (x - r.left) / sx - frame.clientLeft;
      y = (y - r.top) / sy - frame.clientTop;
      doc = frame.contentDocument;
    }
    const r = target.getBoundingClientRect();
    if (x < r.left || x >= r.right || y < r.top || y >= r.bottom) return false;
    const hit = doc.elementFromPoint(x, y);
    return hit === target || target.contains(hit);
  }
  function snapshot(limit) {
    const elements = [];
    function collect(doc) {
      for (const node of doc.querySelectorAll(SELECTOR)) {
        if (elements.length >= limit) break;
        if (!visible(node)) continue;
        const rect = node.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const start = toTop(doc, rect.left, rect.top), end = toTop(doc, rect.right, rect.bottom);
        if (!start || !end) continue;
        const name = (node.getAttribute('aria-label') || node.getAttribute('placeholder') ||
          node.getAttribute('alt') || node.textContent || '').replace(/\s+/g, ' ').trim();
        elements.push({ ref: refFor(node), tag: node.tagName.toLowerCase().slice(0, 32),
          role: defaultRole(node).slice(0, 32), name: name.slice(0, 80),
          disabled: node.disabled === true || node.getAttribute('aria-disabled') === 'true',
          x: Math.round(start.x), y: Math.round(start.y),
          width: Math.round(end.x - start.x), height: Math.round(end.y - start.y) });
      }
      for (const frame of doc.querySelectorAll('iframe,frame')) {
        if (elements.length >= limit) break;
        try { if (frame.contentDocument && frameChain(frame.contentDocument)) collect(frame.contentDocument); } catch {}
      }
    }
    collect(document);
    return elements;
  }
  globalThis.__quietbridgeSnapshot = config => {
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
    const win = node.ownerDocument.defaultView;
    return node instanceof win.HTMLInputElement || node instanceof win.HTMLTextAreaElement;
  }
  function targetFor(config, action) {
    if (!bound(config) || config.snapshotId !== snapshotId) return null;
    return action.ref === undefined ? document.scrollingElement : refs.get(action.ref);
  }
  globalThis.__quietbridgePrepare = (config, action) => {
    const target = targetFor(config, action);
    if (!target?.isConnected) return { ok: false, reason: 'stale_ref' };
    if (!visible(target) || target.disabled || target.getAttribute('aria-disabled') === 'true') {
      return { ok: false, reason: 'not_interactable' };
    }
    target.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
    const chain = frameChain(target.ownerDocument);
    if (!chain) return { ok: false, reason: 'stale_frame' };
    for (const frame of chain) frame.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
    const r = target.getBoundingClientRect();
    const view = target.ownerDocument.defaultView;
    const left = Math.max(0, r.left), top = Math.max(0, r.top);
    const right = Math.min(view.innerWidth, r.right), bottom = Math.min(view.innerHeight, r.bottom);
    if (right <= left || bottom <= top) return { ok: false, reason: 'outside_viewport' };
    const point = toTop(target.ownerDocument, (left + right) / 2, (top + bottom) / 2);
    if (!point || !hitTarget(target, point)) return { ok: false, reason: 'obscured' };
    return { ok: true, ...point };
  };
  globalThis.__quietbridgeScroll = (config, action) => {
    const target = targetFor(config, action);
    if (!target?.isConnected || !Number.isInteger(action.deltaY) || Math.abs(action.deltaY) > 10) return fail('stale_ref');
    if (action.ref !== undefined) {
      const prepared = globalThis.__quietbridgePrepare(config, action);
      if (!prepared.ok) return fail(prepared.reason);
    }
    const root = target.ownerDocument.scrollingElement;
    let scroller = target;
    while (scroller && scroller !== root) {
      const overflow = getComputedStyle(scroller).overflowY;
      if (['auto', 'scroll', 'overlay'].includes(overflow) && scroller.scrollHeight > scroller.clientHeight) break;
      scroller = scroller.parentElement || root;
    }
    if (!scroller?.isConnected || !visible(scroller)) return fail('not_scrollable');
    const beforeY = Math.round(scroller.scrollTop);
    const maxY = Math.max(0, Math.round(scroller.scrollHeight - scroller.clientHeight));
    const deltaPixels = -action.deltaY * 40;
    const expectedY = Math.max(0, Math.min(maxY, beforeY + deltaPixels));
    if (action.deltaY !== 0) scroller.scrollBy({ top: -action.deltaY * 40, left: 0, behavior: 'instant' });
    const afterY = Math.round(scroller.scrollTop);
    const moved = afterY !== beforeY;
    const atBoundary = action.deltaY !== 0 && expectedY === beforeY;
    const verified = moved && Math.sign(afterY - beforeY) === Math.sign(deltaPixels);
    return { performed: true, verified,
      observed: { route: 'extension_dom', direction: deltaPixels > 0 ? 'down' : deltaPixels < 0 ? 'up' : 'none',
        beforeY, afterY, maxY, expectedY, moved, atBoundary,
        outcome: verified ? 'scrolled' : atBoundary ? 'at_boundary' : action.deltaY === 0 ? 'no_op' : 'no_movement' } };
  };
  globalThis.__quietbridgeFocusInput = (config, action) => {
    const target = targetFor(config, action);
    if (!target?.isConnected || (!writable(target) && !target.isContentEditable) ||
        target.disabled || target.readOnly) return false;
    // This is DOM focus inside the bound document, never application activation.
    target.focus({ preventScroll: true });
    if (target.ownerDocument.activeElement !== target) return false;
    if (writable(target)) {
      try { target.setSelectionRange(0, target.value.length); } catch { return false; }
    } else {
      const range = target.ownerDocument.createRange();
      range.selectNodeContents(target);
      const selection = target.ownerDocument.defaultView.getSelection();
      selection.removeAllRanges(); selection.addRange(range);
    }
    return true;
  };
  globalThis.__quietbridgeInputReady = (config, action) => {
    const target = targetFor(config, action);
    return !!target?.isConnected && !!frameChain(target.ownerDocument) && target.ownerDocument.activeElement === target && !target.disabled && !target.readOnly;
  };
  globalThis.__quietbridgePointMatches = (config, action, point) => {
    const target = targetFor(config, action);
    if (!target?.isConnected || !visible(target) || target.disabled) return false;
    return hitTarget(target, point);
  };
  globalThis.__quietbridgeVerifyFill = (config, action) => {
    const target = targetFor(config, action);
    return !!target?.isConnected && !!frameChain(target.ownerDocument) && (target.value ?? target.textContent) === (action.value ?? '');
  };
  globalThis.__quietbridgeSelect = (config, action) => {
    const target = targetFor(config, action);
    if (!target || !frameChain(target.ownerDocument) || !(target instanceof target.ownerDocument.defaultView.HTMLSelectElement) || target.disabled) return fail('not_a_select');
    target.value = action.value ?? '';
    target.dispatchEvent(new target.ownerDocument.defaultView.Event('input', { bubbles: true }));
    target.dispatchEvent(new target.ownerDocument.defaultView.Event('change', { bubbles: true }));
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
  globalThis.__quietbridgeDiagnostics = config => {
    if (!bound(config)) return null;
    return { url: `${location.origin}${location.pathname}`, readyState: document.readyState,
      errors: errors.slice(),
      viewport: { width: Math.round(innerWidth), height: Math.round(innerHeight) },
      scroll: { x: Math.round(scrollX), y: Math.round(scrollY) } };
  };
  globalThis.__quietbridgeObserverStop = () => {
    for (const name of legacyNames) delete globalThis['__cua' + name];
    binding = null;
    observer.disconnect();
    clearTimeout(timer);
    for (const type of types) removeEventListener(type, onLifecycle);
    removeEventListener('error', onError);
    removeEventListener('unhandledrejection', onRejection);
    delete globalThis.__quietbridgeObserver;
    delete globalThis.__quietbridgeSnapshot;
    for (const key of ['__quietbridgePrepare', '__quietbridgeScroll', '__quietbridgeFocusInput', '__quietbridgeInputReady', '__quietbridgePointMatches', '__quietbridgeVerifyFill', '__quietbridgeSelect']) delete globalThis[key];
    delete globalThis.__quietbridgeDiagnostics;
    delete globalThis.__quietbridgeObserverStop;
  };
  // An already-running pre-upgrade worker can finish cleanup before Chrome reloads it.
  for (const name of legacyNames) globalThis['__cua' + name] = globalThis['__quietbridge' + name];
})();
