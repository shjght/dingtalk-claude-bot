
const qs=new URLSearchParams(location.search);
let TOK=qs.get('token')||localStorage.getItem('panel_token')||'';
if(qs.get('token')){localStorage.setItem('panel_token',TOK);
  history.replaceState(null,'',location.pathname);}
let ST=null; // 最近一次状态快照

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

/* ---------- 底部抽屉 ---------- */
function openSheet(title,items){
  document.getElementById('sheetTitle').textContent=title;
  const body=document.getElementById('sheetBody');
  body.innerHTML=items.map((it,i)=>
    it.html
    ?'<div class="optinfo">'+it.html+'</div>'
    :'<button class="opt'+(it.newest?' new':'')+'" data-i="'+i+'">'
    +(it.icon?it.icon+' ':'')+esc(it.label)
    +(it.desc?'<small>'+esc(it.desc)+'</small>':'')
    +(it.cur?'<span class="chk">✓ 当前</span>':'')
    +'</button>').join('');
  body.querySelectorAll('.opt').forEach(btn=>btn.onclick=()=>{
    const it=items[+btn.dataset.i]; closeSheet(); post(it.cmd);
  });
  document.getElementById('mask').classList.add('show');
  document.getElementById('sheet').classList.add('show');
}
function closeSheet(){
  document.getElementById('mask').classList.remove('show');
  document.getElementById('sheet').classList.remove('show');
}
function sheetSession(){
  if(!ST)return;
  const items=(ST.sessions||[]).map(n=>({label:n,cur:n===ST.session,icon:'🗂',cmd:'/s '+n}));
  items.push({label:'新建/清空当前会话',icon:'🆕',newest:true,desc:'新起一条独立记忆的对话',cmd:'/new'});
  openSheet('切换会话',items);
}
function sheetModel(){
  if(!ST)return;
  const cur=ST.model||'';
  const items=[{label:'claude 默认',cur:!cur,icon:'🧠',cmd:'/model 0'}];
  (ST.models||[]).forEach((m,i)=>items.push({label:m,cur:cur===m,icon:'🧠',cmd:'/model '+(i+1)}));
  openSheet('切换模型',items);
}
function sheetMode(){
  if(!ST)return;
  const order=Object.keys(ST.modes||{});
  const items=[{label:'default（默认：白名单外发手机审批）',cur:ST.mode==='default',icon:'🛡',cmd:'/mode 0'}];
  order.forEach((k,i)=>items.push({label:k+' —— '+(ST.modes[k]||''),cur:ST.mode===k,icon:'🛡',cmd:'/mode '+(i+1)}));
  openSheet('切换权限模式',items);
}

/* ---------- 上传 ---------- */
function pick(kind){
  (kind==='photo'?document.getElementById('fphoto'):document.getElementById('ffile')).click();
}
async function upload(file){
  if(!file)return;
  const up=document.getElementById('uploading');
  document.getElementById('upmsg').textContent='正在上传 '+file.name+'…';
  up.style.display='flex';
  try{
    const fd=new FormData(); fd.append('file',file);
    const r=await fetch('/api/media',{method:'POST',headers:{'X-Panel-Token':TOK},body:fd});
    const j=await r.json().catch(()=>({}));
    if(!r.ok||j.err)alert('上传失败：'+(j.err||r.status));
  }catch(e){alert('网络错误：'+e);}
  up.style.display='none';
  setTimeout(poll,400);
}
document.getElementById('fphoto').addEventListener('change',e=>{upload(e.target.files[0]);e.target.value='';});
document.getElementById('ffile').addEventListener('change',e=>{upload(e.target.files[0]);e.target.value='';});

/* ---------- 渲染 ---------- */
function fmtTok(n){n=+n||0;return n>=100000?(Math.round(n/1000)+'k'):(n>=1000?((n/1000).toFixed(1)+'k'):(''+n));}
function usageBadge(u,title){
  if(!u||(!u.out&&!u.in))return '';
  let s='🪙';
  if(u.out)s+=' ↑'+fmtTok(u.out);
  if(u.in)s+=' ↓'+fmtTok(u.in);
  if(u.cache)s+=' ⚡'+fmtTok(u.cache);
  if(u.cost)s+=' · $'+(+u.cost).toFixed(4);
  return '<span class="badge w click" onclick="sheetUsage()" title="'+title+'">'+s+'</span>';
}
function sheetUsage(){
  if(!ST)return;
  const a=ST.usage_all||{},t=ST.usage||{};
  const row=(l,v)=>'<div style="display:flex;justify-content:space-between;padding:3px 0">'
    +'<span style="color:var(--dim)">'+l+'</span><b>'+v+'</b></div>';
  const sec=(title,u)=>{
    if(!u||(!u.out&&!u.in&&!u.turns))return '<div style="color:var(--faint);padding:4px 0">'+title+'：暂无</div>';
    return '<h4>'+title+'</h4>'+row('输出 tokens',fmtTok(u.out||0))+row('输入 tokens',fmtTok(u.in||0))
      +row('缓存命中',fmtTok(u.cache||0))+(u.turns?row('API 轮次',u.turns):'')
      +(u.cost?row('费用','$'+(+u.cost).toFixed(4)):'');
  };
  openSheet('🪙 Token 消耗',[{html:sec('本任务',t)+'</div><div>'+sec('历史累计（'+Math.round(a.tasks||0)+' 个任务）',a)}]);
}
function render(d){
  ST=d;
  const run=!!d.task;
  document.getElementById('dot').className='dot '+(run?'run':(d.approval?'ok':''));
  const ph=run?((d.task.phase||'运行中')+' · '+d.task.mins+'分'):(d.approval?'等你批复':'空闲');
  document.getElementById('phasetxt').textContent=ph;
  document.title=run?('▶ '+ph):'claude 遥控';
  document.getElementById('taskline').innerHTML=run
    ?('🏃 <b>'+esc(d.task.title)+'</b>')
    :'💤 当前没有正在执行的任务';
  const badges=[
    '<span class="badge click" onclick="sheetSession()">🗂 会话 '+esc(d.session)+' <span class="ar">▼</span></span>',
    '<span class="badge click" onclick="sheetModel()">🧠 模型 '+(esc(d.model)||'默认')+' <span class="ar">▼</span></span>',
    '<span class="badge click" onclick="sheetMode()">🛡 模式 '+esc(d.mode)+' <span class="ar">▼</span></span>',
    usageBadge(d.usage_all,'历史累计 token 消耗，点看明细')
  ];
  if(d.usage&&d.usage.out)badges.push(usageBadge(d.usage,'本任务消耗，点看明细').replace('class="badge w click"','class="badge click"'));
  if(d.queue>0)badges.push('<span class="badge w">排队 '+d.queue+'</span>');
  if(!d.dingtalk_ok)badges.push('<span class="badge w">⚠ 手机未发过消息，暂不能发指令</span>');
  document.getElementById('badges').innerHTML=badges.join('');

  let h='';
  if(d.approval){
    h+='<div class="card appr"><h4>⏳【审批 '+d.approval.seq+'】等你批复</h4>'
      +'<div class="desc">'+esc(d.approval.desc)+'</div>'
      +'<div class="row">'
      +'<button class="b b-ok" onclick="appr(1)">✓ 允许</button>'
      +'<button class="b b-no" onclick="appr(2)">✗ 拒绝</button>'
      +'</div><div class="row">'
      +'<button class="b b-all" style="flex:0 0 100%" onclick="appr(4)">✓✓ 允许且本任务内同类不再问</button>'
      +'</div><div class="row">'
      +'<input id="guide" placeholder="拒绝并给指示，如：改用 python">'
      +'<button class="b b-gray" style="flex:0 0 auto;min-width:92px" onclick="appr3()">转达指示</button>'
      +'</div></div>';
  }
  const rows=(d.replies||[]).slice(-30).map(r=>
    '<div class="msg"><span class="t">'+esc(r.t)+'</span>'+esc(r.text)+'</div>').join('');
  const chat=rows
    ?('<div class="card"><h4>💬 机器人回复（镜像钉钉）</h4>'+rows+'</div>')
    :'';
  // 思维链（Claude 风格）：思考块可折叠——最新一块在思考时自动展开
  const tr=d.trace||[];
  let live='';
  if(tr.length){
    const thinking=run&&/思考/.test((d.task&&d.task.phase)||'');
    live='<div class="card"><h4>🧠 思维链（点折叠块展开）</h4>'+tr.map((e,i)=>{
      const last=i===tr.length-1;
      const chip=(e.u&&e.u.out)?'<span class="tk">+'+fmtTok(e.u.out)+'</span>':'';
      if(e.k==='think'){
        return '<div class="thk'+(last&&thinking?' open':'')+'">'
          +'<div class="th"><span class="ar">▶</span>思考'+chip+'</div>'
          +'<div class="tb">'+esc(e.text)+'</div></div>';
      }
      if(e.k==='tool'){
        const one=e.text.indexOf('\n')<0&&e.text.length<=56;
        return '<div class="tlk"><div class="th"><span class="ar">▶</span>🔧 '+esc(e.name)
          +(one?' <code>'+esc(e.text)+'</code>':'')+chip+'</div>'
          +'<div class="tb"><code>'+esc(e.text)+'</code></div></div>';
      }
      if(e.k==='ask'){
        return '<div class="askline">⏳ 等审批：'+esc(e.text)+'</div>';
      }
      return '<div class="msg" style="margin-bottom:8px">'+esc(e.text)+chip+'</div>';
    }).join('')+'</div>';
  }
  document.getElementById('feed').innerHTML=h+chat+live
    || '<div id="hint"><span class="big">🛰️</span>还没有消息<br>在下方输入任务开始<br>或先用手机钉钉发一条消息</div>';
  const m=document.getElementById('feed');
  const nearBottom=m.scrollHeight-m.scrollTop-m.clientHeight<180;
  if(nearBottom||d.approval)m.scrollTop=m.scrollHeight;
  document.getElementById('topbtn').style.display=
    (m.scrollHeight-m.scrollTop-m.clientHeight>400)?'block':'none';
}

/* ---------- 全屏（App 观感：隐藏地址栏等浏览器框） ---------- */
function toggleFs(){
  const d=document;
  if(d.fullscreenElement||d.webkitFullscreenElement){
    (d.exitFullscreen||d.webkitExitFullscreen).call(d);return;}
  const el=d.documentElement;
  const go=el.requestFullscreen||el.webkitRequestFullscreen;
  if(go)go.call(el);
}
document.addEventListener('fullscreenchange',()=>{
  document.getElementById('fsbtn').style.color=
    document.fullscreenElement?'var(--acc)':'';
});

let failCnt=0;
async function poll(){
  try{
    if(!TOK){
      TOK=prompt('首次使用，请粘贴面板令牌（token）。\n在电脑上打开 config.json，复制 panel_token 的值：')||'';
      if(!TOK){document.getElementById('feed').innerHTML=
        '<div id="hint"><span class="big">🔑</span>未填令牌，刷新页面可重新输入</div>';return;}
      localStorage.setItem('panel_token',TOK);
    }
    const ctl=new AbortController();
    const timer=setTimeout(()=>ctl.abort(),4000);   // 4 秒无响应视为离线（中继半死时 fetch 会无限挂起）
    const r=await fetch('/api/state',{headers:{'X-Panel-Token':TOK},signal:ctl.signal});
    clearTimeout(timer);
    if(r.status===403){
      localStorage.removeItem('panel_token');TOK='';
      document.getElementById('feed').innerHTML=
        '<div id="hint"><span class="big">🔒</span>令牌不对，已清除<br>刷新页面可重新输入</div>';
      document.getElementById('phasetxt').textContent='令牌错误';return;}
    failCnt=0;
    render(await r.json());
  }catch(e){
    // 连续失败：大概率是手机 Tailscale 掉线或电脑关机，明确提示而不是干等
    if(++failCnt===3){
      document.getElementById('feed').innerHTML=
        '<div id="hint"><span class="big">📡</span>连不上电脑<br>请检查手机 Tailscale 是否开启（Connected）<br>电脑需开机且机器人黑窗在运行<br><small>恢复后本页会自动连上</small></div>';
      document.getElementById('phasetxt').textContent='离线';
    }
  }
}
async function post(text){
  try{
    const r=await fetch('/api/send',{method:'POST',
      headers:{'Content-Type':'application/json','X-Panel-Token':TOK},
      body:JSON.stringify({text})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok||j.err){alert('发送失败：'+(j.err||r.status));}
  }catch(e){alert('网络错误：'+e);}
  setTimeout(poll,400);
}
function appr(n){post(String(n));}
function appr3(){const g=document.getElementById('guide');
  const t=(g.value||'').trim(); if(!t){g.focus();return;}
  g.value=''; post('3 '+t);}
function send(){const el=document.getElementById('txt');
  const t=el.value.trim(); if(!t)return; el.value=''; post(t);}
document.getElementById('send').onclick=send;
document.getElementById('txt').addEventListener('keydown',e=>{if(e.key==='Enter')send();});
document.querySelectorAll('#quick button').forEach(b=>b.onclick=()=>{
  if(b.dataset.q)post(b.dataset.q);
  else if(b.dataset.a==='sheet')sheetSession();
  else if(b.dataset.a==='new')post('/new');
  else if(b.dataset.a==='sheet2')sheetMode();
});
poll();setInterval(poll,2000);
