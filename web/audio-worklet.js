class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.frameSize = Math.round(sampleRate / 10);
    this.frameStart = null;
    this.expectedFrame = null;
    this.captureSequence = 0;
    this.activityEnergy = 0;
    this.activitySamples = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    if (this.expectedFrame !== null && this.expectedFrame !== currentFrame) {
      this.samples = []; // Never concatenate across a missing input interval.
      this.frameStart = null;
      this.activityEnergy = 0;
      this.activitySamples = 0;
    }
    for (let index = 0; index < input.length; index += 1) {
      this.activityEnergy += input[index]*input[index];
      this.activitySamples++;
      if (this.activitySamples >= Math.round(sampleRate*.02)) {
        this.port.postMessage({type:'voice_activity',
          rms:Math.sqrt(this.activityEnergy/this.activitySamples),
          atCtx:(currentFrame+index+1)/sampleRate});
        this.activityEnergy=0;this.activitySamples=0;
      }
      if (this.frameStart === null) this.frameStart = currentFrame + index;
      this.samples.push(input[index]);
      if (this.samples.length === this.frameSize) {
        const pcm = new Float32Array(this.samples);
        this.port.postMessage({pcm, captureSequence: this.captureSequence++,
          captureStartCtx: this.frameStart / sampleRate,
          captureEndCtx: (this.frameStart + this.frameSize) / sampleRate,
          sampleRate}, [pcm.buffer]);
        this.samples = [];
        this.frameStart = null;
      }
    }
    this.expectedFrame = currentFrame + input.length;
    return true;
  }
}
registerProcessor("capture-processor", CaptureProcessor);
