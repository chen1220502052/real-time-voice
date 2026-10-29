const test=require('node:test'),assert=require('node:assert/strict');
const SentencePlayer=require('../web/sentence-player.js');
class Audio {
 constructor(){this.listeners=new Map();this.paused=true;this.readyState=0;this.duration=18.1;this.currentTime=0;this.playbackRate=1;}
 addEventListener(name,fn,options={}){const listeners=this.listeners.get(name)||[];listeners.push({fn,once:options.once});this.listeners.set(name,listeners);}
 removeEventListener(name,fn){this.listeners.set(name,(this.listeners.get(name)||[]).filter(item=>item.fn!==fn));}
 emit(name){for(const item of [...(this.listeners.get(name)||[])]){if(item.once)this.removeEventListener(name,item.fn);item.fn();}}
 getAttribute(name){return this[name];}
 load(){this.readyState=0;}
 pause(){this.paused=true;}
 async play(){this.playCount=(this.playCount||0)+1;this.paused=false;this.emit('playing');}
 metadata(){this.readyState=1;this.emit('loadedmetadata');}
}
function button(){return {textContent:'▶',setAttribute(k,v){this[k]=v;}};}
const first={url:'/original.mp3',start:8.3,end:9.7};
test('plays only the requested source interval and resets the button at sentence end',async()=>{
 const audio=new Audio(),player=new SentencePlayer(audio),b=button();
 player.play(first,b);assert.equal(audio.playCount,undefined);audio.metadata();await Promise.resolve();
 assert.equal(audio.currentTime,8.3);assert.equal(audio.playCount,1);assert.equal(b['aria-pressed'],'true');
 audio.currentTime=9.7;audio.emit('timeupdate');
 assert.equal(audio.paused,true);assert.equal(b['aria-pressed'],'false');assert.equal(b.textContent,'▶');
});
test('rapid sentence changes cancel stale metadata callbacks and never overlap',async()=>{
 const audio=new Audio(),player=new SentencePlayer(audio),a=button(),b=button();
 player.play(first,a);player.play({...first,start:10.45,end:12.05},b);
 audio.metadata();await Promise.resolve();
 assert.equal(audio.currentTime,10.45);assert.equal(audio.playCount,1);assert.equal(a['aria-pressed'],'false');
 player.stop();assert.equal(audio.paused,true);assert.equal(b['aria-pressed'],'false');
});
test('clicking the active button stops and a later click restarts from its beginning',async()=>{
 const audio=new Audio(),player=new SentencePlayer(audio),b=button();
 player.play(first,b);audio.metadata();await Promise.resolve();audio.currentTime=9;
 player.play(first,b);assert.equal(audio.paused,true);
 player.play(first,b);await Promise.resolve();assert.equal(audio.currentTime,8.3);
 player.stop();
});
test('switching mode during audio loading cancels pending playback',()=>{
 const audio=new Audio(),player=new SentencePlayer(audio),b=button();
 player.play(first,b);player.stop();audio.metadata();assert.equal(audio.playCount,undefined);
 assert.equal(b['aria-pressed'],'false');
});
test('out-of-range excerpts and playback failures show a recoverable error',async()=>{
 const audio=new Audio();let errors=0;const player=new SentencePlayer(audio,{onError:()=>errors++}),b=button();
 player.play({...first,end:20},b);audio.metadata();await Promise.resolve();assert.equal(errors,1);assert.equal(audio.paused,true);
 audio.play=async()=>{throw Error('blocked');};player.play(first,b);await Promise.resolve();
 assert.equal(errors,2);assert.equal(b['aria-pressed'],'false');
});
test('speed changes keep the same media endpoint and buffering does not advance to another sentence',async()=>{
 const audio=new Audio(),player=new SentencePlayer(audio),b=button();
 player.play(first,b);audio.metadata();await Promise.resolve();audio.playbackRate=.75;audio.emit('ratechange');
 audio.emit('waiting');assert.equal(b['aria-pressed'],'true');
 audio.currentTime=9.1;audio.emit('playing');assert.equal(audio.paused,false);
 audio.currentTime=9.7;audio.emit('timeupdate');assert.equal(audio.paused,true);
});
