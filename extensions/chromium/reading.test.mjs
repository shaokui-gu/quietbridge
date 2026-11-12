import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

async function page({ links = 0, posts = 0, longText = null } = {}) {
  const make = (tagName, attrs, text) => ({
    tagName, attrs, textContent: text, innerText: text, isConnected: true, parentElement: null,
    getAttribute(name) { return this.attrs[name] || null; },
    getClientRects: () => [{}],
    getBoundingClientRect: () => ({ left: 10, top: 20, right: 110, bottom: 60, width: 100, height: 40 }),
    contains(hit) { return hit === this; }, scrollIntoView() {},
    querySelector: () => null,
  });
  const anchors = Array.from({ length: links }, (_, i) => {
    const node = make('A', {}, 'Topic ' + i);
    node.href = 'https://example.org/t/topic/' + i + '?token=synthetic-secret#reply';
    return node;
  });
  const articles = Array.from({ length: posts }, (_, i) =>
    make('ARTICLE', { 'data-post-id': String(i + 1) }, longText || 'Rendered post ' + i));
  const root = make('MAIN', {}, '');
  root.querySelectorAll = selector => selector === 'a[href]' ? anchors :
    selector === 'article,[role="article"]' ? articles : [];
  const document = { body: root, title: 'Synthetic forum', readyState: 'complete',
    getElementsByTagName: () => ({ length: links + posts }),
    querySelector: () => root,
    querySelectorAll: selector => selector === 'iframe,frame' ? [] : anchors,
    elementFromPoint: () => anchors[0],
  };
  const context = { document, URL, location: { protocol: 'https:', origin: 'https://example.org', pathname: '/forum' },
    innerWidth: 1000, innerHeight: 800,
    getComputedStyle: node => ({ display: 'block', visibility: 'visible', opacity: '1', ...node.style }),
    chrome: { runtime: { sendMessage: async () => {} } },
    addEventListener() {}, removeEventListener() {}, setTimeout() { return 1; }, clearTimeout() {},
    MutationObserver: class { observe() {} disconnect() {} },
  };
  for (const node of [root, ...anchors, ...articles]) node.ownerDocument = document;
  document.defaultView = context;
  vm.createContext(context);
  vm.runInContext(await readFile(new URL('./content.js', import.meta.url), 'utf8'), context);
  const config = { bindingId: 'a'.repeat(32), navigationEpoch: 1, authorizedOrigin: 'https://example.org', selector: '' };
  context.__quietbridgeObserver(config);
  return { context, config, anchors, articles, root };
}

test('late observer configuration and old-task cleanup preserve the current document binding', async () => {
  const h = await page({ links: 1 });
  assert.equal(h.context.__quietbridgeObserver({ ...h.config, bindingId: 'b'.repeat(32), deadline: Date.now() - 1 }), false);
  assert.ok(h.context.__quietbridgeSnapshot(h.config));
  const current = { ...h.config, bindingId: 'c'.repeat(32), deadline: Date.now() + 1000 };
  assert.equal(h.context.__quietbridgeObserver(current), true);
  assert.equal(h.context.__quietbridgeObserverStop(h.config.bindingId), false);
  assert.ok(h.context.__quietbridgeSnapshot(current));
  assert.equal(h.context.__quietbridgeObserverStop(current.bindingId), true);
  assert.equal(h.context.__quietbridgeSnapshot, undefined);
});

test('link reading exceeds interaction snapshot limits with bounded stable pages and redacted addresses', async () => {
  const h = await page({ links: 550 });
  const snapshot = h.context.__quietbridgeSnapshot(h.config);
  assert.equal(snapshot.elements.length, 300);
  assert.equal(snapshot.elements[0].href, 'https://example.org/t/topic/0');
  assert.equal(snapshot.elements[0].urlHasQuery, true);
  assert.equal(JSON.stringify(snapshot).includes('synthetic-secret'), false);
  const all = [];
  let options = { mode: 'links', limit: 40, maxChars: 4000 };
  let first;
  do {
    const result = h.context.__quietbridgeRead(h.config, options);
    first ||= result;
    assert.equal(result.totalItems, 550);
    assert.equal(result.domOnly, true);
    assert.equal(result.sourceTruncated, false);
    assert.ok(JSON.stringify(result.items).length <= 4000 + 100);
    assert.equal(JSON.stringify(result).includes('synthetic-secret'), false);
    all.push(...result.items);
    options = { ...options, readId: result.readId, offset: result.nextOffset };
  } while (options.offset !== null);
  assert.equal(new Set(all.map(item => item.href)).size, 550);
  // Read is not an input snapshot and must not invalidate an existing ref.
  const prepared = h.context.__quietbridgePrepare({ ...h.config, snapshotId: snapshot.snapshotId },
    { ref: snapshot.elements[0].ref });
  assert.equal(prepared.ok, true);
  assert.equal(first.snapshotId, undefined);
  h.context.__quietbridgeSnapshot(h.config);
  assert.equal(h.context.__quietbridgeRead(h.config, { mode: 'links', readId: first.readId, offset: 1 }), null);
});

test('rendered article reading splits long posts, preserves immutable pagination, and identifies source limits', async () => {
  const h = await page({ posts: 4, longText: 'x'.repeat(6001) });
  const first = h.context.__quietbridgeRead(h.config, { maxChars: 4000 });
  assert.equal(first.items[0].kind, 'article');
  assert.equal(first.items[0].id, '1');
  assert.equal(first.items[0].textComplete, false);
  h.articles[0].innerText = 'changed after capture';
  h.context.document.title = 'changed after capture';
  let result = first, items = [...first.items];
  while (result.nextOffset !== null) {
    result = h.context.__quietbridgeRead(h.config, { readId: first.readId, offset: result.nextOffset, maxChars: 4000 });
    assert.equal(result.title, first.title);
    items.push(...result.items);
  }
  const text = items.filter(item => item.id === '1').map(item => item.text).join('');
  assert.equal(text, 'x'.repeat(6001));
  assert.equal(items.filter(item => item.id === '1').at(-1).textComplete, true);
  assert.equal(new Set(items.map(item => item.id)).size, 4, 'chunks are not separate posts');
  const large = await page({ posts: 200, longText: 'z'.repeat(4000) });
  const capped = large.context.__quietbridgeRead(large.config);
  assert.equal(capped.sourceTruncated, true);
  assert.ok(capped.totalItems < 400);
});

test('same-document URL changes invalidate a frozen reading even before the observer catches up', async () => {
  const h = await page({ links: 2 });
  h.context.location.href = 'https://example.org/forum?view=one';
  const first = h.context.__quietbridgeRead(h.config, { mode: 'links', limit: 1 });
  h.context.location.href = 'https://example.org/forum?view=two';
  assert.equal(h.context.__quietbridgeRead(h.config,
    { mode: 'links', readId: first.readId, offset: first.nextOffset }), null);
});

test('reading rejects unauthorized bindings, stale navigation, unsafe links and invalid cursors', async () => {
  const h = await page({ links: 3 });
  h.anchors[0].href = 'javascript:alert(1)';
  h.anchors[1].href = 'https://user:pass@example.org/private';
  const first = h.context.__quietbridgeRead(h.config, { mode: 'links' });
  assert.equal(first.items.length, 1);
  assert.equal(h.context.__quietbridgeRead({ ...h.config, bindingId: 'b'.repeat(32) }), null);
  assert.equal(h.context.__quietbridgeRead(h.config, { offset: 1 }), null);
  assert.equal(h.context.__quietbridgeRead(h.config, { limit: 101 }), null);
  assert.equal(h.context.__quietbridgeRead(h.config, { maxChars: 3999 }), null);
  h.context.__quietbridgeObserver({ ...h.config, navigationEpoch: 2 });
  assert.equal(h.context.__quietbridgeRead({ ...h.config, navigationEpoch: 2 },
    { mode: 'links', readId: first.readId, offset: 0 }), null);
  h.context.__quietbridgeObserverStop();
  assert.equal(h.context.__quietbridgeRead, undefined);
});

test('empty rendered text never falls back to hidden descendant text', async () => {
  const h = await page({ posts: 3 });
  h.articles[0].innerText = '';
  h.articles[0].textContent = 'synthetic hidden secret';
  h.articles[1].querySelector = () => ({ ...h.articles[1], style: { display: 'none' } });
  const result = h.context.__quietbridgeRead(h.config);
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].id, '3');
  assert.equal(JSON.stringify(result).includes('secret'), false);
});

test('unicode and control characters stay bounded and continuations preserve character offsets', async () => {
  const text = ('你好😀' + String.fromCharCode(1) + '"\\\n').repeat(1000) + 'end';
  const h = await page({ posts: 1, longText: text });
  let result = h.context.__quietbridgeRead(h.config, { maxChars: 4000 });
  let restored = '', offset = 0;
  while (true) {
    assert.ok(result.items.reduce((size, item) => size + JSON.stringify(item).length, 0) <= 4000);
    for (const item of result.items) {
      assert.equal(item.textOffset, offset);
      offset += Array.from(item.text).length;
      restored += item.text;
    }
    if (result.nextOffset === null) break;
    result = h.context.__quietbridgeRead(h.config,
      { readId: result.readId, offset: result.nextOffset, maxChars: 4000 });
  }
  assert.equal(restored, text.replaceAll(String.fromCharCode(1), ''));
  assert.equal(result.items.at(-1).textComplete, true);

  const large = await page({ posts: 1, longText: '😀'.repeat(200000) });
  let captured = large.context.__quietbridgeRead(large.config, { maxChars: 4000 });
  assert.equal(captured.sourceTruncated, true);
  let units = 0;
  while (true) {
    units += captured.items.reduce((size, item) => size + item.text.length, 0);
    if (captured.nextOffset === null) break;
    captured = large.context.__quietbridgeRead(large.config,
      { readId: captured.readId, offset: captured.nextOffset, maxChars: 4000 });
  }
  assert.ok(units <= 262144);
  assert.equal(captured.items.at(-1).textComplete, false);
  assert.equal(captured.items.at(-1).text.endsWith('😀'), true);

  const malformed = await page({ posts: 1, longText: String.fromCharCode(0xd800).repeat(6000) });
  const repaired = malformed.context.__quietbridgeRead(malformed.config, { maxChars: 4000 });
  assert.ok(repaired.items.reduce((size, item) => size + JSON.stringify(item).length, 0) <= 4000);
  assert.equal(repaired.items[0].text, '\uFFFD'.repeat(1000));
});

test('an older injected observer is replaced without a page reload and reinjection stays idempotent', async () => {
  const h = await page({ posts: 1 });
  const original = h.context.__quietbridgeRead;
  const source = await readFile(new URL('./content.js', import.meta.url), 'utf8');
  vm.runInContext(source, h.context);
  assert.equal(h.context.__quietbridgeRead, original);
  h.context.__quietbridgeContentVersion = '0.4.2';
  vm.runInContext(source, h.context);
  assert.notEqual(h.context.__quietbridgeRead, original);
  assert.equal(h.context.__quietbridgeRead(h.config), null, 'a replacement requires fresh binding authorization');
  h.context.__quietbridgeObserver(h.config);
  assert.equal(h.context.__quietbridgeRead(h.config).items.length, 1);
});
