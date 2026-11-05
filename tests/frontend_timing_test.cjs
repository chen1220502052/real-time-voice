const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const window={};const context=vm.createContext({window,performance:{now:()=>1000},Date,Blob,
  Int16Array,Float32Array,ArrayBuffer,DataView,Map,Set,Math,Number});
for(const path of ['web/recorder.js','web/latency.js'])vm.runInContext(fs.readFileSync(path,'utf8'),context);
const clock={currentTime:10};const r=new window.ConversationRecorder();r.start(clock,{});
const pcm=new Int16Array(1600).fill(1000);
r.pushMic(pcm,{captureStartCtx:10.2,captureEndCtx:10.3});
r.pushMic(pcm,{captureStartCtx:10.5,captureEndCtx:10.6,dropped:true});
let samples=r.micSamples();assert.equal(samples.length,9600);
assert.equal(samples[3199],0);assert.equal(samples[3200],1000);
assert.equal(samples[5000],0);assert.equal(samples[8000],1000);
assert.equal(r.buildLog([]).warnings.droppedUplinkFrames,1);
assert.equal(r.buildLog([]).physicalOutput,null);
const auditTrace=new window.LatencyTrace();
r.micChunks[0].sequence=0;r.micChunks[1].sequence=1;
auditTrace.capture(r.micChunks[0]);auditTrace.capture(r.micChunks[1]);
auditTrace.receive({replyId:'audit',timing:{processedSequence:1,speechStartMediaMs:0}},1000,11);
auditTrace.graph({replyId:'audit',onsetCtx:12},{},1000);
const audit=r.buildLog([],auditTrace).latency.replies[0].recordingTimelineAudit;
assert.ok(Math.abs(audit.timelineBiasMs-400)<.001);
assert.ok(Math.abs(audit.oldStyleSignalToGraphMs-audit.correctedSignalToGraphMs-400)<.001);
r.pushRobot(pcm,11,'reply');r.pushRobot(pcm,11.1,'reply');
r.stopRobot('reply',11.05);clock.currentTime=12;
samples=r.robotSamples();assert.equal(samples.length,16800);assert.equal(samples[16799],1000);
assert.equal(r.replyScheduled().reply.classification,'scheduled_not_physical_audio');
r.start(clock,{});r.pushMic(pcm,{captureStartCtx:11.95,captureEndCtx:12.05});
assert.equal(r.micSamples().length,800); // A capture frame crossing recording start is cropped.
r.pushMic(pcm);assert.equal(r.buildLog([]).warnings.untimedMicFrames,1);

const t=new window.LatencyTrace();t.ping('p',100);t.pong({pingId:'p',serverReceivedMs:240,serverSentMs:250},200);
assert.equal(t.report().calibration.uncertaintyWidthMs,90);
t.receive({replyId:'r',sequence:0,timing:{asrFinalMs:300,botRequestStartedMs:310,botFirstTextMs:400}},500,1);
let row=t.report().replies[0];assert.equal(row.serverParts.botToFirstTextMs,90);
assert.equal(row.clientParts.plannedToGraphSignalMs,null);
assert.equal(row.clientParts.captureSignalToGraphMs,null);
assert.equal(row.physicalOutput,null);
// Native inference overlaps the endpoint timer. Its full duration is never
// confused with the residual time that delays the final transcript.
t.receive({replyId:'asr-overlap',timing:{localAsr:{requestStartedMs:200,responseReadyMs:675,
  endpointReachedMs:500,workerInferenceMs:470}}},700,1);
let overlap=t.report().replies.find(r=>r.replyId==='asr-overlap').serverParts;
assert.equal(overlap.asrRequestWallMs,475);
assert.equal(overlap.asrOverlappedEndpointMs,300);
assert.equal(overlap.asrWaitAfterEndpointMs,175);
assert.equal(overlap.asrWorkerInferenceMs,470);
t.receive({replyId:'asr-ready',timing:{localAsr:{requestStartedMs:200,responseReadyMs:450,
  endpointReachedMs:500,workerInferenceMs:245}}},700,1);
overlap=t.report().replies.find(r=>r.replyId==='asr-ready').serverParts;
assert.equal(overlap.asrOverlappedEndpointMs,250);
assert.equal(overlap.asrWaitAfterEndpointMs,0);
t.schedule({replyId:'r',pcm},1.06,501);
t.graph({replyId:'r',onsetCtx:1.06},{baseLatency:.01,outputLatency:.02,
  getOutputTimestamp:()=>({contextTime:1,performanceTime:600})},700);
row=t.report().replies[0];assert.ok(Math.abs(row.clientParts.receiveToPlannedSignalMs-60)<.001);
assert.ok(Math.abs(row.deviceOutputEstimateMs-660)<.001);
assert.equal(row.physicalOutput,null);
assert.equal(row.deviceOutputEstimateStatus,'browser_estimate_not_physical_measurement');
t.receive({sequence:1},501,1);assert.equal(t.report().orphanFrames,1);

let Capture;const posted=[];
const captureContext=vm.createContext({sampleRate:48000,currentFrame:0,Float32Array,
  AudioWorkletProcessor:class {constructor(){this.port={postMessage:data=>posted.push(data)};}},
  registerProcessor:(name,klass)=>{Capture=klass;}});
vm.runInContext(fs.readFileSync('web/audio-worklet.js','utf8'),captureContext);
const capture=new Capture();capture.process([[new Float32Array(4800)]]);
captureContext.currentFrame=9600;capture.process([[new Float32Array(4800)]]);
const pcmPackets=posted.filter(message=>message.pcm);
assert.equal(pcmPackets.length,2);assert.equal(pcmPackets[0].captureStartCtx,0);
assert.equal(pcmPackets[1].captureStartCtx,.2);assert.equal(pcmPackets[1].captureEndCtx,.3);
assert.equal(pcmPackets[1].captureSequence,1);
const activities=posted.filter(message=>message.type==='voice_activity');
assert.equal(activities.length,10);assert.equal(activities[0].atCtx,.02);
assert.equal(activities[5].atCtx,.22);assert.equal(activities[0].rms,0);

let Observer;const observed=[];
const outputContext=vm.createContext({sampleRate:16000,currentFrame:0,
  AudioWorkletProcessor:class {constructor(){this.port={postMessage:data=>observed.push(data)};}},
  registerProcessor:(name,klass)=>{Observer=klass;}});
vm.runInContext(fs.readFileSync('web/output-worklet.js','utf8'),outputContext);
const observer=new Observer();observer.port.onmessage({data:{type:'schedule',replyId:'r',startFrame:0,endFrame:128}});
const input=new Float32Array(128);input[64]=.1;const output=new Float32Array(128);
observer.process([[input]],[[output]]);assert.deepEqual(output,input);
assert.equal(observed.length,1);assert.equal(observed[0].onsetCtx,64/16000);
observer.process([[input]],[[output]]);assert.equal(observed.length,1);
observer.port.onmessage({data:{type:'stop_all',atFrame:0}});
observer.port.onmessage({data:{type:'schedule',replyId:'new',startFrame:0,endFrame:128}});
observer.process([[input]],[[output]]);assert.equal(observed.at(-1).replyId,'new');
console.log('Recorder gaps, crop, cancellation, clock domains, null metrics and both worklets passed.');
