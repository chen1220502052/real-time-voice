const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/learning.js','utf8');
function fixture(){
  const audios=[],messages=[],timers=new Map();let timerId=0;
  const buttons=['visit','visit-word'].map(audio=>({
    dataset:{audio,label:audio},attributes:{},span:{},
    setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k];},
    querySelector(){return this.span;}
  }));
  const document={body:{appendChild(a){audios.push(a);}},
    querySelectorAll(selector){return selector==='audio'?audios.filter(a=>!a.removed):buttons;},
    createElement(){return {paused:true,play(){this.paused=false;return new Promise((resolve,reject)=>{this.reject=reject;});},
      pause(){this.paused=true;},removeAttribute(){},load(){},remove(){this.removed=true;}};}};
  const context=vm.createContext({document,recorder:null,audioURL:n=>`/audio/weekend/${n}.m4a`,
    toast:t=>messages.push(t),setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(source.slice(source.indexOf('let demonstration,'),source.indexOf('function cleanRecording(')),context);
  vm.runInContext('bindAudio()',context);
  return {buttons,audios,messages,timers,stop:()=>vm.runInContext('stopAudio()',context)};
}
test('word and combined playback show state and support stopping and switching',()=>{
  const f=fixture(),[combined,word]=f.buttons;
  combined.onclick();const first=f.audios[0];
  assert.equal(first.src,'/audio/weekend/visit.m4a');assert.equal(combined.dataset.playback,'loading');
  first.onplaying();assert.equal(combined.dataset.playback,'playing');assert.equal(f.timers.size,0);
  word.onclick();const second=f.audios[1];
  assert.ok(first.paused&&first.removed);assert.equal(combined.attributes['aria-pressed'],'false');
  assert.equal(second.src,'/audio/weekend/visit-word.m4a');
  second.onplaying();word.onclick();
  assert.ok(second.paused&&second.removed);assert.equal(word.dataset.playback,'');
  assert.equal(f.timers.size,0);
});
test('late failure from a replaced clip cannot stop the current clip',async()=>{
  const f=fixture();f.buttons[0].onclick();const first=f.audios[0];
  f.buttons[1].onclick();const current=f.audios[1];current.onplaying();
  first.reject(Error('interrupted'));await Promise.resolve();
  assert.equal(current.paused,false);assert.equal(f.messages.length,0);
  current.onended();assert.ok(current.removed);assert.equal(f.buttons[1].attributes['aria-pressed'],'false');
});
test('load errors and network timeout restore a retryable button',()=>{
  const f=fixture();f.buttons[0].onclick();f.audios[0].onerror();
  assert.equal(f.messages.length,1);assert.equal(f.buttons[0].dataset.playback,'');
  f.buttons[0].onclick();[...f.timers.values()][0]();
  assert.equal(f.messages.length,2);assert.ok(f.audios[1].removed);assert.equal(f.timers.size,0);
  f.buttons[1].onclick();f.stop();assert.equal(f.timers.size,0);assert.ok(f.audios[2].removed);
});
