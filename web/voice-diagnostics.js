// Explicit local-only audio fixtures. Uses the normal MediaStream -> capture
// worklet -> websocket -> playback path; it never requests a physical mic.
export function createVoiceDiagnostics(hooks) {
  if (!['localhost','127.0.0.1','[::1]'].includes(location.hostname)
      || new URLSearchParams(location.search).get('diagnostics') !== '1') return null;
  const panel = document.createElement('section');
  panel.className = 'panel'; panel.style.cssText = 'margin:20px auto;padding:20px;max-width:1160px';
  const title = document.createElement('h2'); title.textContent = '本地浏览器语音诊断';
  const note = document.createElement('p');
  note.textContent = '测试音频代替麦克风，经过同一套采集与播放链路。浏览器播放计时不代表扬声器物理实听。';
  const input = document.createElement('input'); input.type = 'file'; input.accept = 'audio/wav'; input.multiple = true;
  input.setAttribute('aria-label','选择本地语音测试 WAV');
  const status = document.createElement('p'); status.setAttribute('role','status'); status.textContent = '请先选择 WAV，再拨通。';
  const files = document.createElement('div'); files.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px';
  const download = document.createElement('button'); download.type = 'button'; download.textContent = '下载浏览器诊断报告';
  const run = document.createElement('button'); run.type = 'button'; run.textContent = '完整验收：对话、插话、继续对话';
  panel.append(title,note,input,status,files,run,download); document.querySelector('main').before(panel);
  const buffers = new Map(), sources = new Set();
  let destination, armed, scenario;
  function play(name, delay = 0) {
    if (!hooks.active() || !destination) { status.textContent = '请先拨通语音通话。'; return; }
    const buffer = buffers.get(name); if (!buffer) return;
    const ctx = destination.context, source = ctx.createBufferSource(); source.buffer = buffer;
    source.connect(destination); sources.add(source);
    const start = ctx.currentTime + delay;
    hooks.emit('diagnostic_input_scheduled',{name,startCtx:start,duration:buffer.duration});
    source.onended = () => {
      sources.delete(source); source.disconnect();
      hooks.emit('diagnostic_input_ended',{name,endCtx:start+buffer.duration});
      status.textContent = `已送入：${name}，等待识别及回复。`;
    };
    source.start(start); status.textContent = `正在送入：${name}`;
  }
  input.addEventListener('change',async () => {
    try {
      const ctx = await hooks.context(); buffers.clear(); files.replaceChildren();
      for (const file of input.files) {
        const buffer = await ctx.decodeAudioData(await file.arrayBuffer()); buffers.set(file.name,buffer);
        const button = document.createElement('button'); button.type = 'button'; button.textContent = `播放：${file.name}`;
        button.onclick = () => play(file.name);
        const interrupt = document.createElement('button'); interrupt.type = 'button'; interrupt.textContent = `插话：${file.name}`;
        interrupt.onclick = () => { armed = file.name; status.textContent = `已安排：下一次回复出声 0.8 秒后插话 ${file.name}`; };
        files.append(button,interrupt);
      }
      status.textContent = `已加载 ${buffers.size} 个音频。诊断模式不会开启物理麦克风。`;
    } catch (error) { status.textContent = `音频加载失败：${error.message}`; }
  });
  run.onclick = () => {
    if (!hooks.active() || buffers.size !== 4) { status.textContent = '请加载四个 WAV 并先拨通，第三个用于插话。'; return; }
    scenario = {names:[...buffers.keys()],stage:0,turn:null}; play(scenario.names[0]);
  };
  download.onclick = () => {
    const report = hooks.report();
    if (!report) { status.textContent = '尚无通话报告。'; return; }
    report.diagnostics = {source:'browser_audio_fixture',physicalMicrophone:false,
      files:[...buffers].map(([name,buffer]) => ({name,duration:buffer.duration}))};
    const blob = new Blob([JSON.stringify(report,null,2)],{type:'application/json'});
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = `browser-voice-${report.meta.sessionId}.json`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url),10000);
    status.textContent = '诊断报告已下载。';
  };
  return {
    async stream() {
      const ctx = await hooks.context(); destination = ctx.createMediaStreamDestination();
      hooks.emit('diagnostic_stream_started',{sampleRate:ctx.sampleRate}); return destination.stream;
    },
    event(event) {
      if (scenario && event.eventType === 'asr_final' && event.attributes?.text) scenario.turn = event.turnId;
      if (event.eventType === 'audio_graph_onset' && armed) {
        const name = armed; armed = null;
        if (scenario) { scenario.stage = 2; scenario.turn = null; }
        play(name,.8);
      }
      if (scenario && event.eventType === 'playback_completed' && event.turnId === scenario.turn) {
        const names = scenario.names;
        if (scenario.stage === 0) {
          scenario.stage = 1; scenario.turn = null; armed = names[2]; play(names[1],.4);
        } else if (scenario.stage === 2) {
          scenario.stage = 3; scenario.turn = null; play(names[3],.4);
        } else if (scenario.stage === 3) {
          scenario = null; status.textContent = '完整测试结束，请下载诊断报告核对回复、打断和延迟。';
          // End the bounded fixture scenario after actual playback completes.
          // The report/recording stays available for download after hangup.
          setTimeout(() => hooks.finish?.(),0);
        }
      }
    },
    stop() {
      armed = null; scenario = null;
      for (const source of sources) { source.onended = null; try { source.stop(); } catch (_) {} source.disconnect(); }
      sources.clear(); destination?.disconnect(); destination = null;
    },
  };
}
