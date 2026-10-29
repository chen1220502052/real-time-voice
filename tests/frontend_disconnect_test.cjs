const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const start=source.indexOf("window.addEventListener('pagehide'");
const end=source.indexOf('\nfunction endCall',start);
const callbacks={},beacons=[],requests=[],ended=[];
const context=vm.createContext({window:{addEventListener:(name,fn)=>callbacks[name]=fn},
  sessionId:'session-1',disconnectKey:'private-disconnect-key',expectedClose:false,
  navigator:{sendBeacon:(url,body)=>{beacons.push({url,body});return true;}},Blob,JSON,
  fetch:(url,options)=>{requests.push({url,options});return Promise.resolve();},
  endCall:(...args)=>ended.push(args)});
vm.runInContext(source.slice(start,end),context);
(async()=>{
  assert.equal(Object.keys(callbacks).join(','),'pagehide'); // Not visibilitychange or idle.
  callbacks.pagehide();
  assert.equal(beacons[0].url,'/api/browser-disconnect');
  assert.deepEqual(JSON.parse(await beacons[0].body.text()),{sessionId:'session-1',disconnectKey:'private-disconnect-key'});
  assert.equal(ended.length,1);assert.equal(context.expectedClose,true);
  assert.equal(requests.length,0);
  context.navigator.sendBeacon=()=>false;callbacks.pagehide();
  assert.equal(requests[0].options.keepalive,true);
  assert.equal(requests[0].options.credentials,'same-origin');
  context.sessionId=undefined;callbacks.pagehide();assert.equal(requests.length,1);
  console.log('Page leave beacon, fallback, no active session and no visibility/idle eviction passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
