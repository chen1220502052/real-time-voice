const callButton = document.querySelector("#callButton");
const statusLabel = document.querySelector("#status");
const cat = document.querySelector("#cat");

let socket;
let sessionId;
let sessionHash;
let inputSequence = 0;
let mediaStream;
let audioContext;
let captureNode;
let nextPlaybackTime = 0;
const avatarFrames = new Map();

function setStatus(text) { statusLabel.textContent = text; }
function setMouth(state) {
  cat.classList.toggle("mouth-open", state === "mouth_open");
  cat.classList.toggle("mouth-closed", state !== "mouth_open");
}

function closeSocket() {
  if (socket && socket.readyState === WebSocket.OPEN) socket.close();
  socket = undefined;
}

async function calculateSessionHash(value) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
  let output = 0n;
  for (let index = 0; index < 8; index += 1) output = (output << 8n) | BigInt(digest[index]);
  return output;
}

function resamplePcm16(input, sourceRate) {
  const output = new Int16Array(1600);
  const ratio = sourceRate / 16000;
  for (let index = 0; index < output.length; index += 1) {
    const position = index * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, input.length - 1);
    const mix = position - left;
    const sample = (input[left] || 0) * (1 - mix) + (input[right] || 0) * mix;
    output[index] = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)));
  }
  return output;
}

function sendAudio(floatFrame) {
  if (!socket || socket.readyState !== WebSocket.OPEN || !sessionId || !sessionHash) return;
  const pcm = resamplePcm16(floatFrame, audioContext.sampleRate);
  const frame = new ArrayBuffer(24 + 3200);
  const view = new DataView(frame);
  view.setUint16(0, 0x5652, false);
  view.setUint8(2, 1);
  view.setUint8(3, 0);
  view.setBigUint64(4, sessionHash, false);
  view.setUint32(12, inputSequence, false);
  view.setBigUint64(16, BigInt(Date.now()), false);
  new Int16Array(frame, 24, 1600).set(pcm);
  inputSequence += 1;
  socket.send(frame);
}

async function startMicrophone() {
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  audioContext = audioContext || new AudioContext();
  await audioContext.audioWorklet.addModule("/audio-worklet.js");
  const source = audioContext.createMediaStreamSource(mediaStream);
  captureNode = new AudioWorkletNode(audioContext, "capture-processor");
  captureNode.port.onmessage = ({ data }) => sendAudio(data);
  source.connect(captureNode);
  // The worklet must be connected to keep rendering; a zero-gain node prevents local echo.
  const mute = audioContext.createGain();
  mute.gain.value = 0;
  captureNode.connect(mute).connect(audioContext.destination);
}

function stopMicrophone() {
  if (captureNode) captureNode.disconnect();
  captureNode = undefined;
  if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
  mediaStream = undefined;
}

function playAudio(buffer) {
  if (!audioContext) return;
  const view = new DataView(buffer, 24);
  const sequence = view.getUint32(12, false);
  const pcm = new Int16Array(1600);
  for (let index = 0; index < pcm.length; index += 1) pcm[index] = view.getInt16(24 + index * 2, true);
  const audioBuffer = audioContext.createBuffer(1, 1600, 16000);
  const channel = audioBuffer.getChannelData(0);
  for (let index = 0; index < pcm.length; index += 1) channel[index] = pcm[index] / 32768;
  const source = audioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioContext.destination);
  const startAt = Math.max(audioContext.currentTime + 0.03, nextPlaybackTime);
  nextPlaybackTime = startAt + 0.1;
  setMouth(avatarFrames.get(sequence) || "mouth_closed");
  avatarFrames.delete(sequence);
  source.start(startAt);
}

async function startCall() {
  callButton.disabled = true;
  setStatus("正在连接…");
  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.binaryType = "arraybuffer";
  socket.onopen = () => socket.send(JSON.stringify({
    type: "prepare", version: 1, requestId: crypto.randomUUID(),
    audio: { codec: "pcm_s16le", sampleRate: 16000, channels: 1, frameDurationMs: 100 },
  }));
  socket.onmessage = async ({ data }) => {
    if (data instanceof ArrayBuffer) {
      playAudio(data);
      return;
    }
    const message = JSON.parse(data);
    if (message.type === "prepare_result") {
      if (!message.ok) return endCall("资源不足，无法通话");
      setStatus("正在建立会话…");
      socket.send(JSON.stringify({ type: "create_session", version: 1, requestId: crypto.randomUUID() }));
    } else if (message.type === "create_session_result") {
      if (!message.ok) return endCall("会话建立失败");
      sessionId = message.sessionId;
      sessionHash = await calculateSessionHash(sessionId);
      inputSequence = 0;
      nextPlaybackTime = 0;
      await startMicrophone();
      setStatus("正在通话");
      callButton.textContent = "挂断";
      callButton.classList.add("hangup");
      callButton.disabled = false;
      callButton.onclick = hangup;
    } else if (message.type === "avatar_frame") {
      avatarFrames.set(message.sequence, message.avatarState);
    } else if (message.type === "playback_interrupt") {
      nextPlaybackTime = audioContext ? audioContext.currentTime : 0;
      avatarFrames.clear();
      setMouth("mouth_closed");
    } else if (message.type === "close_session") {
      endCall("通话已结束");
    } else if (message.type === "error") {
      setStatus(message.message || "服务错误");
    }
  };
  socket.onclose = () => {
    if (sessionId) endCall("通话已结束", false);
  };
  socket.onerror = () => endCall("连接失败");
}

function hangup() {
  stopMicrophone();
  if (socket && socket.readyState === WebSocket.OPEN && sessionId) {
    socket.send(JSON.stringify({ type: "close_session", version: 1, requestId: crypto.randomUUID(), sessionId, initiator: "user" }));
  } else {
    endCall("已挂断");
  }
}

function endCall(message, close = true) {
  stopMicrophone();
  if (close) closeSocket();
  sessionId = undefined;
  sessionHash = undefined;
  avatarFrames.clear();
  setMouth("mouth_closed");
  setStatus(message);
  callButton.textContent = "拨通";
  callButton.classList.remove("hangup");
  callButton.disabled = false;
  callButton.onclick = startCall;
}

callButton.onclick = startCall;
