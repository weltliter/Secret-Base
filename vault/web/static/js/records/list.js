import { $, node } from "../shared/ui.js";

export function createRecordList({ state, getSelection }) {
  function actionButton(text, action, id, className = "text-button") {
    const button = node("button", className, text);
    button.type = "button";
    button.dataset.action = action;
    button.dataset.id = id;
    return button;
  }

  function renderEntries() {
    const list = $("entries-list");
    list.replaceChildren();
    const { visible, invalidRange, filtered } = getSelection();
    const trash = state.view === "trash";
    $("visible-count").textContent = `显示 ${visible.length} / ${state.entries.length} 条记录`;
    $("filter-date-error").hidden = !invalidRange;
    for (const id of ["filter-from", "filter-to"]) $(id).setAttribute("aria-invalid", String(invalidRange));
    $("entries-empty").hidden = visible.length > 0;
    $("empty-title").textContent = invalidRange ? "请调整日期范围" : filtered ? "没有匹配的记录" : trash ? "回收站是空的" : "先添加第一条账号记录";
    $("empty-description").textContent = filtered ? "可以减少筛选条件，或点击“重置搜索与筛选”。" : trash ? "删除的记录会暂时保存在这里。" : "填好用途和场景，以后就能在微信里用自然语言找到它。";
    for (const entry of visible) {
      const card = node("article", trash ? "entry-row is-trash" : "entry-row");
      const header = node("div", "entry-summary");
      header.append(node("div", "entry-avatar", (entry.title || "账").slice(0, 1)));
      const title = node("div", "entry-main-title");
      title.append(node("h3", "", entry.title || "未命名记录"), node("p", "entry-service", entry.service || "未填写服务"));
      header.append(title);
      const account = node("div", "entry-account");
      account.append(node("span", "entry-label", "账号"), node("code", "entry-username", entry.username || "未填写"));
      card.append(header, account);
      if (!trash) {
        const secret = node("div", "entry-secret");
        secret.append(node("span", "entry-label", "密码"));
        const passwordRow = node("div", "entry-password-row");
        const shown = state.revealed.has(entry.id);
        passwordRow.append(node("code", shown ? "" : "password-masked", shown ? state.revealed.get(entry.id) || "（空密码）" : "••••••••••••"));
        passwordRow.append(actionButton(shown ? "隐藏" : "显示密码", shown ? "hide" : "reveal", entry.id));
        if (shown) passwordRow.append(actionButton("复制", "copy", entry.id));
        secret.append(passwordRow);
        card.append(secret);
      }
      const context = node("div", "entry-context");
      if (entry.description) context.append(node("p", "entry-description", entry.description));
      if (Array.isArray(entry.tags) && entry.tags.length) {
        const tags = node("div", "tag-list");
        entry.tags.forEach((tag) => {
          const button = node("button", "tag entry-tag-filter", tag);
          button.type = "button";
          button.dataset.filterTag = tag;
          button.title = `筛选标签：${tag}`;
          tags.append(button);
        });
        context.append(tags);
      }
      if (context.childElementCount) card.append(context);
      const footer = node("div", "entry-operations");
      const updated = new Date(entry.updated_at);
      const time = node("time", "", Number.isNaN(updated.getTime()) ? "" : "更新于 " + updated.toLocaleDateString("zh-CN"));
      if (!Number.isNaN(updated.getTime())) time.dateTime = updated.toISOString();
      const created = new Date(entry.created_at);
      const dates = node("div", "entry-dates");
      if (!Number.isNaN(created.getTime())) {
        const createdTime = node("time", "", "创建于 " + created.toLocaleDateString("zh-CN"));
        createdTime.dateTime = created.toISOString();
        dates.append(createdTime);
      }
      dates.append(time);
      const actions = node("div", "entry-actions");
      if (trash) actions.append(actionButton("恢复记录", "restore", entry.id));
      else actions.append(actionButton("编辑", "edit", entry.id), actionButton("删除", "trash", entry.id, "text-button danger"));
      footer.append(dates, actions);
      card.append(footer);
      list.append(card);
    }
  }

  function hidePassword(id) {
    clearTimeout(state.revealTimers.get(id));
    state.revealTimers.delete(id);
    state.revealed.delete(id);
    if (state.status?.unlocked) renderEntries();
  }

  return { renderEntries, hidePassword };
}
