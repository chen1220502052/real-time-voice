const test=require('node:test'),assert=require('node:assert/strict');
const P=require('../web/learning-store.js'),U=require('../web/learning-data.js');
const NOW=1800000000000,q=U.wordQuestions[0];
test('same-day retries cannot manufacture retained knowledge',()=>{
  const s=P.fresh();P.record(s,q,true,false,NOW);
  assert.equal(s.concepts[q.concept].streak,1);
  P.record(s,q,true,false,NOW+60000);
  assert.equal(s.concepts[q.concept].streak,1);
  assert.equal(s.concepts[q.concept].level,'practiced');
  P.record(s,q,true,false,NOW+P.DAY);
  assert.equal(s.concepts[q.concept].level,'retained');
  assert.equal(s.concepts[q.concept].due,NOW+P.DAY*4);
});
test('wrong answers and help remain due; first-attempt evidence survives correction',()=>{
  const s=P.fresh();P.record(s,q,false,false,NOW);P.record(s,q,true,true,NOW+100);
  assert.equal(s.attempts[q.id].firstCorrect,false);
  assert.equal(s.attempts[q.id].count,2);
  assert.equal(s.concepts[q.concept].level,'supported');
  assert.deepEqual(P.due(s,NOW+100),[q.concept]);
  P.record(s,q,true,false,NOW+P.DAY);assert.equal(s.concepts[q.concept].streak,1);
});
test('lesson completion is idempotent and independent of mastery',()=>{
  const s=P.fresh();P.finish(s,'speaking',NOW);P.finish(s,'speaking',NOW+100);
  assert.equal(s.completed.speaking,NOW);assert.deepEqual(s.concepts,{});
});
test('refresh preserves question progress, first answers and listening aids',()=>{
  let value;const storage={getItem:()=>value,setItem:(key,v)=>{value=v;}};
  const s=P.fresh();s.drafts.words={index:2,answers:{'word-visit':{value:0,passed:true}}};s.listening.hint=true;
  P.record(s,q,true,false,NOW);assert.equal(P.save(storage,s),true);
  assert.deepEqual(P.load(storage),s);
  value='not json';assert.deepEqual(P.load(storage),P.fresh());
  assert.equal(P.save({setItem:()=>{throw Error('quota');}},s),false);
  assert.deepEqual(P.load({getItem:()=>{throw Error('blocked');}}),P.fresh());
});
test('sample questions and bundled recordings are consistent',()=>{
  const fs=require('node:fs'),path=require('node:path');
  const all=[...U.wordQuestions,...U.sentenceQuestions,...U.listening.questions,...U.reading.questions];
  assert.equal(new Set(all.map(q=>q.id)).size,all.length);
  for(const item of all){assert.ok(item.concept);if(item.options){assert.ok(item.answer>=0&&item.answer<item.options.length);assert.equal(new Set(item.options).size,item.options.length);}}
  U.wordQuestions.forEach((question,i)=>assert.equal(question.options[question.answer],U.words[i].meaning));
  const files=[...U.words,...U.sentences,U.listening,U.reading,{audio:'speaking'}];
  for(const item of files)assert.ok(fs.statSync(path.join(__dirname,'../web/audio/weekend',item.audio+'.m4a')).size>1000);
  const manifest=require('../web/audio/weekend/manifest.json');
  for(const word of U.words){
    assert.equal(manifest.clips[word.audio+'-word'],word.word);
    assert.ok(fs.statSync(path.join(__dirname,'../web/audio/weekend',word.audio+'-word.m4a')).size>1000);
  }
  assert.ok(U.sentenceQuestions[2].accepted.test('I usually read a book on Sundays.'));
  assert.ok(!U.sentenceQuestions[2].accepted.test('I usually.'));
});
