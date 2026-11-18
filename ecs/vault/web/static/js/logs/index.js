import { $, node } from "../shared/ui.js";

export function createLogs({ state, api, busy, notify }) {
  const modes = { hybrid_ai: "语义向量＋AI", lexical_ai: "关键词＋AI", local: "本地检索（AI 未参与）" };
  async function load() {
    if (!state.status?.unlocked || state.view !== "logs") return;
    const generation = state.generation;
    const data = await api("/api/query-logs?limit=50");
    if (generation !== state.generation || state.view !== "logs" || !state.status?.unlocked) return;
    const list = $("query-log-list");
    list.replaceChildren();
    $("query-logs-empty").hidden = data.logs.length > 0;
    for (const log of data.logs) {
      const item = node("article", "settings-card query-log-card");
      item.append(node("h3", "", log.query));
      const d = log.diagnostics || {};
      const when = new Date(log.created_at).toLocaleString("zh-CN");
      item.append(node("p", "muted small", `${when} · ${modes[d.mode] || "查询"} · ${d.elapsed_ms ?? "—"} ms`));
      item.append(node("p", "small", `召回 ${d.candidate_count ?? "—"} 条候选，返回 ${log.results.length} 条，其中 ${d.weak_count ?? 0} 条弱匹配。`));
      if (d.fallback_reason) item.append(node("p", "inline-error", `AI 未正常参与：${d.fallback_reason}。本次结果需要核对。`));
      if (d.vector_status === "unavailable") item.append(node("p", "inline-error", "语义向量服务暂不可用，本次使用关键词检索。"));
      const results = node("ul", "query-log-results");
      for (const result of log.results) {
        const similarity = typeof result.similarity === "number" ? ` · 向量相似度 ${result.similarity.toFixed(3)}` : "";
        results.append(node("li", "", `${result.title} · ${result.weak ? "弱匹配候选" : "匹配结果"}${similarity}`));
      }
      if (!log.results.length) results.append(node("li", "muted", "没有返回记录"));
      item.append(results);
      const details = node("details", "small muted");
      details.append(node("summary", "", "检索诊断"), node("p", "", `请求编号：${d.trace_id || "—"}；关键词候选 ${d.lexical_count ?? "—"}；向量候选 ${d.vector_count ?? "—"}；模型调用 ${d.model_calls ?? "—"} 次；工具调用 ${d.tool_calls ?? "—"} 次。`));
      item.append(details);
      list.append(item);
    }
  }
  $("refresh-query-logs").addEventListener("click", (event) => busy(event.currentTarget, load).catch((error) => notify(error.message, true)));
  return { load, clear: () => $("query-log-list").replaceChildren() };
}
