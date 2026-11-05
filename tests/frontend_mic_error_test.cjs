const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
function setup(error){
 const states=[],events=[];
 const c=vm.createContext({DOMException,Error,requestMicrophone:async()=>{throw error;},
  setMicrophoneState:state=>states.push(state),emitClientEvent:(...args)=>events.push(args)});
 for(const [start,end] of [['function timeoutError(','\nfunction withTimeout('],
  ['function microphoneErrorCode(','\nfunction clearMicrophoneWatchdog('],
  ['async function startMicrophone(','\nfunction stopMicrophone(']])
  vm.runInContext('"use strict";'+source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start))),c);
 return {c,states,events};
}
for(const [name,code] of [['NotAllowedError','MIC_PERMISSION_DENIED'],['SecurityError','MIC_PERMISSION_DENIED'],
 ['NotFoundError','MIC_DEVICE_NOT_FOUND'],['NotReadableError','MIC_DEVICE_BUSY']]){
 test('native '+name+' keeps its read-only code and reports '+code,async()=>{
  const original=new DOMException('browser failure',name),nativeCode=original.code;
  const {c,states,events}=setup(original);
  await assert.rejects(c.startMicrophone('session'),failure=>failure!==original&&failure.code===code&&failure.cause===original);
  assert.equal(original.code,nativeCode);assert.deepEqual(states,['RequestingPermission','Failed']);
  assert.equal(events.at(-1)[2].attributes.code,code);
 });
}
test('custom microphone timeout retains its actionable error code',async()=>{
 const original=Object.assign(new Error('timed out'),{code:'MIC_PERMISSION_TIMEOUT'}),{c}=setup(original);
 await assert.rejects(c.startMicrophone('session'),failure=>failure.code==='MIC_PERMISSION_TIMEOUT');
});
