const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let timers=[],requests=[],response={ok:true,status:200,json:async()=>({ok:true,resources:{capacity:3,inUse:0}})};
const window={};let time=0;
const context=vm.createContext({window,crypto:{randomUUID:()=> 'test-attempt'},performance:{now:()=>time},
  AbortController,Set,Error,JSON,setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},
  fetch:async(url,options)=>{requests.push({url,options});return response;}});
vm.runInContext(fs.readFileSync('web/connection.js','utf8'),context);
(async()=>{
  const attempt=new window.ConnectionAttempt();
  const ready=await attempt.bootstrap();assert.equal(ready.resources.capacity,3);
  assert.equal(requests[0].url,'/api/browser-session');
  assert.equal(requests[0].options.credentials,'same-origin');
  assert.equal(timers[0].ms,10000);
  assert.ok(!requests[0].options.headers); // No credential copying into JS.
  for(const status of [401,403]){
    response={status,ok:false};await assert.rejects(()=>attempt.bootstrap(),e=>e.code==='AUTH_REQUIRED');
  }
  response={status:503,ok:false};await assert.rejects(()=>attempt.bootstrap(),e=>e.code==='AUTH_UNAVAILABLE');
  context.fetch=async()=>{const e=new Error('timeout');e.name='AbortError';throw e;};
  await assert.rejects(()=>attempt.bootstrap(),e=>e.code==='AUTH_TIMEOUT');
  context.fetch=async(url,options)=>{requests.push({url,options});return {};};
  time=16000;attempt.report('WEBSOCKET_TIMEOUT');attempt.report('WEBSOCKET_TIMEOUT');
  const reports=requests.filter(r=>r.url==='/api/connection-events');assert.equal(reports.length,1);
  assert.deepEqual(JSON.parse(reports[0].options.body),{attemptId:'test-attempt',code:'WEBSOCKET_TIMEOUT',elapsedMs:16000});
  assert.ok(window.ConnectionAttempt.busyMessage({inUse:3,capacity:3}).includes('3/3'));
  const source=fs.readFileSync('web/app.js','utf8');
  assert.ok(source.includes('websocket: 15000'));
  assert.ok(source.includes('if (socket !== activeSocket || attempt !== callAttempt) return;'));
  assert.ok(source.indexOf('await connectionAttempt.bootstrap()')<source.indexOf('const activeSocket = new WebSocket'));
  console.log('Connection bootstrap, login expiry, timeout, busy status, telemetry dedup and stale sockets passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
