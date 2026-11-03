const values=new URLSearchParams(location.hash.slice(1));
history.replaceState(null,'',location.pathname);
const server=values.get('server'),code=values.get('code');
const link=document.getElementById('connect');
if(server===location.origin&&/^[A-Za-z0-9_-]{32,100}$/.test(code||'')){
  const query=new URLSearchParams({server,code});
  link.href='intent://connect?'+query+'#Intent;scheme=aidjmusic;package=com.aidj.music;end';
  link.hidden=false;document.getElementById('status').textContent='Открой приложение, чтобы подключить свою музыку.';
}else document.getElementById('status').textContent='Ссылка недействительна. Открой новую через Telegram: Музыка → Настройки → Подключить Android-приложение.';
