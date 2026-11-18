import { $, node } from "../shared/ui.js";
export const formatBytes = (value) => typeof value !== "number" ? "—" : value >= 1024 ** 3 ? (value / 1024 ** 3).toFixed(2) + " GiB" : value >= 1024 ** 2 ? (value / 1024 ** 2).toFixed(1) + " MiB" : value.toLocaleString("zh-CN") + " 字节";
const when = (value) => value ? new Date(value).toLocaleString("zh-CN") : "—";
const statuses = { generating: "正在生成", ready: "服务器备份已就绪", failed: "生成失败", expired: "已过期" };
const phases = { queued: "等待处理", dump: "导出数据库", dumping: "导出数据库", validate: "检查转储", validating: "检查转储", hash: "计算校验值", hashing: "计算校验值", ready: "可下载" };

export function createBackups({ state, api, notify, busy, lock }) {
  let timer = null, loading = false, sequence = 0, limits = {};
  function clear() { sequence += 1; loading = false; clearTimeout(timer); timer = null; $("backup-list").replaceChildren(); $("backup-limits").textContent = ""; $("restore-file").value = ""; $("restore-confirm").checked = false; $("restore-progress").textContent = ""; }
  async function load() {
    if (!state.status?.unlocked || state.view !== "backups" || loading) return;
    loading = true;
    clearTimeout(timer);
    const generation = state.generation;
    const current = ++sequence;
    try {
      const result = await api("/api/backups");
      if (current !== sequence || generation !== state.generation || !state.status?.unlocked || state.view !== "backups") return;
      const tasks = result.tasks || [];
      limits = result.limits || {};
      $("backup-limits").textContent = "保留 " + (limits.retention_days ?? 7) + " 天 · 配额 " + formatBytes(limits.quota_bytes ?? 5368709120) + " · 最低剩余空间 " + formatBytes(limits.reserve_bytes ?? 1073741824) + " · 生成时限 " + Math.round((limits.timeout_seconds ?? 1800) / 60) + " 分钟";
      $("backup-list").replaceChildren();
      $("backups-empty").hidden = tasks.length > 0;
      for (const task of tasks) {
        const card = node("article", "settings-card backup-card");
        const heading = node("div", "section-heading");
        heading.append(node("h3", "", statuses[task.status] || "状态未知"), node("span", "muted small", when(task.created_at)));
        card.append(heading);
        if (task.status === "generating") card.append(node("p", "muted", phases[task.phase] || "处理中，请稍候…"));
        if (task.status === "failed") card.append(node("p", "inline-error", task.error || "备份未完成，请检查服务器空间及服务状态后重新生成。"));
        if (task.status === "ready") {
          card.append(node("p", "small", (task.filename || "数据库备份") + " · " + formatBytes(task.size_bytes) + " · 有效至 " + when(task.expires_at)));
          card.append(node("p", "backup-hash small", "SHA-256：" + (task.sha256 || "—")));
          const actions = node("div", "backup-actions");
          for (const [suffix, label] of [["download", "下载数据库到本机"], ["checksum", "下载校验文件"], ["manifest", "下载备份说明"]]) {
            const link = node("a", suffix === "download" ? "button button-primary" : "button button-secondary", label);
            link.href = "/api/backups/" + encodeURIComponent(task.id) + "/" + suffix;
            link.download = "";
            link.addEventListener("click", (event) => {
              if (!state.status?.unlocked) { event.preventDefault(); return; }
              notify("已请求浏览器下载。请检查下载列表，并核对文件大小与 SHA-256；本页面不能确认文件已保存或恢复成功。", false, true);
            });
            actions.append(link);
          }
          card.append(actions);
        }
        $("backup-list").append(card);
      }
      if (tasks.some((task) => task.status === "generating")) timer = setTimeout(() => { load().catch((error) => { if (!error.silent) notify(error.message, true); }); }, 3000);
    } finally { if (current === sequence) loading = false; }
  }
  $("create-backup").addEventListener("click", (event) => busy(event.currentTarget, async () => {
    await api("/api/backups", "POST", {});
    notify("已提交全量数据库备份任务。切后台后管理端会锁定，任务继续生成，可重新登录领取。");
    await load();
  }));
  $("refresh-backups").addEventListener("click", (event) => busy(event.currentTarget, load));
  $("restore-form").addEventListener("submit", (event) => {
    event.preventDefault();
    return busy($("restore-backup"), async () => {
      const file = $("restore-file").files?.[0];
      if (!state.status?.unlocked || !file || !$("restore-confirm").checked) return;
      if (!/\.(dump|pgdump)$/i.test(file.name) || !file.size) throw new Error("请选择非空的 .dump 数据库备份文件。");
      if (file.size > (limits.quota_bytes ?? 5368709120)) throw new Error("文件超过服务器备份目录配额。");
      const generation = state.generation;
      state.restoring = true;
      $("restore-progress").textContent = "正在上传、校验并恢复，请保持页面打开。大文件可能需要较长时间。";
      try {
        await api("/api/backups/restore", "POST", file, { upload: true, timeout: ((limits.timeout_seconds ?? 1800) + 30) * 1000 });
        if (generation === state.generation) lock("全量恢复成功。请使用备份对应的主密码或恢复密钥重新登录，恢复微信查询。");
      } finally {
        state.restoring = false;
        if (generation === state.generation) $("restore-progress").textContent = "";
      }
    });
  });
  return { load, clear };
}
