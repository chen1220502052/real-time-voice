/* Local-only audio review. Imported room recordings have their own file clock. */
(function () {
  const finite=Number.isFinite;
  const ms=value=>finite(value)?`${Math.round(value)} ms`:'—';
  function measurement(end,start,duration,acoustic=false) {
    if(![end,start,duration].every(finite)||end<0||start<0||end>duration||start>duration)throw Error('标记必须在录音时间范围内');
    return {classification:acoustic?'user_labeled_acoustic':'audio_reference',method:'manual_two_markers',
      sourceEndMs:Math.round(end*1000000)/1000,responseStartMs:Math.round(start*1000000)/1000,
      gapMs:Math.round((start-end)*1000000)/1000};
  }
  class RecordingReview {
    constructor(root,recorder,hooks) {
      this.root=root;this.recorder=recorder;this.hooks=hooks;this.urls=[];this.source=null;this.annotations=[];this.rows=[];
      this.$=id=>root.querySelector(`#${id}`);
      this.player=this.$('reviewAudio');this.canvas=this.$('reviewWave');
      this.$('refreshRecording').onclick=()=>this.refresh();
      this.$('importRoomAudio').onchange=event=>this.importFile(event.target.files[0]);
      this.$('reviewSource').onchange=()=>this.selectSource();
      this.$('reviewZoom').onchange=()=>this.draw();
      this.$('reviewCursor').oninput=event=>this.seek(Number(event.target.value));
      this.$('reviewCursorNumber').oninput=event=>this.seek(Number(event.target.value));
      this.$('reviewCursorNumber').onblur=()=>this.updateCursor();
      this.player.ontimeupdate=()=>{this.updateCursor();this.draw();};
      this.canvas.onclick=event=>{if(!this.source)return;const rect=this.canvas.getBoundingClientRect(),view=this.viewport();this.seek(view.start+(event.clientX-rect.left)/rect.width*view.span);};
      for(const which of ['sourceEnd','responseStart'])this.$(`mark-${which}`).onclick=()=>{this.$(which).value=this.player.currentTime.toFixed(3);this.updateMeasurement();};
      for(const which of ['sourceEnd','responseStart'])this.$(which).oninput=()=>this.updateMeasurement();
      this.$('acousticDeclaration').onchange=()=>this.updateMeasurement();
      this.$('saveMeasurement').onclick=()=>this.saveMeasurement();
      this.$('downloadMeasurements').onclick=()=>this.downloadAnnotations();
      this.resizeObserver=new ResizeObserver(()=>this.draw());this.resizeObserver.observe(this.canvas);
    }
    note(text) { this.$('reviewFeedback').textContent=text; }
    reset() { for(const url of this.urls)URL.revokeObjectURL(url);this.urls=[];this.source=null;this.internal=null;this.external=null;this.annotations=[];this.rows=[];this.player.removeAttribute('src');this.player.load();this.$('reviewRows').replaceChildren();this.$('measurementList').replaceChildren();this.$('reviewSource').value='session';this.clearMarkers();this.draw(); }
    clearMarkers() { this.$('sourceEnd').value='';this.$('responseStart').value='';this.$('acousticDeclaration').checked=false;this.updateMeasurement(); }
    async refresh() {
      if(!this.recorder.active){this.note('接通并录音后，可在这里回放；也可直接导入同场录音。');return;}
      const stereo=this.recorder.stereoSamples();
      if(!stereo.length){this.note('还没有录到音频');return;}
      if(this.internal?.url)URL.revokeObjectURL(this.internal.url);
      const url=URL.createObjectURL(window.RecordingAudio.wav(stereo,2));this.urls.push(url);
      // Keep PCM16 for the waveform: no additional full-length float copies.
      this.internal={url,pcm:stereo,channels:2,rate:16000,duration:stereo.length/32000,name:'本次通话 · 同步双声道'};
      const end=this.recorder.endCtx??Infinity,origin=this.recorder.t0.ctx;
      const log=this.recorder.buildLog(this.hooks.events(),this.hooks.timing());this.warnings=log.warnings;
      this.rows=(log.latency?.replies||[])
        .filter(row=>[row.sourceSignalEndCtx,row.graphOnsetCtx].some(t=>finite(t)&&t>=origin&&t<end));
      this.renderRows();
      if(this.$('reviewSource').value==='session')this.selectSource();
      this.note(this.recorder.recording?'已载入当前快照；挂断后点击刷新可查看完整录音。':'录音已就绪。左声道是你，右声道是 AI 实际渲染音频。');
    }
    async importFile(file) {
      if(!file)return;
      if(file.size>80*1024*1024){this.note('请选择小于 80 MB、10 分钟以内的录音');return;}
      this.$('importRoomAudio').disabled=true;
      try {
        const context=await this.hooks.audioContext();
        const buffer=await context.decodeAudioData(await file.arrayBuffer());
        if(buffer.duration>600)throw Error('录音超过 10 分钟，请先剪辑后导入');
        if(this.external?.url)URL.revokeObjectURL(this.external.url);
        const url=URL.createObjectURL(file);this.urls.push(url);
        this.external={url,buffer,channels:buffer.numberOfChannels,rate:buffer.sampleRate,duration:buffer.duration,name:file.name,file};
        this.$('reviewSource').value='external';this.selectSource();this.note(`已在本机打开「${file.name}」，未上传。请确认录音同时包含你和扬声器的声音。`);
      }catch(error){this.note(`无法读取录音：${error.message}`);}
      finally{this.$('importRoomAudio').disabled=false;this.$('importRoomAudio').value='';}
    }
    selectSource() {
      this.source=this.$('reviewSource').value==='external'?this.external:this.internal;
      this.player.pause();this.clearMarkers();
      if(!this.source){this.player.removeAttribute('src');this.player.load();this.note('此来源尚未载入，请先刷新本次录音或导入音频。');this.draw();return;}
      this.player.src=this.source.url;
      this.$('reviewCursor').max=this.source.duration;this.$('reviewCursorNumber').max=this.source.duration;
      this.$('acousticOption').hidden=this.source!==this.external;
      this.$('reviewSourceLabel').textContent=this.source===this.internal?'左：我的声音 · 右：AI 渲染音频（不含设备出声延迟）':`同场录音：${this.source.name}`;
      this.updateCursor();this.draw();
    }
    seek(value) { if(!this.source||!finite(value))return;this.player.currentTime=Math.max(0,Math.min(value,this.source.duration));this.updateCursor();this.draw(); }
    updateCursor() { const t=this.player.currentTime||0;this.$('reviewCursor').value=t;const number=this.$('reviewCursorNumber');if(document.activeElement!==number)number.value=t.toFixed(3); }
    viewport() { const duration=this.source?.duration||1,zoom=Number(this.$('reviewZoom').value),span=Math.min(duration,zoom||duration);return {span,start:Math.max(0,Math.min((this.player.currentTime||0)-span/2,duration-span))}; }
    draw() {
      const canvas=this.canvas,w=Math.max(300,canvas.clientWidth),h=200,ratio=window.devicePixelRatio||1;
      canvas.width=Math.round(w*ratio);canvas.height=h*ratio;
      const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);ctx.fillStyle='#f6f8f6';ctx.fillRect(0,0,w,h);
      if(!this.source){ctx.fillStyle='#64766c';ctx.font='14px sans-serif';ctx.fillText('载入录音后显示波形',16,100);return;}
      const {start,span}=this.viewport(),source=this.source,channels=Math.min(source.channels,2);
      for(let channel=0;channel<channels;channel++){
        const center=25+(h-50)*(channel+.5)/channels,scale=(h-55)/channels/2;
        const data=source.buffer?.getChannelData(channel),total=source.pcm?source.pcm.length/source.channels:data.length;
        ctx.strokeStyle=channel?'#ba713e':'#258878';ctx.beginPath();
        for(let x=0;x<w;x++){
          const a=Math.max(0,Math.floor((start+x/w*span)*source.rate)),b=Math.min(total,Math.max(a+1,Math.ceil((start+(x+1)/w*span)*source.rate)));
          let peak=0;for(let i=a;i<b;i++)peak=Math.max(peak,Math.abs(source.pcm?source.pcm[i*source.channels+channel]/32768:data[i]));
          ctx.moveTo(x,center-peak*scale);ctx.lineTo(x,center+Math.max(.4,peak*scale));
        }ctx.stroke();
      }
      ctx.font='12px sans-serif';ctx.fillStyle='#53655b';
      for(let i=0;i<=4;i++){const t=start+span*i/4;ctx.fillText(`${t.toFixed(2)}s`,Math.min(w-55,Math.max(0,w*i/4)),h-5);}
      const line=(t,color)=>{if(!finite(t)||t<start||t>start+span)return;const x=(t-start)/span*w;ctx.strokeStyle=color;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h-20);ctx.stroke();};
      line(this.player.currentTime,'#263e33');line(this.readMark('sourceEnd'),'#258878');line(this.readMark('responseStart'),'#ba713e');
    }
    readMark(id) { const value=this.$(id).value;return value.trim()===''?null:Number(value); }
    currentMeasurement() { if(!this.source)throw Error('请先载入录音');return measurement(this.readMark('sourceEnd'),this.readMark('responseStart'),this.source.duration,this.source===this.external&&this.$('acousticDeclaration').checked); }
    updateMeasurement() {
      try{const value=this.currentMeasurement();this.$('measurementResult').textContent=`${value.classification==='user_labeled_acoustic'?'人工实听间隔':'录音参考间隔'}：${ms(value.gapMs)}${value.gapMs<0?'（声音重叠）':''}`;this.$('saveMeasurement').disabled=false;}
      catch{this.$('measurementResult').textContent='标记“我说完”和“AI 出声”，计算两点间隔';this.$('saveMeasurement').disabled=true;}
      this.draw();
    }
    saveMeasurement() {
      try {
        const value={...this.currentMeasurement(),sourceFile:this.source.name,sampleRate:this.source.rate,
          createdAtIso:new Date().toISOString(),replyId:this.$('measurementReply').value||null};
        this.annotations.push(value);if(this.recorder.active)this.recorder.addMeasurement(value);
        const li=document.createElement('li');li.textContent=`${value.classification==='user_labeled_acoustic'?'实听（人工标记）':'参考'} ${ms(value.gapMs)} · ${value.sourceEndMs/1000}s → ${value.responseStartMs/1000}s`;
        this.$('measurementList').append(li);this.note('已保存本条标注，可继续标记下一轮或下载标注。');
      }catch(error){this.note(error.message);}
    }
    annotationFile() { return {name:'manual-measurements.json',blob:new Blob([JSON.stringify({schemaVersion:1,measurements:this.annotations,note:'File-relative manual markers; not automatically calibrated to server or AudioContext clocks.'},null,2)],{type:'application/json'})}; }
    extraFiles() { const files=[this.annotationFile()];if(this.external?.file)files.push({name:`room-${this.external.file.name}`,blob:this.external.file});return files; }
    downloadAnnotations() { this.hooks.download(this.annotationFile()); }
    renderRows() {
      const body=this.$('reviewRows'),select=this.$('measurementReply');body.replaceChildren();select.replaceChildren(new Option('不关联轮次',''));
      const origin=this.recorder.t0.ctx;
      this.rows.forEach((row,index)=>{
        const tr=document.createElement('tr'),parts=row.serverParts||{},client=row.clientParts||{};
        const question=[...this.recorder.savedEvents].reverse().find(e=>e.turnId===row.turnId&&e.eventType==='asr_final');
        const title=question?.attributes?.text||`第 ${index+1} 轮`;
        select.add(new Option(`第 ${index+1} 轮 · ${String(title).slice(0,24)}`,row.replyId));
        const stopped=finite(row.stopCtx)||this.recorder.savedEvents.some(e=>e.replyId===row.replyId&&e.eventType==='playback_stopped');
        const graphRecorded=finite(row.graphOnsetCtx)&&row.graphOnsetCtx<(this.recorder.endCtx??Infinity);
        const cells=[`${index+1}`,graphRecorded?ms(client.captureSignalToGraphMs):'—',ms(parts.sourceEndFrameToFinalMs),ms(parts.botToFirstTextMs),ms(parts.ttsToFirstPcmMs),stopped?'已打断':graphRecorded?'已出声':'未录到回复'];
        for(const text of cells){const td=document.createElement('td');td.textContent=text;tr.append(td);}
        const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.className='text-button';button.textContent='定位回放';button.disabled=!finite(row.sourceSignalEndCtx);
        button.onclick=()=>{this.$('reviewSource').value='session';this.selectSource();this.seek(row.sourceSignalEndCtx-origin-1);this.$('measurementReply').value=row.replyId;this.player.play().catch(()=>{});};td.append(button);tr.append(td);body.append(tr);
      });
      const warnings=this.warnings||{},drops=(warnings.droppedUplinkFrames||0)+(warnings.serverInputDropEvents||0);
      this.$('reviewRoundCount').textContent=`${this.rows.length} 轮 · 下表为程序测量，实听请用同场录音标注${drops?` · 注意：发现 ${drops} 次丢帧或队列溢出，请结合原始录音核对` :''}`;
    }
  }
  RecordingReview.measurement=measurement;
  window.RecordingReview=RecordingReview;
})();
