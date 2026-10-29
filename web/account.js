/* SQLite is authoritative. Browser storage is a per-account retry journal only. */
(function () {
  const user = window.KittyAccountUser;
  const prefix = user ? `kitty-user:${user.id}:` : '';
  const keys = {'kitty-learning:weekend-v1':'progress', 'kitty-course-selection:v1':'course'};
  const documents = {}, pending = {};
  let initialized = false, conflict = false, running = null, timer, syncLabel, retryButton, cloudButton;
  let syncAction = 'none';
  let message = user ? '正在读取学习档案…' : '';
  const local = {
    get(key) { try { return localStorage.getItem(prefix + key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(prefix + key, value); return true; } catch { return false; } },
    remove(key) { try { localStorage.removeItem(prefix + key); } catch {} }
  };
  function status(text, action='none') {
    message = text; syncAction = action;
    if (syncLabel) syncLabel.textContent = text;
    if (retryButton) retryButton.hidden = action !== 'retry';
    if (cloudButton) cloudButton.hidden = action !== 'cloud';
  }
  function persist(kind, entry) { return local.set('pending:' + kind, JSON.stringify(entry)); }
  function clearEntry(kind, entry) {
    try {
      if (JSON.parse(local.get('pending:' + kind))?.id === entry.id) local.remove('pending:' + kind);
    } catch {}
  }
  function entry(value, revision) { return {value, revision, id:Date.now()+':'+Math.random()}; }
  function authorized(response) {
    if (response.status === 401) {
      status('登录已变化，请重新登录；未同步记录已保留');
      location.replace('/login.html');
      throw Error('AUTH_REQUIRED');
    }
  }
  async function drain() {
    for (const kind of ['progress', 'course']) {
      while (pending[kind] && !conflict) {
        const sent = pending[kind];
        status('正在同步学习档案…');
        try {
          const response = await fetch('/api/account/learning/' + kind, {
            method:'PUT', keepalive:true,
            headers:{'Content-Type':'application/json', 'X-Account-ID':user.id},
            body:JSON.stringify({revision:sent.revision, value:JSON.parse(sent.value)})
          });
          authorized(response);
          if (response.status === 409) {
            conflict = true;
            status('其他页面或设备已更新进度。请使用云端进度后继续。', 'cloud');
            return false;
          }
          if (!response.ok) throw Error('SAVE_FAILED');
          const result = await response.json();
          if (result.userId !== user.id) throw Error('ACCOUNT_CHANGED');
          documents[kind] = {value:sent.value, revision:result.revision};
          if (pending[kind] === sent) { delete pending[kind]; clearEntry(kind, sent); }
          else { pending[kind].revision = result.revision; persist(kind, pending[kind]); }
        } catch {
          status('尚未同步，请重试；关闭前请等待同步完成', 'retry');
          return false;
        }
      }
    }
    if (conflict) return false;
    status('学习档案已同步');
    return true;
  }
  function flush() {
    clearTimeout(timer);
    if (!user) return Promise.resolve(true);
    if (conflict) return Promise.resolve(false);
    if (running) return running;
    running = drain().finally(() => { running = null; });
    return running;
  }
  async function initialize() {
    if (!user) { initialized = true; return; }
    const response = await fetch('/api/account/learning', {cache:'no-store', headers:{'X-Account-ID':user.id}});
    authorized(response);
    if (!response.ok) throw Error('LOAD_FAILED');
    const result = await response.json();
    if (result.userId !== user.id) throw Error('ACCOUNT_CHANGED');
    for (const [key, kind] of Object.entries(keys)) {
      const server = result.documents[kind];
      documents[kind] = {revision:server.revision, value:server.value === null ? null : JSON.stringify(server.value)};
      let saved;
      try { saved = JSON.parse(local.get('pending:' + kind)); } catch {}
      if (saved && typeof saved.value === 'string' && Number.isSafeInteger(saved.revision) && typeof saved.id === 'string') {
        pending[kind] = saved;
      } else if (server.revision === 0) {
        // Only import this account's old records, never shared anonymous data.
        const legacy = local.get(key);
        try {
          if (JSON.parse(legacy)?.version === 1) { pending[kind] = entry(legacy, 0); persist(kind, pending[kind]); }
        } catch {}
      }
    }
    await flush();
    if (conflict) throw Error('LEARNING_CONFLICT');
    initialized = true;
  }
  const ready = initialize().catch(error => {
    if (!conflict) status('读取学习档案失败，请刷新重试；原有数据已保留', 'retry');
    throw error;
  });
  ready.catch(() => {});
  window.KittyAccount = {user, ready, flush, storage: {
    getItem(key) {
      if (!user) return localStorage.getItem(key);
      if (!initialized) throw Error('Learning profile not ready');
      const kind = keys[key];
      return pending[kind]?.value ?? documents[kind]?.value ?? null;
    },
    setItem(key, value) {
      if (!user) return localStorage.setItem(key, value);
      const kind = keys[key];
      if (!initialized || conflict || !kind) throw Error('Learning profile unavailable');
      const parsed = JSON.parse(value);
      if (parsed?.version !== 1 || value.length > 48000) throw Error('Invalid learning document');
      if ((pending[kind]?.value ?? documents[kind]?.value) === value) return;
      pending[kind] = entry(value, documents[kind].revision);
      const cached = persist(kind, pending[kind]);
      status(cached ? '正在同步学习档案…' : '正在同步；请勿关闭此页');
      clearTimeout(timer); timer = setTimeout(flush, 250);
    }
  }};
  async function verify() {
    try {
      const response = await fetch('/api/auth/me', {cache:'no-store'});
      if (response.status === 401) return location.replace('/login.html');
      if (!response.ok) return;
      const data = await response.json();
      if (data.enabled && data.user?.id !== user?.id) location.reload();
    } catch {}
  }
  window.addEventListener('pageshow', verify);
  window.addEventListener('focus', verify);
  window.addEventListener('online', () => { if (initialized) flush(); });
  window.addEventListener('pagehide', () => { if (initialized) flush(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden && initialized) flush(); });
  window.addEventListener('storage', event => { if (event.key === 'kitty-account-change') verify(); });
  document.addEventListener('DOMContentLoaded', () => {
    if (!user) return;
    const style = document.createElement('link');
    style.rel = 'stylesheet'; style.href = '/account.css'; document.head.append(style);
    const panel = document.createElement('div'); panel.className = 'account-menu';
    const label = document.createElement('span'); label.textContent = user.username;
    syncLabel = document.createElement('span'); syncLabel.className = 'account-sync';
    syncLabel.setAttribute('role', 'status'); syncLabel.textContent = message;
    const retry = document.createElement('button'); retry.textContent = '重试同步'; retry.type = 'button';
    retry.onclick = () => { if (initialized) flush(); else location.reload(); };
    retryButton = retry; retry.hidden = syncAction !== 'retry';
    const reload = document.createElement('button'); reload.textContent = '使用云端进度'; reload.type = 'button';
    cloudButton = reload; reload.hidden = syncAction !== 'cloud';
    reload.onclick = () => {
      // Keep a recovery copy of unsynced edits when explicitly selecting cloud state.
      for (const [kind, value] of Object.entries(pending)) {
        if (!local.set('conflict-backup:' + kind + ':' + Date.now(), JSON.stringify(value))) {
          status('无法备份未同步记录，请勿关闭页面'); return;
        }
        clearEntry(kind, value);
      }
      location.reload();
    };
    const button = document.createElement('button'); button.textContent = '退出登录'; button.type = 'button';
    button.onclick = async () => {
      button.disabled = true;
      try {
        await ready;
        if (!await flush()) { button.disabled = false; return; }
        const response = await fetch('/api/auth/logout', {method:'POST'});
        if (!response.ok) throw Error();
        try { localStorage.setItem('kitty-account-change', String(Date.now())); } catch {}
        location.replace('/login.html');
      } catch { button.disabled = false; if (!conflict) status('请先完成同步后再退出登录', 'retry'); }
    };
    panel.append(label, syncLabel, retry, reload, button); document.body.append(panel);
    const student = document.querySelector('.student');
    if (student) { student.querySelector('b').textContent = user.username; student.querySelector('small').textContent = '学习档案随账户同步'; }
  });
})();
