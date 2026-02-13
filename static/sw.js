// Bump this value on each release to force cache refresh on clients.
const CACHE_NAME = 'tg-assistant-v1.0.0';

const ASSETS = [
    '/',
    '/static/manifest.json',
    '/static/css/style.css',
    '/static/css/login.css',
    '/static/js/app.js',
    '/static/icons/logo.svg',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    // External deps (best-effort)
    'https://cdn.jsdelivr.net/npm/chart.js',
    'https://unpkg.com/lucide@latest',
    'https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js'
];

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE_NAME);
        try {
            await cache.addAll(ASSETS);
        } catch (e) {
            // External URLs may fail to cache depending on CORS; ignore.
        }
        self.skipWaiting();
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys.map((k) => (k === CACHE_NAME ? null : caches.delete(k))));
        await self.clients.claim();
    })());
});

function isSameOrigin(url) {
    try {
        return new URL(url).origin === self.location.origin;
    } catch {
        return false;
    }
}

async function staleWhileRevalidate(request) {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    const fetchPromise = fetch(request).then((resp) => {
        if (resp && resp.ok) cache.put(request, resp.clone());
        return resp;
    }).catch(() => null);
    return cached || (await fetchPromise);
}

async function networkFirst(request) {
    const cache = await caches.open(CACHE_NAME);
    try {
        const resp = await fetch(request);
        if (resp && resp.ok) cache.put(request, resp.clone());
        return resp;
    } catch (e) {
        const cached = await cache.match(request);
        if (cached) return cached;
        throw e;
    }
}

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;

    // Don't try to cache non-http(s).
    if (!req.url.startsWith('http')) return;

    // HTML navigations should not be served from a stale cache.
    if (req.mode === 'navigate') {
        event.respondWith(networkFirst(req));
        return;
    }

    // For same-origin static assets: cache + update in background (SWR).
    if (isSameOrigin(req.url) && new URL(req.url).pathname.startsWith('/static/')) {
        event.respondWith(staleWhileRevalidate(req));
        return;
    }

    // Default: passthrough (or cache if already there).
    event.respondWith((async () => {
        const cached = await caches.match(req);
        return cached || fetch(req);
    })());
});
