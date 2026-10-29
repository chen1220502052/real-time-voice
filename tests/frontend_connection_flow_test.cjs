const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const errors=[],ended=[],reports=[],sockets=[],timers=[];
let access={ok:true,resources:{capacity:2,inUse:0}},failure;
class Probe{constructor(){this.id='attempt';}async bootstrap(){if(failure)throw failure;return access;}report(code){reports.push(code);}static busyMessage(r){return `busy ${r.inUse}/${r.capacity}`;}}
class Socket{constructor(url){this.url=url;this.sent=[];sockets.push(this);}send(text){this.sent.push(JSON.parse(text));}close(){this.closed=true;}}
const c=vm.createContext({window:{ConnectionAttempt:Probe},WebSocket:Socket,crypto:{randomUUID:()=> 'uuid'},
  sessionState:'Idle',conversationRecorder:null,callAttempt:0,callButton:{},expectedClose:false,
  socket:undefined,sessionId:undefined,stageTimer:undefined,connectionAttempt:undefined,
  STAGE_TIMEOUT_MS:{websocket:15000,prepare:8000,createSession:8000,audioWorklet:5000},
  location:{protocol:'https:',host:'test.invalid'},confirm:()=>true,resetSessionView(){},
  setSessionState(v){c.sessionState=v;},setConnection(){},setStatus(){},ensureAudioContext:async()=>{},
  withTimeout:async p=>await p,showError:(...args)=>errors.push(args),emitClientEvent(){},
  setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},
  clearStageTimeout(){c.stageTimer=undefined;},
  endCall:(...args)=>{ended.push(args);c.socket=undefined;c.sessionState='Error';},ArrayBuffer,JSON,
  playAudio(){},handleJson(){}});
vm.runInContext(source.slice(source.indexOf('function armStageTimeout('),source.indexOf('\nfunction timeoutError(')),c);
vm.runInContext(source.slice(source.indexOf('async function startCall('),source.indexOf('\nfunction hangup(')),c);
(async()=>{
  failure=Object.assign(new Error('expired'),{code:'AUTH_REQUIRED'});
  await c.startCall();assert.equal(sockets.length,0);assert.equal(errors.at(-1)[0],'AUTH_REQUIRED');
  failure=undefined;access.resources.inUse=2;c.sessionState='Idle';await c.startCall();
  assert.equal(sockets.length,0);assert.equal(errors.at(-1)[0],'NO_RESOURCE');assert.ok(ended.at(-1)[0].includes('2/2'));
  access.resources.inUse=0;c.sessionState='Idle';await c.startCall();const old=sockets.at(-1);
  assert.equal(old.url,'wss://test.invalid/ws');assert.equal(timers.at(-1).ms,15000);
  old.onopen();assert.equal(old.sent[0].type,'prepare');assert.equal(old.sent[0].attemptId,'attempt');
  c.sessionState='Idle';await c.startCall();const current=sockets.at(-1),before=ended.length;
  old.onclose();old.onerror();assert.equal(ended.length,before);assert.equal(c.socket,current);
  timers.at(-1).fn();assert.equal(errors.at(-1)[0],'WEBSOCKET_TIMEOUT');
  const after=ended.length;current.onclose();assert.equal(ended.length,after); // Does not overwrite timeout.
  assert.ok(reports.includes('WEBSOCKET_TIMEOUT'));
  console.log('Actual startCall: expired auth, busy admission, prepare, late old callbacks and preserved timeout passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
