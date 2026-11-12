(() => {
  if (!['http:', 'https:'].includes(location.protocol)) return;
  const contentVersion = '0.4.6';
  if (globalThis.__quietbridgeObserver && globalThis.__quietbridgeContentVersion === contentVersion) return;
  globalThis.__quietbridgeObserverStop?.();
  globalThis.__cuaObserverStop?.(); // Cleanup previously injected legacy content.
  globalThis.__quietbridgeContentVersion = contentVersion;
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
    if (config?.deadline !== undefined && (!Number.isFinite(config.deadline) || Date.now() >= config.deadline)) return false;
    if (!config || !Number.isSafeInteger(config.navigationEpoch) ||
        typeof config.bindingId !== 'string' || config.authorizedOrigin !== location.origin ||
        typeof config.selector !== 'string' || config.selector.length > 128 ||
        (config.selector && !/^(?:[a-zA-Z][a-zA-Z0-9-]*)?(?:[.#][a-zA-Z_][a-zA-Z0-9_-]*)*$/.test(config.selector))) return;
    binding = config;
    report(true);
    return true;
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
  let readCache = null;
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
  function linkEvidence(node) {
    if (node.tagName.toLowerCase() !== 'a') return {};
    try {
      const u = new URL(node.href, node.ownerDocument.baseURI || location.href);
      if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password ||
          (u.origin + u.pathname).length > 2048) return {};
      return { href: u.origin + u.pathname, urlHasQuery: !!u.search, urlHasFragment: !!u.hash };
    } catch { return {}; }
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
    let truncated = false;
    function collect(doc) {
      for (const node of doc.querySelectorAll(SELECTOR)) {
        if (truncated) return;
        if (!visible(node)) continue;
        const rect = node.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const start = toTop(doc, rect.left, rect.top), end = toTop(doc, rect.right, rect.bottom);
        if (!start || !end) continue;
        if (elements.length === limit) { truncated = true; return; }
        const name = (node.getAttribute('aria-label') || node.getAttribute('placeholder') ||
          node.getAttribute('alt') || node.textContent || '').replace(/\s+/g, ' ').trim();
        elements.push({ ref: refFor(node), tag: node.tagName.toLowerCase().slice(0, 32),
          role: defaultRole(node).slice(0, 32), name: name.slice(0, 80),
          disabled: node.disabled === true || node.getAttribute('aria-disabled') === 'true',
          x: Math.round(start.x), y: Math.round(start.y),
          width: Math.round(end.x - start.x), height: Math.round(end.y - start.y), ...linkEvidence(node) });
      }
      for (const frame of doc.querySelectorAll('iframe,frame')) {
        if (truncated) return;
        try { if (frame.contentDocument && frameChain(frame.contentDocument)) collect(frame.contentDocument); } catch {}
      }
    }
    collect(document);
    return { elements, truncated };
  }
  globalThis.__quietbridgeSnapshot = config => {
    if (!binding || !config || config.bindingId !== binding.bindingId ||
        config.navigationEpoch !== binding.navigationEpoch ||
        location.origin !== binding.authorizedOrigin) return null;
    refs.clear();
    readCache = null;
    snapshotId = `${binding.bindingId}:${binding.navigationEpoch}:${++snapshotSeq}`;
    const captured = snapshot(300);
    return { snapshotId, url: `${location.origin}${location.pathname}`, readyState: document.readyState,
      elementCount: Math.min(document.getElementsByTagName('*').length, 10000),
      elements: captured.elements, coverage: { returnedElements: captured.elements.length, limit: 300,
        truncated: captured.truncated, scope: 'visible_interactive_top_and_accessible_frames',
        elementCountScope: 'top_document_dom_nodes_capped_10000' } };
  };
  function bound(config) {
    return !!(binding && config && config.bindingId === binding.bindingId &&
      config.navigationEpoch === binding.navigationEpoch &&
      location.origin === binding.authorizedOrigin);
  }
  function renderedText(node) {
    // An empty innerText is still an observation. Falling back on truthiness
    // would expose textContent from hidden descendants of a rendered block.
    return typeof node.innerText === 'string' ? node.innerText : (node.textContent || '');
  }
  // Reading uses the bound, rendered DOM and its login state. It never fetches
  // site APIs, evaluates caller code or claims unloaded posts were observed.
  globalThis.__quietbridgeRead = (config, options = {}) => {
    if (!bound(config)) return null;
    const mode = options.mode || 'content';
    const offset = options.offset ?? 0, limit = options.limit ?? 30, maxChars = options.maxChars ?? 10000;
    if (!['links', 'content'].includes(mode) || !Number.isInteger(offset) || offset < 0 ||
        !Number.isInteger(limit) || limit < 1 || limit > 100 || !Number.isInteger(maxChars) ||
        maxChars < 4000 || maxChars > 16000) return null;
    if (options.readId) {
      if (!readCache || readCache.id !== options.readId || readCache.mode !== mode ||
          readCache.bindingId !== binding.bindingId || readCache.navigationEpoch !== binding.navigationEpoch ||
          readCache.documentUrl !== location.href) return null;
    } else {
      if (offset !== 0) return null;
      const items = [];
      let sourceChars = 0, sourceTruncated = false;
      const root = document.querySelector('main,[role="main"]') || document.body;
      if (!root) return null;
      let nodes = mode === 'links' ? root.querySelectorAll('a[href]') :
        root.querySelectorAll('article,[role="article"]');
      const contentKind = nodes.length ? 'article' : 'text';
      if (mode === 'content' && !nodes.length) nodes = root.querySelectorAll('p,h1,h2,h3,li,pre,blockquote');
      const nodeSet = new Set();
      for (let i = 0; i < Math.min(nodes.length, 10000); i++) nodeSet.add(nodes[i]);
      let scanned = 0;
      for (const node of nodes) {
        if (++scanned > 10000 || items.length >= 2000 || sourceChars >= 262144) { sourceTruncated = true; break; }
        if (!visible(node) || !node.isConnected) continue;
        if (mode === 'links') {
          const evidence = linkEvidence(node);
          if (!evidence.href) continue;
          const name = (node.getAttribute('aria-label') || renderedText(node)).slice(0, 4096)
            .replace(/\s+/g, ' ').trim().slice(0, 160);
          const item = { name, ...evidence };
          const cost = name.length + evidence.href.length;
          if (sourceChars + cost > 262144) { sourceTruncated = true; break; }
          items.push(item); sourceChars += cost;
        } else {
          // Skip nested blocks to avoid counting the same rendered post twice.
          let nested = false;
          for (let parent = node.parentElement; parent && parent !== root; parent = parent.parentElement) {
            if (nodeSet.has(parent)) { nested = true; break; }
          }
          if (nested) continue;
          const body = node.querySelector?.('.cooked') || node;
          if (!visible(body)) continue;
          const rawText = renderedText(body);
          let take = Math.min(rawText.length, 262144 - sourceChars);
          // Do not cut a surrogate pair at the bounded source capture edge.
          if (take < rawText.length && /[\uD800-\uDBFF]/u.test(rawText[take - 1] || '') &&
              /[\uDC00-\uDFFF]/u.test(rawText[take])) take--;
          const textTruncated = take < rawText.length;
          sourceTruncated ||= textTruncated;
          sourceChars += take;
          const text = rawText.slice(0, take).replace(/\r/g, '').trim();
          if (!text) continue;
          const rawId = node.getAttribute('data-post-id') || node.getAttribute('data-post-number') || node.id;
          const id = String(rawId || '').replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 120) || ('block-' + items.length);
          const characters = Array.from(text).filter(c => c.charCodeAt(0) >= 32 || c === '\n' || c === '\t')
            .map(c => c.length === 1 && /[\uD800-\uDFFF]/u.test(c) ? '\uFFFD' : c);
          for (let start = 0; start < characters.length; start += 1000) {
            if (items.length >= 2000) { sourceTruncated = true; break; }
            const segment = characters.slice(start, start + 1000);
            const part = segment.join('');
            items.push({ id, kind: contentKind, text: part, textOffset: start,
              textComplete: !textTruncated && start + segment.length === characters.length });
          }
        }
      }
      readCache = { id: binding.bindingId + ':' + binding.navigationEpoch + ':read:' + (++snapshotSeq),
        bindingId: binding.bindingId, navigationEpoch: binding.navigationEpoch,
        documentUrl: location.href, title: String(document.title || '').slice(0, 200),
        mode, items, sourceTruncated, observedAt: Date.now() };
    }
    if (offset > readCache.items.length) return null;
    const items = [];
    let size = 0, next = offset;
    while (next < readCache.items.length && items.length < limit) {
      const item = readCache.items[next];
      const cost = JSON.stringify(item).length;
      if (items.length && size + cost > maxChars) break;
      items.push(item); next++; size += cost;
    }
    return { readId: readCache.id, mode, url: location.origin + location.pathname,
      title: readCache.title, offset,
      nextOffset: next < readCache.items.length ? next : null, totalItems: readCache.items.length,
      sourceTruncated: readCache.sourceTruncated, domOnly: true, observedAt: readCache.observedAt, items };
  };
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
  globalThis.__quietbridgeObserverStop = expectedBindingId => {
    if (expectedBindingId !== undefined && binding?.bindingId !== expectedBindingId) return false;
    for (const name of legacyNames) delete globalThis['__cua' + name];
    binding = null;
    observer.disconnect();
    clearTimeout(timer);
    for (const type of types) removeEventListener(type, onLifecycle);
    removeEventListener('error', onError);
    removeEventListener('unhandledrejection', onRejection);
    delete globalThis.__quietbridgeObserver;
    delete globalThis.__quietbridgeContentVersion;
    delete globalThis.__quietbridgeSnapshot;
    readCache = null;
    for (const key of ['__quietbridgeRead', '__quietbridgePrepare', '__quietbridgeScroll', '__quietbridgeFocusInput', '__quietbridgeInputReady', '__quietbridgePointMatches', '__quietbridgeVerifyFill', '__quietbridgeSelect']) delete globalThis[key];
    delete globalThis.__quietbridgeDiagnostics;
    delete globalThis.__quietbridgeObserverStop;
    return true;
  };
  // An already-running pre-upgrade worker can finish cleanup before Chrome reloads it.
  for (const name of legacyNames) globalThis['__cua' + name] = globalThis['__quietbridge' + name];
})();
