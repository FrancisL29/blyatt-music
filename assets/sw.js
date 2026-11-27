/* Wave: service worker minimo. SOLO las navegaciones (abrir la app / recargar): si el servidor no responde (el tunel
   de Cloudflare da 502/530 o no hay red) se muestra la pantalla propia "Wave no esta disponible" en vez de la pagina
   de error de Cloudflare. Todo lo demas (audio, API, el login de Cloudflare Access) pasa tal cual, sin cache */
const V = "wave-down-v1";
const PAGE = "/assets/caido.html";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(V).then((c) => c.add(new Request(PAGE, { cache: "reload" }))).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k.startsWith("wave-down-") && k !== V).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const r = e.request;
  if (r.mode !== "navigate" || r.method !== "GET") return;
  e.respondWith((async () => {
    try {
      const res = await fetch(r);
      // 5xx del borde/servidor (530 = tunel caido, 502/503/504): la pantalla propia. Las redirecciones de Access
      // (sesion caducada) y cualquier otra respuesta pasan sin tocar
      if (res.status >= 500 && res.status !== 501) return (await caches.match(PAGE)) || res;
      return res;
    } catch (err) {
      const page = await caches.match(PAGE);
      if (page) return page;
      throw err;
    }
  })());
});
