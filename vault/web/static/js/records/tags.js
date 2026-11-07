import { $, node } from "../shared/ui.js";

export function parseTags(value) {
  return [...new Set(value.split(/[,，\n]/u).map((tag) => tag.trim()).filter(Boolean))];
}

export function createTagPicker({ api }) {
  let catalog = [];
  let revision = 0;
  const input = $("entry-tags");
  const choices = $("tag-choices");
  const status = $("tag-choice-status");

  function render() {
    const selected = new Set(parseTags(input.value));
    choices.replaceChildren();
    for (const tag of catalog) {
      const button = node("button", "tag-choice", tag);
      button.type = "button";
      button.setAttribute("aria-pressed", String(selected.has(tag)));
      button.addEventListener("click", () => {
        const values = parseTags(input.value);
        if (values.includes(tag)) input.value = values.filter((value) => value !== tag).join(", ");
        else if (values.length < 20) input.value = [...values, tag].join(", ");
        else { status.textContent = "最多选择 20 个标签。"; return; }
        render();
      });
      choices.append(button);
    }
    status.textContent = catalog.length ? "点击已有标签可选中或取消；也可以在上方输入新标签。" : "还没有已有标签，可以自行输入，也可以留空。";
  }

  function clear() {
    revision += 1;
    catalog = [];
    input.value = "";
    choices.replaceChildren();
    status.textContent = "";
  }

  async function open(values) {
    clear();
    const current = revision;
    input.value = Array.isArray(values) ? values.join(", ") : "";
    status.textContent = "正在读取已有标签…";
    try {
      const result = await api("/api/tags");
      if (current !== revision || !$("entry-dialog").open) return;
      catalog = Array.isArray(result.tags) ? [...new Set(result.tags.filter((tag) => typeof tag === "string" && tag.trim()))] : [];
      render();
    } catch (_) {
      if (current === revision && $("entry-dialog").open) status.textContent = "已有标签暂时无法读取，仍可自行输入或留空。";
    }
  }

  input.addEventListener("input", render);
  return { open, clear };
}
