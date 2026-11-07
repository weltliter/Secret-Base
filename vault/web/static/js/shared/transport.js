export function createApi(onLocked) {
  const state = { csrf: null, csrfPromise: null };

  function errorText(payload, fallback) {
    const detail = payload && (payload.detail || payload.error || payload.message);
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((part) => part.msg || "输入有误").join("；");
    return fallback;
  }

  async function csrfToken() {
    if (state.csrf) return state.csrf;
    if (!state.csrfPromise) {
      state.csrfPromise = fetch("/api/csrf", { cache: "no-store", credentials: "same-origin" })
        .then(async (response) => {
          if (!response.ok) throw new Error("无法建立管理会话，请刷新页面。");
          const data = await response.json();
          if (!data.token) throw new Error("管理会话无效，请刷新页面。");
          state.csrf = data.token;
          return data.token;
        }).finally(() => { state.csrfPromise = null; });
    }
    return state.csrfPromise;
  }

  async function api(path, method = "GET", body) {
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-Vault-CSRF"] = await csrfToken();
    }
    let response;
    try {
      response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", credentials: "same-origin", redirect: "error" });
    } catch (_) {
      throw new Error("无法连接服务，请检查 SSH 隧道和服务器状态。");
    }
    let payload = {};
    if (response.status !== 204) {
      try { payload = await response.json(); } catch (_) { /* Empty or non-JSON error response. */ }
    }
    if (!response.ok) {
      if (response.status === 403) state.csrf = null;
      if (response.status === 423) {
        onLocked();
      }
      throw new Error(errorText(payload, `请求未完成（${response.status}），请重试。`));
    }
    return payload;
  }

  return api;
}
