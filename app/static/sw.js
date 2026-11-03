/* Service worker: pushmeldingen tonen en de laatste stand bewaren voor als er even geen verbinding is. */
const CACHE = "energie-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Netwerk eerst; lukt dat niet, dan de laatst opgeslagen versie (pagina, opmaak en live-stand).
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const keep = req.mode === "navigate" || url.pathname.startsWith("/static/") || url.pathname === "/api/live";
  if (!keep) return;
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req.mode === "navigate" ? "/" : req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req.mode === "navigate" ? "/" : req, { ignoreSearch: req.mode === "navigate" }))
  );
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: "Energie Thuis", body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "Energie Thuis";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/badge-96.png",
      tag: data.tag || undefined,
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ("focus" in c) {
          c.navigate(target).catch(() => {});
          return c.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
