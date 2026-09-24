from typing import Any, Dict, Optional

from dashboard_store import dashboard
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()


@router.get("/api/dashboard")
async def dashboard_api() -> JSONResponse:
    return JSONResponse(dashboard.snapshot())


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>APK ↔ Server Dashboard</title>
<style>
  :root {
    --bg:#0b0f14; --panel:#12181f; --panel2:#1a222c; --border:#2a3441;
    --text:#e8eef5; --dim:#8b98a8; --blue:#3b82f6; --green:#22c55e;
    --yellow:#f59e0b; --red:#ef4444; --purple:#a78bfa; --cyan:#22d3ee;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 "Segoe UI",system-ui,sans-serif; }
  header {
    display:flex; align-items:center; gap:16px; flex-wrap:wrap;
    padding:14px 18px; background:var(--panel); border-bottom:1px solid var(--border);
    position:sticky; top:0; z-index:10;
  }
  header h1 { margin:0; font-size:16px; font-weight:600; letter-spacing:.4px; }
  .badge { background:var(--panel2); border:1px solid var(--border); border-radius:99px; padding:4px 10px; font-size:12px; color:var(--dim); }
  .badge b { color:var(--text); }
  .live { color:var(--green); }
  main { padding:16px; display:grid; gap:16px; max-width:1400px; margin:0 auto; }
  .grid4 { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:14px; }
  .card h2 { margin:0 0 10px; font-size:12px; color:var(--dim); text-transform:uppercase; letter-spacing:.8px; font-weight:600; }
  .stat { font-size:26px; font-weight:700; color:var(--blue); }
  .stat small { font-size:12px; color:var(--dim); font-weight:500; }
  .cols { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:960px){ .cols{grid-template-columns:1fr;} }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px 8px; border-bottom:1px solid var(--border); vertical-align:top; }
  th { color:var(--dim); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  tr:hover td { background:rgba(255,255,255,.02); }
  .pill { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; font-weight:600; }
  .pill.in { background:#14532d; color:#86efac; }
  .pill.out { background:#1e3a5f; color:#93c5fd; }
  .pill.sys { background:#3b2f14; color:#fcd34d; }
  .pill.err { background:#4c1d1d; color:#fca5a5; }
  .num { font-variant-numeric:tabular-nums; color:var(--yellow); font-weight:600; }
  .barwrap { background:var(--panel2); border-radius:6px; height:14px; position:relative; overflow:hidden; min-width:80px; }
  .bar { height:100%; background:linear-gradient(90deg,var(--blue),var(--cyan)); }
  .bar.stt { background:#f59e0b; }
  .bar.llm { background:#a78bfa; }
  .bar.tts { background:#22c55e; }
  .bar.q { background:#f472b6; }
  .muted { color:var(--dim); }
  .mono { font-family:Consolas,ui-monospace,monospace; font-size:12px; }
  .scroll { max-height:360px; overflow:auto; }
  .scroll-lg { max-height:480px; overflow:auto; }
  .empty { color:var(--dim); padding:12px 0; }
  .stagegrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:8px; margin-top:8px; }
  .stage { background:var(--panel2); border:1px solid var(--border); border-radius:8px; padding:8px; }
  .stage .k { font-size:10px; color:var(--dim); text-transform:uppercase; }
  .stage .v { font-size:15px; font-weight:700; color:var(--yellow); margin-top:2px; }
  .q { color:var(--text); }
  .a { color:#86efac; }
  .turn { border-bottom:1px solid var(--border); padding:10px 0; }
  .turn:last-child { border-bottom:0; }
  .wf { display:flex; height:22px; border-radius:6px; overflow:hidden; background:var(--panel2); margin-top:8px; }
  .wf > div { min-width:2px; display:flex; align-items:center; justify-content:center; font-size:10px; color:#0b0f14; font-weight:700; overflow:hidden; }
</style>
</head>
<body>
<header>
  <h1>APK ↔ PC Server Dashboard</h1>
  <span class="badge live">● LIVE <b id="age">—</b>s</span>
  <span class="badge">Uptime <b id="uptime">—</b></span>
  <span class="badge">Live sessions <b id="liveN">0</b></span>
  <span class="badge">Turns <b id="turnsN">0</b></span>
  <span class="badge">Auto-refresh 1s</span>
</header>
<main>
  <section class="grid4" id="kpis"></section>

  <section class="cols">
    <div class="card">
      <h2>Live sessions (from APK)</h2>
      <div class="scroll" id="sessions"><div class="empty">No live sessions</div></div>
    </div>
    <div class="card">
      <h2>Turn timing — where time is spent</h2>
      <div class="scroll-lg" id="turns"><div class="empty">No turns yet</div></div>
    </div>
  </section>

  <section class="card">
    <h2>Message log — what APK sends / receives</h2>
    <div class="scroll-lg">
      <table>
        <thead>
          <tr>
            <th>Time</th><th>Dir</th><th>Type</th><th>Detail</th><th>Bytes</th><th>Session</th>
          </tr>
        </thead>
        <tbody id="events"><tr><td colspan="6" class="muted">Waiting…</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="cols">
    <div class="card">
      <h2>Recent APK → Server audio chunk sizes</h2>
      <div id="chunks" class="mono muted">—</div>
    </div>
    <div class="card">
      <h2>Closed sessions</h2>
      <div class="scroll" id="closed"><div class="empty">None yet</div></div>
    </div>
  </section>
</main>
<script>
function fmtMs(ms){ if(ms==null||Number.isNaN(ms)) return "—"; return ms>=1000?(ms/1000).toFixed(2)+"s":Math.round(ms)+"ms"; }
function fmtBytes(n){
  if(n==null) return "0";
  if(n<1024) return n+" B";
  if(n<1048576) return (n/1024).toFixed(1)+" KB";
  return (n/1048576).toFixed(2)+" MB";
}
function fmtTime(t){
  const d=new Date(t*1000);
  return d.toLocaleTimeString();
}
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function kpi(label, value, sub){
  return `<div class="card"><h2>${label}</h2><div class="stat">${value}${sub?` <small>${sub}</small>`:""}</div></div>`;
}

function renderKpis(d){
  const c=d.counters||{};
  document.getElementById("uptime").textContent=Math.floor(d.uptime_s||0)+"s";
  document.getElementById("liveN").textContent=d.live_sessions?.length||0;
  document.getElementById("turnsN").textContent=c.turns_total||0;
  document.getElementById("age").textContent=Math.floor(d.uptime_s||0);
  document.getElementById("kpis").innerHTML =
    kpi("Bytes from APK", fmtBytes(c.bytes_from_apk), `${c.audio_chunks_from_apk||0} audio chunks`) +
    kpi("Bytes to APK", fmtBytes(c.bytes_to_apk), `${c.audio_frames_to_apk||0} audio frames`) +
    kpi("Events to APK", c.events_to_apk||0, `${c.msgs_from_apk||0} msgs from APK`) +
    kpi("Sessions", (c.sessions_total||0)+" / "+(c.sessions_live||0), "total / live") +
    kpi("Completed turns", c.turns_total||0, "voice Q→A");
}

function stagePill(stages, metrics){
  const b = {};
  if (metrics){
    b.stt=metrics.stt; b.llm=metrics.llm_first_token; b.tts=metrics.tts_first_audio; b.total=metrics.total;
  }
  const items=[
    ["Speak", b.stt!=null?null:stages.speech_start?fmtMs((stages.speech_end||0)-(stages.speech_start||0)):null],
    ["STT", b.stt!=null?fmtMs(b.stt):(stages.speech_end&&stages.stt_final?fmtMs(stages.stt_final-stages.speech_end):null)],
    ["LLM", b.llm!=null?fmtMs(b.llm):(stages.llm_start&&stages.llm_first_token?fmtMs(stages.llm_first_token-stages.llm_start):null)],
    ["TTS", b.tts!=null?fmtMs(b.tts):(stages.tts_start&&stages.tts_first_audio?fmtMs(stages.tts_first_audio-stages.tts_start):null)],
    ["Total", b.total!=null?fmtMs(b.total):(stages.speech_end&&stages.audio_sent?fmtMs(stages.audio_sent-stages.speech_end):null)],
  ];
  return items.map(([k,v])=>`<div class="stage"><div class="k">${k}</div><div class="v">${v||"—"}</div></div>`).join("");
}

function waterfall(turn){
  const bd=turn.breakdown||{};
  const parts=[
    {k:"speak",cls:"",ms:bd.user_speak_ms},
    {k:"stt",cls:"stt",ms:bd.stt_ms},
    {k:"llm",cls:"llm",ms:bd.llm_first_token_ms},
    {k:"tts",cls:"tts",ms:bd.tts_first_audio_ms},
    {k:"wire",cls:"q",ms:bd.tts_queue_to_wire_ms},
  ].filter(p=>p.ms!=null && p.ms>=0);
  const total=parts.reduce((a,b)=>a+(b.ms||0),0)||1;
  if(!parts.length) return "";
  return `<div class="wf">`+parts.map(p=>{
    const w=Math.max(3,(p.ms/total)*100);
    return `<div class="bar ${p.cls}" style="flex:0 0 ${w}%;background:${
      p.k==="speak"?"#64748b":p.k==="stt"?"#f59e0b":p.k==="llm"?"#a78bfa":p.k==="tts"?"#22c55e":"#f472b6"
    }" title="${p.k} ${fmtMs(p.ms)}">${p.ms>=40?p.k+" "+fmtMs(p.ms):""}</div>`;
  }).join("")+`</div>`;
}

function renderSessions(d){
  const el=document.getElementById("sessions");
  const list=d.live_sessions||[];
  if(!list.length){ el.innerHTML=`<div class="empty">No live sessions</div>`; return; }
  el.innerHTML=list.map(s=>`
    <div class="turn">
      <div><b>${esc(s.device_id)}</b>
        <span class="pill out">${esc(s.transport)}</span>
        <span class="muted mono">${esc(s.session_id)} · ${esc(s.client||"")} · ${s.sample_rate}Hz</span>
      </div>
      <div class="stagegrid">
        <div class="stage"><div class="k">Age</div><div class="v">${s.age_s}s</div></div>
        <div class="stage"><div class="k">Idle</div><div class="v">${s.idle_s}s</div></div>
        <div class="stage"><div class="k">In (APK)</div><div class="v">${fmtBytes(s.bytes_from_apk)}</div></div>
        <div class="stage"><div class="k">Out (APK)</div><div class="v">${fmtBytes(s.bytes_to_apk)}</div></div>
        <div class="stage"><div class="k">Chunks in</div><div class="v">${s.audio_chunks_from_apk}</div></div>
        <div class="stage"><div class="k">Frames out</div><div class="v">${s.audio_frames_to_apk}</div></div>
        <div class="stage"><div class="k">Events out</div><div class="v">${s.events_to_apk}</div></div>
        <div class="stage"><div class="k">Server lat.</div><div class="v">${s.latency&&s.latency.total!=null?fmtMs(s.latency.total):"—"}</div></div>
      </div>
      ${s.current_turn?`<div class="q" style="margin-top:8px">Speaking turn: <b>${esc(s.current_turn.question||"…")}</b></div>`:""}
    </div>
  `).join("");
}

function renderTurns(d){
  const el=document.getElementById("turns");
  const list=(d.turns||[]).slice().reverse();
  if(!list.length){ el.innerHTML=`<div class="empty">No turns yet — speak from the APK</div>`; return; }
  el.innerHTML=list.slice(0,30).map(t=>`
    <div class="turn">
      <div><b>#${t.id}</b> <span class="muted">${fmtTime(t.started_at||t.started_ms/1000)}</span>
        <span class="pill sys">${esc(t.session_id)}</span>
        ${t.done?'<span class="pill in">done</span>':'<span class="pill out">live</span>'}
      </div>
      <div class="q">Q: ${esc(t.question||"…")}</div>
      <div class="a">A: ${esc(t.answer||"…")}</div>
      ${waterfall(t)}
      <div class="stagegrid">${stagePill(t.stages||{}, t.metrics||t.server_metrics||{})}</div>
      ${t.breakdown?`<div class="muted mono" style="margin-top:6px">
        speak ${fmtMs(t.breakdown.user_speak_ms)} ·
        wait→LLM ${fmtMs(t.breakdown.llm_wait_ms)} ·
        LLM total ${fmtMs(t.breakdown.llm_total_ms)} ·
        TTS→wire ${fmtMs(t.breakdown.tts_queue_to_wire_ms)} ·
        E2E ${fmtMs(t.breakdown.end_to_end_ms)}
      </div>`:""}
    </div>
  `).join("");
}

function renderEvents(d){
  const el=document.getElementById("events");
  const list=d.events||[];
  if(!list.length){ el.innerHTML=`<tr><td colspan="6" class="muted">No messages yet</td></tr>`; return; }
  el.innerHTML=list.map(e=>{
    const dir=e.direction==="apk->server"?`<span class="pill in">APK → PC</span>`:
              e.direction==="server->apk"?`<span class="pill out">PC → APK</span>`:
              `<span class="pill sys">system</span>`;
    return `<tr>
      <td class="mono">${fmtTime(e.t)}</td>
      <td>${dir}</td>
      <td><b>${esc(e.kind)}</b></td>
      <td class="mono">${esc(e.detail)}</td>
      <td class="num">${e.size?fmtBytes(e.size):"—"}</td>
      <td class="mono muted">${esc(e.session_id)}</td>
    </tr>`;
  }).join("");
}

function renderClosed(d){
  const el=document.getElementById("closed");
  const list=d.closed_sessions||[];
  if(!list.length){ el.innerHTML=`<div class="empty">None</div>`; return; }
  el.innerHTML=list.map(s=>`
    <div class="turn">
      <b>${esc(s.device_id)}</b> <span class="muted mono">${esc(s.session_id)}</span><br/>
      <span class="muted">${s.duration_s}s · in ${fmtBytes(s.bytes_from_apk)} · out ${fmtBytes(s.bytes_to_apk)} · turns closed with session</span>
    </div>
  `).join("");
}

async function tick(){
  try{
    const res=await fetch("/api/dashboard",{cache:"no-store"});
    const d=await res.json();
    renderKpis(d);
    renderSessions(d);
    renderTurns(d);
    renderEvents(d);
    renderClosed(d);
    const cs=d.recent_chunk_sizes||[];
    document.getElementById("chunks").textContent=cs.length?cs.join(", ")+" bytes":"—";
  }catch(e){
    document.getElementById("uptime").textContent="ERR";
  }
}
tick();
setInterval(tick,1000);
</script>
</body>
</html>
"""
