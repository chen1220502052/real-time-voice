// Deterministic AudioContext-clock tests, not a physical speaker benchmark.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.env.PLAYBACK_TEST_SOURCE || "web/app.js", "utf8");
const code = source.slice(source.indexOf("const playbackTracks ="), source.indexOf("function scheduleMouth("));
const sent = [], timers = [];
const context = vm.createContext({
  audioContext: {currentTime: 0, state: "running"}, FRAME_SECONDS: .1,
  scheduledSources: new Set(), completionTimers: new Set(), interruptedReplyIds: new Set(),
  playbackFrames: [], playbackStarted: false, nextPlaybackTime: 0, activePlaybackReply: "r1",
  conversationRecorder: null, latencyTrace: {stop(){}}, outputObserver: null,
  emitClientEvent: (type, component, data) => sent.push({type, ...data}),
  clearMouthSchedule() {}, updatePlaybackBuffer() {}, setMouth() {},
  setTimeout: (fn) => {timers.push(fn); return timers.length;}, clearTimeout() {},
});
vm.runInContext(code, context);
function run(code) { return vm.runInContext(code, context); }

// A future scheduled frame is not yet heard; underrun gaps don't count.
run('playbackTracks.set("r1", {frames: [1, 1.1, 2]}); audioContext.currentTime=1.05;');
assert.ok(run('playedPosition("r1")') >= 49 && run('playedPosition("r1")') <= 50);
run('audioContext.currentTime=1.5;');
assert.ok(run('playedPosition("r1")') >= 199 && run('playedPosition("r1")') <= 200);
run('reportPlaybackProgress();');
assert.equal(sent.at(-1).type, "playback_progress");

// Stop samples the clock before sources stop, then freezes this reply.
run('resetPlayback("barge_in");');
assert.equal(sent.at(-1).type, "playback_stopped");
const frozen = sent.at(-1).attributes.playedMs;
run('audioContext.currentTime=10;');
assert.equal(run('playedPosition("r1")'), frozen);

// Wall time passing while AudioContext is suspended cannot confirm completion.
sent.length = 0;
run('audioContext.currentTime=1; audioContext.state="suspended"; finishPlayback({replyId:"r2"}, 1.5);');
assert.equal(sent.length, 0);
run('audioContext.currentTime=1.5; audioContext.state="running";');
timers.at(-1)();
assert.equal(sent.at(-1).type, "playback_completed");

// A stale completion timer never promotes an interrupted reply.
sent.length = 0;
run('interruptedReplyIds.add("r3"); finishPlayback({replyId:"r3"}, 0);');
assert.equal(sent.length, 0);
// Targeted receipt is required even before playback or while a NEW reply is active.
sent.length = 0;
const stopped = [];
context.scheduledSources.add({replyId:'old', stop(){stopped.push('old');}});
context.scheduledSources.add({replyId:'new', stop(){stopped.push('new');}});
run('activePlaybackReply="new"; reportPlaybackStopped("old", "barge_in");');
assert.deepEqual(stopped, ['old']);
assert.equal(context.scheduledSources.size, 1);
sent.length = 0;
run('activePlaybackReply="new"; reportPlaybackStopped("never_started", "barge_in"); reportPlaybackStopped("never_started", "barge_in");');
assert.equal(sent.length, 1);
assert.equal(sent[0].replyId, 'never_started');
assert.equal(sent[0].attributes.playedMs, 0);
assert.equal(run('activePlaybackReply'), 'new');
run('finishPlayback({replyId:"never_started"}, 0);');
assert.equal(sent.length, 1); // Late completion cannot undo a stop.
sent.length = 0;
run('playbackTracks.set("finished", {frames:[]}); finishPlayback({replyId:"finished"}, 0); finishPlayback({replyId:"finished"}, 0); reportPlaybackStopped("finished", "late_interrupt");');
assert.equal(sent.length, 1);
assert.equal(sent[0].type, 'playback_completed');
console.log("Frontend playback clock checks passed (9 scenarios).");
