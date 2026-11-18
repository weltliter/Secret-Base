import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import test from "node:test";
const readModule = async (path) => import("data:text/javascript;base64," + Buffer.from(await readFile(new URL(path, import.meta.url), "utf8")).toString("base64"));
const { createProtection } = await readModule("../../vault/web/static/js/auth/protection.js");
const { createApi } = await readModule("../../vault/web/static/js/shared/transport.js");
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const response = (payload = {}, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => payload });
class Surface {
  hidden = false;
  listeners = new Map();
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  removeEventListener(name) { this.listeners.delete(name); }
  fire(name, event = {}) { this.listeners.get(name)?.(event); }
}
function fixture() {
  let clock = 100000, authenticated = true, tick, requests = [];
  const doc = new Surface(), win = new Surface(), locks = [], messages = [];
  let channel;
  class Channel { constructor() { channel = this; } postMessage(value) { messages.push(value); } close() {} }
  const p = createProtection({ document: doc, window: win, now: () => clock, BroadcastChannel: Channel,
    isAuthenticated: () => authenticated, lock: (reason) => { locks.push(reason); authenticated = false; },
    api: async (...args) => { requests.push(args); return { idle_remaining_seconds: 300, absolute_remaining_seconds: 3500 }; },
    setInterval: (fn) => { tick = fn; return 1; }, clearInterval() {} });
  return { p, doc, win, locks, messages, requests, channel, tick: () => tick(), advance: (ms) => { clock += ms; }, authenticate: () => { authenticated = true; p.reset(); } };
}
test("only trusted visible user actions report activity; polling never does", async () => {
  const f = fixture();
  f.doc.fire("click", { isTrusted: false });
  f.tick();
  assert.equal(f.requests.length, 0);
  f.doc.fire("input", { isTrusted: true });
  await flush();
  assert.deepEqual(f.requests[0], ["/api/session/activity", "POST", {}]);
  f.doc.fire("scroll", { isTrusted: true });
  await flush();
  assert.equal(f.requests.length, 1);
  f.advance(5000); f.doc.fire("scroll", { isTrusted: true }); await flush();
  assert.equal(f.requests.length, 2);
  f.doc.hidden = true;
  f.advance(5000); f.doc.fire("click", { isTrusted: true }); await flush();
  assert.equal(f.requests.length, 2);
  f.p.dispose();
});
test("hidden and offline lock immediately, broadcast contains no business state", () => {
  const f = fixture();
  f.doc.hidden = true; f.doc.fire("visibilitychange");
  assert.equal(f.locks.length, 1);
  assert.deepEqual(f.messages, [{ type: "lock" }]);
  f.doc.hidden = false; f.doc.fire("visibilitychange");
  assert.equal(f.locks.length, 1);
  f.authenticate(); f.win.fire("offline");
  assert.equal(f.locks.length, 2);
  f.p.dispose();
});
test("received lock does not create a broadcast loop; bfcache and pagehide require login", () => {
  const f = fixture();
  f.channel.onmessage({ data: { type: "lock" } });
  assert.equal(f.locks.length, 1); assert.equal(f.messages.length, 0);
  f.authenticate(); f.win.fire("pageshow", { persisted: true });
  assert.equal(f.locks.length, 2);
  f.authenticate(); f.win.fire("pagehide");
  assert.equal(f.locks.length, 3);
  f.p.dispose();
});
test("deadline expiration and sleep fail closed without automatic activity", () => {
  const f = fixture();
  f.p.sync({ idle_remaining_seconds: 3, absolute_remaining_seconds: 4 });
  for (let i = 0; i < 3; i++) { f.advance(1000); f.tick(); }
  assert.equal(f.locks.length, 1);
  f.authenticate(); f.advance(10000); f.tick();
  assert.equal(f.locks.length, 2);
  assert.equal(f.requests.length, 0);
  f.p.dispose();
});
test("cancellation during CSRF acquisition prevents the pending mutation", async () => {
  const csrf = deferred(), calls = [];
  const api = createApi(() => {}, { fetch: async (path) => { calls.push(path); return csrf.promise; } });
  const result = api("/api/settings/deepseek", "POST", { api_key: "synthetic" });
  api.cancel(); csrf.resolve(response({ token: "test" }));
  await assert.rejects(result, (error) => error.silent === true);
  assert.deepEqual(calls, ["/api/csrf"]);
});
test("late data body and mutation responses are discarded after locking", async () => {
  for (const [path, method, body] of [["/api/entries", "GET"], ["/api/unlock", "POST", { password: "synthetic" }]]) {
    const data = deferred();
    const api = createApi(() => {}, { fetch: async (url) => url === "/api/csrf" ? response({ token: "test" }) : { ok: true, status: 200, json: () => data.promise } });
    const result = api(path, method, body);
    await flush();
    api.cancel(); data.resolve({ entries: [{ username: "synthetic-private" }], recovery_key: "synthetic-recovery" });
    await assert.rejects(result, (error) => error.silent === true);
  }
});
test("new authentication waits for previous keepalive logout", async () => {
  const logout = deferred(), calls = [];
  const api = createApi(() => {}, { fetch: async (path, init) => {
    calls.push([path, init.keepalive]);
    if (path === "/api/csrf") return response({ token: "test" });
    if (path === "/api/lock") return logout.promise;
    return response({ ok: true });
  } });
  const revocation = api.revoke();
  const login = api("/api/unlock", "POST", { password: "synthetic" });
  await flush();
  assert.equal(calls.some(([path]) => path === "/api/unlock"), false);
  assert.ok(calls.some(([path, keepalive]) => path === "/api/lock" && keepalive));
  logout.resolve(response({ ok: true }));
  await revocation; await login;
  assert.equal(calls.at(-1)[0], "/api/unlock");
});
test("two failed health probes confirm connection loss; transient failure does not lock", async () => {
  for (const recovered of [false, true]) {
    let probes = 0, locks = 0;
    const api = createApi(() => { locks += 1; }, {
      fetch: async (path) => { if (path === "/health") { probes++; if (recovered) return response({ ok: true }); } throw new TypeError("offline"); },
      setTimeout: (fn, delay) => delay === 2000 ? setTimeout(fn, 0) : setTimeout(fn, delay),
      clearTimeout
    });
    await assert.rejects(api("/api/entries"));
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(probes, recovered ? 1 : 2);
    assert.equal(locks, recovered ? 0 : 1);
  }
});
test("HTTP errors do not masquerade as network disconnects", async () => {
  const calls = [], locks = [];
  const api = createApi((message) => locks.push(message), { fetch: async (path) => { calls.push(path); return response({ detail: "synthetic denied" }, 423); } });
  await assert.rejects(api("/api/entries"), /synthetic denied/);
  assert.equal(locks.length, 1);
  assert.deepEqual(calls, ["/api/entries"]);
});
