const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const c=vm.createContext({window:{},performance,Date,Blob,TextEncoder,Int16Array,Float32Array,Uint8Array,Uint32Array,ArrayBuffer,DataView});
for(const path of ['web/recorder.js','web/latency.js','web/recording-review.js'])vm.runInContext(fs.readFileSync(path,'utf8'),c);
const {ConversationRecorder:Recorder,RecordingAudio:Audio,RecordingReview:Review,LatencyTrace}=c.window;
test('rendered output and microphone share one timeline and freeze at recording stop',async()=>{
 const clock={currentTime:10},r=new Recorder();r.start(clock,{sessionId:'s'});
 r.pushMic(new Int16Array(1600).fill(1000),{captureStartCtx:10.1,sequence:0});
 r.pushRendered(new Float32Array(4800).fill(.25),10.3,48000,'s');
 r.pushRendered(new Float32Array(4800).fill(1),10.3,48000,'old');
 clock.currentTime=10.35;r.stop();
 // Final worklet buffer is delivered after stop; samples beyond stop are cropped.
 r.pushRendered(new Float32Array(4800).fill(.5),10.4,48000,'s');
 const stereo=r.stereoSamples();assert.equal(stereo.length,11200);
 assert.equal(stereo[1600*2],1000);assert.equal(stereo[1600*2+1],0);
 assert.equal(stereo[4800*2],0);assert.equal(stereo[4800*2+1],8192);
 clock.currentTime=500;assert.equal(r.stereoSamples().length,11200);
 assert.equal(r.pushMic(new Int16Array(1600),{captureStartCtx:10.5}),null);
 const wav=new DataView(await Audio.wav(stereo,2).arrayBuffer());assert.equal(wav.getUint16(22,true),2);assert.equal(wav.getUint32(28,true),64000);
 assert.equal(r.exportSession([],new LatencyTrace()).length,5);
 assert.equal(r.buildLog([]).physicalOutput,null);assert.equal(r.buildLog([]).schemaVersion,3);
});
test('cancelled scheduled audio cannot fabricate rendered output',()=>{
 const clock={currentTime:1},r=new Recorder();r.start(clock,{sessionId:'s'});
 r.pushRobot(new Int16Array(1600).fill(20000),1.2,'r');r.stopRobot('r',1.2);clock.currentTime=2;r.stop();
 assert.equal(r.robotSamples().length,3200);assert.ok(r.robotSamples().every(x=>x===0));
 assert.equal(r.renderedSamples().length,0);
});
test('recording cap bounds tracks and metadata-only timing survives without audio recording',()=>{
 const clock={currentTime:0},r=new Recorder();r.start(clock,{sessionId:'s'});r.maxSeconds=1;
 r.pushMic(new Int16Array(1600).fill(1000),{captureStartCtx:.95});
 r.pushRendered(new Float32Array(4800),1,48000,'s');
 assert.equal(r.limitReached,true);assert.equal(r.recording,false);assert.equal(r.micSamples().length,16000);
 const trace=new LatencyTrace(),pcm=new Int16Array(1600).fill(500);
 const frame=Audio.captureFrame(pcm,{captureStartCtx:10,captureEndCtx:10.1,sequence:123});trace.capture(frame);frame.sequence=999;
 assert.equal(trace.captures[0].sequence,123);assert.equal(trace.captures[0].pcm,undefined);
 assert.equal(trace.captures[0].lastSignalCtx,10+1599/16000);
});
test('asynchronous export only marks its snapshot revision exported',()=>{
 const r=new Recorder();r.start({currentTime:0},{sessionId:'s'});r.pushMic(new Int16Array(1600),{captureStartCtx:0});
 const revision=r.revision;r.pushMic(new Int16Array(1600),{captureStartCtx:.1});r.markExported(revision);assert.equal(r.hasUnexportedChanges,true);
});
test('server input overflow remains visible even when browser uplink has no drops',()=>{
 const r=new Recorder();r.start({currentTime:0},{sessionId:'s'});
 const e={eventId:'overflow',eventType:'queue_overflow',attributes:{queue:'input'}};r.pushEvent(e);
 const log=r.buildLog([e]);assert.equal(log.warnings.serverInputDropEvents,1);assert.equal(log.warnings.droppedUplinkFrames,0);
});
test('manual acoustic labels stay independent of graph timing; overlap and zero are valid',()=>{
 assert.equal(Review.measurement(.4,1.2,3).gapMs,800);
 assert.equal(Review.measurement(0,0,3,true).gapMs,0);
 assert.equal(Review.measurement(2,1.5,3,true).gapMs,-500);
 assert.equal(Review.measurement(.4,1.2,3,true).classification,'user_labeled_acoustic');
 assert.equal(Review.measurement(.4,1.2,3).classification,'audio_reference');
 for(const args of [[null,1,3],[1,NaN,3],[-1,1,3],[0,4,3]])assert.throws(()=>Review.measurement(...args));
});
test('one ZIP keeps UTF-8 names and lossless WAV bytes',async()=>{
 const files=[{name:'同步.wav',blob:Audio.wav(new Int16Array([1,-2,300]))},{name:'log.json',blob:new Blob(['{"ok":true}'])}];
 const blob=await Audio.zip(files),data=Buffer.from(await blob.arrayBuffer());
 assert.equal(data.readUInt32LE(0),0x04034b50);assert.equal(data.readUInt16LE(6),0x800);
 const n=data.readUInt16LE(26);assert.equal(data.subarray(30,30+n).toString(),'同步.wav');
 assert.equal(data.readInt16LE(30+n+44+2),-2);
 assert.equal(data.readUInt32LE(data.length-22),0x06054b50);
 if(process.env.RECORDING_TEST_ZIP)fs.writeFileSync(process.env.RECORDING_TEST_ZIP,data);
});
test('output worklet captures silent gaps, rendered PCM and final partial buffer, then stops',()=>{
 let Observer;const messages=[];const w=vm.createContext({sampleRate:48000,currentFrame:0,Float32Array,
  AudioWorkletProcessor:class {constructor(){this.port={postMessage:data=>messages.push(data)};}},registerProcessor:(_,C)=>Observer=C});
 vm.runInContext(fs.readFileSync('web/output-worklet.js','utf8'),w);const o=new Observer();
 const send=data=>o.port.onmessage({data});send({type:'record_start',recordingId:'s'});
 o.process([[]],[[new Float32Array(128)]]);w.currentFrame=128;
 send({type:'schedule',replyId:'r',startFrame:128,endFrame:256});
 const input=new Float32Array(128);input[64]=.25;o.process([[input]],[[new Float32Array(128)]]);
 send({type:'record_stop',recordingId:'old'});send({type:'record_stop',recordingId:'s'});
 const chunk=messages.find(x=>x.type==='rendered_pcm');assert.equal(chunk.pcm.length,256);assert.equal(chunk.captureStartCtx,0);
 assert.equal(chunk.pcm[191],0);assert.equal(chunk.pcm[192],.25);
 assert.equal(messages.find(x=>x.type==='graph_onset').onsetCtx,192/48000);
 assert.equal(messages.at(-1).type,'recording_stopped');
 w.currentFrame=256;o.process([[input]],[[new Float32Array(128)]]);send({type:'record_stop',recordingId:'s'});
 assert.equal(messages.filter(x=>x.type==='rendered_pcm').length,1);
});
