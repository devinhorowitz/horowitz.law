/* Service worker for horowitz.law — the installable layer of the Watch.
   Strategy, in one breath: pages are network-first so the feed is always
   fresh when you have signal, with the cached copy as the courthouse-basement
   fallback; token-stamped `?v=` assets are cache-first because their content
   hashes make them immutable; the subset fonts carry no token, so they are
   stale-while-revalidate (served fast from cache, refreshed in the background)
   so a re-subset reaches returning visitors without a cache bump; the feeds and
   the subscribe API are never intercepted. Bump CACHE when the strategy changes. */

var CACHE = "gaw-v3";
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

  // Immutable by construction: content-hashed ?v= assets. Their URL changes when
  // the content does, so a cached hit is always current -> cache-first.
  if (url.searchParams.has("v")) {
    e.respondWith(
      caches.match(req).then(function (hit) {
        return hit || fetch(req).then(function (res) {
          if (res.ok) {                                  // never cache a 404/5xx: a deploy-race
            var copy = res.clone();                      // miss would otherwise be served forever
            caches.open(CACHE).then(function (c) { c.put(req, copy); });
          }
          return res;
        });
      })
    );
    return;
  }

  // Subset fonts carry no ?v= token, so a plain cache-first would pin the old
  // subset forever. Stale-while-revalidate: answer from cache for speed, but always
  // refetch in the background and update the cache, so a re-subset (same filename,
  // new glyphs) is picked up on the next load without a CACHE bump.
  if (url.pathname.startsWith("/fonts/")) {
    e.respondWith(
      caches.open(CACHE).then(function (c) {
        return c.match(req).then(function (hit) {
          var net = fetch(req).then(function (res) {
            if (res.ok) c.put(req, res.clone());         // refresh cache; skip 404/5xx
            return res;
          }).catch(function () { return hit; });         // offline: fall back to the cached copy
          return hit || net;                             // serve stale now, or wait on the network
        });
      })
    );
    return;
  }

  // Everything else (pages, permalinks, icons): fresh first, cache fallback,
  // and the Watch itself as the last-resort offline landing.
  e.respondWith(
    fetch(req).then(function (res) {
      if (res.ok) {                                      // only cache a good response
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
      }
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || caches.match("/opinions");
      });
    })
  );
});
