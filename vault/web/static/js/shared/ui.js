export const $ = (id) => document.getElementById(id);

export function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}

export function pill(id, text, mode = "") {
  const target = $(id);
  target.textContent = text;
  target.className = "status-pill" + (mode ? ` ${mode}` : "");
}

export function createUi(state) {
  function notify(message, error = false, persistent = false) {
    clearTimeout(state.noticeTimer);
    const notice = $("notice");
    notice.textContent = message;
    notice.classList.toggle("error", error);
    notice.setAttribute("role", error ? "alert" : "status");
    notice.hidden = false;
    if (!persistent) state.noticeTimer = setTimeout(() => { notice.hidden = true; notice.textContent = ""; }, error ? 10000 : 5500);
  }

  async function busy(button, action) {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { await action(); } catch (error) { notify(error.message || "操作未完成，请重试。", true); }
    finally { button.disabled = false; button.removeAttribute("aria-busy"); }
  }

  async function copyText(text, message) {
    try {
      await navigator.clipboard.writeText(text);
      notify(message);
    } catch (_) { notify("浏览器未允许复制，请选中文字手动复制。", true); }
  }

  return { notify, busy, copyText };
}
