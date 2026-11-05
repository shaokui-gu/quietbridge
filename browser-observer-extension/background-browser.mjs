import { safeUrl } from './state.mjs';
import { installPopupGuard } from './popup-guard.mjs';

// Only browser-scoped input is exposed here. There is deliberately no OS input,
// tab activation, window focusing, debugger takeover, or foreground fallback.
export class BackgroundBrowser {
  constructor(chrome, authorized = async () => true) {
    this.chrome = chrome;
    this.authorized = authorized;
    this.attached = new Map();
    this.detached = new Set();
    this.guards = new Map();
    chrome.debugger?.onDetach.addListener(({ tabId }) => {
      if (this.attached.has(tabId)) this.detached.add(tabId);
      this.attached.delete(tabId);
      // Chrome removes debugger preload scripts on detach, but the current
      // document's installed hooks still need a separate cleanup attempt.
      const guard = this.guards.get(tabId);
      if (guard) guard.scriptId = null;
    });
  }

  async check(s, command) {
    if (!await this.authorized(s) || !s || s.paused || !s.documentId || s.pending ||
        command.taskId !== s.taskId || command.bindingId !== s.bindingId ||
        command.assignmentVersion !== s.assignmentVersion || command.tabId !== s.tabId ||
        command.browserWindowId !== s.browserWindowId || command.documentId !== s.documentId ||
        command.navigationEpoch !== s.navigationEpoch || !Number.isFinite(command.expiresAt) ||
        Date.now() >= command.expiresAt * 1000) throw new Error('STALE_TARGET：任务、文档或命令已失效');
    const [tab, frame] = await Promise.all([
      this.chrome.tabs.get(s.tabId), this.chrome.webNavigation.getFrame({ tabId: s.tabId, frameId: 0 })
    ]);
    if (tab.windowId !== s.browserWindowId || frame?.documentId !== s.documentId ||
        safeUrl(frame.url) !== s.url || safeUrl(tab.url) !== s.url ||
        (s.documentUrl && (frame.url !== s.documentUrl || tab.url !== s.documentUrl ||
          (tab.pendingUrl && tab.pendingUrl !== s.documentUrl)))) {
      throw new Error('STALE_DOCUMENT：目标已移动或导航，等待重新观察');
    }
    return tab;
  }

  async script(s, command, func, args = []) {
    await this.check(s, command);
    const [reply] = await this.chrome.scripting.executeScript({
      target: { tabId: s.tabId, documentIds: [s.documentId] }, func,
      args: [{ bindingId: s.bindingId, navigationEpoch: s.navigationEpoch,
        snapshotId: command.params?.snapshotId }, ...args]
    });
    if (reply?.result == null) throw new Error('STALE_SNAPSHOT：页面或快照已失效');
    return reply.result;
  }

  async attach(s, command) {
    await this.check(s, command);
    if (!this.chrome.debugger) throw new Error('BACKGROUND_UNAVAILABLE：请重载新版扩展并授权 debugger');
    if (this.detached.has(s.tabId)) throw new Error('BACKGROUND_UNAVAILABLE：调试连接被断开，请显式重新分配原标签');
    if (this.attached.has(s.tabId)) return;
    await this.chrome.debugger.attach({ tabId: s.tabId }, '1.3');
    this.attached.set(s.tabId, s.bindingId);
    await this.check(s, command);
  }

  async send(s, command, method, params) {
    await this.check(s, command);
    if (!this.attached.has(s.tabId)) throw new Error('BACKGROUND_UNAVAILABLE：调试连接已断开');
    return this.chrome.debugger.sendCommand({ tabId: s.tabId }, method, params);
  }

  async release(tabId, taskId) {
    const guard = this.guards.get(tabId);
    if (guard?.scriptId && this.attached.has(tabId)) {
      try {
        await this.chrome.debugger.sendCommand({ tabId }, 'Page.removeScriptToEvaluateOnNewDocument', { identifier: guard.scriptId });
      } catch {}
    }
    const owner = guard?.taskId || taskId;
    if (owner) {
      try {
        await this.chrome.scripting.executeScript({ target: { tabId, allFrames: true }, world: 'MAIN',
          func: taskId => globalThis.__cuaPopupGuard?.(taskId, 'release'), args: [owner] });
      } catch {}
    }
    this.guards.delete(tabId);
    if (!this.attached.has(tabId)) return;
    this.attached.delete(tabId);
    try { await this.chrome.debugger.detach({ tabId }); } catch {}
  }

  async reset(s) {
    // An explicit new assignment is the only way to clear a detach suspension.
    this.detached.delete(s.tabId);
  }

  async protectCreatedTab(tabId, windowId, taskId) {
    const tab = await this.chrome.tabs.get(tabId);
    if (tab.windowId !== windowId || tab.url !== 'about:blank') throw new Error('新标签身份已变化，未导航');
    await this.chrome.debugger.attach({ tabId }, '1.3');
    this.attached.set(tabId, taskId);
    const { identifier } = await this.chrome.debugger.sendCommand({ tabId },
      'Page.addScriptToEvaluateOnNewDocument', { source: `(${installPopupGuard.toString()})(${JSON.stringify(taskId)})` });
    this.guards.set(tabId, { taskId, scriptId: identifier });
  }

  async protect(s, command) {
    await this.attach(s, command);
    if (!this.guards.get(s.tabId)?.scriptId) {
      await this.send(s, command, 'Page.enable', {});
      const { identifier } = await this.send(s, command, 'Page.addScriptToEvaluateOnNewDocument', {
        source: `(${installPopupGuard.toString()})(${JSON.stringify(s.taskId)})`
      });
      this.guards.set(s.tabId, { taskId: s.taskId, scriptId: identifier });
    }
    await this.check(s, command);
    await this.chrome.scripting.executeScript({ target: { tabId: s.tabId, documentIds: [s.documentId] },
      world: 'MAIN', func: installPopupGuard, args: [s.taskId] });
  }

  async popup(s, command, action) {
    await this.check(s, command);
    const [reply] = await this.chrome.scripting.executeScript({
      target: { tabId: s.tabId, documentIds: [s.documentId] }, world: 'MAIN',
      func: (taskId, op) => globalThis.__cuaPopupGuard?.(taskId, op), args: [s.taskId, action]
    });
    if (reply?.result == null) throw new Error('BACKGROUND_UNAVAILABLE：弹窗约束未就绪');
    return reply.result;
  }

  async run(command, s) {
    const mutating = !['snapshot', 'diagnostics', 'screenshot'].includes(command.operation);
    if (mutating) { await this.protect(s, command); await this.popup(s, command, 'arm'); }
    let result;
    try { result = await this.execute(command, s); }
    finally {
      if (mutating) {
        // Never query a replacement document, or activate an unexpected popup.
        const urls = await this.popup(s, command, 'drain').catch(() => []);
        const children = [];
        try {
          for (const url of result?.performed ? urls : []) {
            if ((s.ownedTabCount || 1) + children.length >= 64) throw new Error('任务子标签达到上限');
            await this.check(s, command);
            if (!safeUrl(url)) continue;
            const tab = await this.chrome.tabs.create({ active: false, windowId: s.browserWindowId,
              openerTabId: s.tabId, url: 'about:blank' });
            if (!Number.isInteger(tab?.id) || tab.windowId !== s.browserWindowId) throw new Error('子标签创建身份错误');
            children.push({ tabId: tab.id, browserWindowId: s.browserWindowId, url: safeUrl(url) });
            // Install before the destination page executes its own scripts.
            await this.protectCreatedTab(tab.id, s.browserWindowId, s.taskId);
            await this.chrome.tabs.update(tab.id, { url });
          }
        } catch (error) {
          // Do not leave unregistered task tabs if a later popup fails or the
          // command expires after an earlier child was already created.
          for (const child of children) {
            await this.release(child.tabId);
            await this.chrome.tabs.remove(child.tabId).catch(() => {});
          }
          throw error;
        }
        if (result && children.length) result.children = children;
      }
    }
    return result;
  }

  async execute(command, s) {
    await this.check(s, command);
    const op = command.operation, params = command.params || {};
    if (op === 'snapshot') return this.script(s, command, c => globalThis.__cuaSnapshot?.(c));
    if (op === 'diagnostics') return this.script(s, command, c => globalThis.__cuaDiagnostics?.(c));
    if (op === 'navigate') {
      const u = new URL(params.url);
      if (!safeUrl(u.href) || u.username || u.password) throw new Error('导航地址不合法');
      await this.check(s, command);
      await this.chrome.tabs.update(s.tabId, { url: u.href });
      return { performed: true, verified: false, observed: { route: 'extension', tabId: s.tabId } };
    }
    await this.attach(s, command);
    if (op === 'screenshot') {
      const { cssVisualViewport: v } = await this.send(s, command, 'Page.getLayoutMetrics', {});
      const scale = Math.min(1, 1200 / v.clientWidth, 900 / v.clientHeight);
      const { data } = await this.send(s, command, 'Page.captureScreenshot', { format: 'jpeg', quality: 40,
        fromSurface: true, captureBeyondViewport: false,
        clip: { x: v.pageX, y: v.pageY, width: v.clientWidth, height: v.clientHeight, scale } });
      await this.check(s, command);
      if (!data || data.length > 200000) throw new Error('截图超出大小限制，请使用 DOM 快照');
      return { mimeType: 'image/jpeg', data, tabId: s.tabId, documentId: s.documentId };
    }
    if (op === 'keypress') {
      const keys = { Enter: 13, Tab: 9, Escape: 27, ArrowDown: 40, ArrowUp: 38,
        ArrowLeft: 37, ArrowRight: 39, Home: 36, End: 35, PageUp: 33, PageDown: 34,
        Backspace: 8, Delete: 46, Space: 32 };
      if (!(params.key in keys)) throw new Error('不支持的页面按键');
      await this.send(s, command, 'Input.dispatchKeyEvent', { type: 'keyDown', key: params.key === 'Space' ? ' ' : params.key,
        code: params.key, windowsVirtualKeyCode: keys[params.key] });
      await this.send(s, command, 'Input.dispatchKeyEvent', { type: 'keyUp', key: params.key === 'Space' ? ' ' : params.key,
        code: params.key, windowsVirtualKeyCode: keys[params.key] });
      return { performed: true, verified: false, observed: { route: 'cdp', tabId: s.tabId } };
    }
    const prepared = await this.script(s, command, (c, a) => globalThis.__cuaPrepare?.(c, a), [params]);
    if (!prepared.ok) return { performed: false, verified: false, observed: { reason: prepared.reason } };
    if (op === 'select') return this.script(s, command, (c, a) => globalThis.__cuaSelect?.(c, a), [params]);
    if (op === 'fill') {
      const focused = await this.script(s, command, (c, a) => globalThis.__cuaFocusInput?.(c, a), [params]);
      if (!focused) throw new Error('输入框不可编辑');
      const ready = await this.script(s, command, (c, a) => globalThis.__cuaInputReady?.(c, a), [params]);
      if (!ready) throw new Error('STALE_SNAPSHOT：输入框焦点已变化，未输入');
      if (params.value) await this.send(s, command, 'Input.insertText', { text: params.value });
      else {
        await this.send(s, command, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 });
        await this.send(s, command, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 });
      }
      const verified = await this.script(s, command, (c, a) => globalThis.__cuaVerifyFill?.(c, a), [params]);
      return { performed: true, verified, observed: { route: 'cdp', length: (params.value ?? '').length } };
    }
    const point = { x: prepared.x, y: prepared.y };
    if (op === 'scroll') {
      await this.send(s, command, 'Input.dispatchMouseEvent', { type: 'mouseWheel', ...point,
        deltaX: 0, deltaY: -(params.deltaY ?? 0) * 40 });
    } else if (op === 'click') {
      await this.send(s, command, 'Input.dispatchMouseEvent', { type: 'mouseMoved', ...point });
      const hit = await this.script(s, command, (c, a, p) => globalThis.__cuaPointMatches?.(c, a, p), [params, point]);
      if (!hit) throw new Error('STALE_SNAPSHOT：悬停后目标移动或被遮挡，未点击');
      await this.send(s, command, 'Input.dispatchMouseEvent', { type: 'mousePressed', ...point,
        button: 'left', buttons: 1, clickCount: 1 });
      try {
        await this.send(s, command, 'Input.dispatchMouseEvent', { type: 'mouseReleased', ...point,
          button: 'left', buttons: 0, clickCount: 1 });
      } catch (error) {
        // A mousedown handler may navigate. Do not send even an offscreen
        // mouseup to its replacement document; require explicit reassignment.
        await this.release(s.tabId);
        this.detached.add(s.tabId);
        throw error;
      }
    } else throw new Error('不支持的后台操作');
    return { performed: true, verified: false, observed: { route: 'cdp', tabId: s.tabId, ...point } };
  }
}
