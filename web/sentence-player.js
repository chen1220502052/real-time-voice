/* Play one bounded excerpt of the original recording; never continue to the next line. */
(function (root) {
  class SentencePlayer {
    constructor(audio, {onStart = () => {}, onError = () => {}, onFinish = () => {}} = {}) {
      this.audio = audio; this.onStart = onStart; this.onError = onError; this.onFinish = onFinish;
      this.version = 0; this.active = null; this.timer = null; this.metadata = null;
      for (const event of ['playing', 'timeupdate', 'seeked', 'ratechange']) {
        audio.addEventListener(event, () => this.checkBoundary());
      }
      audio.addEventListener('waiting', () => clearTimeout(this.timer));
      audio.addEventListener('ended', () => { const completed = Boolean(this.active); this.stop(); if (completed) this.onFinish(); });
      audio.addEventListener('error', () => {
        if (this.active) { this.stop(); this.onError(); }
      });
    }
    stop() {
      this.version++;
      clearTimeout(this.timer);
      if (this.metadata) this.audio.removeEventListener('loadedmetadata', this.metadata);
      this.metadata = null;
      if (this.active) {
        this.active.button.setAttribute('aria-pressed', 'false');
        this.active.button.textContent = '▶';
      }
      this.active = null;
      this.audio.pause();
    }
    play(clip, button) {
      if (this.active?.button === button) { this.stop(); return; }
      this.stop();
      if (!clip.url || !Number.isFinite(clip.start) || !Number.isFinite(clip.end)
          || clip.start < 0 || clip.end <= clip.start) return;
      this.onStart();
      const version = this.version;
      this.active = {clip, button};
      button.setAttribute('aria-pressed', 'true'); button.textContent = '■';
      const begin = async () => {
        if (version !== this.version) return;
        this.metadata = null;
        try {
          if (Number.isFinite(this.audio.duration) && clip.end > this.audio.duration + 0.05) throw Error('Invalid excerpt');
          this.audio.currentTime = clip.start;
          await this.audio.play();
          if (version === this.version) this.checkBoundary();
        } catch {
          if (version === this.version) { this.stop(); this.onError(); }
        }
      };
      const sameSource = this.audio.getAttribute('src') === clip.url;
      if (sameSource && this.audio.readyState >= 1) { begin(); return; }
      this.metadata = begin;
      this.audio.addEventListener('loadedmetadata', begin, {once: true});
      if (!sameSource) { this.audio.src = clip.url; this.audio.load(); }
    }
    checkBoundary() {
      clearTimeout(this.timer);
      if (!this.active) return;
      const remaining = this.active.clip.end - this.audio.currentTime;
      if (remaining <= 0.015) { this.stop(); this.onFinish(); return; }
      if (!this.audio.paused && !this.audio.seeking) {
        // Recheck media time when the timer fires, since a network stall may delay playback.
        this.timer = setTimeout(() => this.checkBoundary(), Math.max(10, remaining / (this.audio.playbackRate || 1) * 1000));
      }
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = SentencePlayer;
  else root.KittySentencePlayer = SentencePlayer;
})(typeof window !== 'undefined' ? window : globalThis);
