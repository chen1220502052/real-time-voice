/* Diagnostic PCM tracks, NOT physical speaker recordings. All timestamped
   chunks are rendered at their AudioContext sample position, including gaps. */
(function () {
  const SAMPLE_RATE = 16000;
  function encodeWav(samples,channels=1) {
    const buffer=new ArrayBuffer(44+samples.length*2), view=new DataView(buffer);
    const string=(offset,text)=>{for(let i=0;i<text.length;i++)view.setUint8(offset+i,text.charCodeAt(i));};
    string(0,"RIFF");view.setUint32(4,36+samples.length*2,true);string(8,"WAVE");
    string(12,"fmt ");view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,channels,true);
    view.setUint32(24,SAMPLE_RATE,true);view.setUint32(28,SAMPLE_RATE*2*channels,true);
    view.setUint16(32,2*channels,true);view.setUint16(34,16,true);string(36,"data");view.setUint32(40,samples.length*2,true);
    for(let i=0;i<samples.length;i++)view.setInt16(44+i*2,samples[i],true);
    return new Blob([buffer],{type:"audio/wav"});
  }
  function captureFrame(pcm,timing={}) {
    const frame={...timing,timed:Number.isFinite(timing.captureStartCtx)};
    const first=pcm.findIndex(value=>Math.abs(value)>=300);
    let last=pcm.length-1;while(last>=0&&Math.abs(pcm[last])<300)last--;
    frame.firstSignalCtx=frame.timed&&first>=0?frame.captureStartCtx+first/SAMPLE_RATE:null;
    frame.lastSignalCtx=frame.timed&&last>=0?frame.captureStartCtx+last/SAMPLE_RATE:null;
    return frame;
  }
  function resample(input,rate) {
    const pcm=new Int16Array(Math.round(input.length*SAMPLE_RATE/rate));
    for(let i=0;i<pcm.length;i++){const x=i*rate/SAMPLE_RATE,j=Math.floor(x),f=x-j;
      pcm[i]=Math.round(Math.max(-1,Math.min(1,(input[j]||0)*(1-f)+(input[Math.min(j+1,input.length-1)]||0)*f))*32767);}
    return pcm;
  }
  async function zip(files) {
    const parts=[],central=[];let offset=0,centralSize=0;
    const crcTable=Uint32Array.from({length:256},(_,i)=>{for(let k=0;k<8;k++)i=(i>>>1)^((i&1)?0xedb88320:0);return i>>>0;});
    for(const file of files){
      const name=new TextEncoder().encode(file.name.replace(/[\\/]/g,'_'));
      const data=new Uint8Array(await file.blob.arrayBuffer());let crc=0xffffffff;
      for(const byte of data)crc=(crc>>>8)^crcTable[(crc^byte)&255];
      crc=(crc^0xffffffff)>>>0;
      const header=new Uint8Array(30+name.length),h=new DataView(header.buffer);
      h.setUint32(0,0x04034b50,true);h.setUint16(4,20,true);h.setUint16(6,0x800,true);
      h.setUint32(14,crc,true);h.setUint32(18,data.length,true);h.setUint32(22,data.length,true);h.setUint16(26,name.length,true);header.set(name,30);
      const entry=new Uint8Array(46+name.length),c=new DataView(entry.buffer);
      c.setUint32(0,0x02014b50,true);c.setUint16(4,20,true);c.setUint16(6,20,true);c.setUint16(8,0x800,true);
      c.setUint32(16,crc,true);c.setUint32(20,data.length,true);c.setUint32(24,data.length,true);c.setUint16(28,name.length,true);c.setUint32(42,offset,true);entry.set(name,46);
      parts.push(header,data);central.push(entry);offset+=header.length+data.length;centralSize+=entry.length;
    }
    const end=new Uint8Array(22),e=new DataView(end.buffer);e.setUint32(0,0x06054b50,true);
    e.setUint16(8,files.length,true);e.setUint16(10,files.length,true);e.setUint32(12,centralSize,true);e.setUint32(16,offset,true);
    return new Blob([...parts,...central,end],{type:'application/zip'});
  }
  class ConversationRecorder {
    constructor() { this.reset(); }
    reset() { this.t0=null;this.micChunks=[];this.robotFrames=[];this.renderedFrames=[];this.savedEvents=[];this.measurements=[];this.markers=[];this.meta=null;this.audioContext=null;this.revision=0;this.exportedRevision=0;this.recording=false;this.endCtx=null;this.limitReached=false;this.maxSeconds=600; }
    get active() { return this.t0!==null; }
    get hasUnexportedChanges() { return (this.micChunks.length>0||this.renderedFrames.length>0||this.measurements.length>0) && this.revision!==this.exportedRevision; }
    markExported(revision=this.revision) { this.exportedRevision=revision; }
    start(audioContext,meta) {
      this.reset();this.audioContext=audioContext;
      this.t0={ctx:audioContext.currentTime,performanceMs:performance.now(),timestampMs:Date.now()};
      this.meta={...meta,startedAtIso:new Date().toISOString()};
      this.recording=true;
    }
    stop(atCtx=this.audioContext?.currentTime) { if(!this.recording)return;this.endCtx=Math.max(this.t0.ctx,Math.min(atCtx??this.t0.ctx,this.t0.ctx+this.maxSeconds));this.recording=false;this.revision++; }
    accept(atCtx) { if(!this.recording)return false;if(Number.isFinite(atCtx)&&atCtx>=this.t0.ctx+this.maxSeconds){this.limitReached=true;this.stop(this.t0.ctx+this.maxSeconds);return false;}return true; }
    pushMic(pcm,timing={}) {
      if(!this.active||!this.accept(timing.captureStartCtx))return null;
      const frame={...captureFrame(pcm,timing),pcm:new Int16Array(pcm)};
      this.micChunks.push(frame);this.revision++;return frame;
    }
    pushRobot(pcm,startAtCtx,replyId) {
      if(this.active&&this.accept(startAtCtx)){this.robotFrames.push({startAtCtx,replyId,pcm:new Int16Array(pcm)});this.revision++;}
    }
    pushRendered(input,startCtx,rate,id) {
      if(!this.active||id!==this.meta.sessionId)return;
      if(this.recording)this.accept(startCtx);
      if(startCtx<(this.endCtx??this.t0.ctx+this.maxSeconds)){
        this.renderedFrames.push({captureStartCtx:startCtx,pcm:resample(input,rate)});this.revision++;
      }
    }
    pushEvent(event) { if(this.recording&&!event.attributes?.highFrequency&&this.savedEvents.length<10000)this.savedEvents.push({...event}); }
    addMeasurement(value) { this.measurements.push({...value});this.revision++; }
    stopRobot(replyId,atCtx) {
      for(const frame of this.robotFrames)if(frame.replyId===replyId){const stop=Math.min(frame.stopAtCtx??Infinity,atCtx);if(stop!==frame.stopAtCtx){frame.stopAtCtx=stop;this.revision++;}}
    }
    stopAll(atCtx) {
      for(const frame of this.robotFrames){const stop=Math.min(frame.stopAtCtx??Infinity,atCtx);if(stop!==frame.stopAtCtx){frame.stopAtCtx=stop;this.revision++;}}
    }
    mark(tag,note,audioContext) {
      if(!this.active)return null;
      const now=performance.now(),marker={tag,note:note||"",performanceMs:now,
        relMs:now-this.t0.performanceMs,timestampMs:Date.now()};
      if(audioContext)marker.ctxTime=audioContext.currentTime;
      this.markers.push(marker);this.revision++;return marker;
    }
    render(frames,startField,cutoff=Infinity) {
      const origin=this.t0?.ctx??0;
      const valid=frames.filter(frame=>Number.isFinite(frame[startField]));
      cutoff=Math.min(cutoff,this.endCtx??(origin+this.maxSeconds));
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
    renderedSamples() { return this.render(this.renderedFrames,'captureStartCtx'); }
    stereoSamples() {
      const mic=this.micSamples(),output=this.renderedSamples(),samples=new Int16Array(Math.max(mic.length,output.length)*2);
      for(let i=0;i<samples.length/2;i++){samples[i*2]=mic[i]||0;samples[i*2+1]=output[i]||0;}
      return samples;
    }
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
      const allEvents=new Map();for(const event of [...this.savedEvents,...(events||[])])allEvents.set(event.eventId??event,event);
      return {schemaVersion:3,meta:{...this.meta,exportedAtIso:new Date().toISOString()},
        clock:{originCtxTime:this.t0.ctx,originPerformanceMs:this.t0.performanceMs,
          originTimestampMs:this.t0.timestampMs,note:"AudioContext seconds and client performance ms are distinct domains."},
        durationsMs:{mic:this.micSamples().length/SAMPLE_RATE*1000,robotScheduled:this.robotSamples().length/SAMPLE_RATE*1000},
        warnings:{untimedMicFrames:this.micChunks.filter(frame=>!frame.timed).length,
          droppedUplinkFrames:this.micChunks.filter(frame=>frame.dropped).length,
          serverInputDropEvents:[...allEvents.values()].filter(e=>e.eventType==='queue_overflow'&&e.attributes?.queue==='input').length},
        micFrames:this.micChunks.map(({pcm,...metadata})=>metadata),
        robotFrames:this.robotFrames.map(({pcm,...metadata})=>({...metadata,samples:pcm.length})),
        recording:{ongoing:this.recording,endCtx:this.endCtx,maxSeconds:this.maxSeconds,limitReached:this.limitReached,
          renderedChunks:this.renderedFrames.length,renderedSamples:this.renderedFrames.reduce((n,f)=>n+f.pcm.length,0),
          renderedClassification:'audio_context_graph_not_physical_speaker',stereoChannels:['microphone','rendered_output']},
        manualMeasurements:this.measurements,markers:this.markers,replyScheduled:this.replyScheduled(),physicalOutput:null,
        measurement:"Timestamped microphone, scheduled PCM and rendered audio graph; not physical loopback",
        latency,events:[...allEvents.values()]};
    }
    exportSession(events,timing) {
      const files=this.exportBundle(events,this.meta.sessionId,timing);
      const base=files[0].name.replace(/-mic\.wav$/,'');
      files.push({name:base+'-robot-rendered.wav',blob:encodeWav(this.renderedSamples())},
        {name:base+'-stereo.wav',blob:encodeWav(this.stereoSamples(),2)});
      return files;
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
  window.RecordingAudio={wav:encodeWav,zip,resample,captureFrame};
})();
