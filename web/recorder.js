/* Diagnostic PCM tracks, NOT physical speaker recordings. All timestamped
   chunks are rendered at their AudioContext sample position, including gaps. */
(function () {
  const SAMPLE_RATE = 16000;
  function encodeWav(samples) {
    const buffer=new ArrayBuffer(44+samples.length*2), view=new DataView(buffer);
    const string=(offset,text)=>{for(let i=0;i<text.length;i++)view.setUint8(offset+i,text.charCodeAt(i));};
    string(0,"RIFF");view.setUint32(4,36+samples.length*2,true);string(8,"WAVE");
    string(12,"fmt ");view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
    view.setUint32(24,SAMPLE_RATE,true);view.setUint32(28,SAMPLE_RATE*2,true);
    view.setUint16(32,2,true);view.setUint16(34,16,true);string(36,"data");view.setUint32(40,samples.length*2,true);
    for(let i=0;i<samples.length;i++)view.setInt16(44+i*2,samples[i],true);
    return new Blob([buffer],{type:"audio/wav"});
  }
  class ConversationRecorder {
    constructor() { this.reset(); }
    reset() { this.t0=null;this.micChunks=[];this.robotFrames=[];this.markers=[];this.meta=null;this.audioContext=null; }
    get active() { return this.t0!==null; }
    start(audioContext,meta) {
      this.reset();this.audioContext=audioContext;
      this.t0={ctx:audioContext.currentTime,performanceMs:performance.now(),timestampMs:Date.now()};
      this.meta={...meta,startedAtIso:new Date().toISOString()};
    }
    pushMic(pcm,timing={}) {
      if(!this.active)return null;
      const frame={...timing,pcm:new Int16Array(pcm),timed:Number.isFinite(timing.captureStartCtx)};
      const first=frame.pcm.findIndex(value=>Math.abs(value)>=300);
      let last=frame.pcm.length-1;while(last>=0&&Math.abs(frame.pcm[last])<300)last--;
      frame.firstSignalCtx=frame.timed&&first>=0?frame.captureStartCtx+first/SAMPLE_RATE:null;
      frame.lastSignalCtx=frame.timed&&last>=0?frame.captureStartCtx+last/SAMPLE_RATE:null;
      this.micChunks.push(frame);return frame;
    }
    pushRobot(pcm,startAtCtx,replyId) {
      if(this.active)this.robotFrames.push({startAtCtx,replyId,pcm:new Int16Array(pcm)});
    }
    stopRobot(replyId,atCtx) {
      for(const frame of this.robotFrames)if(frame.replyId===replyId)frame.stopAtCtx=Math.min(frame.stopAtCtx??Infinity,atCtx);
    }
    stopAll(atCtx) {
      for(const frame of this.robotFrames)frame.stopAtCtx=Math.min(frame.stopAtCtx??Infinity,atCtx);
    }
    mark(tag,note,audioContext) {
      if(!this.active)return null;
      const now=performance.now(),marker={tag,note:note||"",performanceMs:now,
        relMs:now-this.t0.performanceMs,timestampMs:Date.now()};
      if(audioContext)marker.ctxTime=audioContext.currentTime;
      this.markers.push(marker);return marker;
    }
    render(frames,startField,cutoff=Infinity) {
      const origin=this.t0?.ctx??0;
      const valid=frames.filter(frame=>Number.isFinite(frame[startField]));
      const end=valid.reduce((value,frame)=>Math.max(value,Math.min(
        frame[startField]+frame.pcm.length/SAMPLE_RATE,frame.stopAtCtx??Infinity,cutoff)),origin);
      const merged=new Int16Array(Math.max(0,Math.round((end-origin)*SAMPLE_RATE)));
      for(const frame of valid) {
        const start=frame[startField],stop=Math.min(start+frame.pcm.length/SAMPLE_RATE,frame.stopAtCtx??Infinity,cutoff);
        if(stop<=origin||stop<=start)continue;
        const sourceStart=Math.max(0,Math.round((origin-start)*SAMPLE_RATE));
        const destination=Math.max(0,Math.round((start-origin)*SAMPLE_RATE));
        const count=Math.max(0,Math.min(frame.pcm.length-sourceStart,
          Math.round((stop-start)*SAMPLE_RATE)-sourceStart,merged.length-destination));
        if(count)merged.set(frame.pcm.subarray(sourceStart,sourceStart+count),destination);
      }
      return merged;
    }
    micSamples() { return this.render(this.micChunks,"captureStartCtx"); }
    robotSamples() { return this.render(this.robotFrames,"startAtCtx",this.audioContext?.currentTime??Infinity); }
    replyScheduled() {
      const result={};
      for(const frame of this.robotFrames) {
        const stop=Math.min(frame.stopAtCtx??Infinity,this.audioContext?.currentTime??Infinity);
        const first=frame.pcm.findIndex(value=>Math.abs(value)>=300);
        const onset=first>=0?frame.startAtCtx+first/SAMPLE_RATE:null;
        if(frame.startAtCtx>=stop)continue;
        const r=result[frame.replyId]??={classification:"scheduled_not_physical_audio",firstFrameCtx:frame.startAtCtx,firstSignalCtx:null};
        r.firstFrameCtx=Math.min(r.firstFrameCtx,frame.startAtCtx);
        if(onset!==null&&onset<stop)r.firstSignalCtx=r.firstSignalCtx===null?onset:Math.min(r.firstSignalCtx,onset);
      }
      return result;
    }
    buildLog(events,timing) {
      const latency=timing?.report()??null;
      if(latency)for(const reply of latency.replies) {
        const index=this.micChunks.findIndex(frame=>frame.sequence===reply.sourceSignalEndSequence);
        const frame=this.micChunks[index];
        const preceding=this.micChunks.slice(0,Math.max(0,index)).reduce((total,item)=>total+item.pcm.length,0);
        const concatenatedEnd=frame&&Number.isFinite(frame.lastSignalCtx)?this.t0.ctx+preceding/SAMPLE_RATE+
          frame.lastSignalCtx-frame.captureStartCtx:null;
        reply.recordingTimelineAudit={
          counterfactual:"same_captured_frames_concatenated_at_recording_origin_not_physical_audio",
          concatenatedSignalEndCtx:concatenatedEnd,
          timelineBiasMs:concatenatedEnd===null?null:(reply.sourceSignalEndCtx-concatenatedEnd)*1000,
          oldStyleSignalToGraphMs:concatenatedEnd!==null&&Number.isFinite(reply.graphOnsetCtx)?
            (reply.graphOnsetCtx-concatenatedEnd)*1000:null,
          correctedSignalToGraphMs:reply.clientParts.captureSignalToGraphMs,
        };
      }
      return {schemaVersion:2,meta:{...this.meta,exportedAtIso:new Date().toISOString()},
        clock:{originCtxTime:this.t0.ctx,originPerformanceMs:this.t0.performanceMs,
          originTimestampMs:this.t0.timestampMs,note:"AudioContext seconds and client performance ms are distinct domains."},
        durationsMs:{mic:this.micSamples().length/SAMPLE_RATE*1000,robotScheduled:this.robotSamples().length/SAMPLE_RATE*1000},
        warnings:{untimedMicFrames:this.micChunks.filter(frame=>!frame.timed).length,
          droppedUplinkFrames:this.micChunks.filter(frame=>frame.dropped).length},
        micFrames:this.micChunks.map(({pcm,...metadata})=>metadata),
        robotFrames:this.robotFrames.map(({pcm,...metadata})=>({...metadata,samples:pcm.length})),
        markers:this.markers,replyScheduled:this.replyScheduled(),physicalOutput:null,
        measurement:"Timestamped input and scheduled PCM; not physical loopback",
        latency,events:events.map(event=>({...event}))};
    }
    exportBundle(events,sessionId,timing) {
      const stamp=new Date().toISOString().replace(/[:.]/g,"-").slice(0,19);
      const base=`recording-${sessionId||"session"}-${stamp}`;
      return [
        {name:`${base}-mic.wav`,blob:encodeWav(this.micSamples())},
        {name:`${base}-robot-scheduled.wav`,blob:encodeWav(this.robotSamples())},
        {name:`${base}-log.json`,blob:new Blob([JSON.stringify(this.buildLog(events,timing),null,2)],{type:"application/json"})}
      ];
    }
  }
  window.ConversationRecorder=ConversationRecorder;
})();
