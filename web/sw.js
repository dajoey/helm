/* Helm PWA service worker: cache the app shell, never cache the API.
   The web role stamps VERSION (app/web.py _sw_version: a digest of every file under web/),
   so any static change drops installed clients' old shell. Never edit it by hand.
   (helm-web counted by hand to v43, the Helm app to v13; the digest replaced the count.) */
const VERSION = "__HELM_SW_VERSION__";
const FONTS = ["newsreader-400.woff2", "newsreader-500.woff2", "newsreader-600.woff2", "newsreader-400-italic.woff2", "instrument-sans-400.woff2", "instrument-sans-500.woff2", "instrument-sans-600.woff2", "instrument-sans-700.woff2"].map((f) => "/fonts/" + f);
const SHELL = ["/", "/index.html", "/errors.js", "/shell.js", "/glyphs.js", "/objects.js", "/home.js", "/palette.js", "/preview.js", "/shell.css", "/style.css", "/home.css", "/helm.css", "/app.js", "/actions.js", "/needsyou.js", "/threads.js",
  "/conversation.js", "/conversation.css", "/search.js", "/activity.js", "/tips.js", "/modules.js", ...FONTS,
  "/manifest.webmanifest", "/icon-192.png", "/icon-512.png",
  "/notebook/app.js", "/notebook/lib.js", "/notebook/card.js", "/notebook/editor.js", "/notebook/notebook.css", "/vendor/cm.js",
  "/snes/icon-32.png"];

self.addEventListener("install", (e) => {
  // One missing file must not fail the whole install (the notebook tree is absent where the module is off).
  e.waitUntil(caches.open(VERSION).then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => null)))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/att/")) return; // API: always network
  if (url.pathname.startsWith("/outpost.goauthentik.io/")) return;   // the sign-in's hops belong to the browser, never the cache
  // Network-first for the shell so edits land immediately; cache is the offline fallback.
  e.respondWith(
    fetch(e.request).then((r) => { if (r.ok) { const copy = r.clone(); caches.open(VERSION).then((c) => c.put(e.request, copy)); } return r; })
      .catch(() => caches.match(e.request).then((m) => m || caches.match("/")))
  );
});
