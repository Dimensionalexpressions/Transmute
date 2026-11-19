"""The serve-mode page: one HTML string, no frameworks, no CDNs.

Everything textual in the picker comes from /api/info, which comes from
ConverterController - the same words the desktop app shows, including the
machine's own reason for anything unavailable (F3 stays true on the web).
"""

PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transmute - convert on this machine</title>
<style>
:root{color-scheme:dark;background:#101014;color:#e6e6ea;font-family:system-ui,sans-serif}
body{margin:0;padding:24px;max-width:860px;margin-inline:auto}
h1{font-size:20px;margin:0 0 4px} .sub{color:#8a8a94;font-size:13px;margin-bottom:20px}
#drop{border:1.5px dashed #3a3a46;border-radius:12px;padding:36px;text-align:center;cursor:pointer;transition:.15s}
#drop.over{border-color:#7aa2ff;background:#161622}
.card{border:1px solid #26262e;border-radius:10px;padding:12px;margin-top:10px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.card .name{font-weight:600;flex:1 1 200px;min-width:0;overflow-wrap:anywhere}
.card .cat{color:#8a8a94;font-size:12px}
select,input[type=number]{background:#1a1a22;color:#e6e6ea;border:1px solid #33333d;border-radius:7px;padding:6px 8px}
input[type=number]{width:74px}
button{background:#2f6fed;color:#fff;border:0;border-radius:7px;padding:7px 14px;cursor:pointer}
button:disabled{background:#33333d;color:#77777f;cursor:default}
.why{flex:1 1 100%;color:#c08457;font-size:12px}
table{width:100%;border-collapse:collapse;margin-top:8px}
td{padding:7px 6px;border-bottom:1px solid #22222a;font-size:14px;overflow-wrap:anywhere}
.st{font-size:12px;padding:2px 8px;border-radius:20px;white-space:nowrap}
.live{background:#243b12;color:#a5d97a} .done{background:#123a2a;color:#67d39b}
.bad{background:#46201e;color:#e88} .wait{background:#2a2a33;color:#9a9aa4}
.reason{color:#c08457;font-size:12px}
a.dl{color:#7aa2ff;text-decoration:none;font-size:13px;white-space:nowrap}
footer{margin-top:26px;color:#6a6a74;font-size:12px;line-height:1.6}
h2{font-size:15px;margin:26px 0 2px}
</style></head><body>
<h1>Transmute</h1>
<div class="sub">Drop a file - the conversion runs on this machine and the result comes back to you. Max upload: {{ '%.0f' % max_upload_mb }} MB.</div>
<div id="drop">Drop files here, or click to choose<input id="pick" type="file" multiple hidden></div>
<div id="gate" class="card" style="display:none"><span class="name">This Transmute is locked.</span><span class="cat">open the link shown on the computer running Transmute, or paste its access token</span><input id="gatein" type="password" placeholder="access token" style="flex:1 1 200px;background:#1a1a22;color:#e6e6ea;border:1px solid #33333d;border-radius:7px;padding:6px 8px"><button id="gatebtn">Open</button><span id="gateerr" class="why" style="display:none"></span></div>
<div id="warm" class="why" style="display:none">This machine is still benchmarking itself - the options below appear as soon as it has measured what it can do. You can upload already.</div>
<h2>Convert</h2><div id="files"></div>
<h2>Jobs <button id="cancel" style="float:right;padding:3px 10px;font-size:12px">Cancel queued</button></h2>
<table id="jobs"></table>
<footer>Files stay on this computer - nothing is sent anywhere. Availability, hardware
badges and reasons come from this machine's own benchmark, so what you can pick is
what this machine has measured it can do.</footer>
<script>
"use strict";
let INFO=null, uploads={}, pending=[];
const $=s=>document.querySelector(s);
let TK=sessionStorage.getItem('trtoken')||'';
{const q=new URLSearchParams(location.search),t=q.get('token');
 if(t){TK=t;sessionStorage.setItem('trtoken',t);history.replaceState(null,'',location.pathname)}}
if(TK)document.cookie='trtoken='+encodeURIComponent(TK)+';path=/;samesite=lax';
const esc=t=>{const d=document.createElement('span');d.textContent=t;return d.innerHTML;};
async function j(u,opt){opt=opt||{};const h=opt.headers||{};if(TK)h['x-token']=TK;opt.headers=h;
 const r=await fetch(u,opt);if(!r.ok){let m=r.statusText;try{m=(await r.json()).detail||m}catch(e){}
  if(r.status===401)askToken();
  throw new Error(m)}return r.json()}
function askToken(){$('#gate').style.display='flex';$('#drop').style.visibility='hidden'}
$('#gatebtn').onclick=()=>{TK=$('#gatein').value.trim();if(!TK)return;
 sessionStorage.setItem('trtoken',TK);document.cookie='trtoken='+encodeURIComponent(TK)+';path=/;samesite=lax';
 $('#gateerr').style.display='none';boot().catch(e=>{$('#gateerr').textContent=e.message;$('#gateerr').style.display=''})};
$('#gatein').onkeydown=e=>{if(e.key==='Enter')$('#gatebtn').click()};
async function boot(){
  let info;
  try{info=await j('/api/info')}
  catch(err){if(String(err.message).includes('token')){askToken();return}throw err}
  $('#gate').style.display='none';$('#drop').style.visibility='';
  if(info.warming){$('#warm').style.display='';setTimeout(boot,3000);return}
  INFO=info;$('#warm').style.display='none';
  pending.forEach(renderUpload);pending=[];
  if(!boot.ticked){boot.ticked=true;setInterval(refresh,1000)}
}
const drop=$('#drop');
drop.onclick=()=>$('#pick').click();
$('#pick').onchange=e=>{[...e.target.files].forEach(up);e.target.value=''};
['dragover','dragleave','drop'].forEach(ev=>drop.addEventListener(ev,x=>{
  x.preventDefault();drop.classList.toggle('over',ev==='dragover');
  if(ev==='drop')[...x.dataTransfer.files].forEach(up);}));
async function up(file){
  try{
    const fd=new FormData();fd.append('file',file);
    const u=await j('/api/upload',{method:'POST',body:fd});
    uploads[u.upload_id]=u;
    if(INFO)renderUpload(u);else pending.push(u);refresh();
  }catch(err){alert('Upload failed: '+err.message)}
}
function renderUpload(u){
  const c=INFO.categories[u.category];
  const card=document.createElement('div');card.className='card';
  card.innerHTML=`<span class="name">${esc(u.name)}</span><span class="cat">${esc(u.category||'unknown')}</span>`;
  if(!c||!c.targets.length){
    card.insertAdjacentHTML('beforeend',`<span class="why">no targets for this file type</span>`);
    $('#files').append(card);return;
  }
  const sel=document.createElement('select');
  for(const t of c.targets){
    const o=document.createElement('option');
    o.value=t.target;o.textContent=t.label+(t.available?'':' - not here');
    o.disabled=!t.available;o.dataset.enc=t.encoders.join(',');
    o.title=t.available?(t.note||t.about||t.target):(t.reason||'unavailable');
    sel.append(o);
  }
  sel.onchange=()=>sync();
  const enc=document.createElement('select');enc.style.display='none';
  const qual=document.createElement('input');qual.type='number';qual.min=1;qual.max=100;qual.value=80;
  if(u.category!=='image')qual.style.display='none';
  const why=document.createElement('span');why.className='why';why.style.display='none';
  const btn=document.createElement('button');btn.textContent='Convert';
  btn.onclick=async()=>{
    btn.disabled=true;
    const body={upload_id:u.upload_id,target:sel.value};
    if(enc.style.display!=='none'&&enc.value)body.encoder=enc.value;
    if(qual.style.display!=='none')body.quality=+qual.value;
    try{await j('/api/convert',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify(body)});refresh();
    }catch(err){alert('Could not start: '+err.message)}
    btn.disabled=false;
  };
  function sync(){
    const t=c.targets.find(x=>x.target===sel.value);
    const list=t&&t.available?t.encoders:[];
    enc.innerHTML='';
    if(list&&list.length){
      enc.append(new Option('encoder: auto',''));
      for(const e of list)enc.append(new Option(e,e));
      enc.style.display='';
    }else enc.style.display='none';
    const bad=c.targets.filter(t=>!t.available);
    if(!bad.length){why.style.display='none';return}
    why.textContent=bad.map(t=>t.label+': '+(t.reason||'unavailable')).join('  ·  ');
    why.style.display='';
  }
  sync();
  card.append(sel,enc,qual,btn,why);
  $('#files').append(card);
}
$('#cancel').onclick=()=>j('/api/cancel',{method:'POST'}).then(refresh);
async function refresh(){
  let jobs;try{jobs=await j('/api/jobs')}catch(e){return}
  const tb=$('#jobs');tb.innerHTML='';
  for(const k of jobs){
    const tr=document.createElement('tr');
    let cls='wait',txt=k.state;
    if(k.live){cls='live';txt={queued:'waiting',running:'converting',verifying:'checking output'}[k.state]||k.state}
    else if(k.state==='replaced'){cls='done';txt='done'}
    else if(['failed','interrupted'].includes(k.state)){cls='bad';txt=k.state}
    tr.innerHTML=`<td>${esc(k.name)}</td><td><span class="st ${cls}">${esc(txt)}</span></td>`
      +`<td class="reason">${esc(k.reason||'')}</td>`
      +`<td>${k.download?`<a class="dl" href="/download/${k.job_id}">download</a>`:''}</td>`;
    tb.append(tr);
  }
}
boot();
</script></body></html>
"""
