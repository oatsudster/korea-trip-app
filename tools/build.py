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
var PEND='sb-trip-pending';
var POLL_MS=4000;

var state={names:['OATT','POPP'],rate:40,items:[],checks:{}};
var version=0;
var mode='local';       /* local | sync */
var syncState='init';   /* init | ok | busy | slow | error | local */
var inFlight=false;

function stash(op){try{sessionStorage.setItem(PEND,JSON.stringify(op));}catch(e){}}
function dropStash(){try{sessionStorage.removeItem(PEND);}catch(e){}}
function unstash(){var v=null;try{v=JSON.parse(sessionStorage.getItem(PEND)||'null');}catch(e){}dropStash();return v;}

/* Ops are id-based and idempotent, so replaying one that already landed is a
   no-op. That is what makes the conflict retry below safe. */
function applyOp(op,target){
  if(!op)return false;
  var t=target||state,changed=false;
  if(op.add){
    var seen={};t.items.forEach(function(i){seen[i.id]=1;});
    if(!seen[op.add.id]){t.items.push(op.add);changed=true;}
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
function goLocal(){mode='local';syncState='local';var l=loadLocal();if(l)state=l;}

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
  var pend=unstash();
  try{
    var res=await api('GET');
    if(res.status===200&&res.data&&res.data.ok){
      state=normalise(res.data.doc);version=res.data.version|0;
      mode='sync';syncState='ok';
      if(applyOp(pend)){render();await push(pend);return;}
      return;
    }
    goLocal();
  }catch(e){goLocal();}
  if(applyOp(pend))saveLocal();
})();

ready.then(function(){render();if(mode==='sync')startPolling();});

/* Send the whole document guarded by the version we last read. 409 means the
   other person wrote first: take their document, replay our op on top, retry. */
async function push(op,depth){
  depth=depth||0;
  if(mode!=='sync'){saveLocal();render();return;}
  inFlight=true;syncState='busy';render();
  if(op)stash(op);
  try{
    var res=await api('PUT',{doc:state,version:version});
    if(res.status===409&&res.data&&res.data.doc){
      state=normalise(res.data.doc);version=res.data.version|0;
      if(applyOp(op)&&depth<4){inFlight=false;return push(op,depth+1);}
      dropStash();syncState='ok';inFlight=false;render();return;
    }
    if(res.status===200&&res.data&&res.data.ok){
      state=normalise(res.data.doc);version=res.data.version|0;
      dropStash();syncState='ok';inFlight=false;render();return;
    }
    if(res.status===404||res.status===501){
      goLocal();saveLocal();dropStash();inFlight=false;render();return;
    }
    syncState=(res.status===429)?'slow':'error';saveLocal();inFlight=false;render();
  }catch(e){
    syncState='error';saveLocal();inFlight=false;render();
  }
}
function persist(op){return push(op);}

var pollTimer=null;
async function poll(){
  if(mode!=='sync'||inFlight||document.hidden)return;
  try{
    var res=await api('GET');
    if(res.status===200&&res.data&&res.data.ok){
      var v=res.data.version|0;
      if(v!==version){
        var incoming=normalise(res.data.doc);
        version=v;
        if(!sameDoc(incoming,state)){state=incoming;render();}
      }
      if(syncState==='error'||syncState==='slow'){syncState='ok';render();}
    }
  }catch(e){/* offline - keep showing what we have and retry next tick */}
}
function startPolling(){
  if(pollTimer)return;
  pollTimer=setInterval(poll,POLL_MS);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)poll();});
  window.addEventListener('online',poll);
}

"""

app = app[:start] + NEW + app[end:]

# --- status copy tuned for the REST build ---
reps = [
 ("""   :syncState==='busy'?'\u0e01\u0e33\u0e25\u0e31\u0e07\u0e0b\u0e34\u0e07\u0e04\u0e4c\u0e44\u0e1b\u0e2d\u0e35\u0e01\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u2026 (\u0e2b\u0e19\u0e49\u0e32\u0e08\u0e30\u0e23\u0e35\u0e40\u0e1f\u0e23\u0e0a\u0e40\u0e2d\u0e07)'""",
  """   :syncState==='busy'?'\u0e01\u0e33\u0e25\u0e31\u0e07\u0e1a\u0e31\u0e19\u0e17\u0e36\u0e01\u2026'"""),
 ("""   :syncState==='slow'?'\u0e1a\u0e31\u0e19\u0e17\u0e36\u0e01\u0e16\u0e35\u0e48\u0e40\u0e01\u0e34\u0e19\u0e44\u0e1b \u2014 \u0e40\u0e01\u0e47\u0e1a\u0e44\u0e27\u0e49\u0e43\u0e19\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e19\u0e35\u0e49\u0e01\u0e48\u0e2d\u0e19 \u0e40\u0e1e\u0e34\u0e48\u0e21\u0e23\u0e32\u0e22\u0e01\u0e32\u0e23\u0e16\u0e31\u0e14\u0e44\u0e1b\u0e08\u0e30\u0e0b\u0e34\u0e07\u0e04\u0e4c\u0e43\u0e2b\u0e49\u0e40\u0e2d\u0e07'
   :syncState==='error'?'\u0e0b\u0e34\u0e07\u0e04\u0e4c\u0e44\u0e21\u0e48\u0e2a\u0e33\u0e40\u0e23\u0e47\u0e08 \u0e40\u0e01\u0e47\u0e1a\u0e44\u0e27\u0e49\u0e43\u0e19\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e19\u0e35\u0e49\u0e41\u0e25\u0e49\u0e27 \u0e40\u0e1e\u0e34\u0e48\u0e21\u0e23\u0e32\u0e22\u0e01\u0e32\u0e23\u0e16\u0e31\u0e14\u0e44\u0e1b\u0e08\u0e30\u0e25\u0e2d\u0e07\u0e2a\u0e48\u0e07\u0e43\u0e2b\u0e21\u0e48'""",
  """   :syncState==='slow'?'\u0e1a\u0e31\u0e19\u0e17\u0e36\u0e01\u0e16\u0e35\u0e48\u0e40\u0e01\u0e34\u0e19\u0e44\u0e1b \u2014 \u0e40\u0e01\u0e47\u0e1a\u0e44\u0e27\u0e49\u0e43\u0e19\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e19\u0e35\u0e49\u0e01\u0e48\u0e2d\u0e19 \u0e40\u0e14\u0e35\u0e4b\u0e22\u0e27\u0e25\u0e2d\u0e07\u0e2a\u0e48\u0e07\u0e43\u0e2b\u0e21\u0e48\u0e43\u0e2b\u0e49\u0e40\u0e2d\u0e07'
   :syncState==='error'?'\u0e40\u0e19\u0e47\u0e15\u0e21\u0e35\u0e1b\u0e31\u0e0d\u0e2b\u0e32 \u0e40\u0e01\u0e47\u0e1a\u0e44\u0e27\u0e49\u0e43\u0e19\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e19\u0e35\u0e49\u0e41\u0e25\u0e49\u0e27 \u0e01\u0e25\u0e31\u0e1a\u0e21\u0e32\u0e41\u0e25\u0e49\u0e27\u0e08\u0e30\u0e0b\u0e34\u0e07\u0e04\u0e4c\u0e43\u0e2b\u0e49\u0e40\u0e2d\u0e07'"""),
 ("""   :mode==='sync'?'\u0e0b\u0e34\u0e07\u0e04\u0e4c\u0e2a\u0e14\u0e2d\u0e22\u0e39\u0e48 \u2014 \u0e2d\u0e35\u0e01\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e08\u0e30\u0e40\u0e2b\u0e47\u0e19\u0e40\u0e2d\u0e07\u0e20\u0e32\u0e22\u0e43\u0e19\u0e44\u0e21\u0e48\u0e01\u0e35\u0e48\u0e27\u0e34\u0e19\u0e32\u0e17\u0e35 \u0e44\u0e21\u0e48\u0e15\u0e49\u0e2d\u0e07\u0e23\u0e35\u0e40\u0e1f\u0e23\u0e0a'
   :'\u0e42\u0e2b\u0e21\u0e14\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e19\u0e35\u0e49\u0e40\u0e17\u0e48\u0e32\u0e19\u0e31\u0e49\u0e19 (\u0e1a\u0e31\u0e19\u0e17\u0e36\u0e01\u0e43\u0e19\u0e40\u0e1a\u0e23\u0e32\u0e27\u0e4c\u0e40\u0e0b\u0e2d\u0e23\u0e4c) \u2014 \u0e16\u0e49\u0e32\u0e2d\u0e22\u0e32\u0e01\u0e0b\u0e34\u0e07\u0e04\u0e4c 2 \u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07 \u0e40\u0e08\u0e49\u0e32\u0e02\u0e2d\u0e07\u0e15\u0e49\u0e2d\u0e07\u0e41\u0e0a\u0e23\u0e4c\u0e2b\u0e19\u0e49\u0e32\u0e19\u0e35\u0e49\u0e41\u0e1a\u0e1a\u0e41\u0e01\u0e49\u0e44\u0e02\u0e44\u0e14\u0e49';""",
  """   :mode==='sync'?'\u0e0b\u0e34\u0e07\u0e04\u0e4c\u0e2a\u0e14\u0e2d\u0e22\u0e39\u0e48 \u2014 \u0e2d\u0e35\u0e01\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e08\u0e30\u0e40\u0e2b\u0e47\u0e19\u0e40\u0e2d\u0e07\u0e43\u0e19 ~4 \u0e27\u0e34\u0e19\u0e32\u0e17\u0e35 \u0e44\u0e21\u0e48\u0e15\u0e49\u0e2d\u0e07\u0e23\u0e35\u0e40\u0e1f\u0e23\u0e0a'
   :'\u0e42\u0e2b\u0e21\u0e14\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e19\u0e35\u0e49\u0e40\u0e17\u0e48\u0e32\u0e19\u0e31\u0e49\u0e19 \u2014 \u0e22\u0e31\u0e07\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49\u0e15\u0e48\u0e2d\u0e10\u0e32\u0e19\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25 D1 (\u0e14\u0e39\u0e27\u0e34\u0e18\u0e35\u0e43\u0e19 README)';"""),
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
