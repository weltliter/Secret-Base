import { $ } from "../shared/ui.js";

export function bindSession({ api, notify, busy, copyText, loadStatus, loadEntries, clearSensitive, changeView }) {
  $("setup-form").addEventListener("submit", (event) => {
    event.preventDefault();
    busy(event.submitter || event.target.querySelector('[type="submit"]'), async () => {
      const password = $("setup-password").value;
      if (password !== $("setup-confirm").value) throw new Error("两次输入的主密码不一致。");
      if (password.length < 12) throw new Error("主密码请至少使用 12 个字符。");
      const result = await api("/api/setup", "POST", { password });
      $("setup-form").reset();
      if (result.recovery_key) {
        $("recovery-key").textContent = result.recovery_key;
        $("recovery-saved").checked = false;
        $("finish-recovery").disabled = true;
        $("recovery-dialog").showModal();
      } else notify("密码库已创建。", false, true);
      await loadStatus();
      await loadEntries();
    });
  });
  $("recovery-dialog").addEventListener("cancel", (event) => event.preventDefault());
  $("recovery-saved").addEventListener("change", (event) => { $("finish-recovery").disabled = !event.target.checked; });
  $("finish-recovery").addEventListener("click", () => {
    if (!$("recovery-saved").checked) return;
    $("recovery-key").textContent = "";
    $("recovery-dialog").close();
    changeView("settings").catch((error) => notify(error.message, true));
    notify("密码库已创建。接下来可以绑定微信和填写 DeepSeek Key。");
  });
  $("copy-recovery-key").addEventListener("click", () => copyText($("recovery-key").textContent, "恢复密钥已复制，请保存在服务器之外。"));

  $("unlock-form").addEventListener("submit", (event) => {
    event.preventDefault();
    busy(event.submitter || event.target.querySelector('[type="submit"]'), async () => {
      const password = $("unlock-password").value;
      $("unlock-password").value = "";
      await api("/api/unlock", "POST", { password });
      await loadStatus();
      await loadEntries();
      notify("已登录管理端。");
    });
  });
  $("lock-button").addEventListener("click", (event) => busy(event.currentTarget, async () => {
    await api("/api/lock", "POST", {});
    clearSensitive();
    await loadStatus();
    notify("管理端已锁定，微信查询服务保持运行。");
  }));
}
