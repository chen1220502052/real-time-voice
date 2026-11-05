const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync('web/playback-start-guard.js','utf8').replace('export class','class'),context);
const guard=vm.runInContext('new PlaybackStartGuard()',context);
// Ordinary endpointing finishes after the acoustic guard decays: no fixed tax.
guard.activity(.05,1);guard.activity(.05,1.02);
assert.equal(guard.shouldHold(1.4,'normal'),false);
// A 400 ms clause pause followed by resumed speech is caught BEFORE the
// next 100 ms upload packet reaches the server.
guard.activity(.05,1.42);guard.activity(.05,1.44);
assert.equal(guard.shouldHold(1.45,'resumed'),true);
guard.activity(.05,1.46);assert.equal(guard.shouldHold(1.47,'resumed'),true);
// A one-window click cannot start a hold. A short false candidate releases
// naturally, retaining the same queued PCM for playback.
guard.reset();guard.activity(.1,2);
assert.equal(guard.shouldHold(2.01,'click'),false);
guard.activity(.1,2.02);assert.equal(guard.shouldHold(2.03,'noise'),true);
guard.activity(0,2.04);assert.equal(guard.shouldHold(2.25,'noise'),false);
// Continuous room noise cannot suppress a reply forever.
guard.reset();guard.activity(.1,3);guard.activity(.1,3.02);
assert.equal(guard.shouldHold(3.03,'loud'),true);
for(let t=3.04;t<4;t+=.02)guard.activity(.1,t);
assert.equal(guard.shouldHold(4,'loud'),false);
// Holding is independent per reply and reset between calls.
assert.equal(guard.shouldHold(4,'next'),true);guard.reset();
assert.equal(guard.shouldHold(4,'next'),false);
console.log('Playback start guard: resumed speech, ordinary latency, click rejection, noise bound and reset passed.');

// Exercise the actual media drain: a hold retains every frame and its force
// flag; it does not mark playback started or acknowledge unrendered audio.
const app=fs.readFileSync('web/app.js','utf8');
const drainCode=app.slice(app.indexOf('function playbackStartLead('),app.indexOf('function playAudio('));
const timers=[],scheduled=[];
const media={audioContext:{state:'running',currentTime:5,baseLatency:0},
  firstPlaybackGuard:guard,firstPlaybackGuardTimer:undefined,playbackStarted:false,
  playbackFrames:[{replyId:'tail',pcm:'all original bytes'}],PLAYBACK_PREROLL_FRAMES:3,
  nextPlaybackTime:0,FRAME_SECONDS:.1,interruptedReplyIds:new Set(),
  setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},
  emitClientEvent(){},updatePlaybackBuffer(){},scheduleAudioFrame(f,t){scheduled.push([f,t]);}};
guard.activity(.05,5);guard.activity(.05,5.02);
const mediaContext=vm.createContext(media);vm.runInContext(drainCode,mediaContext);
vm.runInContext('drainPlayback(true)',mediaContext);
assert.equal(scheduled.length,0);assert.equal(media.playbackFrames.length,1);
assert.equal(media.playbackStarted,false);
media.audioContext.currentTime=5.3;timers.shift()();
assert.equal(scheduled.length,1);assert.equal(scheduled[0][0].pcm,'all original bytes');
assert.equal(media.playbackFrames.length,0);assert.equal(media.playbackStarted,true);
