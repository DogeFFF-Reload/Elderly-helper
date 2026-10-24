// 家属端“桌面 App”的离线壳：页面和脚本先走网络、断网时用缓存；接口（/api/...）从不缓存，永远是实时的
const CACHE = "cm-family-v1";
const SHELL = ["/ui/family/", "/ui/shared/bus.js", "/ui/shared/comms.js", "/ui/family/manifest.webmanifest", "/ui/family/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/ui/")) return;
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request, { ignoreSearch: true })));
});

// 点系统通知 → 回到家属端
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((list) => {
    const open = list.find((c) => c.url.includes("/ui/family"));
    return open ? open.focus() : self.clients.openWindow("/ui/family/");
  }));
});
