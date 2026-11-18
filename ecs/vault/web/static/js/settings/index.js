import { $ } from "../shared/ui.js";

export function createSettings({ state, api, notify, busy, loadStatus }) {
  function stopQr() {
    clearInterval(state.qrTimer);
    state.qrTimer = null;
    state.qr = null;
    state.qrPolling = false;
    $("wechat-qr-panel").hidden = true;
    $("wechat-qr-image").removeAttribute("src");
    $("wechat-verify-form").hidden = true;
    $("wechat-verify-code").value = "";
  }

  async function pollQr(verifyCode) {
    if (!state.qr || state.qrPolling || !state.status?.unlocked) return;
    if (Date.now() - state.qrStartedAt > 5 * 60 * 1000) {
      stopQr();
      notify("本次绑定已超时，请重新生成二维码。", true);
      return;
    }
    const currentQr = state.qr;
    state.qrPolling = true;
    try {
      const body = { qrcode: currentQr };
      if (verifyCode) body.verify_code = verifyCode;
      const data = await api("/api/wechat/poll", "POST", body);
      if (currentQr !== state.qr || !state.status?.unlocked) return;
      if (data.status === "confirmed") {
        stopQr();
        await loadStatus();
        notify("微信已绑定。现在可以用这个微信账号发送查询。");
      } else if (data.status === "expired" || data.status === "verify_code_blocked") {
        stopQr();
        notify(data.status === "expired" ? "二维码已过期，请重新生成。" : "微信验证码验证未通过，请重新生成二维码。", true);
      } else if (data.status === "need_verifycode") {
        $("wechat-qr-state").textContent = "请按微信提示输入验证码。";
        $("wechat-verify-form").hidden = false;
        clearInterval(state.qrTimer);
        state.qrTimer = null;
      } else {
        $("wechat-qr-state").textContent = data.status === "scaned" ? "已扫码，请在微信中确认授权。" : "请用你的微信扫码并确认授权。";
        if (verifyCode && !state.qrTimer) state.qrTimer = setInterval(() => { pollQr().catch(() => {}); }, 3000);
      }
    } catch (error) {
      if (currentQr === state.qr) $("wechat-qr-state").textContent = error.message;
    } finally { state.qrPolling = false; }
  }

  $("deepseek-form").addEventListener("submit", (event) => {
    event.preventDefault();
    busy(event.submitter || event.target.querySelector('[type="submit"]'), async () => {
      const key = $("deepseek-key").value.trim();
      if (!key) throw new Error("请填写 DeepSeek API Key。");
      await api("/api/settings/deepseek", "POST", { api_key: key });
      $("deepseek-key").value = "";
      await loadStatus();
      notify("DeepSeek API Key 已加密保存。");
    });
  });
  $("bind-wechat-button").addEventListener("click", (event) => busy(event.currentTarget, async () => {
    if (state.status?.wechat?.bound && !window.confirm("重新绑定会替换当前微信连接。确认继续？")) return;
    stopQr();
    const generation = state.generation;
    const data = await api("/api/wechat/qr", "POST", {});
    if (generation !== state.generation || !state.status?.unlocked) return;
    if (!data.qrcode) throw new Error("微信没有返回有效二维码，请稍后重试。");
    state.qr = data.qrcode;
    state.qrStartedAt = Date.now();
    $("wechat-qr-image").src = "/api/wechat/qr-image?" + new URLSearchParams({ qrcode: data.qrcode_img_content || data.qrcode });
    $("wechat-qr-state").textContent = "请用你的微信扫码并确认授权。";
    $("wechat-qr-panel").hidden = false;
    state.qrTimer = setInterval(() => { pollQr().catch(() => {}); }, 3000);
  }));
  $("cancel-wechat-button").addEventListener("click", stopQr);
  $("wechat-verify-form").addEventListener("submit", (event) => {
    event.preventDefault();
    busy(event.submitter || event.target.querySelector('[type="submit"]'), async () => {
      const code = $("wechat-verify-code").value.trim();
      $("wechat-verify-code").value = "";
      await pollQr(code);
    });
  });

  return { stopQr };
}
