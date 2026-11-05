const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require.resolve('../web/account.js'), 'utf8');
const progress = require('../web/learning-store.js');
const courses = require('../web/course-selection.js');
const clone = x => JSON.parse(JSON.stringify(x));

function database() {
  const rows = new Map();
  return {rows, online:true, writes:[], async fetch(url, options={}) {
    if (!this.online) throw Error('offline');
    const uid = options.headers['X-Account-ID'];
    if (options.method !== 'PUT') return {ok:true, status:200, json:async()=>({userId:uid, documents:
      Object.fromEntries(['progress','course'].map(kind=>[kind, clone(rows.get(uid+kind) || {value:null,revision:0})]))})};
    const kind = url.split('/').pop(), data = JSON.parse(options.body);
    const key = uid+kind, current = rows.get(key) || {value:null,revision:0};
    if (JSON.stringify(current.value) === JSON.stringify(data.value)) {
      return {ok:true, status:200, json:async()=>({userId:uid,...clone(current)})};
    }
    if (current.revision !== data.revision) return {ok:false, status:409, json:async()=>({current:clone(current)})};
    const next = {revision:current.revision+1,value:data.value}; rows.set(key,next); this.writes.push({uid,kind,...data});
    return {ok:true, status:200, json:async()=>({userId:uid,...clone(next)})};
  }};
}
function browser(id, db, values=new Map(), blockedStorage=false) {
  const events = {}, timeouts = new Map(); let timer=0;
  const window = {KittyAccountUser:{id,username:id}, addEventListener:(name,fn)=>{events[name]=fn;}};
  const localStorage = {getItem:key=>{if(blockedStorage)throw Error();return values.get(key)??null;},
    setItem:(key,value)=>{if(blockedStorage)throw Error();values.set(key,value);}, removeItem:key=>values.delete(key)};
  vm.runInNewContext(source, {window, document:{addEventListener(){}}, localStorage,
    location:{replace(){},reload(){}}, fetch:db.fetch.bind(db),
    setTimeout:fn=>{timeouts.set(++timer,fn);return timer;},clearTimeout:id=>timeouts.delete(id)});
  return {account:window.KittyAccount, values, events, timeouts};
}

test('browser records migrate once; another device reads database; users stay isolated', async () => {
  const db=database(), values=new Map();
  const state=progress.fresh();progress.finish(state,'words',1000);
  values.set('kitty-user:alice:'+progress.KEY, JSON.stringify(state));
  values.set('kitty-user:alice:'+courses.KEY, JSON.stringify(courses.normalize({grade:9,track:'textbook'})));
  values.set(progress.KEY,JSON.stringify({...progress.fresh(),lastModule:'legacy'}));
  const a=browser('alice',db,values);await a.account.ready;
  assert.equal(db.rows.get('aliceprogress').value.completed.words,1000);
  assert.equal(db.rows.get('alicecourse').value.grade,9);
  const device=browser('alice',db);await device.account.ready;
  assert.equal(progress.load(device.account.storage).completed.words,1000);
  const bob=browser('bob',db,values);await bob.account.ready;
  assert.equal(progress.load(bob.account.storage).completed.words,undefined);
  assert.equal(courses.load(bob.account.storage).grade,6);
  progress.save(device.account.storage,progress.fresh());await device.account.flush();
  const returning=browser('alice',db,values);await returning.account.ready;
  assert.deepEqual(progress.load(returning.account.storage).completed,{});
  assert.equal(db.rows.get('aliceprogress').revision,2);
  assert.equal(JSON.parse(values.get(progress.KEY)).lastModule,'legacy');
});

test('offline edits survive reload and are retried with the original revision',async()=>{
  const db=database(), a=browser('alice',db);await a.account.ready;
  db.online=false;
  const state=progress.fresh();progress.finish(state,'reading',2000);
  assert.equal(progress.save(a.account.storage,state),true);
  assert.equal(await a.account.flush(),false);
  assert.ok(a.values.has('kitty-user:alice:pending:progress'));
  db.online=true;
  const retry=browser('alice',db,a.values);await retry.account.ready;
  assert.equal(db.rows.get('aliceprogress').value.completed.reading,2000);
  assert.equal(a.values.has('kitty-user:alice:pending:progress'),false);
});

test('simultaneous device edits do not silently overwrite each other',async()=>{
  const db=database(), a=browser('alice',db), b=browser('alice',db);
  await Promise.all([a.account.ready,b.account.ready]);
  courses.save(a.account.storage,{grade:9});await a.account.flush();
  courses.save(b.account.storage,{grade:7});
  assert.equal(await b.account.flush(),false);
  assert.equal(db.rows.get('alicecourse').value.grade,9);
  assert.equal(courses.save(b.account.storage,{grade:8}),false);
  assert.ok(b.values.has('kitty-user:alice:pending:course'));
});

test('changes while a save is in flight use the acknowledged new revision',async()=>{
  const db=database(), a=browser('alice',db);await a.account.ready;
  const original=db.fetch.bind(db);let release;
  db.fetch=async(...args)=>{await new Promise(resolve=>{release=resolve;});return original(...args);};
  // A separate browser is bound to the delayed transport.
  const b=browser('alice',db); release(); await b.account.ready;
  courses.save(b.account.storage,{grade:7});
  const saving=b.account.flush();
  courses.save(b.account.storage,{grade:9});
  release();
  // Let the first acknowledgement trigger the next queued request.
  for(let i=0;i<8;i++) await Promise.resolve();
  release();
  assert.equal(await saving,true);
  assert.equal(db.rows.get('alicecourse').value.grade,9);
  assert.deepEqual(db.writes.map(x=>x.revision),[0,1]);
});

test('storage restrictions do not prevent database saves; initial read failure blocks editing',async()=>{
  const db=database(), a=browser('alice',db,new Map(),true);await a.account.ready;
  assert.equal(courses.save(a.account.storage,{grade:8}),true);assert.equal(await a.account.flush(),true);
  const broken=database();broken.online=false;
  const b=browser('alice',broken);
  await assert.rejects(b.account.ready);
  assert.equal(courses.save(b.account.storage,{grade:7}),false);
  assert.equal(broken.rows.size,0);
});

test('embedded writing reuses only the same authenticated parent sync journal',async()=>{
  const host={user:{id:'alice'},storage:{},ready:Promise.resolve()};
  const child={KittyAccountUser:{id:'alice'},parent:{KittyAccount:host}};
  vm.runInNewContext(source,{window:child,location:{pathname:'/writing.html',search:'?embedded=1'},
    URLSearchParams,fetch(){throw Error('embedded editor must not open a second sync writer');}});
  assert.equal(child.KittyAccount,host);
  const db=database(),events={};
  const other={KittyAccountUser:{id:'bob'},parent:{KittyAccount:host},addEventListener:(name,fn)=>{events[name]=fn;}};
  vm.runInNewContext(source,{window:other,location:{pathname:'/writing.html',search:'?embedded=1',replace(){}},
    URLSearchParams,document:{addEventListener(){}},localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
    fetch:db.fetch.bind(db),setTimeout,clearTimeout});
  await other.KittyAccount.ready;
  assert.notEqual(other.KittyAccount,host);assert.equal(other.KittyAccount.user.id,'bob');
});
