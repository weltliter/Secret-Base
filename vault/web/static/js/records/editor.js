import { $, node } from "../shared/ui.js";
import { createTagPicker, parseTags } from "./tags.js";

export function createEntryEditor({ state, api, notify, busy, refreshAfterMutation }) {
  const tagPicker = createTagPicker({ api });
  function addCustomField(key = "", value = "") {
    const row = node("div", "custom-field-row");
    const nameInput = node("input");
    nameInput.placeholder = "字段名称";
    nameInput.setAttribute("aria-label", "自定义字段名称");
    nameInput.autocomplete = "off";
    nameInput.value = key;
    nameInput.maxLength = 128;
    const valueInput = node("textarea");
    valueInput.rows = 2;
    valueInput.placeholder = "机密内容";
    valueInput.setAttribute("aria-label", "自定义字段内容");
    valueInput.autocomplete = "off";
    valueInput.spellcheck = false;
    valueInput.value = value;
    valueInput.maxLength = 65536;
    const remove = node("button", "icon-button", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "删除此自定义字段");
    remove.addEventListener("click", () => row.remove());
    row.append(nameInput, valueInput, remove);
    $("custom-fields").append(row);
  }

  function closeEditor() {
    tagPicker.clear();
    $("entry-dialog").close();
    $("entry-form").reset();
    $("custom-fields").replaceChildren();
    $("entry-password").type = "password";
    state.editing = null;
  }

  function openEditor(entry = null) {
    state.editing = entry ? { id: entry.id, version: entry.version } : null;
    $("entry-form").reset();
    $("entry-dialog-title").textContent = entry ? "编辑记录" : "新增记录";
    $("password-edit-hint").hidden = !entry;
    $("entry-password").type = "password";
    $("toggle-entry-password").textContent = "显示";
    $("toggle-entry-password").setAttribute("aria-label", "显示输入的密码");
    for (const key of ["title", "service", "username", "description", "notes"]) $("entry-" + key).value = entry?.[key] || "";
    $("custom-fields").replaceChildren();
    for (const [key, value] of Object.entries(entry?.fields || {})) addCustomField(key, String(value));
    $("entry-dialog").showModal();
    tagPicker.open(entry?.tags || []);
    $("entry-title").focus();
  }

  $("new-entry-button").addEventListener("click", () => openEditor());
  $("close-entry-dialog").addEventListener("click", closeEditor);
  $("cancel-entry-dialog").addEventListener("click", closeEditor);
  $("entry-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeEditor(); });
  $("add-custom-field").addEventListener("click", () => addCustomField());
  $("toggle-entry-password").addEventListener("click", () => {
    const shown = $("entry-password").type === "password";
    $("entry-password").type = shown ? "text" : "password";
    $("toggle-entry-password").textContent = shown ? "隐藏" : "显示";
    $("toggle-entry-password").setAttribute("aria-label", shown ? "隐藏输入的密码" : "显示输入的密码");
  });

  $("entry-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const button = event.submitter || $("entry-form").querySelector('[type="submit"]');
    busy(button, async () => {
      const fields = Object.create(null);
      for (const row of $("custom-fields").children) {
        const [keyInput, valueInput] = row.querySelectorAll("input, textarea");
        const key = keyInput.value.trim();
        if (!key && !valueInput.value) continue;
        if (!key) throw new Error("请填写每个自定义字段的名称。");
        if (Object.hasOwn(fields, key)) throw new Error(`自定义字段名称“${key}”重复，请使用不同名称。`);
        fields[key] = valueInput.value;
      }
      const body = { fields };
      for (const key of ["title", "service", "username", "description", "notes"]) body[key] = $("entry-" + key).value;
      body.title = body.title.trim();
      if (!body.title) throw new Error("请填写记录标题。");
      body.tags = parseTags($("entry-tags").value);
      if (body.tags.length > 20 || body.tags.some((tag) => tag.length > 64)) throw new Error("最多保存 20 个标签，每个标签不超过 64 个字符。");
      if (Object.keys(fields).length > 50) throw new Error("每条记录最多保存 50 个自定义字段。");
      const editing = state.editing;
      if (!editing || $("entry-password").value) body.password = $("entry-password").value;
      if (editing) body.version = editing.version;
      await api(editing ? `/api/entries/${encodeURIComponent(editing.id)}` : "/api/entries", editing ? "PUT" : "POST", body);
      closeEditor();
      await refreshAfterMutation();
      notify(editing ? "记录已更新。" : "记录已加密保存。");
    });
  });

  return { openEditor, clearTags: tagPicker.clear };
}
