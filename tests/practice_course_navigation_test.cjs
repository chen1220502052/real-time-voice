/* Exercise the real cascading selection code without loading models or a browser. */
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const C=require('../web/course-selection.js');
const SentencePlayer=require('../web/sentence-player.js');
const ShadowContent=require('../web/shadow-content.js');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/practice.js'),'utf8');
const catalog=[
 {id:'four',grade:'4年级',publisher:'人教版',edition:'上册',unit:'Unit 1'},
 {id:'six',grade:'6年级',publisher:'人教版',edition:'上册',unit:'Unit 1'},
 {id:'nine',grade:'9年级',publisher:'人教版',edition:'全册',unit:'Unit 1'},
 {id:'k-four',grade:'4年级',publisher:'科普版',edition:'下册',unit:'Lesson 1'}
];
async function run(query='',saved,recordings=[],vocabulary=[],textbookPages=[],media={}){
 const controls=new Map(),data={},calls=[];
 if(saved)data[C.KEY]=JSON.stringify(saved);
 const selects=new Set(['publisher','grade','edition','chapter','recording','shadowLesson','shadowFullSelect']);
 class Control {
  constructor(id=''){this.id=id;this.value=id==='kind'?'passage':id==='speed'?'1':'';this.options=id==='kind'?[{value:'words'},{value:'passage'}]:[];this.events={};this.dataset={};this.style={setProperty(name,value){this[name]=value;}};}
  addEventListener(name,fn){this.events[name]=fn;}
  removeEventListener(name,fn){if(this.events[name]===fn)delete this.events[name];}
  replaceChildren(...children){this.children=children;if(selects.has(this.id)){this.options=children;this.value=children[0]?.value||'';}}
  append(...children){this.children=(this.children||[]).concat(children);}
  querySelector(){return new Control();}
  setAttribute(k,v){this[k]=v;}
  getAttribute(k){return this[k];}
  removeAttribute(k){delete this[k];}
  pause(){this.paused=true;} load(){this.duration=NaN;this.currentTime=0;this.error=null;} play(){this.paused=false;this.playCount=(this.playCount||0)+1;return Promise.resolve();}
 }
 const el=id=>{if(!controls.has(id))controls.set(id,new Control(id));return controls.get(id);};
 const modeButtons=['shadow','listening','speaking'].map(mode=>{const b=new Control();b.dataset.mode=mode;return b;});
 const location={href:'http://example.test/practice.html'+query,search:query};
 const storage={getItem:k=>data[k]??null,setItem:(k,v)=>{data[k]=v;}};
 const context=vm.createContext({console,URL,URLSearchParams,Option:class {constructor(text,value){this.text=text;this.value=value;}},
  document:{getElementById:el,querySelectorAll:()=>modeButtons,createElement:()=>new Control(),addEventListener(){}},
  window:{localStorage:storage,KittyCourses:C,KittySentencePlayer:SentencePlayer,KittyShadowContent:ShadowContent,MediaRecorder:media.MediaRecorder,addEventListener(){}},location,history:{replaceState:(a,b,url)=>{location.href=String(url);}},
  navigator:media.navigator || {},MediaRecorder:media.MediaRecorder,Blob,setTimeout,clearTimeout,fetch:async url=>{calls.push(url);if(url==='/api/practice/catalog')return {ok:true,json:async()=>({chapters:catalog})};
   const id=url.split('/').at(-1),chapter=catalog.find(c=>c.id===id);return {ok:!!chapter,json:async()=>({...chapter,recordings,tasks:[],vocabulary,textbookPages,question:'Question',example:'Example'})};}});
 await vm.runInContext(source,context);await new Promise(resolve=>setImmediate(resolve));
 return {controls,el,context,storage,location,calls,modeButtons};
}
test('query deep link selects ninth grade and exact chapter instead of first catalog entry',async()=>{
 const x=await run('?chapter=nine&mode=listening');
 assert.equal(x.el('grade').value,'9年级');assert.equal(x.el('edition').value,'全册');assert.equal(x.el('chapter').value,'nine');
 assert.equal(vm.runInContext('mode',x.context),'listening');assert.equal(C.load(x.storage).chapterId,'nine');
 assert.equal(x.el('chapterTitle').textContent,'Unit 1');
});
test('opening practice without query restores last publisher and chapter',async()=>{
 const x=await run('',C.fromChapter(catalog[3]));
 assert.equal(x.el('publisher').value,'科普版');assert.equal(x.el('grade').value,'4年级');assert.equal(x.el('edition').value,'下册');assert.equal(x.el('chapter').value,'k-four');
});
test('writing deep link opens the selected unit in the composition panel and hides recording',async()=>{
 const x=await run('?chapter=nine&mode=writing');
 assert.equal(vm.runInContext('mode',x.context),'writing');
 assert.equal(x.el('writingTask').hidden,false);
 assert.equal(x.el('writingFrame').getAttribute('src'),'/writing.html?embedded=1&chapter=nine');
 assert.equal(x.el('audioPractice').hidden,true);
 assert.equal(x.el('selfRecordSection').hidden,true);
 assert.equal(C.practiceURL(C.fromChapter(catalog[2]),'writing'),'/practice.html?chapter=nine&mode=writing');
});
test('selecting another grade cascades to its edition and updates the saved course',async()=>{
 const x=await run('?chapter=six');x.el('grade').value='9年级';x.el('grade').events.change();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(x.el('edition').value,'全册');assert.equal(x.el('chapter').value,'nine');assert.equal(C.load(x.storage).grade,9);
});
test('unknown deep-linked chapter shows unavailable rather than silently opening another unit',async()=>{
 const x=await run('?chapter=removed');assert.equal(vm.runInContext('current',x.context),null);
 assert.match(x.el('practiceStatus').textContent,/不在当前目录/);assert.equal(x.calls.length,1);
});
test('chapter speaking entry retains the selected textbook and grade',async()=>{
 const x=await run('?chapter=four');
 const target=new URL(x.el('chapterSpeakingLink').href,'http://example.test');
 assert.equal(target.pathname,'/practice.html');
 assert.equal(target.searchParams.get('chapter'),'four');
 assert.equal(target.searchParams.get('mode'),'speaking');
 x.el('grade').value='9年级';x.el('grade').events.change();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(new URL(x.el('chapterSpeakingLink').href,'http://example.test').searchParams.get('chapter'),'nine');
});

const recording = {id:'audio-one', kind:'passage', title:'Listen', url:'/audio/one.mp3', availability:'local'};
test('textbook pages follow exact recording pairing and page buttons play that recording',async()=>{
 const segment={...recording,id:'segment',title:'教材分段 1'};
 const pages=[{id:'p1',url:'/api/practice/textbook-pages/p1',label:'教材原图 1',recordingId:'segment'},
  {id:'p2',url:'/api/practice/textbook-pages/p2',label:'教材原图 2',recordingId:null}];
 const x=await run('?chapter=four',null,[recording,segment],[],pages);
 assert.equal(x.el('textbookReader').hidden,false);assert.equal(x.el('textbookPages').children.length,2);
 const first=x.el('textbookPages').children[0];
 assert.equal(first.children[0].children[0].src,pages[0].url);
 assert.equal(first.children[0].target,'_blank');
 first.children[1].children[1].events.click();
 assert.equal(x.el('recording').value,'segment');assert.equal(x.el('referenceAudio').playCount,1);
 assert.equal(x.el('textbookPages').children.length,1);assert.match(x.el('textbookNotice').textContent,/点击播放器听读/);
 x.modeButtons[1].events.click();assert.equal(x.el('textbookReader').hidden,true);
 assert.equal(x.el('textbookPages').children.length,0);
});
test('images without recordings can be read, with no stale images after changing chapter',async()=>{
 const x=await run('?chapter=four',null,[],[],[{id:'p',url:'/page.jpg',label:'教材原图 1'}]);
 assert.equal(x.el('kind').value,'passage');assert.equal(x.el('textbookReader').hidden,false);
 assert.equal(x.el('textbookPages').children.length,1);assert.equal(x.el('audioPlaybackControls').hidden,true);
 const pending=vm.runInContext('loadChapter()',x.context);
 assert.equal(x.el('textbookReader').hidden,true);assert.equal(x.el('textbookPages').children.length,0);
 await pending;
});
test('missing textbook pages hide the reader without preparation notices',async()=>{
 const x=await run('?chapter=four',null,[recording]);
 assert.equal(x.el('textbookReader').hidden,true);
 assert.equal(x.el('textbookNotice').textContent,'');assert.equal(x.el('textbookNotice').hidden,true);
 assert.equal(x.el('textbookPages').children.length,0);
});
test('dialogue uses illustration backgrounds, per-sentence buttons and cancels on mode change',async()=>{
 const segment={...recording,id:'breakfast'};
 const pages=[{id:'scene',url:'/scene.jpg',backgroundUrl:'/illustrations/background.png',label:'早餐对话',recordingId:'breakfast',imageSize:[718,672],
  scenes:[{title:'早上好',crop:[68,128,585,260],lines:[
   {text:'Good morning.',start:8.3,end:9.7,bubble:[1,3,40]},
   {text:'Good morning, Mum.',start:10.45,end:12.05,bubble:[59,16,40]}
  ]}]}];
 const x=await run('?chapter=four',null,[recording,segment],[],pages);
 assert.equal(x.el('recording').value,'breakfast');
 const stage=x.el('textbookPages').children[0].children[0].children[1];
 assert.equal(stage.style.backgroundImage,'url("/illustrations/background.png")');assert.equal(stage.children.length,2);
 assert.match(stage.className,/is-clean/);assert.equal(stage.style['--scene-ratio'],'585 / 260');
 const bubble=stage.children[0], button=bubble.children[1];
 assert.equal(bubble.children[0].textContent,'Good morning.');assert.equal(button['aria-label'],'播放：Good morning.');
 button.events.click();x.el('sentenceAudio').events.loadedmetadata();await Promise.resolve();
 assert.equal(x.el('sentenceAudio').currentTime,8.3);assert.equal(button['aria-pressed'],'true');
 x.modeButtons[1].events.click();assert.equal(x.el('sentenceAudio').paused,true);
 assert.equal(button['aria-pressed'],'false');assert.equal(x.el('textbookReader').hidden,true);
});
test('words without samples show their prompt with recording disabled',async()=>{
 const x=await run('?chapter=four',null,[recording],[{word:'bread',meaning:'面包'}]);
 assert.equal(x.el('kind').value,'passage');
 assert.equal(x.el('kind').options.find(o=>o.value==='words').disabled,false);
 x.el('kind').value='words';x.el('kind').events.change();
 assert.equal(x.el('kind').value,'words');assert.equal(x.el('wordSupplement').open,false);
 assert.equal(x.el('words').children[0].children[0].textContent,'bread · 面包');
 assert.equal(x.el('recordingPicker').hidden,true);assert.equal(x.el('audioPlaybackControls').hidden,true);
 assert.equal(x.el('referenceAudio').getAttribute('src'),undefined);
 assert.equal(x.el('audioNotice').textContent,'');assert.equal(x.el('audioNotice').hidden,true);
 x.modeButtons[1].events.click();assert.equal(x.el('recording').value,recording.id);
 assert.equal(x.el('wordSupplement').hidden,true);
 x.modeButtons[0].events.click();assert.equal(x.el('kind').value,'words');
 assert.equal(x.el('wordSupplement').open,false);
 x.el('kind').value='passage';x.el('kind').events.change();
 assert.equal(x.el('recording').value,recording.id);assert.equal(x.el('wordSupplement').open,false);
});
test('word prompts remain available without textbook recordings',async()=>{
 const x=await run('?chapter=four',null,[],[{word:'water',meaning:'水'}]);
 assert.equal(x.el('kindLabel').hidden,false);assert.equal(x.el('kind').value,'words');
 assert.equal(x.el('shadowPractice').hidden,false);assert.equal(x.el('recordSelf').disabled,true);
 assert.equal(x.el('shadowText').textContent,'water');assert.equal(x.el('audioPlaybackControls').hidden,true);
});
test('word audio works without a word list and missing word content remains disabled',async()=>{
 const x=await run('?chapter=four',null,[recording]);
 assert.equal(x.el('kind').options.find(o=>o.value==='words').disabled,true);
 const y=await run('?chapter=four',null,[{...recording,kind:'words'}]);
 assert.equal(y.el('kind').value,'words');assert.equal(y.el('audioPlaybackControls').hidden,false);
 assert.match(y.el('audioNotice').textContent,/官方单词录音/);
});
test('missing recordings hide the picker and all playback tools', async()=>{
 const x=await run('?chapter=four');
 assert.equal(x.el('recordingPicker').hidden,true);
 assert.equal(x.el('audioPlaybackControls').hidden,true);
 assert.equal(x.el('referenceAudio').getAttribute('src'),undefined);
 assert.equal(x.el('speed').disabled,true);
});
test('playback speed applies to the selected recording and recovers after a load error',async()=>{
 const x=await run('?chapter=four',null,[recording]);const audio=x.el('referenceAudio');
 x.el('speed').value='0.75';x.el('speed').events.change();assert.equal(audio.playbackRate,0.75);
 audio.error={code:4};audio.events.error();assert.equal(x.el('speed').disabled,true);
 x.el('recording').events.change();assert.equal(x.el('speed').disabled,false);assert.equal(audio.playbackRate,0.75);
 x.modeButtons[2].events.click();assert.equal(x.el('audioPractice').hidden,true);
});

function fakeMicrophone() {
 const tracks = [];
 class Recorder {
  constructor(){this.state='inactive';this.mimeType='audio/webm';this.events={};}
  addEventListener(name,fn){this.events[name]=fn;}
  start(){this.state='recording';}
  stop(){this.state='inactive';const done=this.events;queueMicrotask(()=>{done.dataavailable({data:new Blob(['voice'])});done.stop();});}
 }
 return {tracks,MediaRecorder:Recorder,navigator:{mediaDevices:{getUserMedia:async()=>{
  const track={stopped:false,stop(){this.stopped=true;}};tracks.push(track);return {getTracks:()=>[track]};
 }}}};
}
test('one-word workspace plays a sample, records and enables replay after finishing',async()=>{
 const mic=fakeMicrophone();
 const x=await run('?chapter=four',null,[],[{word:'bread',meaning:'面包',sampleAudio:{url:'/bread.mp3',kind:'recorded',duration:1.2}}],[],mic);
 assert.equal(x.el('playSelf').disabled,true);
 x.el('playOriginal').events.click();assert.equal(x.el('referenceAudio').src,'/bread.mp3');
 await x.el('recordSelf').events.click();
 assert.match(x.el('recordTarget').textContent,/bread/);
 assert.equal(x.el('playOriginal').hidden,false);
 assert.equal(x.el('stopRecording').disabled,false);
 x.el('stopRecording').events.click();await Promise.resolve();
 assert.equal(mic.tracks[0].stopped,true);
 assert.equal(x.el('playSelf').hidden,false);
 assert.equal(x.el('recordSelf').textContent,'再读一次');
 x.el('playSelf').events.click();assert.equal(x.el('selfAudio').playCount,1);
 x.modeButtons[1].events.click();assert.equal(x.el('playSelf').hidden,true);
 assert.equal(x.el('selfAudio').getAttribute('src'),undefined);
});
test('sentence reference comparison preserves the take and uses only the chosen interval',async()=>{
 const mic=fakeMicrophone();
 const pages=[{id:'p',url:'/page.jpg',recordingId:recording.id,imageSize:[400,300],scenes:[
  {title:'Hi',crop:[0,0,400,300],lines:[{text:'Hello.',start:2,end:3,bubble:[0,0,80]}]}]}];
 const x=await run('?chapter=four',null,[recording],[],pages,mic);
 const bubble=x.el('textbookPages').children[0].children[0].children[1].children[0];
 await x.el('recordSelf').events.click();
 x.el('stopRecording').events.click();await Promise.resolve();
 const take=x.el('selfAudio').src;
 x.el('playOriginal').events.click();x.el('sentenceAudio').events.loadedmetadata();await Promise.resolve();
 assert.equal(x.el('sentenceAudio').currentTime,2);
 assert.equal(x.el('selfAudio').src,take);assert.equal(x.el('playSelf').hidden,false);
 // The existing sentence listen button must also preserve the take.
 bubble.children[1].events.click();x.el('sentenceAudio').events.loadedmetadata();await Promise.resolve();
 assert.equal(x.el('selfAudio').src,take);
 x.el('playSelf').events.click();assert.equal(x.el('sentenceAudio').paused,true);
 x.el('kind').value='words';x.el('kind').events.change();
});
test('switching content while microphone permission is pending discards the late stream',async()=>{
 let grant;
 const mic=fakeMicrophone();
 mic.navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{grant=resolve;});
 const x=await run('?chapter=four',null,[],[{word:'bread',meaning:'面包',sampleAudio:{url:'/bread.mp3',kind:'recorded',duration:1.2}}],[],mic);
 const pending=x.el('recordSelf').events.click();
 x.modeButtons[1].events.click();
 let stopped=false;grant({getTracks:()=>[{stop(){stopped=true;}}]});await pending;
 assert.equal(stopped,true);assert.equal(x.el('playSelf').hidden,true);
 assert.equal(x.el('stopRecording').disabled,true);
});
test('a failed word sample blocks recording until a successful retry starts playing',async()=>{
 const mic=fakeMicrophone();
 const x=await run('?chapter=four&kind=words',null,[],[{word:'sing',meaning:'唱歌',sampleAudio:{url:'/sing.mp3',kind:'recorded',duration:1}}],[],mic);
 x.el('playOriginal').events.click();x.el('referenceAudio').events.error();
 assert.equal(x.el('recordSelf').disabled,true);
 await x.el('recordSelf').events.click();assert.equal(mic.tracks.length,0);
 x.el('referenceAudio').events.playing();assert.equal(x.el('recordSelf').disabled,false);
});
test('word playback visibly loads, plays, stops and offers replay; another word resets the state',async()=>{
 const x=await run('?chapter=four&kind=words',null,[],[
  {word:'sing',meaning:'唱歌',sampleAudio:{url:'/sing.mp3',kind:'recorded',duration:2}},
  {word:'swim',meaning:'游泳',sampleAudio:{url:'/swim.mp3',kind:'recorded',duration:2}}]);
 const button=x.el('playOriginal'),audio=x.el('referenceAudio');
 assert.match(button.textContent,/▶ 听单词发音/);
 button.events.click();assert.match(button.textContent,/加载中/);assert.equal(button['aria-busy'],'true');
 button.events.click();assert.equal(audio.paused,true);assert.match(button.textContent,/听单词发音/);
 button.events.click();audio.events.playing({target:audio});
 assert.match(button.textContent,/■ 正在播放/);assert.equal(button['aria-pressed'],'true');
 button.events.click();assert.equal(audio.paused,true);assert.equal(button['aria-pressed'],'false');
 button.events.click();audio.events.playing({target:audio});audio.events.ended();
 assert.match(button.textContent,/▶ 再听一次/);assert.match(x.el('samplePlaybackStatus').textContent,/播放完了/);
 x.el('shadowNext').events.click();assert.equal(x.el('shadowText').textContent,'swim');assert.match(button.textContent,/听单词发音/);
});
test('a cancelled pending playback cannot overwrite the next word with an error',async()=>{
 const x=await run('?chapter=four&kind=words',null,[],[
  {word:'sing',sampleAudio:{url:'/sing.mp3',kind:'recorded'}},{word:'swim',sampleAudio:{url:'/swim.mp3',kind:'recorded'}}]);
 let reject;
 x.el('referenceAudio').play=()=>new Promise((resolve,fail)=>{reject=fail;});
 x.el('playOriginal').events.click();x.el('shadowNext').events.click();reject(Error('stale playback'));await Promise.resolve();
 assert.equal(x.el('shadowText').textContent,'swim');assert.match(x.el('playOriginal').textContent,/听单词发音/);
 assert.equal(x.el('recordSelf').disabled,false);
});
test('practice choice cards switch the visible target, retain deep links and ignore the selected card',async()=>{
 const x=await run('?chapter=four',null,[{...recording,practiceClips:[{start:1,end:4}]}],
  [{word:'sing',meaning:'唱歌',sampleAudio:{url:'/sing.mp3',kind:'recorded',duration:2}}]);
 assert.equal(x.el('practicePassage')['aria-pressed'],'true');
 x.el('practiceWords').events.click();
 assert.equal(x.el('kind').value,'words');assert.equal(x.el('practiceWords')['aria-pressed'],'true');
 assert.equal(x.el('practicePassage')['aria-pressed'],'false');assert.equal(x.el('shadowText').textContent,'sing');
 assert.equal(new URL(x.location.href).searchParams.get('kind'),'words');
 x.el('playOriginal').events.click();x.el('referenceAudio').events.playing();
 x.el('practiceWords').events.click();assert.equal(x.el('playOriginal')['aria-pressed'],'true');
 x.el('practicePassage').events.click();assert.equal(x.el('referenceAudio').paused,true);
 assert.equal(x.el('practicePassage')['aria-pressed'],'true');assert.equal(x.el('kind').value,'passage');
 const y=await run('?chapter=four',null,[],[{word:'sing'}]);
 assert.equal(y.el('practicePassage').disabled,true);y.el('practicePassage').events.click();
 assert.equal(y.el('kind').value,'words');
});
