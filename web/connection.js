// Connection setup only. No microphone, playback or duplex state lives here.
(() => {
  class ConnectionAttempt {
    constructor() {
      this.id = crypto.randomUUID();
      this.started = performance.now();
      this.reported = new Set();
    }
    async bootstrap() {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch('/api/browser-session', {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        });
        if (response.status === 401 || response.status === 403) {
          throw Object.assign(new Error('登录已失效，请刷新页面重新登录。'), {code:'AUTH_REQUIRED'});
        }
        if (!response.ok) throw new Error('服务暂时无法完成登录验证，请稍后重试。');
        const data = await response.json();
        if (!data.ok) throw new Error('服务未完成登录验证。');
        return data;
      } catch (error) {
        if (error.name === 'AbortError') throw Object.assign(new Error('登录验证超时，请检查网络后重试。'), {code:'AUTH_TIMEOUT'});
        if (error.code) throw error;
        throw Object.assign(new Error('无法连接登录服务，请检查网络后重试。'), {code:'AUTH_UNAVAILABLE'});
      } finally { clearTimeout(timer); }
    }
    report(code) {
      if (this.reported.has(code)) return;
      this.reported.add(code);
      // HTTP channel survives WS setup failures. Never upload arbitrary error
      // text, URLs, browser credentials, transcripts or cookies in the body.
      fetch('/api/connection-events', {method:'POST', credentials:'same-origin', keepalive:true,
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({attemptId:this.id,code,elapsedMs:performance.now()-this.started})}).catch(()=>{});
    }
    static busyMessage(resources) {
      return `当前通话名额已满（${resources.inUse}/${resources.capacity}），请稍后再试。`;
    }
  }
  window.ConnectionAttempt = ConnectionAttempt;
})();
