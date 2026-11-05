function loginDestination() {
  const next = new URLSearchParams(location.search).get('next');
  if (!next || !next.startsWith('/') || next.startsWith('//') || /[\\\x00-\x1f]/.test(next)) return '/';
  try {
    const target = new URL(next, location.origin);
    const pages = ['/', '/index.html', '/home.html', '/practice.html', '/writing.html', '/text.html', '/voice.html'];
    return target.origin === location.origin && pages.includes(target.pathname)
      ? target.pathname + target.search + target.hash : '/';
  } catch { return '/'; }
}
let register = false;
const form = document.getElementById('loginForm');
const error = document.getElementById('error');
const submit = document.getElementById('submit');
const toggle = document.getElementById('switchMode');
toggle.onclick = () => {
  register = !register;
  document.getElementById('title').textContent = register ? '开始你的学习旅程' : '欢迎回来';
  document.getElementById('subtitle').textContent = register ? '创建账户，拥有自己的学习空间。' : '登录你的账户，继续每天的学习。';
  document.getElementById('password').autocomplete = register ? 'new-password' : 'current-password';
  submit.textContent = register ? '注册并登录' : '登录';
  toggle.textContent = register ? '已有账户？去登录' : '还没有账户？注册新账户';
  error.textContent = '';
};
form.onsubmit = async event => {
  event.preventDefault(); submit.disabled = true; toggle.disabled = true; error.textContent = '';
  try {
    const response = await fetch('/api/auth/' + (register ? 'register' : 'login'), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:form.elements.username.value, password:form.elements.password.value})
    });
    const data = await response.json();
    if (!response.ok) { error.textContent = data.message || '请求失败，请稍后重试'; return; }
    form.reset();
    try { localStorage.setItem('kitty-account-change', String(Date.now())); } catch {}
    location.replace(loginDestination());
  } catch { error.textContent = '暂时无法连接，请检查网络后重试'; }
  finally { submit.disabled = false; toggle.disabled = false; }
};
