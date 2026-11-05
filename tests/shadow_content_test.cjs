const {test}=require('node:test');
const assert=require('node:assert/strict');
const S=require('../web/shadow-content.js');
test('word prompts bind only their own sample and disclose synthetic voices',()=>{
 const groups=S.groups({vocabulary:[{word:'sing',meaning:'唱歌',sampleAudio:{url:'/sing.mp3',kind:'recorded'}},
  {word:'paw',sampleAudio:{url:'/paw.wav',kind:'synthetic'}},{word:'missing'}]},'words');
 assert.equal(groups[0].targets[0].source.url,'/sing.mp3');
 assert.equal(groups[0].targets[1].sourceLabel,'合成示范发音');
 assert.equal(groups[0].targets[2].source,null);
});
test('a six-minute lesson never becomes a follow target; short ranges retain exact source',()=>{
 const c={recordings:[{id:'full',kind:'passage',title:'整课录音',url:'/full.mp3',fullLesson:true,duration:407},
  {id:'dialogue',kind:'passage',practiceTitle:'对话 · 你会游泳吗？',url:'/talk.mp3',practiceClips:[{start:3,end:9},{start:10,end:15},{start:20,end:60}]}],
  textbookPages:[{id:'p',url:'/page.jpg',recordingId:'dialogue'}]};
 const groups=S.groups(c,'passage');assert.equal(groups.length,1);
 assert.equal(groups[0].label,'对话 · 你会游泳吗？');assert.equal(groups[0].targets.length,2);
 assert.deepEqual(groups[0].targets[0].source,{url:'/talk.mp3',start:3,end:9});
 assert.equal(groups[0].targets[0].page.url,'/page.jpg');
});
test('verified sentence text takes priority over generic audio chunks',()=>{
 const c={recordings:[{id:'r',kind:'passage',title:'Listen',url:'/r.mp3',practiceClips:[{start:0,end:10}]}],
 textbookPages:[{recordingId:'r',scenes:[{lines:[{text:'Can you swim?',start:4,end:6}]}]}]};
 const target=S.groups(c,'passage')[0].targets[0];assert.equal(target.label,'Can you swim?');
 assert.equal(target.duration,2);assert.equal(target.source.start,4);
});
test('missing short excerpts produce an honest empty state, not a whole-audio fallback',()=>{
 assert.deepEqual(S.groups({recordings:[{kind:'passage',title:'教材分段 1',url:'/long.mp3'}]},'passage'),[]);
});
test('aligned sentences preserve exact source ranges and disclose local synthesis',()=>{
 const c={recordings:[{id:'tts',kind:'passage',title:'教材示范句 · 本地合成',url:'/sample.wav',
  audioKind:'synthetic',sourceLabel:'本地 AI 合成示范音',
  alignedSentences:[{text:'I like my cat.',start:0,end:2.8,chinese:'我喜欢我的猫。'}]}]};
 const t=S.groups(c,'passage')[0].targets[0];
 assert.equal(t.label,'I like my cat.');assert.equal(t.type,'sentence');
 assert.equal(t.sourceLabel,'本地 AI 合成示范音');
 assert.deepEqual(t.source,{url:'/sample.wav',start:0,end:2.8});
});
