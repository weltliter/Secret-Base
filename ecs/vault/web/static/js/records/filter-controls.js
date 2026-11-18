import { $, node } from "../shared/ui.js";
import { defaultFilters, hasFilters, selectEntries } from "./filters.js";

export function createRecordFilters({ state, renderEntries }) {
  let filters = defaultFilters();
  const controls = { field: "filter-field", tagScope: "filter-tag-scope", dateField: "filter-date-field", from: "filter-from", to: "filter-to", sort: "filter-sort", tagMode: "filter-tag-mode" };

  function renderFilterTags() {
    const counts = new Map();
    for (const entry of state.entries) for (const tag of new Set(entry.tags || [])) counts.set(tag, (counts.get(tag) || 0) + 1);
    const tags = [...new Set([...counts.keys(), ...filters.tags])].sort((a, b) => a.localeCompare(b, "zh-CN"));
    const container = $("filter-tags");
    container.replaceChildren();
    if (!tags.length) container.append(node("span", "muted small", "当前列表还没有标签，可在编辑记录时添加。"));
    for (const tag of tags) {
      const button = node("button", "filter-tag", `${tag} · ${counts.get(tag) || 0}`);
      button.type = "button";
      button.dataset.tag = tag;
      button.setAttribute("aria-pressed", String(filters.tags.includes(tag)));
      button.disabled = filters.tagScope === "untagged";
      container.append(button);
    }
  }

  function resetFilters() {
    filters = defaultFilters();
    $("entry-search").value = "";
    for (const [key, id] of Object.entries(controls)) $(id).value = filters[key];
    $("filter-tags").replaceChildren();
    $("filter-date-error").hidden = true;
    for (const id of ["filter-from", "filter-to"]) $(id).removeAttribute("aria-invalid");
    $("visible-count").textContent = "";
  }
  function selectTag(tag) {
    filters.tags = [tag];
    filters.tagScope = "all";
    $("filter-tag-scope").value = "all";
    renderFilterTags();
    renderEntries();
    $("filter-tags").querySelector('button[aria-pressed="true"]')?.focus();
  }
  $("entry-search").addEventListener("input", (event) => { filters.query = event.target.value; renderEntries(); });
  for (const [key, id] of Object.entries(controls)) $(id).addEventListener("change", (event) => {
    filters[key] = event.target.value;
    if (key === "tagScope") {
      if (filters.tagScope === "untagged") filters.tags = [];
      renderFilterTags();
    }
    renderEntries();
  });
  $("filter-tags").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tag]");
    if (!button) return;
    const tag = button.dataset.tag;
    filters.tags = filters.tags.includes(tag) ? filters.tags.filter((value) => value !== tag) : [...filters.tags, tag];
    button.setAttribute("aria-pressed", String(filters.tags.includes(tag)));
    renderEntries();
  });
  $("reset-filters").addEventListener("click", () => { resetFilters(); renderFilterTags(); renderEntries(); });

  return { renderTags: renderFilterTags, reset: resetFilters, selectTag, getSelection: () => ({ ...selectEntries(state.entries, filters), filtered: hasFilters(filters) }) };
}
