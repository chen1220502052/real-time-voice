class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.frameSize = Math.round(sampleRate / 10);
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let index = 0; index < input.length; index += 1) this.samples.push(input[index]);
    while (this.samples.length >= this.frameSize) {
      const frame = new Float32Array(this.samples.splice(0, this.frameSize));
      this.port.postMessage(frame, [frame.buffer]);
    }
    return true;
  }
}
registerProcessor("capture-processor", CaptureProcessor);
