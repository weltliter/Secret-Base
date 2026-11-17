export function createApi(onLocked, options = {}) {
  const fetcher = options.fetch || globalThis.fetch.bind(globalThis);
  const later = options.setTimeout || globalThis.setTimeout;
  const cancelTimer = options.clearTimeout || globalThis.clearTimeout;
  let csrf = null, epoch = 0, probing = null, revoking = Promise.resolve();
  const requests = new Set();
  const cancelled = () => Object.assign(new Error("页面已锁定，请重新登录。"), { silent: true });
  const check = (generation) => { if (generation !== epoch) throw cancelled(); };

  async function request(path, init, timeout = 5000) {
    const controller = new AbortController();
    requests.add(controller);
    const timer = later(() => controller.abort(), timeout);
    try {
      const response = await fetcher(path, { credentials: "same-origin", cache: "no-store", redirect: "error", ...init, signal: controller.signal });
      const payload = response.status === 204 ? {} : await response.json().catch((error) => {
        if (controller.signal.aborted) throw error;
        return {};
      });
      if (controller.signal.aborted) throw new Error("Request timed out");
      return { response, payload };
    } finally { cancelTimer(timer); requests.delete(controller); }
  }

  async function token(generation) {
    if (csrf) return csrf;
    const { response, payload } = await request("/api/csrf", {});
    check(generation);
    if (!response.ok || !payload.token) throw new Error("无法建立管理会话，请刷新页面。");
    csrf = payload.token;
    return csrf;
  }

  function probeConnection(generation) {
    if (probing) return probing;
    const attempt = async () => {
      for (let count = 0; count < 2; count += 1) {
        check(generation);
        try {
          const { response, payload } = await request("/health", {}, 3000);
          check(generation);
          if (response.ok && payload.ok === true) return;
        } catch (error) { if (generation !== epoch) return; }
        if (count === 0) await new Promise((resolve) => later(resolve, 2000));
      }
      if (generation === epoch) onLocked("连接已中断，页面内容已清除，请重新登录。");
    };
    probing = attempt().catch(() => {}).finally(() => { probing = null; });
    return probing;
  }

  async function api(path, method = "GET", body, options = {}) {
    const generation = epoch;
    const headers = { Accept: "application/json" };
    try {
      // Serialize new authentication behind pending cookie revocation.
      if (path === "/api/unlock" || path === "/api/setup") await revoking;
      check(generation);
      if (method !== "GET") {
        headers["Content-Type"] = options.upload ? "application/octet-stream" : "application/json";
        if (options.upload) headers["X-Vault-Confirm-Replace"] = "replace-all";
        headers["X-Vault-CSRF"] = await token(generation);
      }
      check(generation);
      const { response, payload } = await request(path, { method, headers, body: options.upload ? body : body === undefined ? undefined : JSON.stringify(body) }, options.timeout || 5000);
      check(generation);
      if (!response.ok) {
        if (response.status === 403) csrf = null;
        if (response.status === 423) onLocked("管理端已锁定，请重新登录。");
        const detail = payload.detail || payload.error || payload.message;
        throw Object.assign(new Error(typeof detail === "string" ? detail : "请求未完成（" + response.status + "），请重试。"), { http: true });
      }
      return payload;
    } catch (error) {
      check(generation);
      if (error.http || error.silent) throw error;
      void probeConnection(generation);
      throw new Error("无法连接服务，请检查 SSH 隧道和服务器状态。");
    }
  }

  api.cancel = () => { epoch += 1; for (const controller of requests) controller.abort(); requests.clear(); };
  api.revoke = () => {
    // Independent keepalive logout; server deadlines remain authoritative offline.
    const perform = async () => {
      const controller = new AbortController();
      const timer = later(() => controller.abort(), 5000);
      try {
        if (!csrf) {
          const response = await fetcher("/api/csrf", { credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal, keepalive: true });
          if (!response.ok) return;
          csrf = (await response.json()).token;
        }
        if (csrf) await fetcher("/api/lock", { method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error", keepalive: true, signal: controller.signal,
          headers: { "Content-Type": "application/json", "X-Vault-CSRF": csrf }, body: "{}" });
      } catch (_) { /* Local clearing and backend deadlines still apply. */ }
      finally { cancelTimer(timer); }
    };
    revoking = revoking.then(perform);
    return revoking;
  };
  return api;
}
