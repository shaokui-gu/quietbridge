import { safeUrl } from './state.mjs';
import { installPopupGuard } from './popup-guard.mjs';

function debuggerError(error, stage) {
  if (String(error?.message || error).includes('Cannot access a chrome-extension:// URL of different extension')) {
    return new Error('BROWSER_DEBUGGER_RESTRICTED：' + stage +
      ' 被 Chrome 拒绝：目标含其他扩展页面。请通知用户检查扩展冲突；未切换目标、未重放输入，先核实既有结果。');
  }
  return error;
}

// Only browser-scoped input is exposed here. There is deliberately no OS input,
// tab activation, window focusing, debugger takeover, or foreground fallback.
export class BackgroundBrowser {
  constructor(chrome, authorized = async () => true, onDownload = () => {}, onLifecycleError = () => {}) {
    this.chrome = chrome;
    this.authorized = authorized;
    this.onLifecycleError = onLifecycleError;
    this.attached = new Map();
    this.pageEnabled = new Set();
    this.probeLayouts = new Map();
    this.detached = new Set();
    this.guards = new Map();
    this.releasing = new Map();
    chrome.debugger?.onEvent?.addListener((source, method, params) => {
      if (method === 'Page.downloadWillBegin' || method === 'Page.downloadProgress') {
        onDownload(source, method, params);
      }
    });
    chrome.debugger?.onDetach.addListener(({ tabId }) => {
      if (this.attached.has(tabId)) this.detached.add(tabId);
      this.attached.delete(tabId);
      this.pageEnabled.delete(tabId); this.probeLayouts.delete(tabId);
      // Chrome removes debugger preload scripts on detach, but the current
      // document's installed hooks still need a separate cleanup attempt.
      const guard = this.guards.get(tabId);
      if (guard) guard.scriptId = null;
    });
  }

  // Passive Chrome APIs can leave their promise pending when a renderer stops
  // responding. Bound the wait, not a second input attempt or a fake heartbeat.
  async lifecycle(stage, operation, timeoutMs = 1200) {
    const timers = this.chrome.channelTimers || globalThis;
    const deadline = Date.now() + timeoutMs;
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(() => operation(deadline)),
        new Promise((_, reject) => {
          timer = timers.setTimeout(() => {
            const error = new Error('BROWSER_LIFECYCLE_TIMEOUT：' + stage + ' 未回执；保留原目标，未重放操作');
            error.code = 'BROWSER_LIFECYCLE_TIMEOUT';
            reject(error);
          }, timeoutMs);
        })
      ]);
    } catch (cause) {
      const code = cause?.code === 'BROWSER_LIFECYCLE_TIMEOUT' ? cause.code : 'BROWSER_LIFECYCLE_FAILED';
      const error = new Error(code + '：' + stage + ' 未完成');
      error.code = code;
      error.stage = stage;
      this.onLifecycleError(error);
      throw error;
    } finally { timers.clearTimeout(timer); }
  }

  async check(s, command) {
    if (!await this.authorized(s) || !s || s.paused || !s.documentId || s.pending ||
        command.taskId !== s.taskId || command.bindingId !== s.bindingId ||
        command.assignmentVersion !== s.assignmentVersion || command.tabId !== s.tabId ||
        command.browserWindowId !== s.browserWindowId || command.documentId !== s.documentId ||
        command.navigationEpoch !== s.navigationEpoch || !Number.isFinite(command.expiresAt) ||
        Date.now() >= command.expiresAt * 1000) throw new Error('STALE_TARGET：任务、文档或命令已失效');
    const [tab, frame] = await this.lifecycle('document_lookup', () => Promise.all([
      this.chrome.tabs.get(s.tabId), this.chrome.webNavigation.getFrame({ tabId: s.tabId, frameId: 0 })
    ]));
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
    let timer;
    let reply;
    try {
      [reply] = await Promise.race([
        this.chrome.scripting.executeScript({
          target: { tabId: s.tabId, documentIds: [s.documentId] }, func,
          args: [{ bindingId: s.bindingId, navigationEpoch: s.navigationEpoch,
            snapshotId: command.params?.snapshotId }, ...args]
        }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(
            (['snapshot', 'read', 'diagnostics'].includes(command.operation) ? 'BROWSER_OBSERVATION_TIMEOUT' : 'BACKGROUND_COMMAND_TIMEOUT') +
            '：' + command.operation + ' 的文档脚本未回执；未重放，保留原目标核实状态')),
          Math.max(1, Math.min(3000, command.expiresAt * 1000 - Date.now())));
        })
      ]);
    } finally { clearTimeout(timer); }
    await this.check(s, command);
    if (reply?.documentId && reply.documentId !== s.documentId) throw new Error('STALE_DOCUMENT：观察文档已变化');
    if (reply?.result == null) throw new Error('STALE_SNAPSHOT：页面或快照已失效');
    return reply.result;
  }

  async attach(s, command) {
    await this.check(s, command);
    if (this.releasing.has(s.tabId)) await this.lifecycle('debugger_detach', () => this.releasing.get(s.tabId));
    if (!this.chrome.debugger) throw new Error('BACKGROUND_UNAVAILABLE：请重载新版扩展并授权 debugger');
    if (this.detached.has(s.tabId)) throw new Error('BACKGROUND_UNAVAILABLE：调试连接被断开，请显式重新分配原标签');
    if (!this.attached.has(s.tabId)) {
      try { await this.chrome.debugger.attach({ tabId: s.tabId }, '1.3'); }
      catch (error) { throw debuggerError(error, 'attach'); }
      this.attached.set(s.tabId, s.bindingId);
    }
    try { await this.check(s, command); }
    catch (error) { await this.release(s.tabId, s.taskId).catch(() => {}); throw error; }
    if (!this.pageEnabled.has(s.tabId)) {
      await this.send(s, command, 'Page.enable', {});
      this.pageEnabled.add(s.tabId);
    }
    await this.check(s, command);
  }

  async debuggerCommand(target, method, params) {
    try { return await this.chrome.debugger.sendCommand(target, method, params); }
    catch (error) { throw debuggerError(error, method); }
  }

  async send(s, command, method, params) {
    await this.check(s, command);
    if (!this.attached.has(s.tabId)) throw new Error('BACKGROUND_UNAVAILABLE：调试连接已断开');
    let timer;
    try {
      return await Promise.race([
        this.debuggerCommand({ tabId: s.tabId }, method, params),
        new Promise((_, reject) => {
          timer = setTimeout(() => {
            // A missing input acknowledgement has an unknown outcome. Suspend
            // and detach before accepting any further command; never replay it.
            this.detached.add(s.tabId);
            this.attached.delete(s.tabId);
            this.pageEnabled.delete(s.tabId); this.probeLayouts.delete(s.tabId);
            const guard = this.guards.get(s.tabId);
            if (guard) guard.scriptId = null;
            this.chrome.debugger.detach({ tabId: s.tabId }).catch(() => {});
            reject(new Error('BACKGROUND_COMMAND_TIMEOUT：' + method + '/' + (params.type || method) + ' 未回执，结果未知，未重放；先只读核实原标签，通知用户处理。'));
          }, Math.max(1, Math.min(3000, command.expiresAt * 1000 - Date.now())));
        })
      ]);
    } finally { clearTimeout(timer); }
  }

  async release(tabId, taskId) {
    if (this.releasing.has(tabId)) return this.lifecycle('target_release', () => this.releasing.get(tabId));
    const guard = this.guards.get(tabId);
    const attached = this.attached.has(tabId);
    const owner = guard?.taskId || taskId;
    // Remove local ownership before yielding. Detach also removes preloads,
    // so it must not wait for a renderer-side cleanup script to acknowledge.
    this.guards.delete(tabId);
    this.attached.delete(tabId);
    this.pageEnabled.delete(tabId); this.probeLayouts.delete(tabId);
    const work = [];
    if (attached) {
      const detach = Promise.resolve().then(() => this.chrome.debugger.detach({ tabId }));
      this.releasing.set(tabId, detach);
      // Do not allow a new attach while an earlier detach is still in flight.
      detach.finally(() => { if (this.releasing.get(tabId) === detach) this.releasing.delete(tabId); }).catch(() => {});
      work.push(this.lifecycle('debugger_detach', () => detach));
    }
    if (owner) work.push(this.lifecycle('guard_stop', deadline =>
      this.chrome.scripting.executeScript({ target: { tabId, allFrames: true }, world: 'MAIN', injectImmediately: true,
        func: (taskId, deadline, leaseId) => Date.now() < deadline && globalThis.__quietbridgePopupGuard?.(taskId, 'release', leaseId),
        args: [owner, deadline, guard?.leaseId || owner] })));
    const results = await Promise.allSettled(work);
    const failed = results.find(result => result.status === 'rejected');
    if (failed) throw failed.reason;
  }

  async reset(s) {
    if (this.releasing.has(s.tabId)) await this.lifecycle('debugger_detach', () => this.releasing.get(s.tabId));
    // An explicit new assignment is the only way to clear a detach suspension.
    this.detached.delete(s.tabId);
  }

  async protectCreatedTab(tabId, windowId, taskId, check = async () => {}) {
    const verifyIdentity = async () => {
      await check();
      const tab = await this.chrome.tabs.get(tabId);
      if (tab.windowId !== windowId || (tab.url && tab.url !== 'about:blank') ||
          (tab.pendingUrl && tab.pendingUrl !== 'about:blank')) throw new Error('新标签身份已变化，未导航');
    };
    await verifyIdentity();
    try {
      try { await this.chrome.debugger.attach({ tabId }, '1.3'); }
      catch (error) { throw debuggerError(error, 'attach'); }
      this.attached.set(tabId, taskId);
      await verifyIdentity();
      await this.debuggerCommand({ tabId }, 'Page.enable', {});
      this.pageEnabled.add(tabId);
      const verifyBlank = async (wait = false) => {
        const deadline = Date.now() + 1000;
        while (true) {
          await verifyIdentity();
          // HTTP(S) host permissions do not reveal about:blank in tabs.get.
          // Attest the exact created tab's main frame through the already
          // authorized debugger instead of requiring broader tabs permission.
          const { frameTree } = await this.debuggerCommand({ tabId }, 'Page.getFrameTree', {});
          await verifyIdentity();
          const frame = frameTree?.frame;
          if (frame?.url && frame.url !== 'about:blank') throw new Error('新标签文档已变化，未导航');
          if (frame?.url === 'about:blank' && typeof frame.id === 'string' && frame.id) return frame;
          if (!wait || Date.now() >= deadline) throw new Error('新标签空白文档未就绪，未导航');
          await new Promise(resolve => setTimeout(resolve, 50));
        }
      };
      const initial = await verifyBlank(true);
      const { identifier } = await this.debuggerCommand({ tabId },
        'Page.addScriptToEvaluateOnNewDocument', { source: `(${installPopupGuard.toString()})(${JSON.stringify(taskId)})` });
      this.guards.set(tabId, { taskId, leaseId: taskId, scriptId: identifier });
      const current = await verifyBlank();
      if (current.id !== initial.id || (initial.loaderId && current.loaderId !== initial.loaderId)) {
        throw new Error('新标签文档已替换，未导航');
      }
    } catch (error) { await this.release(tabId, taskId).catch(() => {}); throw error; }
  }

  async protect(s, command) {
    await this.attach(s, command);
    const previous = this.guards.get(s.tabId);
    if (previous?.scriptId && (previous.taskId !== s.taskId || previous.leaseId !== s.bindingId)) {
      await this.send(s, command, 'Page.removeScriptToEvaluateOnNewDocument', { identifier: previous.scriptId });
      this.guards.delete(s.tabId);
    }
    if (!this.guards.get(s.tabId)?.scriptId) {
      await this.send(s, command, 'Page.enable', {});
      const { identifier } = await this.send(s, command, 'Page.addScriptToEvaluateOnNewDocument', {
        source: `(${installPopupGuard.toString()})(${JSON.stringify(s.taskId)},${JSON.stringify(s.bindingId)})`
      });
      this.guards.set(s.tabId, { taskId: s.taskId, leaseId: s.bindingId, scriptId: identifier });
    }
    await this.check(s, command);
    const issuedAt = Date.now();
    const replies = await this.lifecycle('guard_install', deadline => this.chrome.scripting.executeScript({
      target: { tabId: s.tabId, documentIds: [s.documentId] }, world: 'MAIN', injectImmediately: true,
      func: installPopupGuard, args: [s.taskId, s.bindingId, { issuedAt, deadline }] }),
    // MAIN-world injection on a just-navigated or off-screen document regularly
    // needs more than the 1.2s lifecycle default. popup-guard rejects any claim
    // whose window exceeds 3000ms, so stay just under it and keep the command
    // deadline as the outer bound; never retry after that deadline passes.
      Math.max(1, Math.min(2900, command.expiresAt * 1000 - Date.now())));
    await this.check(s, command);
    const result = replies?.[0];
    if (result?.documentId && result.documentId !== s.documentId) throw new Error('STALE_DOCUMENT：弹窗守卫文档已变化');
    if (result?.result === 'legacy') throw new Error('POPUP_GUARD_LEGACY：旧版页面守卫残留，请通知用户刷新此专用标签一次；未执行输入、未自动刷新');
    if (result?.result !== 'ready') throw new Error('POPUP_GUARD_CONFLICT：页面守卫租约未确认；未执行输入，请核实原任务');
  }

  async popup(s, command, action) {
    await this.check(s, command);
    const [reply] = await this.chrome.scripting.executeScript({
      target: { tabId: s.tabId, documentIds: [s.documentId] }, world: 'MAIN',
      func: (taskId, op, leaseId) => globalThis.__quietbridgePopupGuard?.(taskId, op, leaseId), args: [s.taskId, action, s.bindingId]
    });
    if (reply?.result == null) throw new Error('BACKGROUND_UNAVAILABLE：弹窗约束未就绪');
    return reply.result;
  }

  async run(command, s) {
    const mutating = !['snapshot', 'read', 'diagnostics', 'screenshot', 'render_probe'].includes(command.operation);
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
            await this.release(child.tabId).catch(() => {});
            await this.chrome.tabs.remove(child.tabId).catch(() => {});
          }
          throw error;
        }
        if (result && children.length) result.children = children;
      }
    }
    // A discarded one-shot capture nudges an occluded Chrome renderer to
    // submit its current compositor frame to WindowServer. The continuous
    // preview remains the native window stream; this image never leaves JS.
    // Navigation is pulsed by the worker after the replacement document is
    // reconciled, because this command's document lease may already be stale.
    if (mutating && result?.performed !== false && command.operation !== 'navigate') {
      await this.pulse(s);
    }
    return result;
  }

  async pulse(s) {
    if (!s || s.pending || !this.attached.has(s.tabId)) return false;
    const now = Date.now() / 1000;
    const command = { taskId: s.taskId, bindingId: s.bindingId, assignmentVersion: s.assignmentVersion,
      tabId: s.tabId, browserWindowId: s.browserWindowId, documentId: s.documentId,
      navigationEpoch: s.navigationEpoch, operation: 'render_refresh', params: {},
      expiresAt: now + 2 };
    let timer;
    try {
      await this.check(s, command);
      const reply = await Promise.race([
        this.debuggerCommand({ tabId: s.tabId }, 'Page.captureScreenshot', {
          format: 'jpeg', quality: 1, fromSurface: true, captureBeyondViewport: false
        }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error('RENDER_REFRESH_TIMEOUT')), 1500);
        })
      ]);
      await this.check(s, command);
      return typeof reply?.data === 'string' && reply.data.length > 0;
    } catch {
      // Rendering assistance is best effort and never changes the command
      // result or the debugger lifecycle.
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  // Explicit diagnostic stages: no input, activation, arbitrary CDP, or image export.
  async renderProbe(command, s) {
    const stage = command.params?.stage;
    if (!['inspect', 'attach', 'enable', 'layout', 'capture'].includes(stage) ||
        Object.keys(command.params || {}).some(k => k !== 'stage')) throw new Error('诊断阶段不合法');
    await this.check(s, command);
    const startedAt = Date.now() / 1000;
    const attachedBefore = this.attached.has(s.tabId), pageEnabledBefore = this.pageEnabled.has(s.tabId);
    let performed = false, extra = {};
    if (stage === 'attach' && !attachedBefore) {
      if (this.detached.has(s.tabId) || this.releasing.has(s.tabId)) throw new Error('BACKGROUND_UNAVAILABLE：原调试连接状态未确认，未重新附加');
      let attachError;
      const pending = Promise.resolve().then(() => this.chrome.debugger.attach({ tabId: s.tabId }, '1.3'))
        .catch(error => { attachError = error; throw error; });
      try {
        await this.lifecycle('probe_attach', () => pending, Math.max(1, Math.min(1200, command.expiresAt * 1000 - Date.now())));
        this.attached.set(s.tabId, s.bindingId);
      } catch (error) {
        this.detached.add(s.tabId);
        // If attach returns late, release only this wrapper's owned debugger.
        // Block reassignment until that cleanup settles; never retry attach.
        const cleanup = pending.then(() => this.chrome.debugger.detach({ tabId: s.tabId })).catch(() => {});
        this.releasing.set(s.tabId, cleanup);
        cleanup.finally(() => { if (this.releasing.get(s.tabId) === cleanup) this.releasing.delete(s.tabId); }).catch(() => {});
        throw debuggerError(attachError || error, 'probe_attach');
      }
      try { await this.check(s, command); }
      catch (error) { await this.release(s.tabId, s.taskId).catch(() => {}); throw error; }
      performed = true;
    } else if (stage !== 'inspect' && stage !== 'attach') {
      if (!this.attached.has(s.tabId)) throw new Error('RENDER_PROBE_STAGE_REQUIRED：先显式完成 attach；未自动附加');
      if (stage === 'enable') {
        if (!pageEnabledBefore) {
          await this.send(s, command, 'Page.enable', {});
          this.pageEnabled.add(s.tabId); performed = true;
        }
      } else {
        if (!this.pageEnabled.has(s.tabId)) throw new Error('RENDER_PROBE_STAGE_REQUIRED：先显式完成 enable；未自动启用');
        if (stage === 'layout') {
          this.probeLayouts.delete(s.tabId);
          const { cssVisualViewport: v } = await this.send(s, command, 'Page.getLayoutMetrics', {});
          if (!v || ![v.pageX, v.pageY, v.clientWidth, v.clientHeight].every(Number.isFinite) ||
              v.clientWidth <= 0 || v.clientHeight <= 0 || v.clientWidth > 100000 || v.clientHeight > 100000)
            throw new Error('INVALID_BROWSER_RESULT：视口尺寸无效');
          this.probeLayouts.set(s.tabId, { ...v, bindingId: s.bindingId, documentId: s.documentId,
            navigationEpoch: s.navigationEpoch, at: Date.now() });
          extra = { viewportWidth: v.clientWidth, viewportHeight: v.clientHeight }; performed = true;
        } else {
          const v = this.probeLayouts.get(s.tabId);
          if (!v || v.bindingId !== s.bindingId || v.documentId !== s.documentId || v.navigationEpoch !== s.navigationEpoch ||
              Date.now() - v.at > 30000) throw new Error('RENDER_PROBE_STAGE_REQUIRED：当前文档缺少有效 layout 阶段；未截图');
          // Consume before dispatch: failure/unknown outcome cannot repeat this pulse.
          this.probeLayouts.delete(s.tabId);
          const { data } = await this.send(s, command, 'Page.captureScreenshot', { format: 'jpeg', quality: 55,
            fromSurface: true, captureBeyondViewport: false,
            clip: { x: v.pageX, y: v.pageY, width: v.clientWidth, height: v.clientHeight,
              scale: Math.min(1, 1600 / v.clientWidth, 1200 / v.clientHeight) } });
          if (typeof data !== 'string' || !data.length || data.length > 34000000) throw new Error('INVALID_BROWSER_RESULT：截图数据长度无效');
          // The image is intentionally discarded; only base64 length leaves this call.
          extra = { captureDataLength: data.length }; performed = true;
        }
      }
    }
    await this.check(s, command);
    return { stage, startedAt, completedAt: Date.now() / 1000, performed,
      attachedBefore, pageEnabledBefore, attachedAfter: this.attached.has(s.tabId),
      pageEnabledAfter: this.pageEnabled.has(s.tabId), ...extra };
  }

  // Preview is an independent observation; it cannot satisfy action verification.
  async previewFrame(lease, s) {
    const command = { ...lease, expiresAt: Math.min(lease.expiresAt, Date.now() / 1000 + 2), params: {} };
    await this.check(s, command);
    if (!this.attached.has(s.tabId)) {
      await this.renderProbe({ ...command, params: { stage: 'attach' } }, s);
    }
    if (!this.pageEnabled.has(s.tabId)) {
      await this.send(s, command, 'Page.enable', {});
      this.pageEnabled.add(s.tabId);
    }
    const { cssVisualViewport: v } = await this.send(s, command, 'Page.getLayoutMetrics', {});
    if (!v || ![v.pageX, v.pageY, v.clientWidth, v.clientHeight].every(Number.isFinite) ||
        v.clientWidth <= 0 || v.clientHeight <= 0 || v.clientWidth > 100000 || v.clientHeight > 100000)
      throw new Error('INVALID_BROWSER_RESULT');
    const { data } = await this.send(s, command, 'Page.captureScreenshot', {
      format: 'jpeg', quality: 45, fromSurface: true, captureBeyondViewport: false,
      clip: { x: v.pageX, y: v.pageY, width: v.clientWidth, height: v.clientHeight,
        scale: Math.min(1, 1000 / v.clientWidth, 750 / v.clientHeight) }
    });
    await this.check(s, command);
    if (typeof data !== 'string' || !data.length || data.length > 180000) throw new Error('PREVIEW_FRAME_TOO_LARGE');
    return { leaseId: lease.leaseId,
      identity: Object.fromEntries(['taskId', 'bindingId', 'assignmentVersion', 'browserWindowId', 'tabId',
        'documentId', 'navigationEpoch'].map(k => [k, lease[k]])), data, capturedAt: Date.now() / 1000 };
  }

  async execute(command, s) {
    await this.check(s, command);
    const op = command.operation, params = command.params || {};
    if (op === 'render_probe') return this.renderProbe(command, s);
    if (op === 'snapshot') return this.script(s, command, c => globalThis.__quietbridgeSnapshot?.(c));
    if (op === 'read') return this.script(s, command, (c, a) => globalThis.__quietbridgeRead?.(c, a), [params]);
    if (op === 'diagnostics') return this.script(s, command, c => globalThis.__quietbridgeDiagnostics?.(c));
    // Wheel input may wait for a hidden window's compositor indefinitely.
    // Scroll the verified document/element directly, without focus or OS input.
    if (op === 'scroll') return this.script(s, command, (c, a) => globalThis.__quietbridgeScroll?.(c, a), [params]);
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
      const scale = Math.min(1, 1600 / v.clientWidth, 1200 / v.clientHeight);
      for (const [quality, factor] of [[75, 1], [55, 1], [45, 0.75]]) {
        const { data } = await this.send(s, command, 'Page.captureScreenshot', { format: 'jpeg', quality,
          fromSurface: true, captureBeyondViewport: false,
          clip: { x: v.pageX, y: v.pageY, width: v.clientWidth, height: v.clientHeight, scale: scale * factor } });
        await this.check(s, command);
        if (data && data.length <= 500000) return { mimeType: 'image/jpeg', data, tabId: s.tabId, documentId: s.documentId };
      }
      throw new Error('截图超出大小限制，请使用 DOM 快照');
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
    const prepared = await this.script(s, command, (c, a) => globalThis.__quietbridgePrepare?.(c, a), [params]);
    if (!prepared.ok) return { performed: false, verified: false, observed: { reason: prepared.reason } };
    if (op === 'select') return this.script(s, command, (c, a) => globalThis.__quietbridgeSelect?.(c, a), [params]);
    if (op === 'fill') {
      const focused = await this.script(s, command, (c, a) => globalThis.__quietbridgeFocusInput?.(c, a), [params]);
      if (!focused) throw new Error('输入框不可编辑');
      const ready = await this.script(s, command, (c, a) => globalThis.__quietbridgeInputReady?.(c, a), [params]);
      if (!ready) throw new Error('STALE_SNAPSHOT：输入框焦点已变化，未输入');
      if (params.value) await this.send(s, command, 'Input.insertText', { text: params.value });
      else {
        await this.send(s, command, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 });
        await this.send(s, command, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 });
      }
      const verified = await this.script(s, command, (c, a) => globalThis.__quietbridgeVerifyFill?.(c, a), [params]);
      return { performed: true, verified, observed: { route: 'cdp', length: (params.value ?? '').length } };
    }
    const point = { x: prepared.x, y: prepared.y };
    if (op === 'click') {
      // A background renderer may never acknowledge hover/compositor work.
      // A click does not require a separate hover: validate the current hit
      // target immediately, then dispatch only the bound press/release pair.
      const hit = await this.script(s, command, (c, a, p) => globalThis.__quietbridgePointMatches?.(c, a, p), [params, point]);
      if (!hit) throw new Error('STALE_SNAPSHOT：点击前目标移动或被遮挡，未点击');
      await this.send(s, command, 'Input.dispatchMouseEvent', { type: 'mousePressed', ...point,
        button: 'left', buttons: 1, clickCount: 1 });
      try {
        await this.send(s, command, 'Input.dispatchMouseEvent', { type: 'mouseReleased', ...point,
          button: 'left', buttons: 0, clickCount: 1 });
      } catch (error) {
        // A mousedown handler may navigate. Do not send even an offscreen
        // mouseup to its replacement document; require explicit reassignment.
        await this.release(s.tabId).catch(() => {});
        this.detached.add(s.tabId);
        throw error;
      }
    } else throw new Error('不支持的后台操作');
    return { performed: true, verified: false, observed: { route: 'cdp', tabId: s.tabId, ...point } };
  }
}
