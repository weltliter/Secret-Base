import { $ } from "../shared/ui.js";
import { createRecordFilters } from "./filter-controls.js";
import { createRecordList } from "./list.js";
import { createEntryEditor } from "./editor.js";
import { bindDuplicates } from "./duplicates.js";

export function createRecords({ state, api, notify, busy, copyText, loadStatus }) {
  const filters = createRecordFilters({ state, renderEntries });
  const list = createRecordList({ state, getSelection: filters.getSelection });
  const { hidePassword } = list;
  const { openEditor, clearTags } = createEntryEditor({ state, api, notify, busy, refreshAfterMutation });
  bindDuplicates({ state, api, busy, openEditor });

  function renderEntries() { list.renderEntries(); }

  async function loadEntries() {
    if (!state.status?.unlocked || !["entries", "trash"].includes(state.view)) return;
    const generation = state.generation;
    const view = state.view;
    const data = await api(`/api/entries?trash=${view === "trash"}`);
    if (generation !== state.generation || view !== state.view || !state.status?.unlocked) return;
    state.entries = Array.isArray(data.entries) ? data.entries : [];
    filters.renderTags();
    renderEntries();
  }

  async function refreshAfterMutation() {
    $("duplicates-panel").hidden = true;
    $("duplicate-groups").replaceChildren();
    await loadStatus();
    await loadEntries();
  }

  $("entries-list").addEventListener("click", (event) => {
    const tagButton = event.target.closest("button[data-filter-tag]");
    if (tagButton) {
      filters.selectTag(tagButton.dataset.filterTag);
      return;
    }
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.dataset.id;
    const entry = state.entries.find((item) => item.id === id);
    if (!entry) return;
    const action = button.dataset.action;
    if (action === "edit") { openEditor(entry); return; }
    if (action === "hide") { hidePassword(id); return; }
    if (action === "copy") { copyText(state.revealed.get(id) || "", "密码已复制到剪贴板。"); return; }
    busy(button, async () => {
      if (action === "reveal") {
        const generation = state.generation;
        const result = await api(`/api/entries/${encodeURIComponent(id)}/reveal`);
        if (generation !== state.generation || !state.status?.unlocked) return;
        state.revealed.set(id, result.password || "");
        clearTimeout(state.revealTimers.get(id));
        state.revealTimers.set(id, setTimeout(() => hidePassword(id), 30000));
        renderEntries();
      } else if (action === "trash") {
        if (!window.confirm(`将“${entry.title}”移入回收站？\n30 天内可以恢复。`)) return;
        await api(`/api/entries/${encodeURIComponent(id)}/trash`, "POST", { version: entry.version });
        hidePassword(id);
        await refreshAfterMutation();
        notify("记录已移入回收站，可在 30 天内恢复。");
      } else if (action === "restore") {
        await api(`/api/entries/${encodeURIComponent(id)}/restore`, "POST", {});
        await refreshAfterMutation();
        notify("记录已恢复到账号列表。");
      }
    });
  });

  return { loadEntries, renderEntries, resetFilters: filters.reset, clearTags };
}
