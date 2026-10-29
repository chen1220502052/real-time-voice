// Audio-graph observation only. This is NOT a microphone/physical loopback.
class OutputObserver extends AudioWorkletProcessor {
  constructor() {
    super();
    this.intervals = [];
    this.seen = new Set();
    this.port.onmessage = ({data}) => {
      if (data.type === "schedule") {
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
  process(inputs, outputs) {
    const input = inputs[0]?.[0], output = outputs[0]?.[0];
    if (output && input) output.set(input);
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
