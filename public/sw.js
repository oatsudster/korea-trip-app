/* Offline shell.

   The trip plan has to open on a Seoul subway platform with no signal, so
   same-origin GETs are served from the cache FIRST and refreshed in the
   background. The previous version went to the network first, which meant a
   weak platform signal (not no signal - weak) left the page waiting on a fetch
   that had no timeout, exactly when it was needed most.

   Trade-off: right after a deploy the first open still shows the previous
   version, and the one after that is current. Google Fonts are cached too, so
   offline no longer falls back to system fonts and reflows the Thai text.
   /api/state is never cached - shared expenses must never be served stale. */
var CACHE = 'korea-trip-v2';
var SHELL = ['./', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png'];
var FONT_ORIGINS = ['https://fonts.googleapis.com', 'https://fonts.gstatic.com'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () {
    return self.skipWaiting();
  }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) { return k === CACHE ? null : caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;

  var url = new URL(req.url);
  if (url.pathname.indexOf('/api/') === 0) return;   // never cache shared state

  var sameOrigin = url.origin === self.location.origin;
  if (!sameOrigin && FONT_ORIGINS.indexOf(url.origin) < 0) return;

  // Any failure in the cache path must degrade to a plain network fetch:
  // a rejected respondWith() gives a blank page, which is worse than no
  // service worker at all. This cannot be exercised in the test harness, so
  // it is written to be unable to fail rather than verified not to.
  e.respondWith(caches.open(CACHE).then(function (c) {
    return c.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) {
        // opaque font responses report status 0 but are still worth keeping
        if (res && (res.status === 200 || res.type === 'opaque')) {
          c.put(req, res.clone()).catch(function () {});
        }
        return res;
      });

      // Cached copy wins the race; the refresh keeps running for next time.
      if (hit) { net.catch(function () {}); return hit; }

      return net.catch(function () {
        return c.match('./index.html').then(function (shell) {
          return shell || new Response('offline', { status: 503, statusText: 'offline' });
        });
      });
    });
  }).catch(function () { return fetch(req); }));
});
