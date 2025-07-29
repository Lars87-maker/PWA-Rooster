self.addEventListener("install", (e) => {
  console.log("Service Worker: Installed");
});

self.addEventListener("fetch", (e) => {
  // Je kunt hier caching logica toevoegen
  console.log("Service Worker: Fetching", e.request.url);
});
