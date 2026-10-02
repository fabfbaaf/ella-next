// Run actual model form handlers with deferred HTTP responses and simulated
// React state/ref lifecycles. No browser, services, keys, or devices are used.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(new URL("../apps/desktop/package.json", import.meta.url));
const ts = require("typescript");
const source = readFileSync(new URL("../apps/desktop/src/features/admin/ModelStatusPanel.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("ModelStatusPanel.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declarations = new Map();
let initialEffect;
const walk = (node) => {
  if (ts.isVariableStatement(node)) {
    for (const declaration of node.declarationList.declarations) {
      if (ts.isIdentifier(declaration.name)) declarations.set(declaration.name.text, node.getText(ast));
    }
  }
  if (ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)
      && node.expression.expression.getText(ast) === "useEffect"
      && node.getText(ast).includes("void refresh()")) initialEffect = node.getText(ast);
  ts.forEachChild(node, walk);
};
walk(ast);
const names = ["asForm", "refresh", "select", "update", "operate", "probe", "chooseProvider", "chooseModel", "testGeneration"];
for (const name of [...names, "slots", "defaults", "presets", "serviceOrigin"]) assert.equal(declarations.has(name), true, `Missing form declaration ${name}`);
assert.equal(typeof initialEffect, "string");
const { outputText } = ts.transpileModule(`
${["slots", "defaults", "presets", "serviceOrigin"].map((name) => declarations.get(name)).join("\n")}
export function createHandlers(scope) {
  const { status, slot, forms, clearKeys, busyRef, mounted, version, dirty,
    setStatus, setSlot, setForms, setClearKeys, setBusy, setError, setNotice, setModelLists, setTestResult,
    runtimeFetch, useCallback, useEffect } = scope;
  const runtime = "http://127.0.0.1:8766";
  const form = forms[slot], clearKey = clearKeys[slot];
  const savedProvider = status?.[slot];
  const changedHost = Boolean(savedProvider && serviceOrigin(savedProvider.base_url) !== serviceOrigin(form.base_url));
  ${names.map((name) => declarations.get(name)).join("\n")}
  ${initialEffect}
  return { refresh, select, update, save: () => operate("PUT"), reset: () => operate("DELETE"), probe, chooseProvider, chooseModel, testGeneration };
}
export { defaults };`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
});
const factory = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const ref = (current) => ({ current });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = () => new Promise((resolve) => setImmediate(resolve));
const status = (prefix = "saved") => Object.fromEntries([
  ...["chat", "action", "vision", "persona"].map((slot) => [slot, {
    provider: "fixture", model: `${prefix}-${slot}`, base_url: "https://fixture.example/v1",
    source: "saved", key_saved: true, configured: true,
  }]), ["action_uses_chat", false],
]);
const response = (value, ok = true) => ({ ok, json: async () => value });
const fixture = () => {
  const state = {
    status: null, slot: "chat", forms: structuredClone(factory.defaults),
    clearKeys: { chat: false, action: false, vision: false, persona: false },
    busy: false, error: "", notice: "", testResult: null,
    modelLists: { chat: [], action: [], vision: [], persona: [] },
  };
  const requests = [];
  const refs = { busyRef: ref(false), mounted: ref(true), version: ref(0), dirty: ref(new Set()) };
  const render = () => {
    const effects = [];
    const setters = Object.fromEntries(Object.keys(state).map((key) => [
      `set${key[0].toUpperCase()}${key.slice(1)}`,
      (value) => { state[key] = typeof value === "function" ? value(state[key]) : value; },
    ]));
    const handlers = factory.createHandlers({
      ...state, ...refs, ...setters,
      useCallback: (callback) => callback, useEffect: (callback) => effects.push(callback),
      runtimeFetch: (url, init) => {
        const pending = deferred(); requests.push({ url, init, ...pending }); return pending.promise;
      },
    });
    return { ...handlers, mount: () => effects[0]() };
  };
  return { state, requests, refs, render };
};
let passed = 0;
const failures = [];
const scenario = async (name, run) => {
  try { await run(); passed += 1; }
  catch (error) { failures.push(name); console.error(`FAIL ${name}: ${error.message}`); }
};

await scenario("late initial success preserves per-slot edits", async () => {
  const f = fixture(); f.render().mount();
  f.render().update("model", "unsaved-chat");
  f.render().select("vision"); f.render().update("model", "unsaved-vision");
  f.requests[0].resolve(response(status())); await flush();
  assert.equal(f.state.forms.chat.model, "unsaved-chat");
  assert.equal(f.state.forms.vision.model, "unsaved-vision");
  assert.equal(f.state.forms.action.model, "saved-action");
  assert.equal(f.state.slot, "vision");
});

await scenario("save captures its route and blocks synchronous reentry or switching", async () => {
  const f = fixture(); const loading = f.render().refresh();
  f.requests[0].resolve(response(status())); await loading;
  f.render().select("action"); f.render().update("model", "new-action");
  const handler = f.render(); const saving = handler.save();
  handler.select("vision"); handler.update("model", "must-not-replace"); await handler.save(); await handler.probe();
  assert.equal(f.requests.length, 2);
  assert.equal(f.state.slot, "action");
  assert.equal(JSON.parse(f.requests[1].init.body).model, "new-action");
  assert.match(f.requests[1].url, /config\/action$/);
  f.requests[1].resolve(response(status("new"))); await saving;
  assert.equal(f.state.forms.action.model, "new-action");
  assert.equal(f.refs.busyRef.current, false);
  assert.equal(f.refs.dirty.current.has("action"), false);
});

await scenario("saving one route keeps other route drafts and keys", async () => {
  const f = fixture(); const loading = f.render().refresh(); f.requests[0].resolve(response(status())); await loading;
  f.render().select("vision"); f.render().update("model", "vision-draft"); f.render().update("api_key", "fixture-draft-key");
  f.render().select("chat"); f.render().update("model", "chat-new");
  const saving = f.render().save(); f.requests[1].resolve(response(status("chat-new"))); await saving;
  assert.equal(f.state.forms.vision.model, "vision-draft");
  assert.equal(f.state.forms.vision.api_key, "fixture-draft-key");
  assert.equal(f.refs.dirty.current.has("vision"), true);
});

await scenario("failed save retains draft and releases busy reservation", async () => {
  const f = fixture(); const loading = f.render().refresh(); f.requests[0].resolve(response(status())); await loading;
  f.render().update("model", "retry-draft"); const saving = f.render().save();
  f.requests[1].resolve(response({ detail: "模拟保存失败" }, false)); await saving;
  assert.equal(f.state.forms.chat.model, "retry-draft");
  assert.equal(f.refs.busyRef.current, false);
  assert.equal(f.refs.dirty.current.has("chat"), true);
  assert.equal(f.state.error, "模拟保存失败");
});

await scenario("probe is disabled for edited drafts and blocks a second action", async () => {
  const f = fixture(); const loading = f.render().refresh(); f.requests[0].resolve(response(status())); await loading;
  f.render().update("model", "draft"); await f.render().probe(); assert.equal(f.requests.length, 1);
  f.render().select("vision"); const checking = f.render().probe();
  const current = f.render(); await current.save(); current.select("chat");
  assert.equal(f.requests.length, 2); assert.equal(f.state.slot, "vision");
  f.requests[1].resolve(response({ detail: "已保存连接可用" })); await checking;
  assert.equal(f.state.notice, "已保存连接可用"); assert.equal(f.refs.busyRef.current, false);
});

await scenario("unmounted save never overwrites form state", async () => {
  const f = fixture(); const cleanup = f.render().mount(); f.requests[0].resolve(response(status())); await flush();
  f.render().update("model", "saved-draft"); const saving = f.render().save();
  const before = structuredClone(f.state); cleanup();
  f.requests[1].resolve(response(status("late"))); await saving;
  assert.deepEqual(f.state, before); assert.equal(f.refs.busyRef.current, false);
});

await scenario("StrictMode older initial success cannot overwrite the current load", async () => {
  const f = fixture(); const cleanup = f.render().mount(); cleanup(); f.render().mount();
  f.requests[1].resolve(response(status("current"))); await flush();
  f.requests[0].resolve(response(status("obsolete"))); await flush();
  assert.equal(f.state.status.chat.model, "current-chat");
  assert.equal(f.state.forms.chat.model, "current-chat");
});

await scenario("StrictMode older initial failure cannot publish a stale error", async () => {
  const f = fixture(); const cleanup = f.render().mount(); cleanup(); f.render().mount();
  f.requests[1].resolve(response(status("current"))); await flush();
  f.requests[0].reject(new Error("旧请求失败")); await flush();
  assert.equal(f.state.status.chat.model, "current-chat");
  assert.equal(f.state.error, "");
});

await scenario("provider preset replaces mismatched endpoint and clears credential draft", async () => {
  const f = fixture(); const loading = f.render().refresh(); f.requests[0].resolve(response(status())); await loading;
  f.render().update("api_key", "draft-other-host");
  f.render().chooseProvider("gemini");
  assert.equal(f.state.forms.chat.base_url, "https://generativelanguage.googleapis.com/v1beta/openai");
  assert.equal(f.state.forms.chat.api_key, ""); assert.equal(f.state.forms.chat.model, "");
  assert.equal(f.refs.dirty.current.has("chat"), true);
  await f.render().testGeneration("stream"); assert.equal(f.requests.length, 1);
});

await scenario("model list belongs to checked slot and clears on endpoint edits", async () => {
  const f = fixture(); const checking = f.render().probe();
  f.requests[0].resolve(response({ detail: "列表可用", model_ids: ["m1", "m2", null] })); await checking;
  assert.deepEqual(f.state.modelLists.chat, ["m1", "m2"]);
  f.render().update("base_url", "https://other.example/v1");
  assert.deepEqual(f.state.modelLists.chat, []);
});

await scenario("generation diagnostics reserve action and report failed stream", async () => {
  const f = fixture(); const loading = f.render().refresh(); f.requests[0].resolve(response(status())); await loading;
  const checking = f.render().testGeneration("stream");
  await f.render().testGeneration("text"); await f.render().save();
  assert.equal(f.requests.length, 2); assert.match(f.requests[1].url, /chat\/test\?mode=stream$/);
  f.requests[1].resolve(response({ detail: "流式响应提前结束" }, false)); await checking;
  assert.equal(f.state.error, "流式响应提前结束"); assert.equal(f.state.testResult, null);
  assert.equal(f.refs.busyRef.current, false);
});

await scenario("generation result is ignored after unmount", async () => {
  const f = fixture(); const cleanup = f.render().mount(); f.requests[0].resolve(response(status())); await flush();
  const checking = f.render().testGeneration("text"); const before = structuredClone(f.state); cleanup();
  f.requests[1].resolve(response({ preview: "late", latency_ms: 1 })); await checking;
  assert.deepEqual(f.state, before);
});

await scenario("inherited model selection binds the actual chat service", async () => {
  const f = fixture(); const loaded = status(); loaded.action = null; loaded.vision = null;
  loaded.action_uses_chat = true; loaded.vision_uses_chat = true;
  loaded.chat = { ...loaded.chat, provider: "ollama", base_url: "http://localhost:11434/v1", model: "local-chat" };
  const loading = f.render().refresh(); f.requests[0].resolve(response(loaded)); await loading;
  f.render().select("vision"); f.render().chooseModel("local-vision");
  assert.equal(f.state.forms.vision.provider, "ollama");
  assert.equal(f.state.forms.vision.base_url, "http://localhost:11434/v1");
  assert.equal(f.state.forms.vision.model, "local-vision"); assert.equal(f.state.forms.vision.api_key, "");
  const saving = f.render().save();
  assert.equal(JSON.parse(f.requests[1].init.body).provider, "ollama");
  f.requests[1].resolve(response(status("independent"))); await saving;
});

console.log(`Model form async state: ${passed} scenarios passed${failures.length ? `, ${failures.length} failed` : ""}.`);
if (failures.length) process.exitCode = 1;
