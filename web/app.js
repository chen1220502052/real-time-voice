const $ = (selector) => document.querySelector(selector);
const callButton = $("#callButton");
const gradeSelect = $("#gradeSelect");
const statusLabel = $("#status");
const cat = $("#cat");
const events = [];
const eventComponents = new Set();
const eventTurns = new Set();
const replyElements = new Map();
const replyTexts = new Map();
const eventTimes = new Map();
const interruptedReplyIds = new Set();
const countedErrorKeys = new Set();

let socket, sessionId, sessionHash, mediaStream, audioContext, captureNode, captureSource, muteNode, hangupTimer, stageTimer, microphoneWatchdog;
let connectionAttempt;
let disconnectKey;
let inputSequence = 0, expectedDownSequence = 0, nextPlaybackTime = 0, sessionStartedAt, sessionTimerHandle;
let messageCount = 0, currentTurnId, sessionState = "Idle", expectedClose = false;
let microphoneState = "Idle", lastUplinkAt = 0, uplinkDropCount = 0, uplinkBackpressureActive = false, callAttempt = 0;
let playbackStarted = false, activePlaybackReply;
const avatarFrames = new Map(), pendingAudioFrames = [], playbackFrames = [];
const scheduledSources = new Set(), mouthTimers = new Set(), completionTimers = new Set();
const playbackStartedReplies = new Set(), underrunReplies = new Set();
let timelineRenderTimer;
const FRAME_SECONDS = 0.1, PLAYBACK_PREROLL_FRAMES = 3;
const latencyTrace = new window.LatencyTrace();
let outputObserver, outputObserverPromise;
const clockProbeTimers = new Set();
const MAX_UPLINK_BUFFERED_BYTES = 3224 * 10;
const UPLINK_STALL_MS = 3000;
const STAGE_TIMEOUT_MS = { websocket: 15000, prepare: 8000, createSession: 8000, microphone: 8000, audioWorklet: 5000 };
const ERROR_ACTIONS = {
  MIC_PERMISSION_DENIED: "请在浏览器地址栏允许麦克风权限后重新拨通。",
  MIC_PERMISSION_TIMEOUT: "请处理浏览器的麦克风权限提示，然后重新拨通。",
  MIC_DEVICE_NOT_FOUND: "请连接输入设备，并在系统设置中确认浏览器可以使用它。",
  MIC_DEVICE_BUSY: "请关闭正在独占麦克风的程序后重新拨通。",
  MIC_DEVICE_LOST: "请重新连接麦克风后再次拨通。",
  MIC_NO_UPLINK: "采集已启动但没有音频包发出，请重新拨通或检查输入设备。",
  AUDIO_WORKLET_UNAVAILABLE: "请使用最新版 Chrome、Edge 或 Safari。",
  AUDIO_SEQUENCE_ERROR: "挂断后重新拨通可重建上行音频流。",
  QUEUE_OVERFLOW: "可切换 Stub 模式或更轻量模型。",
  ASR_FAILED: "检查模型文件与 ASR_BACKEND 配置。",
  LLM_FAILED: "检查文本模型服务、API 凭证和网络连接。",
  TTS_FAILED: "检查 Piper 模型文件与 TTS_BACKEND 配置。",
  REPLY_FAILED: "检查 Ollama、TTS 服务与模型配置。",
  CONNECTION_FAILED: "检查网络后重试；若仍失败，请刷新页面重新登录。",
  WEBSOCKET_TIMEOUT: "连接未在 15 秒内建立，请检查网络后重试。",
  AUTH_REQUIRED: "请刷新页面，在登录提示中重新输入试用账号。",
  AUTH_TIMEOUT: "登录验证未及时完成，请检查网络后重试。",
  AUTH_UNAVAILABLE: "登录服务暂时不可用，请稍后重试。",
  NO_RESOURCE: "已有其他通话占满名额，稍后点击拨通即可，无需更换账号。",
  PREPARE_TIMEOUT: "后端未及时返回资源状态，请检查模型预热和服务日志。",
  CREATE_SESSION_TIMEOUT: "后端未及时建立会话，请检查资源占用和服务日志。",
};

function setStatus(text) { statusLabel.textContent = text; }
function setSessionState(value) { sessionState = value; if(gradeSelect)gradeSelect.disabled=["Connecting","Ready","Starting","Active","Closing"].includes(value)||Boolean(window.KittyVoiceChapter); const el = $("#sessionState"); el.textContent = value; el.className = `state-pill ${value === "Active" ? "active" : value === "Error" ? "error" : "idle"}`; const hints={Idle:"点击开始，随时说话即可打断",Connecting:"正在建立连接",Ready:"正在准备语音会话",Starting:"正在启动麦克风",Active:"直接开口，需要时可随时打断",Closing:"正在结束本次通话",Closed:"点击开始一段新对话",Error:"请处理提示后重新拨通"}; $("#statusHint").textContent=hints[value]||hints.Idle; }
function setUserState(value) { $("#userState").textContent = value; }
function setRobotState(value) { $("#robotState").textContent = value; $("#cat").classList.toggle("speaking", ["Thinking", "Synthesizing", "Speaking"].includes(value)); }
function setMicrophoneState(value) { microphoneState = value; const labels = { Idle:"未启动", RequestingPermission:"等待权限", Capturing:"采集中", Muted:"已静音", Failed:"异常" }; $("#micState").textContent = labels[value] || value; }
function setConnection(value, tone = "neutral") { const el = $("#connectionBadge"); el.className = `badge ${tone}`; el.replaceChildren(document.createElement("i"), document.createTextNode(value)); }
function setMouth(state) { cat.classList.toggle("mouth-open", state === "mouth_open"); cat.classList.toggle("mouth-closed", state !== "mouth_open"); }
function setCurrentTurn(turnId) { if (turnId) { currentTurnId = turnId; $("#turnId").textContent = turnId; } }
function formatDuration(ms) { const total = Math.max(0, Math.floor(ms / 1000)); return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`; }
function updateSessionTimer() { $("#sessionTimer").textContent = sessionStartedAt ? formatDuration(Date.now() - sessionStartedAt) : "00:00"; }

function commonEvent(eventType, component, options = {}) {
  return { eventId: `evt_client_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`, eventType, component, level: options.level || "info", sessionId: sessionId || null, turnId: options.turnId || currentTurnId || null, replyId: options.replyId || null, generationEpoch: options.generationEpoch ?? null, sequence: options.sequence ?? null, timestampMs: Date.now(), monotonicMs: performance.now(), clockDomain:"client_performance", attributes: options.attributes || {} };
}
function emitClientEvent(eventType, component, options = {}, send = true) { if(["playback_progress","playback_stopped","playback_completed"].includes(eventType)&&options.replyId){const seq=(playbackEventSequences.get(options.replyId)||0)+1;playbackEventSequences.set(options.replyId,seq);options={...options,sequence:seq};}const event = commonEvent(eventType, component, options); ingestEvent(event); if (send && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "client_event", version: 1, event })); return event; }
function rememberMetric(event) {
  const key=event.turnId||event.replyId||"session";if(!eventTimes.has(key))eventTimes.set(key,new Map());
  const times=eventTimes.get(key);if(!times.has(event.eventType))times.set(event.eventType,{time:event.monotonicMs,domain:event.clockDomain});
  const show=(selector,start,end)=>{const a=times.get(start),b=times.get(end);if(a&&b&&a.domain&&a.domain===b.domain)$(selector).textContent=`${Math.max(0,Math.round(b.time-a.time))} ms`;};
  if(times.has("text_bot_request_started"))show("#metricLlm", "text_bot_request_started", "text_bot_first_text");else show("#metricLlm", "llm_request_started", "llm_first_token"); show("#metricTts", "tts_segment_started", "tts_first_audio"); show("#metricInterrupt", "barge_in_detected", "playback_stopped");
}
function ingestEvent(event) {
  if (!event?.eventType) return; event={...event,clientObservedMs:performance.now()}; events.push(event); if (events.length > 500) events.shift(); const newComponent=event.component&&!eventComponents.has(event.component),newTurn=event.turnId&&!eventTurns.has(event.turnId); if(event.component)eventComponents.add(event.component);if(event.turnId){eventTurns.add(event.turnId);setCurrentTurn(event.turnId);}rememberMetric(event);applyEventState(event);if(newComponent||newTurn)rebuildFilterOptions();scheduleTimelineRender(Boolean(event.attributes?.highFrequency));
}
function scheduleTimelineRender(highFrequency=false){if(timelineRenderTimer)return;timelineRenderTimer=setTimeout(()=>{timelineRenderTimer=undefined;renderTimeline();},highFrequency?250:16);}
function applyEventState(event) {
  const type = event.eventType;
  if (type === "speech_started") setUserState("SpeechStarted"); else if (type === "asr_partial") setUserState("Speaking"); else if (type === "speech_ended") setUserState("SpeechEnded");
  else if (type === "llm_request_started") setRobotState("Thinking"); else if (type === "tts_segment_started") setRobotState("Synthesizing"); else if (type === "playback_started") setRobotState("Speaking"); else if (["barge_in_detected", "reply_invalidated"].includes(type)) setRobotState("Cancelling"); else if (["reply_cancelled", "reply_failed", "playback_completed", "session_closed"].includes(type)) setRobotState("Idle");
  if (event.level === "error") { const errorKey=event.replyId&&event.attributes?.code?`${event.replyId}:${event.attributes.code}`:event.eventId;if(!countedErrorKeys.has(errorKey)){countedErrorKeys.add(errorKey);$("#metricErrors").textContent=String(Number($("#metricErrors").textContent||0)+1);} }
}
function rebuildFilterOptions() {
  const sync = (selector, values) => { const select = $(selector), selected = select.value, existing = new Set([...select.options].map((o) => o.value)); [...values].sort().forEach((value) => { if (!existing.has(value)) select.add(new Option(value.slice(0, 22), value)); }); select.value = selected; };
  sync("#componentFilter", eventComponents); sync("#turnFilter", eventTurns);
}
function renderTimeline() {
  const root = $("#timeline"), component = $("#componentFilter").value, level = $("#levelFilter").value, turn = $("#turnFilter").value, showFrames = $("#frameEvents").checked;
  const filtered = events.filter((event) => (!component || event.component === component) && (!level || event.level === level) && (!turn || event.turnId === turn) && (showFrames || !event.attributes?.highFrequency)); root.replaceChildren();
  if (!filtered.length) { const empty = document.createElement("p"); empty.className = "timeline-empty"; empty.textContent = "没有符合条件的事件"; root.append(empty); return; }
  filtered.slice(-250).reverse().forEach((event) => { const row = document.createElement("div"); row.className = `timeline-event ${event.level || "info"}`; const values = [["event-time", new Date(event.timestampMs).toLocaleTimeString("zh-CN", { hour12:false })], ["component", event.component || "—"], ["event-name", event.eventType], ["ids", [event.turnId, event.replyId, event.generationEpoch == null ? null : `epoch:${event.generationEpoch}`, event.sequence == null ? null : `seq:${event.sequence}`].filter(Boolean).join(" · ") || "—"], ["details", Object.entries(event.attributes || {}).map(([key,value]) => `${key}=${String(value)}`).join(" · ") || "—"]]; values.forEach(([className,text]) => { const cell = document.createElement("span"); cell.className = className; cell.textContent = text; cell.title = text; row.append(cell); }); root.append(row); });
}

function addMessage(role, text, ids = {}) {
  $("#emptyConversation")?.remove(); const item = document.createElement("article"); item.className = `message ${role}`; const meta = document.createElement("div"); meta.className = "message-meta"; const who = document.createElement("span"); who.textContent = role === "user" ? "用户" : "猫猫"; const id = document.createElement("span"); id.textContent = (ids.turnId || ids.replyId || "").slice(0,20); meta.append(who,id); const body = document.createElement("p"); body.textContent = text; item.append(meta,body); $("#conversation").append(item); $("#conversation").scrollTop = $("#conversation").scrollHeight; messageCount += 1; $("#messageCount").textContent = `${messageCount} 条`; return { item, body };
}
function updateAssistant(message) { if (interruptedReplyIds.has(message.replyId)) return; let entry = replyElements.get(message.replyId); if (!entry) { entry = addMessage("assistant", "", message); replyElements.set(message.replyId, entry); } const text = (replyTexts.get(message.replyId) || "") + (message.text || ""); replyTexts.set(message.replyId,text); entry.body.textContent = text; }
function updateAsr(message) {
  const partial = $("#partialTranscript"); if (message.status === 0) setUserState("Silent");
  if (message.status === 1) { setUserState("SpeechStarted"); partial.hidden = false; partial.querySelector("p").textContent = "…"; }
  if (message.status === 2) { setUserState("Speaking"); partial.hidden = false; partial.querySelector("p").textContent = message.text || "正在聆听…"; }
  if (message.status === 3) { setUserState("SpeechEnded"); partial.hidden = true; if (message.text) addMessage("user", message.text, { turnId:message.turnId || currentTurnId }); setTimeout(() => { if (sessionState === "Active") setUserState("Silent"); },300); }
}
function showError(code, message) { const banner = $("#errorBanner"); banner.hidden = false; $("#errorCode").textContent = code || "ERROR"; $("#errorMessage").textContent = `${message || "发生未知错误"} ${ERROR_ACTIONS[code] || "请查看时间线中的相关事件。"}`; }

function clearMouthSchedule() { for (const timer of mouthTimers) clearTimeout(timer); mouthTimers.clear(); setMouth("mouth_closed"); }
const playbackTracks = new Map();
const playbackEventSequences = new Map();
function playedPosition(replyId) {
  const track = playbackTracks.get(replyId);
  if (!track || !audioContext) return 0;
  if (track.stoppedMs !== undefined) return track.stoppedMs;
  return Math.floor(track.frames.reduce((sum, start) => sum + Math.max(0, Math.min(FRAME_SECONDS, audioContext.currentTime - start)) * 1000, 0));
}
function reportPlaybackProgress() {
  if (!activePlaybackReply) return;
  const track = playbackTracks.get(activePlaybackReply);
  if (!track || track.stoppedMs !== undefined) return;
  const playedMs = playedPosition(activePlaybackReply);
  if (playedMs <= (track.reportedMs || 0)) return;
  track.reportedMs = playedMs;
  emitClientEvent("playback_progress", "playback", {replyId: activePlaybackReply,
    attributes: {playedMs, highFrequency: true}});
}
function finishPlayback(message, endAt) {
  if (interruptedReplyIds.has(message.replyId)) return;
  const track = playbackTracks.get(message.replyId);
  if (track?.completed || track?.stoppedMs !== undefined) return;
  const remaining = Math.max(0, (endAt - (audioContext?.currentTime || 0)) * 1000);
  if (remaining > 2 || audioContext?.state !== "running") {
    const timer = setTimeout(() => { completionTimers.delete(timer); finishPlayback(message, endAt); }, Math.max(25, Math.min(250, remaining)));
    completionTimers.add(timer); return;
  }
  if (track) track.completed = true;
  emitClientEvent("playback_completed", "playback", {...message, attributes: {playedMs: playedPosition(message.replyId)}});
  if (activePlaybackReply === message.replyId) activePlaybackReply = undefined;
  playbackStarted = false; nextPlaybackTime = audioContext?.currentTime || 0;
  setMouth("mouth_closed"); updatePlaybackBuffer();
}
function reportPlaybackStopped(replyId, reason) {
  if (!replyId) return;
  let track = playbackTracks.get(replyId);
  if (track?.completed || track?.stoppedMs !== undefined) return;
  const playedMs = playedPosition(replyId);
  if (!track) { track = {frames: []}; playbackTracks.set(replyId, track); }
  track.stoppedMs = playedMs;
  const stoppedCtx = audioContext?.currentTime ?? 0;
  conversationRecorder?.stopRobot(replyId, stoppedCtx);
  latencyTrace.stop(replyId, stoppedCtx);
  outputObserver?.port.postMessage({type:"stop",replyId,atFrame:Math.round(stoppedCtx*(audioContext?.sampleRate||48000))});
  // A delayed interrupt may target an OLD reply while a new one is active.
  // Stop only its sources/queued frames; never report a stop and leave audio running.
  for (const source of scheduledSources) {
    if (source.replyId === replyId) { try { source.stop(); } catch (_) {} scheduledSources.delete(source); }
  }
  for (let i = playbackFrames.length - 1; i >= 0; i--) {
    if (playbackFrames[i].replyId === replyId) playbackFrames.splice(i, 1);
  }
  emitClientEvent("playback_stopped", "playback", {replyId, attributes: {reason, playedMs}});
}
function resetPlayback(reason = "reset") {
  // Sample the AudioContext clock BEFORE stopping sources. Queued/network
  // audio and jitter-buffer silence must never count as played speech.
  if (activePlaybackReply) {
    reportPlaybackStopped(activePlaybackReply, reason);
  }
  conversationRecorder?.stopAll(audioContext?.currentTime ?? 0);
  outputObserver?.port.postMessage({type:"stop_all",atFrame:Math.round((audioContext?.currentTime??0)*(audioContext?.sampleRate??16000))});
  for (const source of scheduledSources) { try { source.stop(); } catch (_) {} }
  scheduledSources.clear(); for (const timer of completionTimers) clearTimeout(timer);
  completionTimers.clear(); playbackFrames.length = 0; playbackStarted = false;
  nextPlaybackTime = audioContext ? audioContext.currentTime : 0;
  clearMouthSchedule(); activePlaybackReply = undefined; updatePlaybackBuffer();
  if (reason === "session_end") { playbackTracks.clear(); playbackEventSequences.clear(); }
}
function scheduleMouth(state,startAt) { const delay = Math.max(0,(startAt-audioContext.currentTime)*1000); const timer = setTimeout(() => { mouthTimers.delete(timer); setMouth(state); },delay); mouthTimers.add(timer); }
function clearStageTimeout() { if (stageTimer) clearTimeout(stageTimer); stageTimer = undefined; }
function armStageTimeout(code, message, duration) { clearStageTimeout(); const attempt = callAttempt; stageTimer = setTimeout(() => { if (attempt !== callAttempt) return; showError(code, message); connectionAttempt?.report(code); emitClientEvent("connection_stage_timeout", "websocket", { level:"error", attributes:{code,attemptId:connectionAttempt?.id} }); endCall(message, true, true); }, duration); }
function timeoutError(code, message) { const error = new Error(message); error.code = code; return error; }
function withTimeout(promise, duration, code, message) { return new Promise((resolve, reject) => { const timer = setTimeout(() => reject(timeoutError(code, message)), duration); promise.then((value) => { clearTimeout(timer); resolve(value); }, (error) => { clearTimeout(timer); reject(error); }); }); }
function requestMicrophone() { return new Promise((resolve, reject) => { let expired = false; const timer = setTimeout(() => { expired = true; reject(timeoutError("MIC_PERMISSION_TIMEOUT", "等待麦克风权限超时。")); }, STAGE_TIMEOUT_MS.microphone); navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}}).then((stream) => { clearTimeout(timer); if (expired) { stream.getTracks().forEach((track) => track.stop()); return; } resolve(stream); }, (error) => { clearTimeout(timer); reject(error); }); }); }
function microphoneErrorCode(error) { if (error?.code) return error.code; if (error?.name === "NotAllowedError" || error?.name === "SecurityError") return "MIC_PERMISSION_DENIED"; if (error?.name === "NotFoundError" || error?.name === "DevicesNotFoundError") return "MIC_DEVICE_NOT_FOUND"; if (error?.name === "NotReadableError" || error?.name === "TrackStartError") return "MIC_DEVICE_BUSY"; return "AUDIO_WORKLET_UNAVAILABLE"; }
function clearMicrophoneWatchdog() { if (microphoneWatchdog) clearInterval(microphoneWatchdog); microphoneWatchdog = undefined; }
function failMicrophone(code, message) { if (!["Starting", "Active"].includes(sessionState)) return; setMicrophoneState("Failed"); showError(code, message); emitClientEvent("microphone_failed", "microphone", { level:"error", attributes:{code, message} }); endCall("麦克风异常，通话已结束", true, true); }
function startMicrophoneWatchdog() { clearMicrophoneWatchdog(); microphoneWatchdog = setInterval(() => { if (microphoneState === "Capturing" && performance.now() - lastUplinkAt > UPLINK_STALL_MS) failMicrophone("MIC_NO_UPLINK", "麦克风已启动，但连续 3 秒没有音频上行。"); }, 500); }
function closeSocket() { if (activePlaybackReply) resetPlayback("socket_closing"); if (socket && socket.readyState < WebSocket.CLOSING) socket.close(); socket = undefined; }
async function ensureAudioContext() {
  audioContext ||= new AudioContext();
  if(audioContext.state!=="running")await audioContext.resume();
  outputObserverPromise ||= audioContext.audioWorklet.addModule("/output-worklet.js?v=20261022-timing1").then(()=>{
    outputObserver=new AudioWorkletNode(audioContext,"output-observer",{outputChannelCount:[1]});
    const silentTap=audioContext.createGain();silentTap.gain.value=0;
    outputObserver.connect(silentTap).connect(audioContext.destination);
    outputObserver.port.onmessage=({data})=>{
      if(data.type!=="graph_onset"||!latencyTrace.replies.has(data.replyId))return;
      latencyTrace.graph(data,audioContext,performance.now());
      emitClientEvent("audio_graph_onset","playback",{replyId:data.replyId,
        generationEpoch:data.generationEpoch,attributes:{onsetCtx:data.onsetCtx,measurement:"graph_not_physical"}},false);
      updateLatencyMetrics(data.replyId);
    };
  }).catch(()=>{emitClientEvent("output_observer_unavailable","playback",{level:"warning"},false);});
  await outputObserverPromise;
}
function updateLatencyMetrics(replyId){
  const r=latencyTrace.report().replies.find(row=>row.replyId===replyId);if(!r)return;
  const show=(id,value)=>{if(Number.isFinite(value))$(id).textContent=`${Math.round(value)} ms`;};
  show("#metricAsr",r.serverParts.sourceEndFrameToFinalMs);
  show("#metricAudio",r.clientParts.captureSignalToGraphMs);
}
function probeClocks(){
  const expected=sessionId;
  for(let i=0;i<5;i++){
    const timer=setTimeout(()=>{clockProbeTimers.delete(timer);
      if(sessionId!==expected||socket?.readyState!==WebSocket.OPEN)return;
      const pingId=crypto.randomUUID();latencyTrace.ping(pingId,performance.now());
      socket.send(JSON.stringify({type:"timing_ping",version:1,pingId}));
    },i*150);clockProbeTimers.add(timer);
  }
}
async function calculateSessionHash(value) { const digest = new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(value))); let output = 0n; for (let index=0;index<8;index+=1) output = (output<<8n)|BigInt(digest[index]); return output; }
function resamplePcm16(input,sourceRate) { const output = new Int16Array(1600), ratio = sourceRate/16000; for (let index=0;index<output.length;index+=1) { const position=index*ratio,left=Math.floor(position),right=Math.min(left+1,input.length-1),mix=position-left,sample=(input[left]||0)*(1-mix)+(input[right]||0)*mix; output[index]=Math.max(-32768,Math.min(32767,Math.round(sample*32767))); } return output; }
function sendAudio(floatFrame, captureTiming = {}) {
  const pcm=resamplePcm16(floatFrame,audioContext.sampleRate);
  const captured=conversationRecorder?.pushMic(pcm,{...captureTiming,
    deliveredCtx:audioContext.currentTime,deliveredClientMs:performance.now(),sequence:null});
  if(captured)latencyTrace.capture(captured);
  if (!socket || socket.readyState !== WebSocket.OPEN || !sessionId || !sessionHash) return;
  const rms=Math.sqrt(floatFrame.reduce((sum,sample)=>sum+sample*sample,0)/Math.max(1,floatFrame.length)), micPercent=Math.min(100,Math.round(rms*420));
  $("#micMeter").style.width=`${micPercent}%`; $("#micValue").textContent=`${micPercent}%`;
  if (socket.bufferedAmount > MAX_UPLINK_BUFFERED_BYTES) {
    if(captured)captured.dropped=true;
    uplinkDropCount += 1;
    if (!uplinkBackpressureActive) {
      uplinkBackpressureActive = true;
      emitClientEvent("uplink_backpressure", "uplink", { level:"warning", attributes:{bufferedBytes:socket.bufferedAmount, thresholdBytes:MAX_UPLINK_BUFFERED_BYTES, strategy:"drop_newest"} });
    }
    return;
  }
  if (uplinkBackpressureActive) {
    emitClientEvent("uplink_backpressure_recovered", "uplink", { attributes:{droppedFrames:uplinkDropCount, bufferedBytes:socket.bufferedAmount} });
    uplinkBackpressureActive = false; uplinkDropCount = 0;
  }
  const frame=new ArrayBuffer(3224),view=new DataView(frame),sequence=inputSequence;
  if(captured){captured.sequence=sequence;captured.sentClientMs=performance.now();}
  view.setUint16(0,0x5652,false);view.setUint8(2,1);view.setUint8(3,0);view.setBigUint64(4,sessionHash,false);view.setUint32(12,sequence,false);view.setBigUint64(16,BigInt(Date.now()),false);new Int16Array(frame,24,1600).set(pcm);
  inputSequence+=1; socket.send(frame); lastUplinkAt=performance.now(); $("#lastUplink").textContent=new Date().toLocaleTimeString("zh-CN",{hour12:false});
  if(sequence%10===0)emitClientEvent("microphone_frame_captured","microphone",{sequence,attributes:{highFrequency:true,rms:Number(rms.toFixed(4))}});
  if(sequence%10===0)emitClientEvent("uplink_frame_sent","uplink",{sequence,attributes:{highFrequency:true,bytes:3200,bufferedBytes:socket.bufferedAmount}});
}
async function startMicrophone(expectedSessionId) {
  let stream;
  try {
    setMicrophoneState("RequestingPermission");
    emitClientEvent("microphone_permission_requested", "microphone");
    stream=await requestMicrophone();
    if(sessionId!==expectedSessionId){stream.getTracks().forEach((track)=>track.stop());return false;}
    mediaStream=stream;
    await withTimeout(ensureAudioContext(), STAGE_TIMEOUT_MS.audioWorklet, "AUDIO_WORKLET_UNAVAILABLE", "音频上下文启动超时。");
    await withTimeout(audioContext.audioWorklet.addModule("/audio-worklet.js?v=20261022-timing1"), STAGE_TIMEOUT_MS.audioWorklet, "AUDIO_WORKLET_UNAVAILABLE", "麦克风采集模块加载超时。");
    captureSource=audioContext.createMediaStreamSource(mediaStream);
    captureNode=new AudioWorkletNode(audioContext,"capture-processor");
    captureNode.port.onmessage=({data})=>sendAudio(data.pcm||data,data.pcm?{
      captureStartCtx:data.captureStartCtx,captureEndCtx:data.captureEndCtx,
      captureSequence:data.captureSequence,captureSampleRate:data.sampleRate}:{});
    captureSource.connect(captureNode);
    muteNode=audioContext.createGain();muteNode.gain.value=0;captureNode.connect(muteNode).connect(audioContext.destination);
    const track=mediaStream.getAudioTracks()[0];
    if(!track)throw timeoutError("MIC_DEVICE_NOT_FOUND","没有可用的麦克风输入轨道。");
    track.onmute=()=>{if(microphoneState==="Capturing")setMicrophoneState("Muted");};
    track.onunmute=()=>{if(sessionState==="Active")setMicrophoneState("Capturing");};
    track.onended=()=>failMicrophone("MIC_DEVICE_LOST","麦克风连接已中断。");
    const settings=track.getSettings?.()||{};
    lastUplinkAt=performance.now(); setMicrophoneState(track.muted?"Muted":"Capturing"); startMicrophoneWatchdog();
    emitClientEvent("microphone_started","microphone",{attributes:{sampleRate:settings.sampleRate||audioContext.sampleRate,channelCount:settings.channelCount||1,echoCancellation:settings.echoCancellation,noiseSuppression:settings.noiseSuppression,autoGainControl:settings.autoGainControl}});
    return true;
  } catch(error) {
    if(stream && stream!==mediaStream)stream.getTracks().forEach((track)=>track.stop());
    const code=microphoneErrorCode(error); error.code=code; setMicrophoneState("Failed");
    emitClientEvent("microphone_failed","microphone",{level:"error",attributes:{code,message:error?.message||""}});
    throw error;
  }
}
function stopMicrophone(){clearMicrophoneWatchdog();setMicrophoneState("Idle");if(captureNode){captureNode.port.onmessage=null;captureNode.disconnect();}if(captureSource)captureSource.disconnect();if(muteNode)muteNode.disconnect();captureNode=undefined;captureSource=undefined;muteNode=undefined;if(mediaStream)mediaStream.getTracks().forEach((track)=>{track.onended=null;track.onmute=null;track.onunmute=null;track.stop();});mediaStream=undefined;lastUplinkAt=0;uplinkDropCount=0;uplinkBackpressureActive=false;$("#micMeter").style.width="0";$("#micValue").textContent="0%";}
function decodeAudioFrame(buffer){
  if(buffer.byteLength!==3224){emitClientEvent("protocol_error","downlink",{level:"error",attributes:{code:"INVALID_AUDIO_LENGTH",length:buffer.byteLength}});return null;}
  const view=new DataView(buffer),magic=view.getUint16(0,false),version=view.getUint8(2),direction=view.getUint8(3),hashed=view.getBigUint64(4,false),sequence=view.getUint32(12,false);
  if(magic!==0x5652||version!==1||direction!==1||hashed!==sessionHash){emitClientEvent("protocol_error","downlink",{level:"error",sequence,attributes:{code:"INVALID_AUDIO_HEADER"}});return null;}
  if(sequence<expectedDownSequence){emitClientEvent("audio_sequence_duplicate","downlink",{level:"warning",sequence,attributes:{expected:expectedDownSequence,received:sequence}});return null;}
  if(sequence!==expectedDownSequence)emitClientEvent("audio_sequence_gap","downlink",{level:"warning",sequence,attributes:{expected:expectedDownSequence,received:sequence}});
  expectedDownSequence=sequence+1;const pcm=new Int16Array(1600);for(let index=0;index<pcm.length;index+=1)pcm[index]=view.getInt16(24+index*2,true);const meta=avatarFrames.get(sequence)||{};return{sequence,pcm,avatarState:meta.avatarState||"mouth_closed",...meta};
}
function updatePlaybackBuffer(){const ms=audioContext?Math.max(0,Math.round((nextPlaybackTime-audioContext.currentTime)*1000)):0;$("#bufferValue").textContent=`${ms} ms`;$("#bufferMeter").style.width=`${Math.min(100,ms/6)}%`;}
function scheduleAudioFrame(frame,startAt){if(interruptedReplyIds.has(frame.replyId))return;const audioBuffer=audioContext.createBuffer(1,1600,16000),channel=audioBuffer.getChannelData(0);for(let index=0;index<frame.pcm.length;index+=1)channel[index]=frame.pcm[index]/32768;const source=audioContext.createBufferSource();source.replyId=frame.replyId;source.buffer=audioBuffer;source.connect(audioContext.destination);if(outputObserver)source.connect(outputObserver);source.onended=()=>scheduledSources.delete(source);scheduledSources.add(source);scheduleMouth(frame.avatarState,startAt);latencyTrace.schedule(frame,startAt,performance.now());outputObserver?.port.postMessage({type:"schedule",replyId:frame.replyId,generationEpoch:frame.generationEpoch,startFrame:Math.round(startAt*audioContext.sampleRate),endFrame:Math.round((startAt+FRAME_SECONDS)*audioContext.sampleRate)});source.start(startAt);conversationRecorder?.pushRobot(frame.pcm,startAt,frame.replyId);if(!playbackTracks.has(frame.replyId))playbackTracks.set(frame.replyId,{frames:[]});playbackTracks.get(frame.replyId).frames.push(startAt);activePlaybackReply=frame.replyId;if(!playbackStartedReplies.has(frame.replyId)){playbackStartedReplies.add(frame.replyId);const timer=setTimeout(()=>{completionTimers.delete(timer);emitClientEvent("playback_started","playback",frame);},Math.max(0,(startAt-audioContext.currentTime)*1000));completionTimers.add(timer);}}
function drainPlayback(force=false){if(!audioContext||audioContext.state!=="running")return;if(!playbackStarted){if(!force&&playbackFrames.length<PLAYBACK_PREROLL_FRAMES)return;if(!playbackFrames.length)return;playbackStarted=true;nextPlaybackTime=audioContext.currentTime+.06;}while(playbackFrames.length){const frame=playbackFrames.shift();if(!interruptedReplyIds.has(frame.replyId)){scheduleAudioFrame(frame,nextPlaybackTime);nextPlaybackTime+=FRAME_SECONDS;}}updatePlaybackBuffer();}
function playAudio(buffer,receivedMs=performance.now(),receivedCtx=audioContext?.currentTime){if(!audioContext||audioContext.state!=="running"||!sessionHash){pendingAudioFrames.push({buffer,receivedMs,receivedCtx});return;}const frame=decodeAudioFrame(buffer);if(!frame)return;latencyTrace.receive(frame,receivedMs,receivedCtx);avatarFrames.delete(frame.sequence);if(interruptedReplyIds.has(frame.replyId))return;if(playbackStarted&&nextPlaybackTime<audioContext.currentTime+.015&&!underrunReplies.has(frame.replyId)){underrunReplies.add(frame.replyId);emitClientEvent("playback_underrun","playback",{...frame,level:"warning",attributes:{bufferMs:0}});nextPlaybackTime=audioContext.currentTime+.04;}playbackFrames.push(frame);drainPlayback();}
function flushPendingAudio(){while(pendingAudioFrames.length){const item=pendingAudioFrames.shift();playAudio(item.buffer,item.receivedMs,item.receivedCtx);}}

function handleJson(message){
  if(message.type==="timing_pong"){latencyTrace.pong(message,performance.now());return;}
  if(message.type==="session_event")latencyTrace.serverEvent(message.event,performance.now());
  $("#lastMessage").textContent=new Date().toLocaleTimeString("zh-CN",{hour12:false});if(message.type==="session_event"){ingestEvent(message.event);return;}if(message.type==="runtime_stats"){$("#queueInput").textContent=message.inputQueue;$("#queueControl").textContent=message.controlQueue;$("#queueMedia").textContent=message.mediaOutputQueue;$("#queueCache").textContent=message.mediaCache;$("#packetCounts").textContent=`${message.audioReceived} / ${message.audioSent}`;$("#metricErrors").textContent=message.errorCount;return;}
  if(message.type==="prepare_result"){clearStageTimeout();if(!message.ok){const text=message.code==="NO_RESOURCE"&&message.resources?window.ConnectionAttempt.busyMessage(message.resources):message.message;connectionAttempt?.report(message.code);showError(message.code,text);return endCall(text,true,true);}setSessionState("Ready");setStatus("资源就绪，正在建立会话…");armStageTimeout("CREATE_SESSION_TIMEOUT","建立会话超时。",STAGE_TIMEOUT_MS.createSession);socket.send(JSON.stringify({type:"create_session",version:1,requestId:crypto.randomUUID(),grade:Number(gradeSelect?.value||7),...(window.KittyVoiceChapter?{chapterId:window.KittyVoiceChapter.id}:{})}));return;}
  if(message.type==="create_session_result"){clearStageTimeout();if(!message.ok){showError(message.code,message.message);return endCall("会话建立失败");}activateSession(message).catch((error)=>{const code=microphoneErrorCode(error);showError(code,error.message);endCall("会话初始化失败",true,true);});return;}if(message.type==="asr_result"){updateAsr(message);return;}if(message.type==="assistant_delta"){setCurrentTurn(message.turnId);updateAssistant(message);return;}if(message.type==="avatar_frame"){avatarFrames.set(message.sequence,message);return;}
  if(message.type==="playback_interrupt"){interruptedReplyIds.add(message.replyId);reportPlaybackStopped(message.replyId,"barge_in");const entry=replyElements.get(message.replyId);if(entry)entry.item.classList.add("interrupted");cat.classList.remove("interrupted");void cat.offsetWidth;cat.classList.add("interrupted");setTimeout(()=>cat.classList.remove("interrupted"),600);if(activePlaybackReply===message.replyId||!activePlaybackReply)resetPlayback("barge_in");return;}if(message.type==="reply_failed"){interruptedReplyIds.add(message.replyId);const entry=replyElements.get(message.replyId);if(entry)entry.item.classList.add("failed");if(activePlaybackReply===message.replyId||!activePlaybackReply)resetPlayback("reply_failed");setRobotState("Idle");return;}if(message.type==="reply_complete"){if(!interruptedReplyIds.has(message.replyId)){drainPlayback(true);finishPlayback(message,nextPlaybackTime);}return;}if(message.type==="close_session"){expectedClose=true;endCall("通话已结束");return;}if(message.type==="error"){showError(message.code,message.message);setStatus(message.message||"服务错误");}
}
async function activateSession(message){const expectedSessionId=message.sessionId;sessionId=expectedSessionId;disconnectKey=message.disconnectKey;latencyTrace.reset();probeClocks();conversationRecorder?.start(audioContext,{sessionId:expectedSessionId,grade:Number(gradeSelect?.value||7)});$("#sessionIdLabel").textContent=sessionId;sessionHash=await calculateSessionHash(sessionId);inputSequence=0;expectedDownSequence=0;expectedClose=false;resetPlayback();flushPendingAudio();setSessionState("Starting");setConnection("已连接","active");setStatus("通话已接通，正在启动麦克风…");callButton.textContent="挂断";callButton.setAttribute("aria-label","结束语音通话");callButton.classList.add("hangup");callButton.disabled=false;callButton.onclick=hangup;sessionStartedAt=Date.now();updateSessionTimer();sessionTimerHandle=setInterval(updateSessionTimer,1000);const started=await startMicrophone(expectedSessionId);if(started&&sessionId===expectedSessionId){setSessionState("Active");setStatus("正在聆听");}}
function resetSessionView(){events.length=0;eventComponents.clear();eventTurns.clear();eventTimes.clear();replyElements.clear();replyTexts.clear();interruptedReplyIds.clear();countedErrorKeys.clear();playbackStartedReplies.clear();underrunReplies.clear();messageCount=0;currentTurnId=undefined;$("#errorBanner").hidden=true;setMicrophoneState("Idle");$("#lastUplink").textContent="—";$("#messageCount").textContent="0 条";$("#sessionIdLabel").textContent="—";$("#turnId").textContent="—";$("#conversation").replaceChildren();const empty=document.createElement("div");empty.id="emptyConversation";empty.className="empty-state";const orb=document.createElement("span");orb.className="empty-orb";orb.setAttribute("aria-hidden","true");const title=document.createElement("b");title.textContent="对话会出现在这里";const hint=document.createElement("span");hint.textContent="接通后直接开口，猫猫会实时回应你";empty.append(orb,title,hint);$("#conversation").append(empty);$("#partialTranscript").hidden=true;["#metricAsr","#metricLlm","#metricTts","#metricAudio","#metricInterrupt"].forEach((selector)=>$(selector).textContent="—");$("#metricErrors").textContent="0";for(const selector of["#componentFilter","#turnFilter"]){const select=$(selector);while(select.options.length>1)select.remove(1);}renderTimeline();}
async function startCall() {
  if (window.KittyVoiceUnitReady && !await window.KittyVoiceUnitReady) return;
  if (["Connecting","Ready","Starting","Active","Closing"].includes(sessionState)) return;
  if (conversationRecorder && conversationRecorder.micChunks.length && !confirm("上一次通话的录音尚未导出，开始新通话会丢弃它。继续？")) return;
  resetSessionView(); callAttempt += 1;
  const attempt = callAttempt;
  connectionAttempt = new window.ConnectionAttempt();
  callButton.disabled = true; setSessionState("Connecting"); setConnection("连接中","pending");
  setStatus("正在验证登录…"); expectedClose = false;
  try {
    // Resume while still in the user's gesture; microphone starts only after
    // authenticated admission. Media and barge-in code below are unchanged.
    await withTimeout(ensureAudioContext(), STAGE_TIMEOUT_MS.audioWorklet, "AUDIO_WORKLET_UNAVAILABLE", "无法启动音频播放上下文。");
    const access = await connectionAttempt.bootstrap();
    if (attempt !== callAttempt) return;
    if (access.resources.inUse >= access.resources.capacity) {
      const message = window.ConnectionAttempt.busyMessage(access.resources);
      connectionAttempt.report("NO_RESOURCE"); showError("NO_RESOURCE", message);
      return endCall(message, true, true);
    }
  } catch (error) {
    if (attempt !== callAttempt) return;
    if (error.code?.startsWith("AUTH_")) connectionAttempt.report(error.code);
    showError(error.code || "AUTH_UNAVAILABLE", error.message);
    return endCall(error.message, true, true);
  }
  if (attempt !== callAttempt) return;
  setStatus("正在建立语音连接…");
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const activeSocket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket = activeSocket; socket.binaryType = "arraybuffer";
  armStageTimeout("WEBSOCKET_TIMEOUT", "语音连接超时，请检查网络后重试。", STAGE_TIMEOUT_MS.websocket);
  activeSocket.onopen = () => {
    if (socket !== activeSocket || attempt !== callAttempt) return;
    clearStageTimeout(); setConnection("已连接","active"); setStatus("正在检查通话资源…");
    armStageTimeout("PREPARE_TIMEOUT","等待通话名额确认超时。",STAGE_TIMEOUT_MS.prepare);
    activeSocket.send(JSON.stringify({type:"prepare",version:1,requestId:crypto.randomUUID(),attemptId:connectionAttempt.id,
      audio:{codec:"pcm_s16le",sampleRate:16000,channels:1,frameDurationMs:100}}));
  };
  activeSocket.onmessage = ({data}) => {
    if (socket !== activeSocket || attempt !== callAttempt) return;
    if (data instanceof ArrayBuffer) playAudio(data);
    else { try { handleJson(JSON.parse(data)); } catch (error) { showError("INVALID_MESSAGE",error.message); } }
  };
  activeSocket.onclose = () => {
    if (socket !== activeSocket || attempt !== callAttempt) return;
    clearStageTimeout(); setConnection("已断开","neutral");
    if (!expectedClose) {
      emitClientEvent("connection_lost","websocket",{level:"error"},false);
      connectionAttempt.report("CONNECTION_FAILED");
      if (!sessionId) showError("CONNECTION_FAILED","语音连接在建立会话前断开，请重试或刷新登录。");
      endCall("连接意外断开",false,true);
    } else endCall("连接已关闭",false,false);
  };
  activeSocket.onerror = () => {
    if (socket !== activeSocket || attempt !== callAttempt) return;
    showError("CONNECTION_FAILED","语音连接失败，请检查网络或刷新页面重新登录。");
    connectionAttempt.report("CONNECTION_FAILED");
    emitClientEvent("error","websocket",{level:"error",attributes:{code:"CONNECTION_FAILED"}},false);
    endCall("连接失败",true,true);
  };
}
function hangup(){stopMicrophone();expectedClose=true;setSessionState("Closing");if(socket?.readyState===WebSocket.OPEN&&sessionId){socket.send(JSON.stringify({type:"close_session",version:1,requestId:crypto.randomUUID(),sessionId,initiator:"user"}));setStatus("正在挂断…");callButton.disabled=true;hangupTimer=setTimeout(()=>endCall("已挂断"),1500);}else endCall("已挂断");}
window.addEventListener('pagehide', () => {
  if (sessionId && disconnectKey) {
    const body = JSON.stringify({sessionId, disconnectKey});
    // Beacon is best effort; socket close + server heartbeat remain the fallback.
    if (!navigator.sendBeacon?.('/api/browser-disconnect', new Blob([body], {type:'application/json'}))) {
      fetch('/api/browser-disconnect',{method:'POST',credentials:'same-origin',keepalive:true,
        headers:{'Content-Type':'application/json'},body}).catch(()=>{});
    }
  }
  expectedClose = true;
  endCall('页面已离开，通话已结束', true, false);
});
function endCall(message,close=true,isError=false){for(const timer of clockProbeTimers)clearTimeout(timer);clockProbeTimers.clear();conversationRecorder?.mark("call_end",message,audioContext);const preserveMicFailure=microphoneState==="Failed";callAttempt+=1;clearStageTimeout();clearMicrophoneWatchdog();if(hangupTimer)clearTimeout(hangupTimer);hangupTimer=undefined;if(sessionTimerHandle)clearInterval(sessionTimerHandle);sessionTimerHandle=undefined;stopMicrophone();if(preserveMicFailure)setMicrophoneState("Failed");if(close)closeSocket();sessionId=undefined;disconnectKey=undefined;sessionHash=undefined;currentTurnId=undefined;$("#sessionIdLabel").textContent="—";$("#turnId").textContent="—";avatarFrames.clear();pendingAudioFrames.length=0;resetPlayback("session_end");setStatus(message);setSessionState(isError?"Error":"Closed");setUserState("Silent");setRobotState("Idle");setConnection("未连接",isError?"error":"neutral");callButton.textContent="拨通";callButton.setAttribute("aria-label","开始语音通话");callButton.classList.remove("hangup");callButton.disabled=false;callButton.onclick=startCall;sessionStartedAt=undefined;updateSessionTimer();}

async function checkEnvironment(){const missing=[];if(!window.WebSocket)missing.push("WebSocket");if(!window.AudioContext)missing.push("Web Audio");if(!navigator.mediaDevices?.getUserMedia)missing.push("麦克风 API");if(!window.AudioWorkletNode)missing.push("AudioWorklet");try{const response=await fetch("/health",{cache:"no-store"}),health=await response.json();if(!response.ok||!health.ok)throw new Error("服务健康检查失败");const badge=$("#serviceBadge");badge.className="badge ok";badge.innerHTML="<i></i>服务可用";if(window.KittyVoiceChapterReadyState==='ready')callButton.disabled=false;const capabilities=$("#capabilities");capabilities.replaceChildren();Object.entries(health.capabilities).forEach(([key,value])=>{const tag=document.createElement("span");tag.textContent=`${key}: ${value}`;capabilities.append(tag);});[["version",health.version],["commit",health.gitCommit],["started",health.startedAt],["pid",health.pid],["resource",`${health.resources.inUse}/${health.resources.capacity}`]].forEach(([key,value])=>{const tag=document.createElement("span");tag.textContent=`${key}: ${value}`;capabilities.append(tag);});emitClientEvent("service_check_completed","browser",{attributes:{ok:true,missing,version:health.version,gitCommit:health.gitCommit,pid:health.pid}},false);}catch(error){const badge=$("#serviceBadge");badge.className="badge error";badge.innerHTML="<i></i>服务不可用";showError("CONNECTION_FAILED",error.message);callButton.disabled=true;}if(missing.length)showError("BROWSER_CAPABILITY_MISSING",`缺少：${missing.join("、")}。`);}

callButton.onclick=startCall;$("#dismissError").onclick=()=>{$("#errorBanner").hidden=true;};$("#clearTimeline").onclick=()=>{events.length=0;renderTimeline();};["#componentFilter","#levelFilter","#turnFilter","#frameEvents"].forEach((selector)=>$(selector).addEventListener("change",renderTimeline));setInterval(updatePlaybackBuffer,200);setInterval(reportPlaybackProgress,250);checkEnvironment();

/* Conversation recorder: dual-track capture, quick tags, offline export. */
const conversationRecorder = window.ConversationRecorder ? new window.ConversationRecorder() : null;
window.__rtv = { get events() { return events; }, get recorder() { return conversationRecorder; }, get audioContext() { return audioContext; }, get latency() { return latencyTrace; } };
const TAG_LABELS = { reply_delay: "回复延迟大", false_interrupt: "误打断", cut_in: "抢话", good: "体验好" };
function tagCurrentConversation(tag) {
  if (!conversationRecorder || !conversationRecorder.active) { const el = $("#tagFeedback"); if (el) el.textContent = "接通通话后才能打点"; return; }
  const note = $("#tagNote") ? $("#tagNote").value.trim() : "";
  const marker = conversationRecorder.mark(tag, note, audioContext);
  const feedback = $("#tagFeedback");
  if (feedback && marker) feedback.textContent = `已打标「${TAG_LABELS[tag] || tag}」 @ ${Math.round(marker.relMs / 1000)}s`;
}
document.querySelectorAll("[data-tag]").forEach((button) => button.addEventListener("click", () => tagCurrentConversation(button.dataset.tag)));
if ($("#exportRecording")) $("#exportRecording").onclick = () => {
  if (!conversationRecorder || !conversationRecorder.micChunks.length) { const el = $("#tagFeedback"); if (el) el.textContent = "还没有可导出的录音"; return; }
  conversationRecorder.exportBundle(events, sessionId, latencyTrace).forEach((file) => {
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(file.blob);
    anchor.download = file.name;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 10000);
  });
  const feedback = $("#tagFeedback");
  if (feedback) feedback.textContent = "已导出 mic / robot 双轨 WAV 与事件日志 JSON";
};


async function loadVoiceUnit() {
  window.KittyVoiceChapterReadyState='loading';callButton.disabled=true;
  try {
    if (window.KittyAccount) await window.KittyAccount.ready;
    const C = window.KittyCourses;
    let preferred;
    try { preferred = C?.load(window.KittyAccount ? window.KittyAccount.storage : localStorage); } catch {}
    const id = new URLSearchParams(location.search).get('chapter') || preferred?.chapterId;
    if (!id) {
      if (preferred?.track === 'textbook') throw Error('请返回学习首页选择教材单元，再开始聊天。');
      if (preferred) {
        const value = String(preferred.grade);
        if (![...gradeSelect.options].some(option=>option.value===value)) gradeSelect.add(new Option(C.gradeNames[preferred.grade],value));
        gradeSelect.value=value;
      }
      gradeSelect.disabled=true;
      $('#voiceCourse').textContent = `${C?.gradeNames[Number(gradeSelect.value)] || '当前年级'} · 分级拓展`;
      window.KittyVoiceChapterReadyState='ready';
      if ($('#serviceBadge').classList.contains('ok')) callButton.disabled=false;
      return true;
    }
    const response = await fetch(`/api/text-bot/scopes/${encodeURIComponent(id)}`);
    const data = await response.json();
    if (!response.ok) throw Error(data.message || '教材单元加载失败');
    const grade = String(parseInt(data.selection.grade,10));
    if (![...gradeSelect.options].some(option=>option.value===grade)) gradeSelect.add(new Option(`${grade}年级`,grade));
    gradeSelect.value=grade;gradeSelect.disabled=true;
    window.KittyVoiceChapter={id,unit:data.selection.unit};
    $('#voiceCourse').textContent = [data.selection.grade, data.selection.publisher, window.KittyCourses?.editionLabel(data.selection.edition) || data.selection.edition, data.selection.unit].filter(Boolean).join(' · ');
    const textLink = document.querySelector?.('a[href^="/text.html"]');
    if (textLink) textLink.href = `/text.html?chapter=${encodeURIComponent(id)}`;
    window.KittyVoiceChapterReadyState='ready';
    if ($('#serviceBadge').classList.contains('ok')) callButton.disabled=false;
    return true;
  } catch (error) {
    window.KittyVoiceChapterReadyState='failed';
    callButton.disabled=true;showError('INVALID_GRADE',error.message);
    return false;
  }
}
window.KittyVoiceUnitReady=loadVoiceUnit();
