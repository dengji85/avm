/* 片匣 Service Worker：让首页可像 App 一样从主屏启动。

策略：
  * 网络優先用于导航请求（保证拿到最新版本），失败时回落到缓存的首页
  * 同源静态资源网络优先（保证前端改动刷新即可见），离线时才回落缓存
  * /api/ 一律走网络，绝不缓存（数据实时且带会话）
*/
const CACHE = 'avm-shell-v2'
// 后端只暴露 `/` 不暴露 `/index.html`（返回 404），预缓存里不能放它，
// 否则 install 的 addAll 整体失败，SW 根本接管不了页面。
const SHELL = ['./', './manifest.webmanifest',
  './icons/icon-192.png', './icons/icon-512.png']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (e) => {
  const req = e.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) return

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).catch(() => caches.match('./')),
    )
    return
  }
  // 网络优先：构建产物文件名带 contenthash，缓存优先会让旧 bundle 一直生效，
  // 表现为「发布了新版本，用户刷新仍看到旧界面」。离线时才用缓存兜底。
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone()
          caches.open(CACHE).then((c) => c.put(req, copy))
        }
        return res
      })
      .catch(() => caches.match(req)),
  )
})
