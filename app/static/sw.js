// Minimaler Service Worker: macht die App installierbar (Chrome/Android).
// Seiten enthalten persönliche Daten und werden bewusst nicht zwischengespeichert.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

const OFFLINE = `<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline – Vorrat</title>
<body style="font-family:sans-serif;padding:2rem;text-align:center">
<h1>Keine Verbindung</h1><p>Vorrat braucht Internet. Bitte später noch einmal versuchen.</p></body></html>`;

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(
      () => new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })
    )
  );
});
