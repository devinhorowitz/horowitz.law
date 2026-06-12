/* Service worker for horowitz.law — the installable layer of the Watch.
   Strategy, in one breath: pages are network-first so the feed is always
   fresh when you have signal, with the cached copy as the courthouse-basement
   fallback; token-stamped assets and fonts are cache-first because their
   `?v=` content hashes make them immutable; the feeds and the subscribe API
   are never intercepted. Bump CACHE when the strategy changes. */

var CACHE = "gaw-v1";
var SHELL = ["/", "/opinions", "/archive", "/changes", "/stats", "/digests",
             "/subscribe", "/colophon", "/resume"];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })
          .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
                             .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;            // Turnstile etc. pass through
  if (url.pathname.startsWith("/api/")) return;                // never cache the API
  if (url.pathname.endsWith(".xml")) return;                   // feeds stay live

  // Immutable by construction: content-hashed assets and the subset fonts.
  if (url.searchParams.has("v") || url.pathname.startsWith("/fonts/")) {
    e.respondWith(
      caches.match(req).then(function (hit) {
        return hit || fetch(req).then(function (res) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); });
          return res;
        });
      })
    );
    return;
  }

  // Everything else (pages, permalinks, icons): fresh first, cache fallback,
  // and the Watch itself as the last-resort offline landing.
  e.respondWith(
    fetch(req).then(function (res) {
      var copy = res.clone();
      caches.open(CACHE).then(function (c) { c.put(req, copy); });
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || caches.match("/opinions");
      });
    })
  );
});
