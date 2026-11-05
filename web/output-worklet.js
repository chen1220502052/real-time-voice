// Audio-graph observation only. This is NOT a microphone/physical loopback.
class OutputObserver extends AudioWorkletProcessor {
  constructor() {
    super();
    this.intervals = [];
    this.seen = new Set();
    this.recordingId = null;
    this.recordBuffer = new Float32Array(Math.round(sampleRate / 10));
    this.recordLength = 0;
    this.port.onmessage = ({data}) => {
      if (data.type === "record_start") {
        this.recordingId=data.recordingId;this.recordLength=0;
      } else if (data.type === "record_stop") {
        if(data.recordingId===this.recordingId){this.flushRecording();this.recordingId=null;}
        this.port.postMessage({type:"recording_stopped",recordingId:data.recordingId});
      } else if (data.type === "schedule") {
        this.intervals.push(data);
        if (this.intervals.length > 200) this.intervals.shift();
      } else if (data.type === "stop") {
        for (const frame of this.intervals) {
          if (frame.replyId === data.replyId) frame.endFrame = Math.min(frame.endFrame, data.atFrame);
        }
      } else if (data.type === "stop_all") {
        for (const frame of this.intervals) frame.endFrame = Math.min(frame.endFrame, data.atFrame);
      }
    };
  }
  flushRecording() {
    if(!this.recordLength)return;
    const pcm=this.recordBuffer.slice(0,this.recordLength);
    this.port.postMessage({type:"rendered_pcm",recordingId:this.recordingId,
      captureStartCtx:this.recordStartFrame/sampleRate,sampleRate,pcm},[pcm.buffer]);
    this.recordLength=0;
  }
  process(inputs, outputs) {
    const input = inputs[0]?.[0], output = outputs[0]?.[0];
    if (output && input) output.set(input);
    if(this.recordingId){
      const count=output?.length||input?.length||128;
      for(let i=0;i<count;i++){
        if(!this.recordLength)this.recordStartFrame=currentFrame+i;
        this.recordBuffer[this.recordLength++]=input?.[i]||0;
        if(this.recordLength===this.recordBuffer.length)this.flushRecording();
      }
    }
    if (!input) return true;
    this.intervals = this.intervals.filter(frame => frame.endFrame > currentFrame);
    for (let i = 0; i < input.length; i += 1) {
      if (Math.abs(input[i]) < 300 / 32768) continue;
      const at = currentFrame + i;
      const frame = this.intervals.find(item => item.startFrame <= at && item.endFrame > at);
      if (frame && !this.seen.has(frame.replyId)) {
        this.seen.add(frame.replyId);
        if (this.seen.size > 128) this.seen.delete(this.seen.values().next().value);
        this.port.postMessage({type:"graph_onset", replyId:frame.replyId,
          generationEpoch:frame.generationEpoch, onsetCtx:at/sampleRate});
      }
    }
    return true;
  }
}
registerProcessor("output-observer", OutputObserver);
