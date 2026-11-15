import { $, pill, createUi } from "./shared/ui.js";
import { createApi } from "./shared/transport.js";
import { createRecords } from "./records/index.js";
import { createSettings } from "./settings/index.js";
import { bindSession } from "./auth/session.js";
import { createLogs } from "./logs/index.js";
import { createProtection } from "./auth/protection.js";
import { createBackups } from "./backups/index.js";

const state = { reauthRequired: true, status: null, view: "entries", entries: [], editing: null, generation: 0, revealed: new Map(), revealTimers: new Map(), qr: null, qrTimer: null, qrStartedAt: 0, qrPolling: false, noticeTimer: null };
const { notify, busy, copyText } = createUi(state);
let protection;
const transport = createApi((message) => protection.lock(message));
const api = Object.assign(async (path, ...args) => {
  if (state.reauthRequired && !["/api/status", "/api/setup", "/api/unlock", "/api/csrf", "/api/lock"].includes(path)) {
    throw Object.assign(new Error("请先重新登录。"), { silent: true });
  }
  return transport(path, ...args);
}, transport);
const settings = createSettings({ state, api, notify, busy, loadStatus });
const records = createRecords({ state, api, notify, busy, copyText, loadStatus });
const logs = createLogs({ state, api, busy, notify });
const backups = createBackups({ state, api, notify, busy });
const loadView = () => state.view === "backups" ? backups.load() : state.view === "logs" ? logs.load() : records.loadEntries();
protection = createProtection({ api, isAuthenticated: () => !!state.status?.unlocked && !state.reauthRequired, lock: lockLocal });
bindSession({ api, notify, busy, copyText, loadStatus, loadEntries: loadView, changeView,
  lock: () => protection.lock("管理端已锁定，微信查询服务保持运行。"),
  authenticated: () => {
    if (document.hidden) {
      protection.lock("页面已切到后台，请重新登录。");
      throw Object.assign(new Error("页面已锁定。"), { silent: true });
    }
    clearSensitive(); // Discard status polls started before this login.
    state.reauthRequired = false;
    protection.reset();
  }
});

function lockLocal(message) {
  state.reauthRequired = true;
  clearSensitive();
  if (state.status) state.status = publicStatus(state.status);
  renderState();
  if (message) notify(message, false, true);
  void api.revoke();
}

function publicStatus(status) {
  return { initialized: status.initialized, unlocked: false, session: null,
    query_ready: status.query_ready, auto_restore_ready: status.auto_restore_ready,
    wechat: { bound: status.wechat?.bound, connected: status.wechat?.connected } };
}

function clearSensitive() {
  state.generation += 1;
  api.cancel();
  backups.clear();
  clearTimeout(state.noticeTimer);
  $("notice").hidden = true;
  $("notice").textContent = "";
  state.entries = [];
  state.editing = null;
  state.revealed.clear();
  state.revealTimers.forEach(clearTimeout);
  state.revealTimers.clear();
  settings.stopQr();
  records.clearTags();
  records.resetFilters();
  logs.clear();
  $("include-reuse").checked = false;
  $("entries-list").replaceChildren();
  $("duplicate-groups").replaceChildren();
  $("duplicates-panel").hidden = true;
  $("entry-search").value = "";
  $("entry-form").reset();
  $("custom-fields").replaceChildren();
  $("deepseek-key").value = "";
  $("setup-form").reset();
  $("unlock-form").reset();
  $("entry-password").type = "password";
  $("recovery-key").textContent = "";
  $("recovery-saved").checked = false;
  $("finish-recovery").disabled = true;
  if ($("entry-dialog").open) $("entry-dialog").close();
  if ($("recovery-dialog").open) $("recovery-dialog").close();
}

function renderState() {
  const status = state.status;
  $("loading-panel").hidden = !!status;
  const unlocked = !!status?.unlocked;
  $("setup-panel").hidden = !status || !!status.initialized;
  $("unlock-panel").hidden = !status?.initialized || unlocked;
  $("navigation").hidden = !unlocked;
  $("lock-button").hidden = !unlocked;
  $("entries-panel").hidden = !unlocked || !["entries", "trash"].includes(state.view);
  $("settings-panel").hidden = !unlocked || state.view !== "settings";
  $("query-logs-panel").hidden = !unlocked || state.view !== "logs";
  $("backups-panel").hidden = !unlocked || state.view !== "backups";
  if (!status) return;

  $("nav-entry-count").textContent = String(status.entry_count ?? 0);
  $("nav-trash-count").textContent = String(status.trash_count ?? 0);
  pill("vault-status", unlocked ? "管理端已登录" : status.initialized ? "管理端已锁定" : "等待初始化", unlocked ? "good" : "");
  pill("query-status", status.query_ready ? (status.wechat?.connected ? "微信查询已连接" : "微信查询等待连接") : "微信查询未就绪", status.query_ready && status.wechat?.connected ? "good" : "warn");
  $("query-description").textContent = status.query_ready
    ? (status.auto_restore_ready ? "微信查询独立运行，锁定管理端不受影响；服务重启后会自动恢复。" : "微信查询独立运行；自动恢复尚未启用，请登录管理端。")
    : (status.auto_restore_ready ? "自动恢复已启用；登录管理端检查微信绑定和连接状态。" : "首次使用或升级后，请登录一次以启用查询自动恢复；主密码不会保存到服务器。");
  if (!status.initialized) {
    $("page-title").textContent = "欢迎使用你的私人密码库";
    $("page-description").textContent = "先设置主密码，再绑定微信和配置 AI。";
  } else if (!unlocked) {
    $("page-title").textContent = "登录管理端";
    $("page-description").textContent = "登录后可以新增、修改和删除账号记录。微信查询独立运行。";
  } else {
    const headings = { entries: ["账号记录", "把账号和使用场景放在一起，想起来时就能找到。"], trash: ["回收站", "已删除的记录保留 30 天，可以在这里恢复。"], settings: ["微信与 AI", "绑定你的微信，并配置用于理解查询的模型。"], logs: ["查询日志", "查看微信查询的召回、排序和运行情况，便于核对结果。"], backups: ["备份与灾备", "保存完整数据库，在新服务器恢复你的密码库。"] };
    $("page-title").textContent = headings[state.view][0];
    $("page-description").textContent = headings[state.view][1];
  }
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === state.view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  const trash = state.view === "trash";
  $("new-entry-button").hidden = trash;
  $("duplicates-button").hidden = trash;
  $("include-reuse").closest("label").hidden = trash;
  if (trash) $("duplicates-panel").hidden = true;
  $("records-heading").textContent = trash ? "已删除记录" : "全部记录";
  const wechat = status.wechat || {};
  pill("wechat-status", wechat.connected ? "已连接" : wechat.bound ? "已绑定" : "未绑定", wechat.connected ? "good" : wechat.bound ? "warn" : "");
  $("wechat-owner").textContent = wechat.owner_id || "尚未绑定";
  $("wechat-connection").textContent = wechat.connected ? "正常接收查询" : wechat.bound ? "等待连接" : "等待绑定";
  $("wechat-error").textContent = wechat.error || "";
  $("wechat-error").hidden = !wechat.error;
  $("bind-wechat-button").textContent = wechat.bound ? "重新扫码绑定" : "生成绑定二维码";
  pill("model-status", status.model_configured ? "已配置" : "未配置", status.model_configured ? "good" : "");
}

async function loadStatus() {
  const generation = state.generation;
  const previousUnlocked = state.status?.unlocked;
  let status = await api("/api/status");
  if (generation !== state.generation) return state.status;
  if (previousUnlocked && !status.unlocked) {
    protection.lock("管理会话已到期，请重新登录。");
    return state.status;
  }
  if (state.reauthRequired) status = publicStatus(status);
  else protection.sync(status.session);
  state.status = status;
  renderState();
  return status;
}

async function changeView(view) {
  if (!state.status?.unlocked) return;
  state.generation += 1;
  state.view = view;
  backups.clear();
  records.resetFilters();
  state.entries = [];
  state.revealed.clear();
  state.revealTimers.forEach(clearTimeout);
  state.revealTimers.clear();
  $("entry-search").value = "";
  $("entries-list").replaceChildren();
  $("duplicates-panel").hidden = true;
  renderState();
  await loadView();
}

$("navigation").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (button) changeView(button.dataset.view).catch((error) => notify(error.message, true));
});

async function start() {
  $("retry-button").hidden = true;
  try { await api.revoke(); await loadStatus(); await loadView(); }
  catch (error) {
    pill("vault-status", "连接未完成", "warn");
    $("retry-button").hidden = false;
    notify(error.message, true, true);
  }
}
$("retry-button").addEventListener("click", start);
// Protection owns visibility, bfcache, offline and sleep handling. Polls only
// inspect state; they never renew inactivity or silently authenticate.
setInterval(() => {
  if (!document.hidden) loadStatus().catch(() => {});
}, 15000);
start();
