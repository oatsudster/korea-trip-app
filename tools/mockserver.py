# -*- coding: utf-8 -*-
"""Local stand-in for the Cloudflare Pages Function, so the app can be tested
end to end without deploying. Implements the same contract as
functions/api/state.js, including compare-and-set 409s.

    python3 tools/mockserver.py public 8801        # normal
    python3 tools/mockserver.py public 8801 down   # /api/state returns 501,
                                                   # i.e. a static-only deploy

Extra endpoints for testing only (the real Function has neither):
    GET  /__debug   read the stored document
    POST /__seed    overwrite it, to stage a scenario
"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = sys.argv[1] if len(sys.argv) > 1 else "public"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8801
MODE = sys.argv[3] if len(sys.argv) > 3 else "ok"

EMPTY = {"names": ["OATT", "POPP"], "rate": 40, "items": [], "checks": {}}
LOCK = threading.Lock()
STORE = {"doc": json.loads(json.dumps(EMPTY)), "version": 0}

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def valid(doc):
    return (
        isinstance(doc, dict)
        and isinstance(doc.get("items"), list)
        and isinstance(doc.get("names"), list)
        and len(doc["names"]) >= 2
        and isinstance(doc.get("rate"), (int, float))
        and doc["rate"] > 0
    )


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            if MODE == "down":
                return self._json(501, {"ok": False, "error": "no D1 binding named DB"})
            with LOCK:
                return self._json(200, {"ok": True, "doc": STORE["doc"], "version": STORE["version"]})
        if path == "/__debug":
            with LOCK:
                return self._json(200, STORE)
        rel = path.lstrip("/") or "index.html"
        full = os.path.join(ROOT, rel)
        if not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        with open(full, "rb") as f:
            self._send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_PUT(self):
        if self.path.split("?")[0] != "/api/state":
            return self._send(404, b"not found", "text/plain")
        if MODE == "down":
            return self._json(501, {"ok": False, "error": "no D1 binding named DB"})
        n = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._json(400, {"ok": False, "error": str(e)})
        doc, base = body.get("doc"), int(body.get("version") or 0)
        if not valid(doc):
            return self._json(400, {"ok": False, "error": "bad document"})
        with LOCK:
            if base != STORE["version"]:
                return self._json(409, {"ok": False, "conflict": True,
                                        "doc": STORE["doc"], "version": STORE["version"]})
            STORE["doc"] = doc
            STORE["version"] += 1
            return self._json(200, {"ok": True, "doc": STORE["doc"], "version": STORE["version"]})

    def do_POST(self):
        if self.path.split("?")[0] != "/__seed":
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        with LOCK:
            STORE["doc"] = body["doc"]
            STORE["version"] += 1
            return self._json(200, {"ok": True, "version": STORE["version"]})


print("serving %s on http://127.0.0.1:%d  (mode: %s)" % (ROOT, PORT, MODE))
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
