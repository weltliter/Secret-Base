// Pure filtering: no credential reveal, network calls, or browser persistence.
export function defaultFilters() {
  return { query: "", field: "all", tags: [], tagMode: "any", tagScope: "all", dateField: "created_at", from: "", to: "", sort: "updated_desc" };
}

const normalize = (value) => String(value ?? "").normalize("NFKC").toLocaleLowerCase();
export function localDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export function hasFilters(filters) {
  return !!(filters.query.trim() || filters.tags.length || filters.tagScope !== "all" || filters.from || filters.to);
}

export function selectEntries(entries, filters) {
  const words = normalize(filters.query).trim().split(/\s+/).filter(Boolean);
  const invalidRange = !!(filters.from && filters.to && filters.from > filters.to);
  const visible = invalidRange ? [] : entries.filter((entry) => {
    const tags = Array.isArray(entry.tags) ? entry.tags : [];
    const fields = { title: entry.title, service: entry.service, username: entry.username, description: entry.description };
    const text = normalize(filters.field === "all" ? [...Object.values(fields), ...tags].join(" ") : fields[filters.field]);
    if (!words.every((word) => text.includes(word))) return false;
    if (filters.tagScope === "untagged" && tags.length) return false;
    if (filters.tagScope === "tagged" && !tags.length) return false;
    if (filters.tags.length && !(filters.tagMode === "all" ? filters.tags.every((tag) => tags.includes(tag)) : filters.tags.some((tag) => tags.includes(tag)))) return false;
    if (filters.from || filters.to) {
      const date = localDate(entry[filters.dateField]);
      if (!date || (filters.from && date < filters.from) || (filters.to && date > filters.to)) return false;
    }
    return true;
  });
  const order = {
    updated_desc: ["updated_at", -1], created_desc: ["created_at", -1], created_asc: ["created_at", 1],
  }[filters.sort];
  visible.sort((a, b) => {
    if (!order) return (filters.sort === "title_desc" ? -1 : 1) * String(a.title || "").localeCompare(String(b.title || ""), "zh-CN", { numeric: true }) || String(a.id).localeCompare(String(b.id));
    const left = Date.parse(a[order[0]]), right = Date.parse(b[order[0]]);
    if (Number.isNaN(left) !== Number.isNaN(right)) return Number.isNaN(left) ? 1 : -1;
    return (Number.isNaN(left) ? 0 : (left - right) * order[1]) || String(a.id).localeCompare(String(b.id));
  });
  return { visible, invalidRange };
}
