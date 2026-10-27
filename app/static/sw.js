// Offline support: app pages are precached, API reads fall back to their last
// response, and TTS audio is kept in its own cache so it plays without the server.

// Bump to refresh the precached pages; the audio cache is deliberately unversioned
// so saved audio survives app updates.
const VERSION = "v2";
const SHELL_CACHE = `shell-${VERSION}`;
const DATA_CACHE = `data-${VERSION}`;
const AUDIO_CACHE = "tts-audio";

const SHELL = [
  "/",
  "/review",
  "/my-cards",
  "/library",
  "/manifest.json",
  "/icons/icon.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

// How long to wait for the laptop before using a cached copy. When the phone can't
// reach it at all, a request can hang far longer than this before failing.
const NETWORK_TIMEOUT_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  const keep = new Set([SHELL_CACHE, DATA_CACHE, AUDIO_CACHE]);
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => !keep.has(key)).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  // Writes (saving cards, submitting reviews) and other origins go straight to the network.
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  // The review page keeps its own copy of the due list, updated as cards are rated
  // offline; a cached response here would bring those cards back.
  if (url.pathname === "/cards/due") return;

  if (url.pathname.startsWith("/api/tts")) {
    event.respondWith(audio(request));
  } else if (request.mode === "navigate") {
    // The HTML is the same for every query string (e.g. /?text=3), so cache by path.
    event.respondWith(networkFirst(request, SHELL_CACHE, url.origin + url.pathname));
  } else {
    event.respondWith(networkFirst(request, DATA_CACHE, request));
  }
});

async function networkFirst(request, cacheName, cacheKey) {
  const cache = await caches.open(cacheName);
  const network = fetch(request).then((resp) => {
    if (resp.ok) cache.put(cacheKey, resp.clone());
    return resp;
  });
  const cached = await cache.match(cacheKey);
  if (!cached) return network.catch(() => offline());
  network.catch(() => {}); // a late failure after falling back is expected
  const timeout = new Promise((resolve) => setTimeout(() => resolve(cached), NETWORK_TIMEOUT_MS));
  return Promise.race([network, timeout]).catch(() => cached);
}

// Cache-first. Audio elements ask for byte ranges, and a 206 can't be stored, so the
// whole file is fetched and cached, then the requested range is cut from it.
async function audio(request) {
  const cache = await caches.open(AUDIO_CACHE);
  let resp = await cache.match(request.url);
  if (!resp) {
    try {
      resp = await fetch(request.url); // no Range header: get the full file
    } catch {
      return offline();
    }
    if (resp.status !== 200) return resp;
    await cache.put(request.url, resp.clone());
  }
  const range = request.headers.get("Range");
  return range ? sliceRange(resp, range) : resp;
}

async function sliceRange(resp, header) {
  const blob = await resp.blob();
  const size = blob.size;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  let start, end;
  if (match && match[1]) {
    start = Number(match[1]);
    end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  } else if (match && match[2]) {
    start = Math.max(size - Number(match[2]), 0); // suffix range: the last N bytes
    end = size - 1;
  }
  if (start === undefined || start > end || start >= size) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "audio/mpeg",
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Content-Length": String(end - start + 1),
      "Accept-Ranges": "bytes",
    },
  });
}

function offline() {
  const body = JSON.stringify({ detail: "Offline, and this hasn't been saved for offline use yet." });
  return new Response(body, { status: 503, headers: { "Content-Type": "application/json" } });
}
