import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";
const source = await readFile(new URL("../../vault/web/static/js/records/filters.js", import.meta.url), "utf8");
const { defaultFilters, selectEntries, localDate } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const entries = [
  { id: "a", title: "OA 平台", service: "oa.example.com", username: "Alice", tags: ["工作", "生产"], created_at: "2026-10-01T16:00:00Z", updated_at: "2026-10-20T00:00:00Z" },
  { id: "b", title: "测试 OA", service: "test.example.com", username: "Bob", tags: ["工作", "测试"], created_at: "2026-10-02T15:59:59Z", updated_at: "2026-10-21T00:00:00Z" },
  { id: "c", title: "个人邮箱", username: "Alice", tags: [], created_at: "2026-10-03T00:00:00Z", updated_at: "2026-10-22T00:00:00Z", password: "should-not-search", notes: "secret-note" },
];
const ids = (changes) => selectEntries(entries, { ...defaultFilters(), ...changes }).visible.map((entry) => entry.id);
test("keyword fields, normalization and AND words; excludes secrets", () => {
  assert.deepEqual(ids({ query: "ＯＡ ALICE" }), ["a"]);
  assert.deepEqual(ids({ query: "Alice", field: "title" }), []);
  assert.deepEqual(ids({ query: "Alice", field: "username" }), ["c", "a"]);
  assert.deepEqual(ids({ query: "should-not-search" }), []);
  assert.deepEqual(ids({ query: "secret-note" }), []);
});
test("exact tags, any/all and untagged", () => {
  assert.deepEqual(ids({ tags: ["生产", "测试"] }), ["b", "a"]);
  assert.deepEqual(ids({ tags: ["工作", "生产"], tagMode: "all" }), ["a"]);
  assert.deepEqual(ids({ tags: ["工"] }), []);
  assert.deepEqual(ids({ tagScope: "untagged" }), ["c"]);
  assert.deepEqual(ids({ tagScope: "tagged" }), ["b", "a"]);
});
test("local calendar bounds inclusive and invalid range", () => {
  process.env.TZ = "Asia/Shanghai";
  assert.equal(localDate(entries[0].created_at), "2026-10-02");
  assert.deepEqual(ids({ from: "2026-10-02", to: "2026-10-02" }), ["b", "a"]);
  assert.deepEqual(ids({ dateField: "updated_at", from: "2026-10-21", to: "2026-10-21" }), ["b"]);
  assert.equal(selectEntries(entries, { ...defaultFilters(), from: "2026-10-03", to: "2026-10-01" }).invalidRange, true);
  assert.deepEqual(ids({ from: "2026-10-03", to: "2026-10-01" }), []);
});
test("combined filters and sorting do not mutate source", () => {
  assert.deepEqual(ids({ query: "oa", tags: ["工作"], to: "2026-10-02", sort: "created_asc" }), ["a", "b"]);
  assert.deepEqual(ids({ sort: "created_desc" }), ["c", "b", "a"]);
  assert.deepEqual(entries.map((entry) => entry.id), ["a", "b", "c"]);
});
