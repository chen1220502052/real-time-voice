const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const c=vm.createContext({window:{},Int16Array,Blob,ArrayBuffer,DataView,Date,performance});
vm.runInContext(fs.readFileSync('web/recorder.js','utf8'),c);
test('export covers only the captured revision, including subsequent audio and markers',()=>{
 const r=new c.window.ConversationRecorder(),audio={currentTime:0};
 r.start(audio,{sessionId:'s'});assert.equal(r.hasUnexportedChanges,false);
 r.pushMic(new Int16Array(1600),{captureStartCtx:0});assert.equal(r.hasUnexportedChanges,true);
 r.markExported();assert.equal(r.hasUnexportedChanges,false);
 r.pushMic(new Int16Array(1600),{captureStartCtx:.1});assert.equal(r.hasUnexportedChanges,true);
 r.markExported();r.mark('good','new note',audio);assert.equal(r.hasUnexportedChanges,true);
 r.markExported();r.pushRobot(new Int16Array(1600),.2,'reply');assert.equal(r.hasUnexportedChanges,true);
 r.markExported();r.stopRobot('reply',.25);assert.equal(r.hasUnexportedChanges,true);
 r.markExported();r.stopRobot('reply',.25);assert.equal(r.hasUnexportedChanges,false);
 r.start(audio,{sessionId:'new'});assert.equal(r.hasUnexportedChanges,false);
 r.pushMic(new Int16Array(1600),{captureStartCtx:0});assert.equal(r.hasUnexportedChanges,true);
});

test('an asynchronous export snapshot never marks newer audio as saved',()=>{
 const r=new c.window.ConversationRecorder();r.start({currentTime:0},{sessionId:'snapshot'});
 r.pushMic(new Int16Array(1600),{captureStartCtx:0});const snapshot=r.revision;
 r.pushMic(new Int16Array(1600),{captureStartCtx:.1});r.markExported(snapshot);
 assert.equal(r.hasUnexportedChanges,true);r.markExported();assert.equal(r.hasUnexportedChanges,false);
});

test('ten-minute recording limit crops the boundary and rejects later frames',()=>{
 const r=new c.window.ConversationRecorder();r.start({currentTime:0},{sessionId:'limit'});
 r.pushMic(new Int16Array(1600),{captureStartCtx:599.95});
 assert.equal(r.pushMic(new Int16Array(1600),{captureStartCtx:600}),null);
 assert.equal(r.limitReached,true);assert.equal(r.recording,false);assert.equal(r.endCtx,600);
 assert.equal(r.micChunks.length,1);assert.equal(r.micSamples().length,600*16000);
});
