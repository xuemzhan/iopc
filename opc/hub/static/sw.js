// OPC 控制台离线外壳：
// - 导航请求 network-first（改完 UI 刷新即生效，离线回退缓存）
// - 静态资源 stale-while-revalidate
// - /api/* 永远走网络
const CACHE = "opc-shell-v14";
const SHELL = [
  "/",
  "/static/app.css?v=0.9.11-1",
  "/static/app.js?v=0.9.11-1",
  "/static/logo.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    const old = keys.filter((k) => k !== CACHE);
    await Promise.all(old.map((k) => caches.delete(k)));
    await self.clients.claim();
    if (old.length) {
      const clients = await self.clients.matchAll({ type: "window" });
      clients.forEach((c) => c.postMessage({ type: "update", cache: CACHE }));
    }
  })());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // 数据永远要新鲜

  // 导航：先网络，失败再缓存
  if (event.request.mode === "navigate") {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(event.request);
        if (fresh.ok) {
          const copy = fresh.clone();
          caches.open(CACHE).then((c) => c.put("/", copy)).catch(() => {});
        }
        return fresh;
      } catch {
        return (await caches.match(event.request)) || (await caches.match("/")) || Response.error();
      }
    })());
    return;
  }

  // 静态资源：先缓存，同时后台更新（下次生效）
  event.respondWith((async () => {
    const hit = await caches.match(event.request);
    const network = fetch(event.request).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
      }
      return res;
    }).catch(() => null);
    return hit || (await network) || Response.error();
  })());
});
