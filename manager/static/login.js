document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = document.getElementById('err');
  err.textContent = '';
  const r = await fetch('/api/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: document.getElementById('pw').value}),
  });
  if (r.ok) location.href = '/';
  else err.textContent = '密码错误';
});
