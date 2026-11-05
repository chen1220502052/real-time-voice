const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
test('only active calls accept user markers while an ended recorder remains exportable',()=>{
 const nodes={'#tagFeedback':{},'#tagNote':{value:'my note'}},markers=[];
 const c=vm.createContext({sessionState:'Closed',conversationRecorder:{active:true,recording:true,mark:(...args)=>{markers.push(args);return {relMs:1000};}},
  $:id=>nodes[id],audioContext:{},TAG_LABELS:{good:'体验好'}});
 vm.runInContext(source.slice(source.indexOf('function tagCurrentConversation('),source.indexOf('\ndocument.querySelectorAll("[data-tag]")')),c);
 for(const state of ['Idle','Starting','Closing','Closed','Error']){
  c.sessionState=state;c.tagCurrentConversation('good');assert.equal(markers.length,0);
  assert.equal(nodes['#tagFeedback'].textContent,'接通并开启录音后才能打点');
 }
 c.sessionState='Active';c.tagCurrentConversation('good');assert.equal(markers.length,1);
 assert.equal(markers[0][1],'my note');assert.match(nodes['#tagFeedback'].textContent,/已打标/);
 c.conversationRecorder.recording=false;c.tagCurrentConversation('good');assert.equal(markers.length,1);
 assert.equal(c.conversationRecorder.active,true);
});
