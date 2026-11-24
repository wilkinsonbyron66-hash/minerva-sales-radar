/* Service worker: app shell cached for offline use; data files always fetched fresh when online. */
const CACHE = "msr-v5";
const SHELL = ["./", "index.html", "css/style.css", "js/app.js", "manifest.json", "icons/icon.svg", "icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Network first for everything (so updates show at once), cache as fallback when offline.
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        const key = url.pathname.endsWith(".json") ? url.pathname : e.request;
        caches.open(CACHE).then((c) => c.put(key, copy));
      }
      return res;
    }).catch(() => caches.match(url.pathname.endsWith(".json") ? url.pathname : e.request, { ignoreSearch: true }))
  );
});
