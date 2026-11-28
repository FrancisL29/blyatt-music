/* Wave: service worker. Solo toca dos cosas (todo lo demas -audio, API, el login de Cloudflare Access- pasa tal cual):
   - abrir la app ("/"): se pide al servidor y se guarda una copia. Si el servidor no responde (el tunel de Cloudflare
     da 502/530, no hay red o tarda demasiado) y el dispositivo tiene musica descargada, se abre esa copia: la app entra
     en modo offline y suenan las descargas. Sin descargas, la pantalla propia "Wave no esta disponible".
   - los archivos de /assets (iconos, fuentes): del servidor y, si no responde, la copia guardada */
const V = "wave-down-v2";
const PAGE = "/assets/caido.html";
const SHELL = ["/assets/phosphor/regular/style.css", "/assets/phosphor/fill/style.css", "/assets/phosphor/regular/Phosphor.woff2",
  "/assets/phosphor/fill/Phosphor-Fill.woff2", "/assets/fonts/doto.woff2", "/assets/favicon.png", "/assets/icon-512.png",
  "/assets/manifest.json", "/assets/apple-touch-icon.png"];
const OFF = "wave-offline-v1";   // descargas (las escribe la app)

const fresh = (res) => res && res.ok && res.type === "basic" && !res.redirected;
const timeout = (p, ms) => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error("timeout")), ms))]);

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(V);
    await c.add(new Request(PAGE, { cache: "reload" }));
    await Promise.all(["/", ...SHELL].map(async (u) => {   // opcionales: un fallo no impide instalar
      try { const r = await fetch(new Request(u, { cache: "reload" })); if (fresh(r)) await c.put(u, r); } catch (err) {}
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k.startsWith("wave-down-") && k !== V).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function hasDownloads() {
  try { return !!(await (await caches.open(OFF)).match("/__dl")); } catch (err) { return false; }
}
async function fallback() {   // con descargas: la app (modo offline); sin ellas, la pantalla de caido
  const c = await caches.open(V);
  return ((await hasDownloads()) && (await c.match("/"))) || (await c.match(PAGE));
}

self.addEventListener("fetch", (e) => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url);
  if (u.origin !== location.origin) return;
  if (r.mode === "navigate") {
    const app = u.pathname === "/" || u.pathname === "/index.html";
    const net = fetch(r);
    // la app: cada carga buena deja su copia al dia (en segundo plano, sin retrasar la respuesta)
    if (app) e.waitUntil(net.then(async (res) => {
      if (fresh(res) && (res.headers.get("content-type") || "").includes("text/html")) await (await caches.open(V)).put("/", res.clone());
    }).catch(() => {}));
    e.respondWith((async () => {
      try {
        // servidor colgado: Cloudflare tarda hasta 100 s en rendirse; la app no espera tanto si tiene descargas
        const res = await (app && (await hasDownloads()) ? timeout(net, 8000) : net);
        // 5xx del borde/servidor (530 = tunel caido, 502/503/504). Las redirecciones de Access pasan sin tocar
        if (res.status >= 500 && res.status !== 501) return (await fallback()) || res;
        return res;
      } catch (err) {
        const page = await fallback();
        if (page) return page;
        throw err;
      }
    })());
    return;
  }
  if (u.pathname.startsWith("/assets/") && u.pathname !== "/assets/sw.js") {
    e.respondWith((async () => {
      const c = await caches.open(V);
      try {
        const res = await timeout(fetch(r), 8000);
        if (fresh(res)) c.put(r, res.clone()).catch(() => {});
        if (res.status >= 500) return (await c.match(r)) || res;
        return res;
      } catch (err) {
        const hit = await c.match(r);
        if (hit) return hit;
        throw err;
      }
    })());
  }
});
