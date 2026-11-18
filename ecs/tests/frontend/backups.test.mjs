import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";
const dataUrl = (source) => "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const ui = await readFile(new URL("../../vault/web/static/js/shared/ui.js", import.meta.url), "utf8");
const source = (await readFile(new URL("../../vault/web/static/js/backups/index.js", import.meta.url), "utf8")).replace('"../shared/ui.js"', JSON.stringify(dataUrl(ui)));
const { createBackups } = await import(dataUrl(source));
class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.textContent = ""; this.listeners = new Map(); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  fire(name, event) { return this.listeners.get(name)?.(event); }
}
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
function fixture(t, api) {
  const previous = globalThis.document;
  const elements = new Map();
  globalThis.document = { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }, createElement(tag) { return new Element(tag); } };

  const state = { status: { unlocked: true }, view: "backups", generation: 0 };
  const notices = [];
  const locks = [];
  const backups = createBackups({ state, api, notify: (...args) => notices.push(args), busy: (_, action) => action(), lock: (message) => locks.push(message) });
  t.after(() => { backups.clear(); globalThis.document = previous; });
  return { state, backups, notices, locks, get: (id) => document.getElementById(id) };
}
const ready = { id: "synthetic", status: "ready", filename: "vault.dump", size_bytes: 600 * 1024 ** 2, sha256: "a".repeat(64), created_at: "2026-01-01T00:00:00Z", expires_at: "2026-01-08T00:00:00Z" };
test("backup page offers native authenticated attachment links and does not claim success", async (t) => {
  const calls = [];
  const f = fixture(t, async (path) => { calls.push(path); return { tasks: [ready], limits: {} }; });
  await f.backups.load();
  const links = descendants(f.get("backup-list")).filter((element) => element.tag === "a");
  assert.deepEqual(links.map((link) => link.href), ["/api/backups/synthetic/download", "/api/backups/synthetic/checksum", "/api/backups/synthetic/manifest"]);
  assert.ok(links.every((link) => link.download === ""));
  links[0].fire("click", { preventDefault() { assert.fail("logged in"); } });
  assert.match(f.notices.at(-1)[0], /不能确认文件已保存或恢复成功/);
  assert.deepEqual(calls, ["/api/backups"]);
  f.state.status.unlocked = false;
  let prevented = false;
  links[0].fire("click", { preventDefault() { prevented = true; } });
  assert.ok(prevented);
});
test("locking clears tasks and rejects a late task response", async (t) => {
  let finish;
  const f = fixture(t, () => new Promise((resolve) => { finish = resolve; }));
  const pending = f.backups.load();
  f.state.generation++; f.state.status.unlocked = false; f.backups.clear();
  finish({ tasks: [ready] });
  await pending;
  assert.equal(f.get("backup-list").children.length, 0);
});
test("a new login can load tasks while an older cancelled request is still settling", async (t) => {
  const resolvers = [];
  const f = fixture(t, () => new Promise((resolve) => resolvers.push(resolve)));
  const oldRequest = f.backups.load();
  f.state.generation++; f.backups.clear();
  const freshRequest = f.backups.load();
  assert.equal(resolvers.length, 2);
  resolvers[1]({ tasks: [ready] }); await freshRequest;
  resolvers[0]({ tasks: [{ ...ready, filename: "stale.dump" }] }); await oldRequest;
  const text = descendants(f.get("backup-list")).map((element) => element.textContent).join(" ");
  assert.match(text, /vault.dump/);
  assert.doesNotMatch(text, /stale.dump/);
});

test("restore requires file and confirmation, sends raw file, and locks on success", async (t) => {
  const calls = [];
  const f = fixture(t, async (...args) => { calls.push(args); return { ok: true }; });
  const file = { name: "backup.dump", size: 300000 };
  f.get("restore-file").files = [file];
  const submit = () => f.get("restore-form").fire("submit", { preventDefault() {} });
  await submit();
  assert.equal(calls.length, 0);
  f.get("restore-confirm").checked = true;
  await submit();
  assert.equal(calls[0][0], "/api/backups/restore");
  assert.equal(calls[0][2], file);
  assert.equal(calls[0][3].upload, true);
  assert.match(f.locks[0], /全量恢复成功/);
  assert.equal(f.state.restoring, false);
  f.backups.clear();
  assert.equal(f.get("restore-file").value, "");
  assert.equal(f.get("restore-confirm").checked, false);
});

test("restore failure and late response never claim successful recovery", async (t) => {
  let finish;
  const f = fixture(t, () => new Promise((resolve) => { finish = resolve; }));
  f.get("restore-file").files = [{ name: "backup.dump", size: 100 }];
  f.get("restore-confirm").checked = true;
  const pending = f.get("restore-form").fire("submit", { preventDefault() {} });
  f.state.generation++; f.backups.clear();
  finish({ ok: true }); await pending;
  assert.deepEqual(f.locks, []);
});
