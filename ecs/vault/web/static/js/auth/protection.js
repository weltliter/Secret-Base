// Broadcast only logout notifications, never credentials or business data.
export function createProtection({ api, isAuthenticated, lock, document: doc = globalThis.document,
  window: win = globalThis.window, now = Date.now, setInterval: repeat = globalThis.setInterval,
  clearInterval: stopRepeat = globalThis.clearInterval, BroadcastChannel: Channel = globalThis.BroadcastChannel }) {
  const channel = Channel ? new Channel("vault-admin-lock") : null;
  let idleDeadline = 0, absoluteDeadline = 0, lastTick = now(), lastReport = -Infinity, reporting = false;
  function sync(session) {
    if (!session) return;
    idleDeadline = now() + Math.max(0, session.idle_remaining_seconds) * 1000;
    absoluteDeadline = now() + Math.max(0, session.absolute_remaining_seconds) * 1000;
  }
  function reset() { idleDeadline = 0; absoluteDeadline = 0; lastReport = -Infinity; lastTick = now(); }
  function force(reason, broadcast = true) {
    reset();
    lock(reason);
    if (broadcast) channel?.postMessage({ type: "lock" });
  }
  async function activity(event) {
    if (!event.isTrusted || doc.hidden || !isAuthenticated() || reporting || now() - lastReport < 5000) return;
    lastReport = now();
    reporting = true;
    try { const timing = await api("/api/session/activity", "POST", {}); if (isAuthenticated()) sync(timing); }
    catch (_) { /* Transport handles expiry and connection loss. */ }
    finally { reporting = false; }
  }
  const visibility = () => {
    if (doc.hidden) force("页面已切到后台，未保存内容已清除，请重新登录。");
    lastTick = now();
  };
  const pagehide = () => force("页面已关闭或离开，请重新登录。");
  const pageshow = (event) => { if (event.persisted) force("页面已恢复，请重新登录。"); };
  const offline = () => force("网络已断开，未保存内容已清除，请重新登录。");
  const tick = () => {
    const current = now();
    if (isAuthenticated()) {
      if (current - lastTick > 5000 || current < lastTick) force("检测到休眠或运行中断，请重新登录。");
      else if ((idleDeadline && current >= idleDeadline) || (absoluteDeadline && current >= absoluteDeadline)) force("管理会话已到期，请重新登录。");
    }
    lastTick = current;
  };
  for (const event of ["click", "input", "scroll"]) doc.addEventListener(event, activity, { capture: true, passive: true });
  doc.addEventListener("visibilitychange", visibility);
  win.addEventListener("pagehide", pagehide);
  win.addEventListener("pageshow", pageshow);
  win.addEventListener("offline", offline);
  if (channel) channel.onmessage = (event) => { if (event.data?.type === "lock") force("此浏览器的管理会话已锁定，请重新登录。", false); };
  const interval = repeat(tick, 1000);
  return { sync, reset, lock: force, dispose() {
    stopRepeat(interval); channel?.close();
    for (const event of ["click", "input", "scroll"]) doc.removeEventListener(event, activity, true);
    doc.removeEventListener("visibilitychange", visibility);
    win.removeEventListener("pagehide", pagehide); win.removeEventListener("pageshow", pageshow); win.removeEventListener("offline", offline);
  } };
}
