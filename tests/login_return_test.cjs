const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/login.js'),'utf8');
async function signIn(next){
 const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,{elements:{username:{value:'tester'},password:{value:'test-password'}},reset(){}});return nodes.get(id);};
 let destination;
 const ctx=vm.createContext({URL,URLSearchParams,Date,document:{getElementById:get},location:{origin:'http://local.test',search:'?next='+encodeURIComponent(next),replace:url=>destination=url},localStorage:{setItem(){}},fetch:async()=>({ok:true,json:async()=>({})})});
 vm.runInContext(source,ctx);await get('loginForm').onsubmit({preventDefault(){}});return destination;
}
test('login returns to the selected unit and exercise',async()=>{
 assert.equal(await signIn('/practice.html?chapter=unit3&mode=shadow&kind=words'),'/practice.html?chapter=unit3&mode=shadow&kind=words');
 assert.equal(await signIn('/#course'),'/#course');
});
test('login rejects external redirects, login loops and non-page endpoints',async()=>{
 for(const next of ['https://evil.test','//evil.test','/\\evil.test','/login.html','/api/auth/logout','/\n/evil.test'])assert.equal(await signIn(next),'/');
});
