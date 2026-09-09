# -*- coding: utf-8 -*-
"""Derive the Cloudflare Pages build from the Claude-artifact source.

Only the persistence layer differs: the artifact republishes itself through
window.claude, the web app talks to /api/state (D1) and polls for changes.
Everything else - itinerary content, CSS, renderers - is reused verbatim so
the two builds cannot drift apart.
"""
import io, os, re, sys

# Paths resolve from this file, so the build runs the same on Windows and macOS
# from any working directory:  python3 tools/build.py
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("TRIP_SRC") or os.path.join(REPO, "src", "trip.html")
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else REPO

s = io.open(SRC, encoding="utf-8").read()
css = s.split('<style id="css">')[1].split("</style>")[0]
app = s.split('<script id="app">')[1].rsplit("</script>", 1)[0]

# DAYS and TRIP_DATES describe the same trip in two places: DAYS drives the
# itinerary, TRIP_DATES drives "today" mode and the countdown. Nothing at
# runtime notices when they disagree - the app just quietly stops knowing what
# day it is. Fail the build instead, while someone is still looking.
_days = len(re.findall(r"\{n:\d+,dow:", app))
_dates = re.findall(r"var TRIP_DATES=\[([^\]]*)\]", app)
assert _dates, "TRIP_DATES not found in src/trip.html"
_dates = len(re.findall(r"'\d{4}-\d{2}-\d{2}'", _dates[0]))
assert _days == _dates, (
    "DAYS has %d days but TRIP_DATES has %d. They must match or the app loses "
    "track of which day it is - fix src/trip.html." % (_days, _dates)
)

start = app.index("/* ================= state ================= */")
end = app.index("/* ================= renderers ================= */")
old = app[start:end]
assert "async function persist" in old and "buildPage" in old, "unexpected source layout"

NEW = r"""/* ================= state + sync =================
   Cloudflare build: the shared document lives in D1 behind /api/state.
   Writes carry the version we last read (optimistic concurrency); a poll
   picks up the other person's changes. If /api/state is not deployed the
   whole thing degrades to this-device-only storage. */
var API='/api/state';
var LS='sb-trip-2026';
var PEND='sb-trip-pending';   /* legacy single-slot stash - migrated once, then unused */
var QK='sb-trip-queue';       /* ops written but not yet accepted by the server */
var SEEN='sb-trip-seen';      /* item ids the server has confirmed at least once */
var POLL_FAST=4000;           /* just after a change, so the other phone sees it quickly */
var POLL_SLOW=15000;          /* idle: polling every 4s all day was a battery and data tax */
var FAST_FOR=60000;

var state={names:['OATT','POPP'],rate:40,items:[],checks:{}};
var version=0;
var mode='local';       /* local | sync */
var syncState='init';   /* init | ok | busy | slow | error | local */
var inFlight=false;
var queue=[];
var seen={};
var fastUntil=0;

/* Unsent changes live in a QUEUE in localStorage, not one slot in
   sessionStorage. Whatever is typed on a Seoul platform with no signal stays
   here until a PUT actually succeeds - it survives a reload and a flat
   battery, and is replayed on top of whatever the other phone wrote. */
function loadQueue(){
  try{var a=JSON.parse(localStorage.getItem(QK)||'[]');return Array.isArray(a)?a:[];}catch(e){return [];}
}
function saveQueue(){try{localStorage.setItem(QK,JSON.stringify(queue));}catch(e){}}
function enqueue(op){if(!op)return;queue.push(op);if(queue.length>300)queue=queue.slice(-300);saveQueue();}
function applyQueue(target){
  var ch=false;
  for(var i=0;i<queue.length;i++){if(applyOp(queue[i],target))ch=true;}
  return ch;
}
/* One-off: adopt the single pending op the previous version stashed. */
function migrateStash(){
  try{
    var v=JSON.parse(sessionStorage.getItem(PEND)||'null');
    sessionStorage.removeItem(PEND);
    if(v)enqueue(v);
  }catch(e){}
}
/* Ids the server has already shown us. An item missing from the server doc but
   present in `seen` was deleted by the other phone - it must NOT be resurrected
   when this device rejoins. An item never seen is ours, made offline, and must
   be. Without this distinction one of the two is always wrong. */
function loadSeen(){
  try{
    var a=JSON.parse(localStorage.getItem(SEEN)||'[]');
    if(Array.isArray(a))a.forEach(function(id){seen[id]=1;});
  }catch(e){}
}
function markSeen(doc){
  var ch=false;
  doc.items.forEach(function(i){if(!seen[i.id]){seen[i.id]=1;ch=true;}});
  if(ch){try{localStorage.setItem(SEEN,JSON.stringify(Object.keys(seen).slice(-600)));}catch(e){}}
}

/* Ops are id-based and idempotent, so replaying one that already landed is a
   no-op. That is what makes the conflict retry below safe. */
function applyOp(op,target){
  if(!op)return false;
  var t=target||state,changed=false;
  if(op.add){
    var s2={};t.items.forEach(function(i){s2[i.id]=1;});
    if(!s2[op.add.id]){t.items.push(op.add);changed=true;}
  }
  if(op.del){
    var kept=t.items.filter(function(i){return i.id!==op.del;});
    if(kept.length!==t.items.length){t.items=kept;changed=true;}
  }
  if(op.clear&&t.items.length){t.items=[];changed=true;}
  if(op.check){
    t.checks=t.checks||{};
    var want=!!op.check.on;
    if(!!t.checks[op.check.id]!==want){t.checks[op.check.id]=want;changed=true;}
  }
  if(op.upd){
    for(var u=0;u<t.items.length;u++){
      if(t.items[u].id===op.upd.id){
        if(JSON.stringify(t.items[u])!==JSON.stringify(op.upd)){t.items[u]=op.upd;changed=true;}
        break;
      }
    }
  }
  if(op.rerate){
    t.items.forEach(function(i){
      if(i.cur==='KRW'&&i.rate!==op.rerate.rate){i.rate=op.rerate.rate;changed=true;}
    });
  }
  if(op.meta){
    if(op.meta.names&&(op.meta.names[0]!==t.names[0]||op.meta.names[1]!==t.names[1])){
      t.names=op.meta.names.slice();changed=true;}
    if(op.meta.rate&&op.meta.rate!==t.rate){t.rate=op.meta.rate;changed=true;}
  }
  return changed;
}

function normalise(d){
  if(!d||typeof d!=='object')d={};
  if(!Array.isArray(d.items))d.items=[];
  if(!Array.isArray(d.names)||d.names.length<2)d.names=['OATT','POPP'];
  if(typeof d.rate!=='number'||!isFinite(d.rate)||d.rate<=0)d.rate=40;
  if(!d.checks||typeof d.checks!=='object')d.checks={};
  return d;
}
function saveLocal(){try{localStorage.setItem(LS,JSON.stringify(state));}catch(e){}}
function loadLocal(){
  try{var raw=localStorage.getItem(LS);if(raw)return normalise(JSON.parse(raw));}catch(e){}
  return null;
}
function sameDoc(a,b){return JSON.stringify(a)===JSON.stringify(b);}
function goLocal(){mode='local';if(syncState!=='error')syncState='local';}
/* This device was on its own and the server has just answered. Every item made
   here that the server has never seen is replayed as an add, so the two lists
   become the union - instead of the server silently winning, which is what
   used to happen on the next reload. */
function promote(serverDoc){
  var have={};
  serverDoc.items.forEach(function(i){have[i.id]=1;});
  state.items.forEach(function(i){if(!have[i.id]&&!seen[i.id])enqueue({add:i});});
  var ck=state.checks||{},sck=serverDoc.checks||{};
  Object.keys(ck).forEach(function(k){if(ck[k]&&!sck[k])enqueue({check:{id:k,on:true}});});
  if(state.rate!==40&&serverDoc.rate===40)enqueue({meta:{names:state.names.slice(),rate:state.rate}});
  mode='sync';
}

async function api(method,body){
  var r=await fetch(API,{method:method,
    headers:{'content-type':'application/json'},
    cache:'no-store',
    body:body?JSON.stringify(body):undefined});
  var data=null;
  try{data=await r.json();}catch(e){}
  return {status:r.status,data:data};
}

var ready=(async function(){
  loadSeen();
  migrateStash();
  queue=loadQueue();
  var l=loadLocal();if(l)state=l;   /* show the last known list even with no signal */
  try{
    var res=await api('GET');
    if(res.status===200&&res.data&&res.data.ok){
      var doc=normalise(res.data.doc);
      version=res.data.version|0;
      if(queue.length||!sameDoc(doc,state))promote(doc);else mode='sync';
      markSeen(doc);
      state=doc;
      applyQueue(state);
      saveLocal();
      syncState=queue.length?'busy':'ok';
      if(queue.length){render();await flush();}
      return;
    }
    goLocal();
  }catch(e){goLocal();}
  applyQueue(state);
  saveLocal();
})();

/* Polling runs in every mode: it is also what notices the signal came back,
   promotes a device-only session to the shared one, and sends what is queued. */
ready.then(function(){render();startPolling();});

/* Send the whole document guarded by the version we last read, and drop from
   the queue only the ops this PUT actually carried: anything enqueued while
   the request was in the air is still owed and goes out on the next pass. */
async function flush(depth){
  depth=depth||0;
  if(mode!=='sync'){saveLocal();render();return;}
  if(inFlight||!queue.length)return;
  inFlight=true;syncState='busy';render();
  var sent=queue.slice();
  try{
    var res=await api('PUT',{doc:state,version:version});
    inFlight=false;
    if(res.status===409&&res.data&&res.data.doc){
      /* the other phone wrote first: take their document, replay what we owe */
      state=normalise(res.data.doc);version=res.data.version|0;
      markSeen(state);applyQueue(state);saveLocal();
      if(depth<5)return flush(depth+1);
      syncState='error';render();return;
    }
    if(res.status===200&&res.data&&res.data.ok){
      state=normalise(res.data.doc);version=res.data.version|0;
      markSeen(state);
      queue=queue.filter(function(op){return sent.indexOf(op)<0;});
      saveQueue();saveLocal();
      if(queue.length)return flush(depth+1);
      syncState='ok';render();return;
    }
    if(res.status===404||res.status===501){goLocal();saveLocal();render();return;}
    syncState=(res.status===429)?'slow':'error';saveLocal();render();
  }catch(e){
    inFlight=false;syncState='error';saveLocal();render();
  }
}
/* A local change: it is already in `state`, so remember it, save it, send it. */
function persist(op){
  enqueue(op);
  saveLocal();
  fastUntil=Date.now()+FAST_FOR;
  return flush();
}

var pollTimer=null;
async function poll(){
  if(inFlight||document.hidden)return;
  try{
    var res=await api('GET');
    if(res.status===200&&res.data&&res.data.ok){
      var incoming=normalise(res.data.doc),v=res.data.version|0;
      if(mode!=='sync')promote(incoming);      /* the signal came back */
      if(v!==version||queue.length){
        version=v;
        markSeen(incoming);
        applyQueue(incoming);
        if(!sameDoc(incoming,state)){state=incoming;saveLocal();render();}
      }
      if(queue.length){await flush();return;}
      if(syncState!=='ok'&&syncState!=='busy'){syncState='ok';render();}
    }else if(res.status===404||res.status===501){
      if(mode==='sync'){goLocal();render();}
    }
  }catch(e){
    /* no signal: keep showing what we have, say so honestly, retry next tick */
    if(mode==='sync'&&syncState==='ok'){syncState='error';render();}
  }
}
function startPolling(){
  if(pollTimer)return;
  function tick(){
    poll();
    var soon=queue.length>0||Date.now()<fastUntil;
    pollTimer=setTimeout(tick,soon?POLL_FAST:POLL_SLOW);
  }
  pollTimer=setTimeout(tick,POLL_FAST);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)poll();});
  window.addEventListener('online',function(){fastUntil=Date.now()+FAST_FOR;poll();});
}

"""

app = app[:start] + NEW + app[end:]

# --- status copy tuned for the REST build ---
reps = [
 ("""   :syncState==='busy'?'กำลังซิงค์ไปอีกเครื่อง… (หน้าจะรีเฟรชเอง)'""",
  """   :syncState==='busy'?'กำลังส่งขึ้นเซิร์ฟเวอร์…'"""),
 ("""   :syncState==='slow'?'บันทึกถี่เกินไป — เก็บไว้ในเครื่องนี้ก่อน เพิ่มรายการถัดไปจะซิงค์ให้เอง'
   :syncState==='error'?'ซิงค์ไม่สำเร็จ เก็บไว้ในเครื่องนี้แล้ว เพิ่มรายการถัดไปจะลองส่งใหม่'""",
  """   :syncState==='slow'?'ส่งถี่เกินไป — เก็บไว้ในเครื่องนี้ครบแล้ว เดี๋ยวส่งใหม่ให้เอง'
   :syncState==='error'?(qn?'ยังส่งไม่ได้ '+qn+' รายการ — เก็บไว้ในเครื่องนี้ครบ จะส่งเองเมื่อเน็ตกลับมา'
                           :'เน็ตมีปัญหา — กำลังลองใหม่เรื่อย ๆ ที่เห็นอยู่คือข้อมูลล่าสุดที่ได้มา')"""),
 ("""   :mode==='sync'?'ซิงค์สดอยู่ — อีกเครื่องจะเห็นเองภายในไม่กี่วินาที ไม่ต้องรีเฟรช'
   :'โหมดเครื่องนี้เท่านั้น (บันทึกในเบราว์เซอร์) — ถ้าอยากซิงค์ 2 เครื่อง เจ้าของต้องแชร์หน้านี้แบบแก้ไขได้';""",
  """   :mode==='sync'?'ซิงค์สดอยู่ — อีกเครื่องจะเห็นเองไม่เกิน ~15 วินาที ไม่ต้องรีเฟรช'
   :(qn?'ยังต่อเซิร์ฟเวอร์ไม่ได้ — เก็บไว้ในเครื่องนี้ '+qn+' รายการ จะส่งเองเมื่อต่อได้'
      :'โหมดเครื่องนี้เท่านั้น — กำลังลองต่อเซิร์ฟเวอร์ให้เรื่อย ๆ');"""),
]
for a, b in reps:
    assert app.count(a) == 1, ("copy replacement missed", a[:50], app.count(a))
    app = app.replace(a, b)

# the trailing render() is now driven by ready.then
assert app.count("\nrender();\n})();") == 1
app = app.replace("\nrender();\n})();", "\n})();")
assert app.count("ready.then(function(){render();") == 1

# Title comes from the source too, so the tab name cannot drift from the app
TITLE = re.search(r"var TITLE='([^']*)'", s).group(1)
FONT = ("https://fonts.googleapis.com/css2?family=Mali:wght@400;600;700&amp;"
        "family=IBM+Plex+Sans+Thai:wght@400;500;600&amp;family=Gaegu:wght@700&amp;"
        "family=IBM+Plex+Mono:wght@500&amp;display=swap")
DESC = ("\u0e41\u0e1c\u0e19\u0e40\u0e17\u0e35\u0e48\u0e22\u0e27\u0e42\u0e0b\u0e25 5 \u0e27\u0e31\u0e19 4 \u0e04\u0e37\u0e19 "
        "12-16 \u0e01.\u0e22. 2569 \u0e1e\u0e23\u0e49\u0e2d\u0e21\u0e27\u0e34\u0e18\u0e35\u0e40\u0e14\u0e34\u0e19\u0e17\u0e32\u0e07\u0e25\u0e30\u0e40\u0e2d\u0e35\u0e22\u0e14"
        "\u0e41\u0e25\u0e30\u0e0a\u0e48\u0e2d\u0e07\u0e1a\u0e31\u0e19\u0e17\u0e36\u0e01\u0e04\u0e48\u0e32\u0e43\u0e0a\u0e49\u0e08\u0e48\u0e32\u0e22\u0e23\u0e48\u0e27\u0e21\u0e01\u0e31\u0e19")

page = (
 "<!doctype html>\n<html lang=\"th\">\n<head>\n"
 "<meta charset=\"utf-8\">\n"
 "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
 "<title>" + TITLE + "</title>\n"
 "<meta name=\"description\" content=\"" + DESC + "\">\n"
 "<meta name=\"theme-color\" content=\"#FFF6F2\" media=\"(prefers-color-scheme: light)\">\n"
 "<meta name=\"theme-color\" content=\"#1C1828\" media=\"(prefers-color-scheme: dark)\">\n"
 "<meta name=\"mobile-web-app-capable\" content=\"yes\">\n"
 "<meta name=\"apple-mobile-web-app-capable\" content=\"yes\">\n"
 "<meta name=\"apple-mobile-web-app-status-bar-style\" content=\"default\">\n"
 "<meta name=\"apple-mobile-web-app-title\" content=\"\u0e42\u0e0b\u0e25\u0e41\u0e25\u0e30\u0e23\u0e2d\u0e1a\u0e46\">\n"
 "<link rel=\"manifest\" href=\"manifest.webmanifest\">\n"
 "<link rel=\"icon\" href=\"icon-192.png\" sizes=\"192x192\" type=\"image/png\">\n"
 "<link rel=\"apple-touch-icon\" href=\"icon-192.png\">\n"
 "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">\n"
 "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>\n"
 "<link rel=\"stylesheet\" href=\"" + FONT + "\">\n"
 "<style>" + css + "</style>\n"
 "</head>\n<body>\n<div id=\"root\"></div>\n"
 "<script>" + app + "</script>\n"
 "<script>\n"
 "if('serviceWorker' in navigator){addEventListener('load',function(){\n"
 "  navigator.serviceWorker.register('sw.js').catch(function(){});\n"
 "});}\n"
 "</script>\n"
 "</body>\n</html>\n")

path = os.path.join(OUT_DIR, "public", "index.html")
io.open(path, "w", encoding="utf-8", newline="\n").write(page)
print("wrote", path, len(page), "bytes")
