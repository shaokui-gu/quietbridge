// Executed in the page's main world, including before scripts on navigation.
// This is a focus constraint, not a source of authorization: all reported URLs
// are validated again by the worker against its own task/window binding.
export function installPopupGuard(taskId) {
  if (globalThis.__quietbridgePopupGuard) return;
  const originalOpen = window.open;
  let armed = false;
  let pending = [];
  const enqueue = value => {
    if (!armed || pending.length >= 4) return;
    try {
      const u = new URL(String(value), location.href);
      if (['http:', 'https:'].includes(u.protocol) && !u.username && !u.password &&
          !pending.includes(u.href)) pending.push(u.href);
    } catch {}
  };
  const guardedOpen = function(url) { enqueue(url); return null; };
  window.open = guardedOpen;
  const externalTarget = value => value && !['_self', '_top', '_parent'].includes(value.toLowerCase());
  const baseTarget = () => document.querySelector('base[target]')?.target;
  const click = event => {
    const a = event.composedPath().find(node => node instanceof HTMLAnchorElement);
    if (!a || !externalTarget(a.getAttribute('target') || baseTarget())) return;
    event.preventDefault();
    enqueue(a.href);
  };
  const submit = event => {
    if (!externalTarget(event.submitter?.formTarget || event.target.target || baseTarget())) return;
    // POST/new-window forms need an explicit interactive workflow. Never let a
    // native popup interrupt the user, and never silently replay a form submit.
    event.preventDefault();
  };
  addEventListener('click', click, true);
  addEventListener('submit', submit, true);
  const formPrototype = typeof HTMLFormElement === 'undefined' ? null : HTMLFormElement.prototype;
  const originalSubmit = formPrototype?.submit;
  const guardedSubmit = function(...args) {
    if (externalTarget(this.target || baseTarget())) return;
    return originalSubmit.apply(this, args);
  };
  if (formPrototype) formPrototype.submit = guardedSubmit;
  const api = (owner, action) => {
    if (owner !== taskId) return null;
    if (action === 'arm') { pending = []; armed = true; return true; }
    if (action === 'drain') { armed = false; const urls = pending; pending = []; return urls; }
    if (action === 'release') {
      armed = false;
      if (window.open === guardedOpen) window.open = originalOpen;
      if (formPrototype?.submit === guardedSubmit) formPrototype.submit = originalSubmit;
      removeEventListener('click', click, true);
      removeEventListener('submit', submit, true);
      delete globalThis.__quietbridgePopupGuard;
      return true;
    }
    return false;
  };
  Object.defineProperty(globalThis, '__quietbridgePopupGuard', { value: api, configurable: true });
}
