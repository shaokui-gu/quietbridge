import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState, reduce, safeUrl, originOf, validSelector } from './state.mjs';

const url = 'https://example.org/t/123';
const start = (selector = '') => initialState({ taskId: 'task', tabId: 7, browserWindowId: 2,
  authorizedOrigin: 'https://example.org', selector, bindingId: 'a'.repeat(32) });
const event = (type, timeStamp, documentId = 'doc-1', extra = {}) => ({
  type, timeStamp, documentId, tabId: 7, frameId: 0, url, ...extra });
const commit = selector => reduce(start(selector), event('committed', 10));
const readiness = { bodyPresent: true, bodyVisible: true, elementCount: 12,
  readyState: 'complete', conditionMatched: null };

test('通用 HTTP/HTTPS URL 脱敏及 origin 保留协议端口', () => {
  assert.equal(safeUrl('https://u:p@example.org/t/1?secret=a#x'), 'https://example.org/t/1');
  assert.equal(safeUrl('http://localhost:8080/a?q=1'), 'http://localhost:8080/a');
  assert.equal(originOf('https://other.example:444/a'), 'https://other.example:444');
  for (const u of ['chrome://settings', 'file:///tmp/a', 'data:text/plain,x', 'about:blank',
    'https://chromewebstore.google.com/detail/x', 'https://chrome.google.com/webstore/detail/x'])
    assert.equal(safeUrl(u), null);
});
test('CSS 规则仅接受单个简单选择器', () => {
  for (const s of ['', 'main', '#app', '.loaded', 'main#app.ready']) assert.ok(validSelector(s));
  for (const s of [' ', '*', 'a,b', 'div span', '[value]', ':has(a)', '#x > a', 'a'.repeat(129), null])
    assert.equal(validSelector(s), false);
});
test('加载与布局不推导 contentReady，可选条件单独报告', () => {
  let s = reduce(commit(), event('completed', 11));
  assert.equal(s.loaded, true);
  assert.equal(s.contentReady, null);
  s = reduce(s, event('content', 12, 'doc-1', { readiness }));
  assert.equal(s.elementCount, 12);
  assert.equal(s.conditionMatched, null);
  assert.equal(s.contentReady, null);
  const c = reduce(commit('main.ready'), event('content', 12, 'doc-1', {
    readiness: { ...readiness, readyState: 'loading', conditionMatched: true } }));
  assert.equal(c.conditionMatched, true);
  assert.equal(c.loaded, false);
  assert.equal(c.mappingVerified, false);
});
test('新导航清空报告，旧文档、迟到事件、子 frame 不污染', () => {
  const old = reduce(commit(), event('content', 11, 'doc-1', { readiness }));
  let s = reduce(old, event('before', 20));
  assert.equal(s.elementCount, 0);
  assert.equal(s.conditionMatched, null);
  assert.equal(reduce(s, event('completed', 21)), s);
  s = reduce(s, event('committed', 22, 'doc-2'));
  for (const e of [event('completed', 23), event('committed', 19), event('committed', 25),
    event('completed', 24, 'doc-2', { frameId: 1 }), event('completed', 24, 'doc-2', { tabId: 9 })])
    assert.equal(reduce(s, e), s);
});
test('BFCache document 重用仍产生新 epoch，pageshow 独立记录', () => {
  let s = commit();
  s = reduce(s, event('before', 20));
  s = reduce(s, event('committed', 21, 'doc-2'));
  s = reduce(s, event('before', 30));
  s = reduce(s, event('committed', 31));
  s = reduce(s, event('pageshow', 32, 'doc-1', { readiness }));
  assert.equal(s.navigationEpoch, 3);
  assert.equal(s.phase, 'pageshow');
});
test('SPA 递增 epoch 并重置报告，旧路径不污染', () => {
  let s = reduce(commit(), event('content', 11, 'doc-1', { readiness }));
  s = reduce(s, event('history', 12, 'doc-1', { url: 'https://example.org/latest?q=x' }));
  assert.equal(s.navigationEpoch, 2);
  assert.equal(s.elementCount, 0);
  assert.equal(reduce(s, event('content', 13, 'doc-1', { readiness })), s);
});
test('session 序列化恢复一致', () => {
  const s = reduce(commit(), event('completed', 11));
  assert.deepEqual(reduce(JSON.parse(JSON.stringify(s)), event('content', 12, 'doc-1', { readiness })),
    reduce(s, event('content', 12, 'doc-1', { readiness })));
});
test('跨 origin、协议、端口或受限页暂停，不记录目标地址且不能自动恢复', () => {
  for (const other of ['https://elsewhere.example/private', 'http://example.org/',
    'https://example.org:444/', 'chrome://settings', 'file:///tmp/a']) {
    let s = reduce(commit(), event('content', 11, 'doc-1', { readiness }));
    s = reduce(s, event('before', 12, 'doc-1', { url: other }));
    assert.equal(s.paused, true);
    assert.equal(s.url, null);
    assert.equal(s.documentId, null);
    assert.equal(s.readyState, null);
    assert.equal(s.elementCount, 0);
    assert.equal(reduce(s, event('committed', 13)), s);
  }
});
test('错误导航和非法布局报告', () => {
  const s = commit();
  for (const r of [{ ...readiness, elementCount: -1 }, { ...readiness, conditionMatched: true },
    { ...readiness, readyState: 'stable' }])
    assert.equal(reduce(s, event('content', 12, 'doc-1', { readiness: r })), s);
  const next = reduce(reduce(s, event('before', 12)), event('error', 13));
  assert.equal(next.phase, 'error');
  assert.equal(next.loaded, false);
  assert.equal(reduce(next, event('content', 14, 'doc-1', { readiness })), next);
});
