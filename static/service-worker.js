// static/service-worker.js
const CACHE_NAME = "rooster-pwa-cache-v4"; // <— bump
const ASSETS = [
  "/manifest.json",
  "/icon-192.png",
  "/icon-512.png",
];

// Neem nieuwe SW meteen over
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((c) => c.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: cache alleen non-HTML assets. HTML altijd network-first.
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Laat niet-GET ongemoeid (zoals POST /upload)
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Navigaties en HTML → network-first
  const isHTML = req.mode === "navigate" || (req.headers.get("accept") || "").includes("text/html");
  if (isHTML) {
    event.respondWith(
      fetch(req).catch(() => caches.match("/")) // offline fallback (optioneel)
    );
    return;
  }

  // Alleen onze vast gedefinieerde assets uit cache
  if (ASSETS.includes(url.pathname)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req))
    );
  }
});
