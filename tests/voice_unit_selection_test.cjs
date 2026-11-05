const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const code=source.slice(source.indexOf('async function loadVoiceUnit()'),source.indexOf('window.KittyVoiceUnitReady=loadVoiceUnit();'));
function setup(ok=true, search='?chapter=unit5', preferred){
 const errors=[],labels=[],course={},select={options:[{value:'6'}],disabled:false,parentElement:{append:x=>labels.push(x)},add(x){this.options.push(x);}};
 const c=vm.createContext({window:{KittyCourses:require('../web/course-selection.js')},localStorage:{getItem:()=>preferred?JSON.stringify(preferred):null},location:{search},URLSearchParams,
  gradeSelect:select,callButton:{disabled:false},document:{createElement:()=>({})},
  Option:function(label,value){this.label=label;this.value=value;},
  $:id=>id==='#voiceCourse'?course:{classList:{contains:()=>true}},setStatus:text=>{c.status=text;},showError:(...x)=>errors.push(x),encodeURIComponent,
  fetch:async path=>{assert.equal(path,'/api/text-bot/scopes/unit5');return {ok,json:async()=>ok?{selection:{grade:'5年级',unit:'Unit 10'}}:{message:'invalid unit'}};}});
 vm.runInContext(code,c);return {c,errors,labels,select,course};
}
test('selected unit fixes the actual grade before voice admission',async()=>{
 const {c,course,select}=setup();assert.equal(await c.loadVoiceUnit(),true);
 assert.equal(c.window.KittyVoiceChapter.id,'unit5');assert.equal(select.value,'5');
 assert.equal(select.disabled,true);assert.match(course.textContent,/Unit 10/);assert.equal(c.callButton.disabled,false);
});
test('invalid unit prevents dialing instead of silently using another grade',async()=>{
 const {c,errors,course}=setup(false);assert.equal(await c.loadVoiceUnit(),false);
 assert.equal(c.callButton.disabled,true);assert.equal(c.window.KittyVoiceChapter,undefined);
 assert.equal(errors[0][0],'INVALID_GRADE');
 assert.match(course.textContent,/暂时不可用/);assert.match(c.status,/请先选择课程/);
});

test('direct voice entry inherits the saved homepage chapter',async()=>{
 const preferred={version:1,grade:5,track:'textbook',publisher:'人教版',edition:'上册',chapterId:'unit5'};
 const {c,select}=setup(true,'',preferred);
 assert.equal(await c.loadVoiceUnit(),true);
 assert.equal(c.window.KittyVoiceChapter.id,'unit5');assert.equal(select.value,'5');
});
