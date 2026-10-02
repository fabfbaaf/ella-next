// Runs the actual component's async start / pause / resume / toggle handlers
// against simulated tracks, contexts, sockets, and timers. No real devices or
// services are opened. Run: node scripts/test-voice-notification-lease.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(new URL("../apps/desktop/package.json", import.meta.url));
const ts = require("typescript");
const importSource = async (source) => {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
};
const helper = await importSource(readFileSync(new URL("../apps/desktop/src/features/pet/voice-notification-lease.ts", import.meta.url), "utf8"));
const componentSource = readFileSync(new URL("../apps/desktop/src/features/pet/StreamingVoiceControl.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("StreamingVoiceControl.tsx", componentSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declarations = new Map();
let toggleAssignment;
const walk = (node) => {
  if (ts.isVariableStatement(node)) {
    for (const declaration of node.declarationList.declarations) {
      if (ts.isIdentifier(declaration.name)) declarations.set(declaration.name.text, node.getText(ast));
    }
  }
  if (ts.isExpressionStatement(node) && ts.isBinaryExpression(node.expression)
    && node.expression.left.getText(ast) === "toggleRef.current") toggleAssignment = node.getText(ast);
  ts.forEachChild(node, walk);
};
walk(ast);
const handlerNames = ["publishVoiceStatus", "clearResume", "scheduleResume", "stopCapture", "stopAll", "start", "publish", "pause", "resume"];
for (const name of handlerNames) assert.equal(declarations.has(name), true, `Missing component handler: ${name}`);
assert.equal(typeof toggleAssignment, "string");
const factory = await importSource(`export function createHandlers(scope) {
  const {
    mounted, availableRef, phaseRef, generation, continuous, paused, starting,
    notificationLease, resumeTimer, retryAttempt, publishedStatus, socket, closingSockets,
    microphone, audioContext, worklet, playback, playbackUrl, queue, done,
    playingText, echoReference, echoCancellation, preRoll, interrupting, vad,
    startRef, toggleRef, bargeIn, window, navigator, WebSocket, AudioContext,
    AudioWorkletNode, CustomEvent, emit, invoke, runtimeVoiceSocket, finish,
    playNext, setMessage, updatePhase, closeVoiceSocket, mayGrantNotificationLease,
    stopMouth,
  } = scope;
  ${handlerNames.map((name) => declarations.get(name)).join("\n")}
  startRef.current = start;
  ${toggleAssignment}
  return { start, pause, resume, stopAll, scheduleResume, toggle: () => toggleRef.current() };
}`);

const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
class FakeSocket extends EventTarget {
  readyState = 0;
  closeCalls = 0;
  send() {}
  open() { this.readyState = 1; this.dispatchEvent(new Event("open")); }
  close() { this.closeCalls += 1; this.readyState = 2; }
  completeClose() { this.readyState = 3; this.dispatchEvent(new Event("close")); }
}
class DetailEvent extends Event {
  constructor(type, options = {}) { super(type); this.detail = options.detail; }
}
const ref = (current) => ({ current });
const media = () => {
  const track = { stopped: 0, stop() { this.stopped += 1; }, getSettings: () => ({ echoCancellation: true }) };
  return { track, stream: { getTracks: () => [track], getAudioTracks: () => [track] } };
};
const context = (closed = Promise.resolve()) => ({
  state: "running", closeCalls: 0,
  close() { this.closeCalls += 1; return closed.then(() => { this.state = "closed"; }); },
  audioWorklet: { addModule: async () => {} },
  createMediaStreamSource: () => ({ connect() {} }),
  destination: {},
});
const fixture = (options = {}) => {
  const events = [];
  const timers = new Map();
  const messages = [];
  const calls = { socket: 0, microphone: 0, admin: 0 };
  const window = new EventTarget();
  window.dispatchEvent = (event) => { events.push(event); return EventTarget.prototype.dispatchEvent.call(window, event); };
  window.setTimeout = (callback, delay) => { const id = timers.size + 1; timers.set(id, { callback, delay }); return id; };
  window.clearTimeout = (id) => timers.delete(id);
  window.open = () => { calls.admin += 1; };
  window.AudioWorkletNode = true;
  const scope = {
    mounted: ref(true), availableRef: ref(true), phaseRef: ref("idle"), generation: ref(0),
    continuous: ref(true), paused: ref(false), starting: ref(false), notificationLease: ref(null),
    resumeTimer: ref(null), retryAttempt: ref(0), publishedStatus: ref({ available: true, phase: "idle", message: "fixture" }),
    socket: ref(null), closingSockets: ref(new Set()), microphone: ref(null), audioContext: ref(null), worklet: ref(null),
    stopMouth: ref(() => {}), playback: ref(null), playbackUrl: ref(null), queue: ref([]), done: ref(false), playingText: ref(""),
    echoReference: ref(""), echoCancellation: ref(false), preRoll: ref([]), interrupting: ref(false),
    vad: ref({ speechStarted: false, voicedMs: 0 }), startRef: ref(() => {}), toggleRef: ref(() => {}), bargeIn: ref(false),
    window, CustomEvent: DetailEvent, WebSocket: { OPEN: 1 },
    navigator: { mediaDevices: { getUserMedia: async () => { calls.microphone += 1; throw new Error("Unexpected microphone acquisition"); } } },
    AudioContext: class { constructor() { return context(); } },
    AudioWorkletNode: class { port = { onmessage: null }; connect() {} disconnect() {} },
    emit: async () => {}, invoke: async () => { calls.admin += 1; },
    runtimeVoiceSocket: async () => { calls.socket += 1; throw new Error("Simulated transport failure"); },
    finish: async () => {}, playNext: async () => {}, setMessage: (message) => messages.push(message),
    closeVoiceSocket: helper.closeVoiceSocket, mayGrantNotificationLease: helper.mayGrantNotificationLease,
  };
  Object.assign(scope, options);
  scope.updatePhase = (phase) => { scope.phaseRef.current = phase; };
  const handlers = factory.createHandlers(scope);
  const grants = () => events.filter((event) => event.type === "ella-notification-granted").map((event) => event.detail);
  const request = (requestId) => new DetailEvent("request", { detail: { requestId } });
  return { scope, handlers, calls, events, timers, messages, grants, request };
};

{
  const pendingClose = deferred();
  const oldSocket = new FakeSocket(); oldSocket.open();
  const oldMedia = media();
  const f = fixture({ socket: ref(oldSocket), microphone: ref(oldMedia.stream), audioContext: ref(context(pendingClose.promise)) });
  const starting = f.handlers.start();
  assert.equal(f.scope.starting.current, true);
  f.handlers.pause(f.request("during-start"));
  assert.deepEqual(f.grants(), [{ requestId: "during-start", granted: false }]);
  pendingClose.resolve(); oldSocket.completeClose();
  await starting;
  assert.equal(f.scope.starting.current, false);
  assert.equal(oldMedia.track.stopped, 1);
  assert.equal(f.calls.microphone, 0);
}

{
  const oldSocket = new FakeSocket(); oldSocket.open();
  const f = fixture({ socket: ref(oldSocket) });
  const pending = f.handlers.start();
  f.scope.notificationLease.current = "late-lease";
  oldSocket.completeClose();
  await pending;
  assert.equal(f.calls.socket, 0, "An acquired lease after await stopAll must block reopening");
  assert.equal(f.scope.starting.current, false);
}

{
  const oldSocket = new FakeSocket(); oldSocket.open();
  const f = fixture({ socket: ref(oldSocket) });
  const first = f.handlers.start();
  await f.handlers.start();
  assert.equal(oldSocket.closeCalls, 1, "A second start must not interrupt the in-flight start");
  f.scope.paused.current = true;
  oldSocket.completeClose();
  await first;
  assert.equal(f.calls.socket, 0);
  assert.equal(f.scope.starting.current, false);
}

{
  const capture = media();
  const f = fixture({ phaseRef: ref("recording"), continuous: ref(false), microphone: ref(capture.stream) });
  f.handlers.pause(f.request("manual"));
  assert.deepEqual(f.grants(), [{ requestId: "manual", granted: false }]);
  assert.equal(capture.track.stopped, 0, "Manual recording is never taken away for a notification");
}

{
  const capture = media();
  const f = fixture({ phaseRef: ref("recording"), microphone: ref(capture.stream), vad: ref({ speechStarted: false, voicedMs: 80 }) });
  f.handlers.pause(f.request("speech-onset"));
  assert.deepEqual(f.grants(), [{ requestId: "speech-onset", granted: false }]);
  assert.equal(capture.track.stopped, 0, "The first voiced chunk reserves the microphone");
}

{
  const closeContext = deferred();
  const oldSocket = new FakeSocket(); oldSocket.open();
  const capture = media();
  const f = fixture({ phaseRef: ref("recording"), socket: ref(oldSocket), microphone: ref(capture.stream), audioContext: ref(context(closeContext.promise)) });
  f.handlers.pause(f.request("silent"));
  assert.equal(capture.track.stopped, 1);
  assert.equal(oldSocket.readyState, 2);
  assert.deepEqual(f.grants(), []);
  oldSocket.completeClose();
  await flush();
  assert.deepEqual(f.grants(), [], "A closed socket alone is insufficient while capture is still closing");
  closeContext.resolve();
  await flush();
  assert.deepEqual(f.grants(), [{ requestId: "silent", granted: true }]);
  f.handlers.resume(f.request("silent"));
  assert.equal(f.scope.notificationLease.current, null);
  assert.equal(f.timers.size, 1);
}

{
  const closeContext = deferred();
  const f = fixture({ audioContext: ref(context(closeContext.promise)) });
  f.handlers.pause(f.request("canceled"));
  f.handlers.resume(f.request("canceled"));
  closeContext.resolve();
  await flush();
  assert.equal(f.grants().some((grant) => grant.granted), false, "An expired request cannot receive a late lease");
  assert.equal(f.scope.notificationLease.current, null);
}

{
  const f = fixture({ availableRef: ref(false), notificationLease: ref("no-asr") });
  f.handlers.toggle();
  assert.equal(f.scope.paused.current, true);
  assert.equal(f.scope.notificationLease.current, null);
  assert.equal(f.events.some((event) => event.type === "ella-notification-interrupt"), true);
  assert.equal(f.events.filter((event) => event.type === "ella-voice-status").at(-1).detail.paused, true);
  f.handlers.pause(f.request("automatic-replay"));
  assert.equal(f.grants().at(-1).granted, false);
  f.handlers.toggle();
  assert.equal(f.scope.paused.current, false, "A deliberate second interaction can restore notifications");
  assert.equal(f.calls.admin, 1);
}

{
  const socket = new FakeSocket(); socket.open();
  assert.equal(await helper.closeVoiceSocket(socket, 5), false, "Close waiting must be bounded");
  socket.completeClose();
  assert.equal(await helper.closeVoiceSocket(socket, 5), true);
  assert.equal(socket.closeCalls, 1);
}

{
  const getMedia = deferred();
  const newSocket = new FakeSocket();
  const capture = media();
  const f = fixture({
    runtimeVoiceSocket: async () => newSocket,
    navigator: { mediaDevices: { getUserMedia: () => getMedia.promise } },
  });
  const starting = f.handlers.start();
  await flush();
  newSocket.open();
  await flush();
  assert.equal(f.scope.starting.current, true);
  f.handlers.pause(f.request("permission-pending"));
  assert.equal(f.grants().at(-1).granted, false, "A pending microphone acquisition cannot coexist with notification synthesis");
  getMedia.resolve(capture.stream);
  await starting;
  assert.equal(f.scope.starting.current, false);
  assert.equal(f.scope.phaseRef.current, "recording");
  const stopping = f.handlers.stopAll();
  newSocket.completeClose();
  await stopping;
  assert.equal(capture.track.stopped, 1);
}

{
  const f = fixture();
  f.scope.starting.current = true;
  f.handlers.scheduleResume();
  assert.equal(f.timers.size, 0, "Automatic recovery must not interrupt a start in progress");
  f.scope.starting.current = false;
  await f.handlers.start();
  assert.equal(f.scope.starting.current, false, "A rejected transport must release its startup reservation");
  assert.equal(f.timers.size, 1, "Recovery is scheduled only after the reservation is released");
}

{
  const stuck = new FakeSocket(); stuck.open();
  const f = fixture({ socket: ref(stuck), closeVoiceSocket: (socket) => helper.closeVoiceSocket(socket, 5) });
  f.handlers.pause(f.request("stuck-first"));
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.deepEqual(f.grants(), [{ requestId: "stuck-first", granted: false }]);
  assert.equal(f.scope.closingSockets.current.has(stuck), true);
  f.handlers.pause(f.request("stuck-again"));
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(f.grants().at(-1).granted, false, "A timeout cannot make the next request assume the old socket is closed");
  stuck.completeClose();
  f.handlers.pause(f.request("finally-closed"));
  await flush();
  assert.equal(f.grants().at(-1).granted, true);
  assert.equal(f.scope.closingSockets.current.size, 0);
  f.handlers.resume(f.request("finally-closed"));
}

{
  const getMedia = deferred();
  const newSocket = new FakeSocket();
  const capture = media();
  const f = fixture({ runtimeVoiceSocket: async () => newSocket, navigator: { mediaDevices: { getUserMedia: () => getMedia.promise } } });
  const starting = f.handlers.start(); await flush(); newSocket.open(); await flush(); getMedia.resolve(capture.stream); await starting;
  newSocket.onmessage({ data: JSON.stringify({ type: "listening_pause" }) }); await flush();
  assert.equal(f.scope.paused.current, true, "An explicit spoken pause prevents automatic listening recovery");
  assert.equal(capture.track.stopped, 1, "Spoken pause releases the microphone immediately");
  f.handlers.scheduleResume(400);
  assert.equal(f.scope.resumeTimer.current, null);
  const stopping = f.handlers.stopAll(); newSocket.completeClose(); await stopping;
}
{
  const oldSocket = new FakeSocket(); oldSocket.open(); oldSocket.send = () => { throw new Error("raced close"); };
  const capture = media();
  const f = fixture({ socket: ref(oldSocket), microphone: ref(capture.stream), closeVoiceSocket: async () => true });
  await f.handlers.stopAll();
  assert.equal(capture.track.stopped, 1, "A send exception cannot keep the microphone live");
}
{
  const capture = media();
  const f = fixture({ microphone: ref(capture.stream), worklet: ref({ disconnect: () => { throw new Error("closed graph"); } }) });
  await f.handlers.stopAll();
  assert.equal(capture.track.stopped, 1, "A graph exception cannot keep the microphone live");
}
console.log("Voice / notification lease: 15 simulated scenarios passed.");
