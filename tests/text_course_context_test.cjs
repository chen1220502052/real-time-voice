const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const C=require('../web/course-selection.js');
const source=fs.readFileSync('web/text.js','utf8').replace(/loadCatalog\(\);\s*$/, 'window.ready=loadCatalog();');
async function setup(search='',saved={version:1,grade:4,track:'textbook',publisher:'科普版',edition:'上册',chapterId:'unit4'},ok=true){
 const nodes={},requests=[],link={};
 function element(id){return nodes[id]??= {value:'',textContent:'',disabled:false,replaceChildren(){},addEventListener(){}};}
 const context=vm.createContext({window:{KittyCourses:C},localStorage:{getItem:()=>JSON.stringify(saved)},
 document:{getElementById:element,querySelector:()=>link},crypto:{randomUUID:()=> 'uuid'},
 location:{href:'https://example.test/text.html'+search},URL,encodeURIComponent,
 fetch:async url=>{requests.push(url);return {ok,json:async()=>({selection:{grade:'4年级',publisher:'科普版',edition:'上册',unit:'Unit 2'},textbookReady:true,vocabularySize:36,unit:'Unit 2',notice:'参考教材词汇'})};}});
 vm.runInContext(source,context);await context.window.ready;return {nodes,requests,link};
}
test('text chat inherits homepage context without choosing another textbook',async()=>{
 const {nodes,requests,link}=await setup();
 assert.deepEqual(requests,['/api/text-bot/scopes/unit4']);
 assert.equal(nodes.textGrade.value,'4');assert.equal(nodes.textChapter.value,'unit4');
 assert.match(nodes.textCourse.textContent,/四年级 · 科普版 · 上册 · Unit 2/);
 assert.equal(nodes.sendText.disabled,false);assert.equal(link.href,'/voice.html?chapter=unit4');
});
test('explicit chapter survives a different saved homepage choice',async()=>{
 const {nodes,requests}=await setup('?chapter=shared-unit');
 assert.deepEqual(requests,['/api/text-bot/scopes/shared-unit']);assert.equal(nodes.textChapter.value,'shared-unit');
});
test('missing or invalid chapter cannot silently start on a fallback course',async()=>{
 const missing=await setup('',C.normalize({grade:6,track:'extension'}));
 assert.deepEqual(missing.requests,[]);assert.equal(missing.nodes.sendText.disabled,true);
 const invalid=await setup('?chapter=missing',undefined,false);
 assert.equal(invalid.nodes.sendText.disabled,true);assert.equal(invalid.nodes.newChat.disabled,true);
});
