/* 宝妈指数 Service Worker：界面缓存优先秒开，data.js 网络优先保证指数永远最新 */
// 界面壳（dashboard.js 等）有更新时务必升版本号：bmsx-v6 → bmsx-v10（2026-11-25 名次null修复）
// 否则缓存优先策略下，用户手机永远跑旧 JS，修复推了也白推
const CACHE = "bmsx-v10";
const SHELL = ["./dashboard.html", "styles.css", "dashboard.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"];
/* data.js 永不缓存：每次取网络，失败才回落本地缓存 */
const NETWORK_FIRST = ["data.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  const name = url.pathname.split("/").pop() || "dashboard.html";
  if (NETWORK_FIRST.includes(name)) {
    // 网络优先，失败回落缓存，再失败才报错
    e.respondWith(
      fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  // 其余资源：缓存优先，后台更新缓存
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const fresh = fetch(e.request).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || fresh;
    })
  );
});
