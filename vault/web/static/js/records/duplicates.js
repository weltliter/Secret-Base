import { $, node } from "../shared/ui.js";

export function bindDuplicates({ state, api, busy, openEditor }) {
  $("duplicates-button").addEventListener("click", (event) => busy(event.currentTarget, async () => {
    const generation = state.generation;
    const result = await api(`/api/duplicates?include_reuse=${$("include-reuse").checked}`);
    if (generation !== state.generation || !state.status?.unlocked) return;
    const container = $("duplicate-groups");
    container.replaceChildren();
    const groups = Array.isArray(result.groups) ? result.groups : [];
    const labels = { exact: "相同服务下的重复凭据", duplicate: "相同服务下的重复凭据", credential_duplicate: "相同服务下的重复凭据", conflict: "同一账号存在不同密码", version: "同一账号存在不同密码", reuse: "不同服务复用了密码", password_reuse: "不同服务复用了密码", data_difference: "凭据相同，其他资料不同" };
    if (!groups.length) container.append(node("p", "muted small", "目前没有发现符合综合判断条件的记录。"));
    for (const group of groups) {
      const block = node("div", "duplicate-group");
      block.append(node("strong", "", labels[group.kind] || group.kind || "需要核对的记录"));
      if (group.description) block.append(node("p", "", group.description));
      (group.ids || []).forEach((id, index) => {
        const entry = state.entries.find((item) => item.id === id);
        const button = node("button", "text-button", group.titles?.[index] || entry?.title || "查看记录");
        button.type = "button";
        button.disabled = !entry;
        if (entry) button.addEventListener("click", () => openEditor(entry));
        block.append(button);
      });
      container.append(block);
    }
    $("duplicates-panel").hidden = false;
  }));
  $("close-duplicates").addEventListener("click", () => { $("duplicates-panel").hidden = true; });

}
