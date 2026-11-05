const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const code=source.slice(source.indexOf('function playbackStartLead('),source.indexOf('function playAudio('));
function fixture(baseLatency=0){
  const scheduled=[];
  const state={audioContext:{state:'running',currentTime:1,baseLatency},playbackStarted:false,
    playbackFrames:[],PLAYBACK_PREROLL_FRAMES:3,nextPlaybackTime:0,FRAME_SECONDS:.1,
    interruptedReplyIds:new Set(),scheduleAudioFrame:(frame,time)=>scheduled.push({frame,time}),
    firstPlaybackGuard:{shouldHold:()=>false},firstPlaybackGuardTimer:undefined,clearTimeout(){},
    updatePlaybackBuffer(){}};
  const context=vm.createContext(state);vm.runInContext(code,context);
  return {state,scheduled,drain:force=>vm.runInContext(`drainPlayback(${Boolean(force)})`,context)};
}
// Retain the existing jitter prebuffer; once ready, use a shorter scheduling
// lead without gaps or overlaps between the 100 ms audio frames.
let f=fixture();f.state.playbackFrames.push({replyId:'r'},{replyId:'r'});f.drain();
assert.equal(f.scheduled.length,0);
f.state.playbackFrames.push({replyId:'r'});f.drain();
assert.equal(f.scheduled.length,3);assert.ok(Math.abs(f.scheduled[0].time-1.025)<1e-9);
assert.ok(Math.abs(f.scheduled[2].time-f.scheduled[0].time-.2)<1e-9);
// Devices with larger render blocks retain an explicit block margin.
f=fixture(.05);f.state.playbackFrames.push({replyId:'r'});f.drain(true);
assert.ok(f.scheduled[0].time>=1.05+.012-1e-9);
// Forced short tails still play, while interrupted replies never start.
f=fixture();f.state.interruptedReplyIds.add('old');
f.state.playbackFrames.push({replyId:'old'},{replyId:'new'});f.drain(true);
assert.equal(f.scheduled.length,1);assert.equal(f.scheduled[0].frame.replyId,'new');
assert.ok(f.scheduled[0].time>f.state.audioContext.currentTime);
console.log('Playback startup, jitter prebuffer, device lead and interruption passed.');

// Two underruns in one reply must both restart on the audio clock.
const receiveCode=source.slice(source.indexOf('function playAudio('),source.indexOf('function flushPendingAudio('));
const scheduledTimes=[], warnings=[];
const receive=vm.createContext({
  audioContext:{state:'running',currentTime:2},sessionHash:1n,
  playbackStarted:true,nextPlaybackTime:1,underrunReplies:new Set(),
  playbackFrames:[],avatarFrames:new Map(),interruptedReplyIds:new Set(),
  decodeAudioFrame:()=>({replyId:'r'}),latencyTrace:{receive(){}},
  playbackStartLead:()=>.025,emitClientEvent:(name)=>warnings.push(name),
  drainPlayback(){
    scheduledTimes.push(receive.nextPlaybackTime);
    receive.nextPlaybackTime+=.1;
    receive.playbackFrames.shift();
  }
});
vm.runInContext(receiveCode+'\nplayAudio(null,0);',receive);
receive.audioContext.currentTime=3;
vm.runInContext('playAudio(null,0);',receive);
assert.deepEqual(scheduledTimes,[2.025,3.025]);
assert.deepEqual(warnings,['playback_underrun']);
