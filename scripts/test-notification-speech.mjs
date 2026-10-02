// Run with: node scripts/test-notification-speech.mjs
// No microphone, browser, runtime, or external TTS service is used.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../apps/desktop/package.json", import.meta.url));
const ts = require("typescript");
const source = readFileSync(new URL("../apps/desktop/src/features/pet/notification-speech.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  fileName: fileURLToPath(new URL("../apps/desktop/src/features/pet/notification-speech.ts", import.meta.url)),
});
const { NotificationSpeechDeferredError, NotificationSpeechQueue } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const note = (id, extra = {}) => ({ id, type: "reminder", text: "喝点水", delivered_at: "2026-10-01T04:00:00+00:00", read_at: null, ...extra });
const flush = () => new Promise((resolve) => setImmediate(resolve));
const fixture = (overrides = {}, completed = []) => {
  const calls = { acquire: [], release: [], synthesize: [], play: [], report: [], remembered: [] };
  const dependencies = {
    allowed: () => true,
    acquire: async (id) => { calls.acquire.push(id); return true; },
    release: (id) => { calls.release.push(id); },
    synthesize: async (item) => { calls.synthesize.push(item.id); return { audio_base64: "AAAA", media_type: "audio/wav" }; },
    play: async (audio) => { calls.play.push(audio); },
    report: async (...args) => { calls.report.push(args); },
    rememberCompleted: (ids) => { calls.remembered.push([...ids]); },
    ...overrides,
  };
  return { queue: new NotificationSpeechQueue(dependencies, completed), calls, dependencies };
};

{
  const { queue, calls } = fixture({ allowed: () => false });
  queue.update([note("busy")]);
  await queue.pump();
  assert.equal(calls.acquire.length, 0);
  assert.equal(calls.synthesize.length, 0);
  queue.dispose();
}

{
  let grant = false;
  const { queue, calls } = fixture({ acquire: async () => grant });
  queue.update([note("lease")]);
  await queue.pump();
  assert.equal(calls.synthesize.length, 0);
  assert.equal(calls.release.length, 1);
  grant = true;
  await queue.pump();
  assert.deepEqual(calls.synthesize, ["lease"]);
  assert.equal(calls.release.length, 2);
  queue.dispose();
}

{
  let finishPlayback;
  const { queue, calls } = fixture({ play: () => new Promise((resolve) => { finishPlayback = resolve; }) });
  queue.update([note("once")]);
  const first = queue.pump();
  await flush();
  const duplicate = queue.pump();
  assert.equal(first, duplicate);
  queue.update([note("once"), note("once")]);
  assert.equal(calls.report.length, 0, "Audio queued/started is not yet heard");
  finishPlayback();
  await first;
  await queue.pump();
  assert.deepEqual(calls.synthesize, ["once"]);
  assert.equal(calls.report.filter(([, status]) => status === "played").length, 1);
  assert.deepEqual(calls.remembered.at(-1), ["once"]);
  queue.dispose();
}

{
  let finishPlayback;
  let firstPlayback = true;
  const { queue, calls } = fixture({
    play: (_audio, signal) => firstPlayback
      ? new Promise((resolve, reject) => {
        finishPlayback = resolve;
        signal.addEventListener("abort", () => reject(new Error("stopped")), { once: true });
      }) : Promise.resolve(),
  });
  queue.update([note("interrupted")]);
  const pending = queue.pump();
  await flush();
  assert.equal(typeof finishPlayback, "function");
  queue.interrupt();
  await pending;
  assert.equal(calls.report.length, 0, "Stopping playback must not claim delivery");
  assert.equal(calls.release.length, 1);
  firstPlayback = false;
  await queue.pump();
  assert.equal(calls.synthesize.length, 1, "Resume reuses prepared audio without another TTS request");
  assert.equal(calls.report[0][1], "played");
  queue.dispose();
}

{
  const { queue, calls } = fixture({ play: async () => { throw new Error("speaker unavailable"); } });
  queue.update([note("failed")]);
  await queue.pump();
  assert.deepEqual(calls.report, [["failed", "failed", "speaker unavailable"]]);
  queue.update([note("failed")]);
  await queue.pump();
  assert.equal(calls.synthesize.length, 1, "A failure stays in history without an automatic retry loop");
  queue.dispose();
}

{
  let reports = 0;
  const { queue, calls } = fixture({ report: async () => { reports += 1; throw new Error("offline"); } });
  queue.update([note("ack-offline")]);
  await queue.pump();
  await flush();
  assert.deepEqual(calls.remembered.at(-1), ["ack-offline"]);
  queue.update([note("ack-offline")]);
  await queue.pump();
  assert.equal(calls.synthesize.length, 1);
  assert.equal(reports, 2, "Only the acknowledgement retries");
  const restart = fixture({}, calls.remembered.at(-1));
  restart.queue.update([note("ack-offline")]);
  await restart.queue.pump();
  assert.equal(restart.calls.synthesize.length, 0, "A restart must not replay heard audio");
  assert.deepEqual(restart.calls.report, [["ack-offline", "played"]]);
  queue.dispose();
  restart.queue.dispose();
}

{
  const { queue, calls } = fixture();
  queue.update([
    note("read", { read_at: "2026-10-01T05:00:00+00:00" }),
    note("heard", { spoken_at: "2026-10-01T05:00:00+00:00" }),
    note("previous-error", { speech_error: "TTS was unavailable" }),
    note("unknown", { type: "unknown" }),
    note("newer", { delivered_at: "2026-10-01T06:00:00+00:00" }),
    note("older", { delivered_at: "2026-10-01T05:00:00+00:00" }),
  ]);
  await queue.pump();
  await queue.pump();
  assert.deepEqual(calls.synthesize, ["older", "newer"]);
  queue.dispose();
}

{
  let playbackStarted = false;
  const { queue, calls } = fixture({
    play: (_audio, signal) => new Promise((_resolve, reject) => {
      playbackStarted = true;
      signal.addEventListener("abort", () => reject(new Error("stopped")), { once: true });
    }),
  });
  queue.update([note("read-during-speech")]);
  const pending = queue.pump();
  await flush();
  assert.equal(playbackStarted, true);
  queue.update([note("read-during-speech", { read_at: "2026-10-01T05:00:00+00:00" })]);
  await pending;
  assert.equal(calls.report.length, 0);
  assert.equal(calls.release.length, 1);
  queue.dispose();
}

{
  let acquisitions = 0;
  let queue;
  const context = fixture({
    acquire: async () => {
      acquisitions += 1;
      if (acquisitions === 1) void queue.pump();
      return true;
    },
  });
  queue = context.queue;
  queue.update([note("reentrant")]);
  await queue.pump();
  assert.equal(acquisitions, 1, "Synchronous voice events must not acquire a second lease");
  assert.equal(context.calls.synthesize.length, 1);
  queue.dispose();
}

{
  let configured = false;
  let synthesisAttempts = 0;
  const { queue, calls } = fixture({
    synthesize: async () => {
      synthesisAttempts += 1;
      if (!configured) throw new NotificationSpeechDeferredError("HTTP 409: TTS not configured");
      return { audio_base64: "AAAA", media_type: "audio/wav" };
    },
  });
  queue.update([note("deferred")]);
  await queue.pump();
  assert.equal(calls.report.length, 0, "A temporary 409 must not claim playback or persist a failure");
  assert.equal(calls.release.length, 1);
  configured = true;
  queue.update([note("deferred")]);
  await queue.pump();
  assert.equal(synthesisAttempts, 2);
  assert.deepEqual(calls.report, [["deferred", "played"]]);
  assert.equal(calls.release.length, 2);
  queue.dispose();
}

{
  let synthesisAttempts = 0;
  const { queue, calls } = fixture({
    synthesize: async () => { synthesisAttempts += 1; throw new Error("HTTP 502: TTS failed"); },
  });
  queue.update([note("tts-failed")]);
  await queue.pump();
  queue.update([note("tts-failed")]);
  await queue.pump();
  assert.equal(synthesisAttempts, 1, "A real TTS error must not enter an automatic retry loop");
  assert.deepEqual(calls.report, [["tts-failed", "failed", "HTTP 502: TTS failed"]]);
  queue.dispose();
}

console.log("Notification speech queue: 11 scenarios passed.");
