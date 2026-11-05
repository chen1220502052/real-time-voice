// This energy gate can defer an unheard reply, never cancel playing speech.
export class PlaybackStartGuard {
  constructor() { this.reset(); }
  reset() { this.until = 0; this.streak = 0; this.replyId = null; this.heldAt = null; this.announced = false; }
  activity(rms, atCtx) {
    this.streak = rms >= .006 ? this.streak + 1 : 0;
    if (this.streak >= 2) this.until = atCtx + .2;
  }
  shouldHold(now, replyId) {
    if (replyId !== this.replyId) { this.replyId = replyId; this.heldAt = null; this.announced = false; }
    if (now >= this.until) return false;
    this.heldAt ??= now;
    // Sustained background energy must not suppress a response indefinitely.
    return now - this.heldAt < .9;
  }
  takeHoldNotice() { if(this.announced)return false;this.announced=true;return true; }
}
