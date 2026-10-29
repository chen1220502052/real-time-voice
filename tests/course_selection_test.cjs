const test=require('node:test'),assert=require('node:assert/strict');
const C=require('../web/course-selection.js');
const catalog=[
 {id:'p4a',grade:'4年级',publisher:'人教版',edition:'上册',unit:'Unit 1'},
 {id:'p6a',grade:'6年级',publisher:'人教版',edition:'上册',unit:'Unit 1'},
 {id:'p6b',grade:'6年级',publisher:'人教版',edition:'上册',unit:'Unit 2'},
 {id:'k4a',grade:'4年级',publisher:'科普版',edition:'下册',unit:'Lesson 1'},
 {id:'p9a',grade:'9年级',publisher:'人教版',edition:'全册',unit:'Unit 1'}
];
function storage(initial={}){const data={...initial};return {data,getItem:k=>data[k]??null,setItem:(k,v)=>{data[k]=v;}};}
test('all seven grades can be selected without relabeling the sample as another grade',()=>{
 for(const grade of C.grades){const s=C.normalize({grade,track:'extension'});assert.equal(s.grade,grade);assert.equal(s.unitId,grade===6?'weekend-v1':'');}
 assert.equal(C.normalize({grade:3}).grade,3);
 assert.equal(C.gradeNames[3],'三年级');
 assert.equal(C.normalize({grade:2}).grade,6);
 assert.equal(C.normalize({grade:10}).grade,6);
});
test('textbook selection resolves exact publisher, grade, edition and chapter',()=>{
 const s=C.fromChapter(catalog[2]);assert.equal(C.resolve(catalog,s).id,'p6b');
 assert.equal(C.resolve(catalog,{...s,grade:4}),null);
 assert.equal(C.resolve(catalog,{...s,publisher:'科普版'}),null);
 assert.equal(C.resolve(catalog,{...s,edition:'下册'}),null);
 assert.equal(C.resolve(catalog,{...s,chapterId:'removed'}),null);
 assert.deepEqual(C.rows(catalog,{grade:6}).map(c=>c.id),['p6a','p6b']);
});
test('last course persists separately from sample learning evidence',()=>{
 const progress=JSON.stringify({completed:{words:123}});
 const s=storage({'kitty-learning:weekend-v1':progress});
 const choice=C.fromChapter(catalog[3]);assert.equal(C.save(s,choice),true);
 assert.deepEqual(C.load(s),choice);assert.equal(s.data['kitty-learning:weekend-v1'],progress);
 C.save(s,{grade:9,track:'extension'});assert.equal(C.load(s).grade,9);
 assert.equal(s.data['kitty-learning:weekend-v1'],progress);
});
test('missing, corrupt or unavailable storage falls back safely',()=>{
 assert.equal(C.load(storage()).grade,6);
 assert.equal(C.load(storage({[C.KEY]:'{bad json'})).grade,6);
 assert.equal(C.load({getItem:()=>{throw Error('blocked');}}).grade,6);
 assert.equal(C.save({setItem:()=>{throw Error('quota');}},{}),false);
});
test('deep links carry exact chapter and mode and safely encode identifiers',()=>{
 const url=C.practiceURL({chapterId:'chapter & 4'},'listening');
 const params=new URL(url,'http://example.test').searchParams;
 assert.equal(params.get('chapter'),'chapter & 4');assert.equal(params.get('mode'),'listening');
 assert.equal(new URL(C.practiceURL({},'invalid'),'http://example.test').searchParams.get('mode'),'shadow');
});

test('chat links carry the selected chapter through both modes',()=>{
 const selection=C.fromChapter(catalog[2]);
 assert.equal(C.chatURL(selection,'voice'),'/voice.html?chapter=p6b');
 assert.equal(C.chatURL(selection,'text'),'/text.html?chapter=p6b');
 assert.equal(C.chatURL(C.normalize({grade:6,track:'extension'})),'/text.html');
});

test('third grade textbook selection persists and carries the same chapter to every entry',()=>{
 const row={id:'k3a',grade:'3年级',publisher:'科普版',edition:'上册',unit:'Unit 1 Hello'};
 const choice=C.fromChapter(row),s=storage();
 assert.equal(choice.grade,3);assert.equal(C.save(s,choice),true);
 assert.deepEqual(C.resolve([row],C.load(s)),row);
 assert.equal(C.chatURL(choice,'voice'),'/voice.html?chapter=k3a');
 assert.equal(C.chatURL(choice,'text'),'/text.html?chapter=k3a');
 assert.equal(new URL(C.practiceURL(choice,'speaking'),'http://test').searchParams.get('chapter'),'k3a');
});
