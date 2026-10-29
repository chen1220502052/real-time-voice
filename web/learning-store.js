/* Learning evidence persisted through the account storage adapter. No recordings or chat transcripts are persisted. */
(function(root) {
  const KEY='kitty-learning:weekend-v1';
  const DAY=86400000;
  function fresh(){return {version:1,completed:{},drafts:{},attempts:{},concepts:{},lastModule:'words',listening:{plays:0,heard:false,transcript:false,slow:false,hint:false},review:null};}
  function load(storage){try{const s=JSON.parse(storage.getItem(KEY));if(!s||s.version!==1)return fresh(); const clean=fresh(); for(const k of ['completed','drafts','attempts','concepts'])if(s[k]&&typeof s[k]==='object'&&!Array.isArray(s[k]))clean[k]=s[k]; if(typeof s.lastModule==='string')clean.lastModule=s.lastModule;if(s.listening&&typeof s.listening==='object')Object.assign(clean.listening,s.listening);if(s.review&&Array.isArray(s.review.ids))clean.review=s.review;return clean;}catch{return fresh();}}
  function save(storage,state){try{storage.setItem(KEY,JSON.stringify(state));return true;}catch{return false;}}
  function record(state,q,correct,assisted=false,now=Date.now()){
    const history=state.attempts[q.id]||{count:0,firstCorrect:correct,firstAssisted:assisted};
    history.count++;history.correct=correct;history.assisted=assisted;history.lastAt=now;state.attempts[q.id]=history;
    const c=state.concepts[q.concept]||{streak:0,level:'learning',due:now,lastIndependent:0};
    if(!correct||assisted){c.level=correct?'supported':'learning';c.due=now;c.streak=0;}
    else if(!c.lastIndependent||now-c.lastIndependent>=DAY){c.streak++;c.lastIndependent=now;c.level=c.streak>=2?'retained':'practiced';c.due=now+DAY*(c.streak>=2?3:1);}
    // Immediate retries are corrections, never extra long-term mastery evidence.
    state.concepts[q.concept]=c;
  }
  function finish(state,id,now=Date.now()){state.completed[id]=state.completed[id]||now;}
  function due(state,now=Date.now()){return Object.entries(state.concepts).filter(([,c])=>c.due<=now).map(([id])=>id);}
  const api={KEY,DAY,fresh,load,save,record,finish,due};
  if(typeof module!=='undefined')module.exports=api;else root.KittyProgress=api;
})(typeof window!=='undefined'?window:globalThis);
