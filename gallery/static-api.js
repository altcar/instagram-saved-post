/* Static replacement for webapp.py's /api/* endpoints on GitHub Pages. */
(() => {
  window.__STATIC = true;
  const nativeFetch = window.fetch.bind(window);
  const ready = nativeFetch("manifest.json?nc=202611270610", { cache: "no-store" })
    .then((r) => r.json())
    .then(async (data) => {
      posts = data.posts || [];
      window.__STATIC_POSTS = posts;
      await loadCloud();
      posts.forEach(applyOverride);
      await loadMediaTokens();
      posts.forEach(rewritePostMedia);
      // A previous session may have died before its last write landed (or the
      // page was closed while workers.dev was unreachable).  The pending
      // revision is persisted in localStorage, so resume it on load.
      if (sync.rev > Math.max(sync.d1Rev, sync.gistRev)) scheduleFlush();
      return data;
    });
  const STORE = "ig_static_overrides_v1";
  const CLOUD = window.SAVED_CLOUD || {};
  const GIST_ID = CLOUD.gistId || "5dc24371e7371776648695269b30cfd2";
  const GIST_FILE = CLOUD.gistFile || "static_overrides.json";
  // The deployed site must work with no local server.  The state Worker
  // (Gist-backed, no D1) is the primary store even when config.js only
  // carries the gist id; a browser can override it (or disable it with an
  // empty value) in the Cloud sync panel.  Reads/writes of the "default" doc
  // are allowed by the Worker itself from this origin, so no secret ships to
  // a browser.
  const D1_DEFAULT = CLOUD.d1Url || "https://ig-archive-state.hheylau.workers.dev/";
  let cloudToken = localStorage.getItem("ig_cloud_token") || CLOUD.token || "";
  const d1Stored = localStorage.getItem("ig_cloud_d1");
  let d1Url = d1Stored === null ? D1_DEFAULT : d1Stored;
  let d1Key = localStorage.getItem("ig_cloud_d1_key") || "";

  // ---- serverless gallery API ------------------------------------------
  // Prefer the deployed gallery Worker (cached manifest + batched edits) and
  // fall back to the local static manifest handling below whenever it cannot
  // be reached.  Nothing here is required for the page to work.
  const GALLERY_API = String(
    window.SAVED_GALLERY_API || CLOUD.galleryApi
    || "https://ig-archive-gallery.hheylau.workers.dev").replace(/\/+$/, "");
  let galleryDownUntil = 0;

  function galleryReady() {
    return !!GALLERY_API && Date.now() >= galleryDownUntil;
  }

  async function galleryJson(path, params, init, timeoutMs) {
    if (!galleryReady()) return null;
    let target;
    try { target = new URL(GALLERY_API.replace(/\/+$/, "") + path); }
    catch (_) { return null; }
    // The Worker cannot see this page's query string.  Seed source/platform
    // (and any other route params) so /api/posts and /api/facets stay scoped
    // to the gallery the iframe is showing.
    try {
      new URLSearchParams(location.search).forEach((value, key) => {
        target.searchParams.set(key, value);
      });
    } catch (_) {}
    if (params) {
      const entries = typeof params.forEach === "function"
        ? (() => { const out = []; params.forEach((v, k) => out.push([k, v])); return out; })()
        : Object.entries(params);
      entries.forEach(([key, value]) => {
        if (value !== null && value !== undefined && value !== "") {
          target.searchParams.set(key, value);
        }
      });
    }
    const controller = typeof AbortController === "function"
      ? new AbortController() : null;
    const timer = controller && timeoutMs !== 0
      ? setTimeout(() => controller.abort(), timeoutMs || 3000) : null;
    try {
      const opts = {
        cache: "no-store",
        credentials: "omit",
        ...(init || {}),
      };
      if (controller) opts.signal = controller.signal;
      const res = await nativeFetch(target.href, opts);
      if (!res.ok) {
        if (res.status >= 500 || res.status === 429) {
          galleryDownUntil = Date.now() + 30000;
        }
        return null;
      }
      return await res.json();
    } catch (_) {
      // One short outage: stay on the static path for 30 s, then retry.
      galleryDownUntil = Date.now() + 30000;
      return null;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function galleryEdit(ops, baseRev, keepalive) {
    if (!galleryReady() || !ops || !ops.length) return Promise.resolve(null);
    const headers = { "Content-Type": "application/json" };
    if (baseRev) headers["If-Match"] = `"${baseRev}"`;
    return galleryJson("/api/edits", null, {
      method: "POST",
      headers,
      body: JSON.stringify({ ops, base_rev: baseRev || 0 }),
      keepalive: !!keepalive,
    }, keepalive ? 0 : 6000);
  }

  // ---- gated media ------------------------------------------------------
  // Media URLs are rewritten through the HF proxy, which requires both the
  // site origin and a short-lived HMAC token.  If the proxy or the token
  // endpoint is unreachable we keep the direct Hugging Face URL, so the site
  // still works during cutover (while the dataset is public).
  const MEDIA = CLOUD.media || {};
  const MEDIA_PROXY = String(MEDIA.proxy
    || "https://ig-archive-media.hheylau.workers.dev").replace(/\/+$/, "");
  const MEDIA_TOKEN_URL = String(MEDIA.tokenUrl
    || (d1Url ? d1Url.replace(/\/+$/, "") + "/media-token" : "")).replace(/\/+$/, "");
  const MEDIA_REPOS = ["ig-archive-test", "ig-archive-nsfw"];
  const MEDIA_PREFIXES = ["posts/", "threads/", "x/", "rednote/", "reddit/"];
  const mediaTokens = {};
  let mediaTokenExp = 0;
  let overrides = {};
  try { overrides = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (_) {}

  // ---- write-behind sync -------------------------------------------------
  // Local storage is always written first, so the UI never waits on a network
  // round trip.  Cloud writes are coalesced and rate limited: state Worker at most once
  // per 30s, the gist directly at most once per 5 minutes (one commit per write), plus
  // one urgent flush when the tab is hidden.  A change made while typing is
  // therefore at most one write, no matter how many keystrokes it took.
  const SYNC = {
    idle: 5000,        // save 5s after the last edit
    min: 30000,        // never more than one state write per 30s
    max: 120000,       // a continuous editor still gets a write every 2 min
    gistMin: 300000,   // one gist commit per 5 min (max 288/day)
    gistHideMin: 15000,// urgent flush on tab hide needs 15s since last gist
    retryMin: 30000,
    retryMax: 1800000, // cap backoff at 30 min after an error/rate limit
  };
  const SYNC_TIME_KEY = "ig_sync_times_v1";
  const GIST_CACHE_KEY = "ig_gist_cache_v1";
  const GIST_CACHE_MS = 300000;   // read the gist at most once per 5 min
  let savedTimes = {};
  try { savedTimes = JSON.parse(localStorage.getItem(SYNC_TIME_KEY) || "{}"); } catch (_) {}
  const sync = {
    rev: Number(localStorage.getItem("ig_sync_rev") || 0),
    d1Rev: Number(localStorage.getItem("ig_sync_d1_rev") || 0),
    gistRev: Number(localStorage.getItem("ig_sync_gist_rev") || 0),
    firstDirty: 0,
    lastEdit: 0,
    lastD1: Number(savedTimes.lastD1 || 0),
    lastGist: Number(savedTimes.lastGist || 0),
    nextD1: Number(savedTimes.nextD1 || 0),
    nextGist: Number(savedTimes.nextGist || 0),
    backoff: SYNC.retryMin,
    lastError: "",
    inFlight: false,
    lastOk: "",
    d1Sig: localStorage.getItem("ig_sync_d1_sig") || "",
    gistSig: localStorage.getItem("ig_sync_gist_sig") || "",
  };
  let syncTimer = null;
  let db = null;
  let posts = [];

  function saveSyncState() {
    localStorage.setItem("ig_sync_rev", String(sync.rev));
    localStorage.setItem("ig_sync_d1_rev", String(sync.d1Rev));
    localStorage.setItem("ig_sync_gist_rev", String(sync.gistRev));
    localStorage.setItem("ig_sync_d1_sig", sync.d1Sig || "");
    localStorage.setItem("ig_sync_gist_sig", sync.gistSig || "");
    localStorage.setItem(SYNC_TIME_KEY, JSON.stringify({
      lastD1: sync.lastD1, lastGist: sync.lastGist,
      nextD1: sync.nextD1, nextGist: sync.nextGist,
    }));
  }

  function markDirty() {
    sync.rev += 1;
    sync.lastEdit = Date.now();
    if (!sync.firstDirty) sync.firstDirty = sync.lastEdit;
    saveSyncState();
    scheduleFlush();
  }

  function targetPending() {
    const pendingD1 = !!d1Url && sync.rev > sync.d1Rev;
    // When the state Worker is configured it is the single writer (it owns the
    // Gist and the merge lock).  A legacy token in localStorage must not PATCH
    // the same gist out of band and lose another tab's merge.
    const pendingGist = !d1Url && !!cloudToken && !!GIST_ID && sync.rev > sync.gistRev;
    return { pendingD1, pendingGist };
  }

  function scheduleFlush() {
    if (syncTimer) clearTimeout(syncTimer);
    syncTimer = null;
    const now = Date.now();
    const { pendingD1, pendingGist } = targetPending();
    if (!pendingD1 && !pendingGist) { sync.firstDirty = 0; return; }
    const lastCloud = Math.max(sync.lastD1, sync.lastGist);
    const idleWait = Math.max(0, SYNC.idle - (now - sync.lastEdit));
    const forceWait = Math.max(0, SYNC.max - (now - (sync.firstDirty || now)));
    const waits = [Math.min(idleWait, forceWait)];
    if (pendingD1) {
      waits.push(Math.max(0, sync.nextD1 - now,
        SYNC.min - (now - lastCloud)));
    }
    if (pendingGist) {
      waits.push(Math.max(0, sync.nextGist - now,
        SYNC.gistMin - (now - sync.lastGist)));
    }
    const wait = Math.max(0, Math.max(...waits));
    syncTimer = setTimeout(() => { syncTimer = null; pushCloud(false); }, wait);
  }

  function headerValue(res, name) {
    try { return res.headers.get(name) || ""; } catch (_) { return ""; }
  }

  function parseRetry(res) {
    const after = headerValue(res, "Retry-After");
    if (after) {
      const secs = Number(after);
      if (Number.isFinite(secs)) return Date.now() + Math.max(1, secs) * 1000;
      const at = Date.parse(after);
      if (Number.isFinite(at)) return at;
    }
    const reset = headerValue(res, "X-RateLimit-Reset");
    if (reset) {
      const at = Number(reset) * 1000;
      if (Number.isFinite(at) && at > Date.now()) return at;
    }
    return 0;
  }

  function dropStaleKey() {
    if (!d1Key) return false;
    d1Key = "";
    try { localStorage.removeItem("ig_cloud_d1_key"); } catch (_) {}
    return true;
  }

  function syncBackoff(until, provider) {    const base = until || (Date.now() + sync.backoff);
    const at = Math.min(base, Date.now() + SYNC.retryMax);
    if (provider === "d1") sync.nextD1 = at;
    else if (provider === "gist") sync.nextGist = at;
    else { sync.nextD1 = at; sync.nextGist = at; }
    sync.backoff = Math.min(sync.backoff * 2, SYNC.retryMax);
    saveSyncState();
  }

  function syncOk() {
    sync.backoff = SYNC.retryMin;
    sync.lastError = "";
    sync.lastOk = new Date().toISOString();
    saveSyncState();
  }

  function syncError(err) {
    sync.lastError = String(err && err.message || err || "sync failed");
  }

  const REGISTRY_PLATFORMS = ["instagram", "x", "threads", "reddit", "rednote"];
  const REGISTRY_RESERVED = new Set([
    "platforms", "t", "pt", "platform_t", "version", "updated_at",
    "migrated_from", "migrated_at",
  ]);

  function platformKey(value) {
    const text = String(value === undefined || value === null ? "" : value)
      .trim().toLowerCase();
    if (!text || text === "all" || text === "__all__") return "";
    if (text === "twitter" || text === "x") return "x";
    if (text === "ig") return "instagram";
    if (text === "rednote-main") return "rednote";
    return /^[a-z0-9_-]{1,32}$/.test(text) ? text : "";
  }

  function platformFromSource(source) {
    const key = String(source || "").toLowerCase();
    if (!key || key === "all" || key === "__all__") return "";
    const post = posts.find((p) => p.source === key || p.platform === key);
    return platformKey((post && post.platform) || key);
  }

  function cleanPlatforms(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    const out = {};
    Object.entries(value).forEach(([rawKey, rawNames]) => {
      const key = platformKey(rawKey);
      if (!key || REGISTRY_RESERVED.has(key)) return;
      out[key] = (Array.isArray(rawNames) ? rawNames : [])
        .map((name) => String(name || "").trim())
        .filter((name, index, list) => name && list.indexOf(name) === index);
    });
    return out;
  }

  function cleanPlatformTimes(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    const out = {};
    Object.entries(value).forEach(([rawKey, rawTime]) => {
      const key = platformKey(rawKey);
      const time = Number(rawTime) || 0;
      if (key && time) out[key] = time;
    });
    return out;
  }

  function registryFromDoc(doc) {
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) return null;
    let raw = doc.__category_registry;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      raw = doc.category_registry;
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const nested = !!(raw.platforms && typeof raw.platforms === "object"
      && !Array.isArray(raw.platforms));
    const platforms = cleanPlatforms(nested ? raw.platforms : raw);
    if (!nested && !Object.keys(platforms).length) return null;
    let t = 0;
    let pt = {};
    if (nested) {
      if (raw.t && typeof raw.t === "object") pt = cleanPlatformTimes(raw.t);
      else t = Number(raw.t) || 0;
      pt = { ...pt, ...cleanPlatformTimes(raw.pt) };
      pt = { ...pt, ...cleanPlatformTimes(raw.platform_t) };
    }
    const docT = doc.__category_registry_t;
    if (docT && typeof docT === "object") pt = { ...pt, ...cleanPlatformTimes(docT) };
    else t = t || Number(docT) || 0;
    pt = { ...pt, ...cleanPlatformTimes(doc.__category_registry_platform_t) };
    if (!t) t = Number(doc.__cat_t) || 0;
    return { platforms, t, pt };
  }

  function legacyCategories(doc) {
    const list = (doc && (doc.__category_order || doc.__categories)) || [];
    return (Array.isArray(list) ? list : [])
      .map((name) => String(name || "").trim())
      .filter((name, index, all) => name && all.indexOf(name) === index);
  }

  function unionPlatforms(platforms) {
    const out = [];
    Object.values(platforms || {}).forEach((names) => {
      (names || []).forEach((name) => {
        if (!out.includes(name)) out.push(name);
      });
    });
    return out;
  }

  function migrateLegacyRegistry(doc) {
    const legacy = legacyCategories(doc);
    if (!legacy.length) return { platforms: {}, t: 0, pt: {} };
    const t = Number(doc && doc.__cat_t) || Date.now();
    const platforms = {};
    const used = new Set();
    posts.forEach((post) => {
      const key = platformKey(post.platform);
      if (!key) return;
      const override = doc && (
        (doc.overrides && doc.overrides[post.id]) || doc[post.id]
      );
      const names = override && Array.isArray(override.categories)
        ? override.categories : cats(post);
      names.forEach((raw) => {
        const name = String(raw || "").trim();
        if (!name || !legacy.includes(name)) return;
        const bucket = platforms[key] || (platforms[key] = []);
        if (!bucket.includes(name)) bucket.push(name);
        used.add(name);
      });
    });
    const unknown = legacy.filter((name) => !used.has(name));
    if (unknown.length && !Object.keys(platforms).length) {
      platforms.instagram = unknown.slice();
    }
    REGISTRY_PLATFORMS.forEach((key) => {
      if (!(key in platforms)) platforms[key] = [];
    });
    const pt = {};
    Object.keys(platforms).forEach((key) => { pt[key] = t; });
    return { platforms, t, pt };
  }

  function registryTime(registry, key) {
    if (!registry) return 0;
    return Number(registry.pt && registry.pt[key]) || Number(registry.t) || 0;
  }

  function mergeRegistries(a, b, mergedOverrides) {
    const left = registryFromDoc(a);
    const right = registryFromDoc(b);
    if (!left && !right) {
      const at = Number(a.__cat_t || 0);
      const bt = Number(b.__cat_t || 0);
      let legacy;
      if (at > bt) legacy = legacyCategories(a);
      else if (bt > at) legacy = legacyCategories(b);
      else {
        legacy = legacyCategories(a);
        legacyCategories(b).forEach((name) => {
          if (!legacy.includes(name)) legacy.push(name);
        });
      }
      if (!legacy.length) return null;
      return migrateLegacyRegistry({
        ...a, ...b,
        overrides: mergedOverrides,
        __category_order: legacy,
        __categories: legacy,
        __cat_t: Math.max(at, bt) || Date.now(),
      });
    }
    const l = left || { platforms: {}, t: 0, pt: {} };
    const r = right || { platforms: {}, t: 0, pt: {} };
    if (!left) {
      const seed = migrateLegacyRegistry(a);
      Object.entries(seed.platforms).forEach(([key, names]) => {
        if (!(key in l.platforms)) {
          l.platforms[key] = names;
          l.pt[key] = seed.pt[key] || seed.t;
          l.t = Math.max(l.t, seed.t);
        }
      });
    }
    if (!right) {
      const seed = migrateLegacyRegistry(b);
      Object.entries(seed.platforms).forEach(([key, names]) => {
        if (!(key in r.platforms)) {
          r.platforms[key] = names;
          r.pt[key] = seed.pt[key] || seed.t;
          r.t = Math.max(r.t, seed.t);
        }
      });
    }
    const platforms = {};
    const pt = {};
    new Set([...Object.keys(l.platforms), ...Object.keys(r.platforms)]).forEach((key) => {
      const lHas = Object.prototype.hasOwnProperty.call(l.platforms, key);
      const rHas = Object.prototype.hasOwnProperty.call(r.platforms, key);
      const lt = registryTime(l, key);
      const rt = registryTime(r, key);
      if (lHas && rHas) {
        if (left && !right) platforms[key] = l.platforms[key];
        else if (!left && right) platforms[key] = r.platforms[key];
        else platforms[key] = rt >= lt ? r.platforms[key] : l.platforms[key];
      } else if (rHas) platforms[key] = r.platforms[key];
      else platforms[key] = l.platforms[key];
      const stamp = lHas && rHas ? Math.max(lt, rt) : (rHas ? rt : lt);
      if (stamp) pt[key] = stamp;
    });
    const t = Math.max(l.t, r.t, ...Object.values(pt));
    return { platforms, t, pt };
  }

  function mergeDocs(left, right) {
    const a = left || {};
    const b = right || {};
    const ao = a.overrides || {};
    const bo = b.overrides || {};
    const merged = {};
    const keys = new Set([...Object.keys(ao), ...Object.keys(bo)]);
    keys.forEach((key) => {
      if (key.startsWith("__")) return;
      const x = ao[key];
      const y = bo[key];
      if (!x) { merged[key] = y; return; }
      if (!y) { merged[key] = x; return; }
      const tx = Number(x.t || 0);
      const ty = Number(y.t || 0);
      merged[key] = ty > tx ? { ...x, ...y } : { ...y, ...x };
    });
    const registry = mergeRegistries(a, b, merged);
    // Manual order is a permutation, so the newest list wins outright.
    const aot = Number(a.__order_t || 0);
    const bot = Number(b.__order_t || 0);
    let manual = null;
    if (aot > bot) manual = a.__order || null;
    else if (bot > aot) manual = b.__order || null;
    else {
      const l = a.__order || [];
      const r = b.__order || [];
      manual = r.length >= l.length ? r : l;
    }
    const out = { ...a, ...b, overrides: merged };
    if (registry) {
      out.__category_registry = registry.platforms;
      out.__category_registry_v = 2;
      out.__category_registry_t = registry.t || Date.now();
      if (Object.keys(registry.pt).length) {
        out.__category_registry_platform_t = registry.pt;
      }
      out.__categories = unionPlatforms(registry.platforms);
      out.__category_order = out.__categories;
      out.__cat_t = out.__category_registry_t;
    } else {
      const at = Number(a.__cat_t || 0);
      const bt = Number(b.__cat_t || 0);
      let order;
      if (at > bt) order = legacyCategories(a);
      else if (bt > at) order = legacyCategories(b);
      else {
        order = legacyCategories(a);
        legacyCategories(b).forEach((name) => {
          if (!order.includes(name)) order.push(name);
        });
      }
      out.__categories = order;
      out.__category_order = order;
      out.__cat_t = Math.max(at, bt);
    }
    if (manual && manual.length) out.__order = manual;
    if (aot || bot) out.__order_t = Math.max(aot, bot);
    out.__updated_at = new Date().toISOString();
    return out;
  }

  function stateToOverrides(state) {
    const out = { overrides: {} };
    const byKey = {};
    posts.forEach((p) => {
      byKey[p.folder || p.pk || String(p.id)] = p;
    });
    const sources = (state && state.sources) || {};
    const registryByPlatform = {};
    Object.values(sources).forEach((src) => {
      const platform = platformKey(src.platform || "");
      if (platform) {
        const bucket = registryByPlatform[platform] || (registryByPlatform[platform] = []);
        (src.categories || []).forEach((raw) => {
          const name = String(raw || "").trim();
          if (name && !bucket.includes(name)) bucket.push(name);
        });
      }
      const assign = src.assignments || {};
      const notes = src.notes || {};
      const tags = src.tags || {};
      const meta = src.meta || {};
      const keys = new Set([...Object.keys(assign), ...Object.keys(notes),
        ...Object.keys(tags), ...Object.keys(meta)]);
      keys.forEach((key) => {
        const post = byKey[key];
        if (!post) return;
        const patch = {};
        if (Array.isArray(assign[key]) && assign[key].length) {
          patch.categories = assign[key];
        }
        if (typeof notes[key] === "string" && notes[key]) patch.notes = notes[key];
        if (Array.isArray(tags[key]) && tags[key].length) patch.tags = tags[key];
        if (meta[key]) {
          if (typeof meta[key].notes === "string" && meta[key].notes) {
            patch.notes = meta[key].notes;
          }
          if (Array.isArray(meta[key].tags) && meta[key].tags.length) {
            patch.tags = meta[key].tags;
          }
        }
        if (Object.keys(patch).length) {
          if (post.platform) patch.platform = post.platform;
          if (post.folder) patch.folder = String(post.folder);
          out.overrides[post.id] = { ...(out.overrides[post.id] || {}), ...patch };
        }
      });
    });
    const stateRegistry = state && state.category_registry;
    if (stateRegistry && typeof stateRegistry === "object" && !Array.isArray(stateRegistry)) {
      Object.entries(stateRegistry).forEach(([key, names]) => {
        const platform = platformKey(key);
        if (!platform) return;
        registryByPlatform[platform] = (Array.isArray(names) ? names : [])
          .map((name) => String(name || "").trim())
          .filter(Boolean);
      });
    }
    // Carry per-platform registries so a gist export alone still seeds the
    // category bars without sharing X's categories with Reddit.
    if (Object.keys(registryByPlatform).length) {
      const stamp = Date.parse((state && state.updated_at) || "");
      const t = Number.isFinite(stamp) ? stamp : Date.now();
      out.__category_registry = registryByPlatform;
      out.__category_registry_v = 2;
      out.__category_registry_t = t;
      out.__category_registry_platform_t = {};
      Object.keys(registryByPlatform).forEach((key) => {
        out.__category_registry_platform_t[key] = t;
      });
      out.__categories = unionPlatforms(registryByPlatform);
      out.__category_order = out.__categories.slice();
      out.__cat_t = t;
    }
    return out;
  }

  function adoptDoc(doc) {
    doc = doc || {};
    overrides = doc.overrides || {};
    let registry = registryFromDoc(doc);
    if (!registry && legacyCategories(doc).length) {
      registry = migrateLegacyRegistry(doc);
    }
    if (registry) {
      overrides.__category_registry = registry.platforms;
      overrides.__category_registry_v = 2;
      overrides.__category_registry_t = registry.t || Date.now();
      if (Object.keys(registry.pt).length) {
        overrides.__category_registry_platform_t = registry.pt;
      }
      overrides.__categories = unionPlatforms(registry.platforms);
      overrides.__category_order = overrides.__categories.slice();
      overrides.__cat_t = overrides.__category_registry_t;
    } else {
      overrides.__categories = doc.__categories || [];
      overrides.__category_order = doc.__category_order || overrides.__categories;
      overrides.__cat_t = doc.__cat_t || overrides.__cat_t || 0;
    }
    if (Array.isArray(doc.__order) && doc.__order.length) {
      overrides.__order = doc.__order;
      overrides.__order_t = doc.__order_t || overrides.__order_t || 0;
    }
    localStorage.setItem(STORE, JSON.stringify(overrides));
  }

  function copyRegistryFields(target, source) {
    if (!source) return target;
    if (source.__category_registry) {
      target.__category_registry = source.__category_registry;
      target.__category_registry_v = source.__category_registry_v || 2;
      target.__category_registry_t = source.__category_registry_t || 0;
      if (source.__category_registry_platform_t) {
        target.__category_registry_platform_t = source.__category_registry_platform_t;
      }
    }
    return target;
  }

  async function loadCloud(force) {
    let doc = { ...overrides };
    // Manual order is kept inside the overrides blob locally, but the Worker
    // document stores it at the top level; lift it before merging.
    if (Array.isArray(overrides.__order)) {
      doc.__order = overrides.__order;
      doc.__order_t = Number(overrides.__order_t || 0);
    }
    let localDoc = null;
    try {
      const local = await nativeFetch(`static_overrides.json?nc=${Date.now()}`,
        { cache: "no-store" });
      if (local.ok) localDoc = await local.json();
    } catch (_) {}
    if (localDoc) doc = mergeDocs(localDoc, doc);
    if (doc.__categories) {
      doc.__categories = doc.__categories || doc.__category_order || [];
      doc.__category_order = doc.__category_order || doc.__categories;
    }

    // The state Worker (Gist) is the fast primary. One read per page load is
    // cheap and keeps different devices in sync even when the gist is rate
    // limited.
    if (d1Url) {
      try {
        const headers = d1Key ? { "X-Archive-Key": d1Key } : {};
        let res = await nativeFetch(`${d1Url}${d1Url.includes("?") ? "&" : "?"}id=default`,
          { headers, cache: "no-store" });
        if ((res.status === 401 || res.status === 403) && dropStaleKey()) {
          // The public document needs no key; an old key from a previous
          // install must not lock this browser out of reads.
          res = await nativeFetch(`${d1Url}${d1Url.includes("?") ? "&" : "?"}id=default`,
            { cache: "no-store" });
        }
        if (res.ok) {
          const data = await res.json();
          if (data && data.doc) doc = mergeDocs(doc, data.doc);
        }
      } catch (_) {}
    }

    if (!GIST_ID) {
      adoptDoc(doc);
      return;
    }

    // Gist read cache: anonymous callers only get 60 requests/hour.
    let remote = null;
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem(GIST_CACHE_KEY) || "null"); } catch (_) {}
    const fresh = cached && Date.now() - Number(cached.at || 0) < GIST_CACHE_MS;
    if (!force && fresh) {
      remote = cached.doc;
    } else {
      try {
        const headers = cloudToken ? { Authorization: `Bearer ${cloudToken}` } : {};
        const res = await nativeFetch(`https://api.github.com/gists/${GIST_ID}?t=${Date.now()}`, {
          headers, cache: "no-store" });
        if (res.ok) {
          const gist = await res.json();
          const files = gist.files || {};
          const direct = files[GIST_FILE] && files[GIST_FILE].content;
          if (direct) {
            try { remote = JSON.parse(direct); } catch (_) {}
          }
          // The browser export (cloud_state.json) holds the full personal
          // history keyed by media id.  Merge anything that matches a post in
          // the unified manifest, even when static_overrides.json exists.
          if (files["cloud_state.json"] && files["cloud_state.json"].content) {
            try {
              const mapped = stateToOverrides(JSON.parse(files["cloud_state.json"].content));
              remote = remote ? mergeDocs(remote, mapped) : mapped;
            } catch (_) {}
          }
          if (remote) {
            localStorage.setItem(GIST_CACHE_KEY, JSON.stringify({
              at: Date.now(), doc: remote,
            }));
          }
        } else if (cached && res.status === 403) {
          remote = cached.doc;  // anonymous rate limit: use the last copy
        }
      } catch (_) {
        if (cached) remote = cached.doc;
      }
    }
    if (remote) doc = mergeDocs(doc, remote);
    adoptDoc(doc);
  }

  function syncDoc() {
    const doc = {
      overrides,
      __categories: overrides.__categories || overrides.__category_order || [],
      __category_order: overrides.__category_order || overrides.__categories || [],
      __cat_t: overrides.__cat_t || 0,
      __order: Array.isArray(overrides.__order) ? overrides.__order : [],
      __order_t: overrides.__order_t || 0,
      updated_at: new Date().toISOString(),
    };
    if (overrides.__category_registry
      && typeof overrides.__category_registry === "object") {
      doc.__category_registry = overrides.__category_registry;
      doc.__category_registry_v = 2;
      doc.__category_registry_t = Number(overrides.__category_registry_t) || 0;
      if (overrides.__category_registry_platform_t) {
        doc.__category_registry_platform_t =
          overrides.__category_registry_platform_t;
      }
    }
    return doc;
  }

  async function pushD1(revAtSend, keepalive) {
    const send = (withKey) => nativeFetch(d1Url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(withKey && d1Key ? { "X-Archive-Key": d1Key } : {}),
      },
      body: JSON.stringify({ doc: syncDoc(), rev: revAtSend }),
      keepalive: !!keepalive,
    });
    let res = await send(true);
    if ((res.status === 401 || res.status === 403) && dropStaleKey()) {
      // A key saved by an older install can be wrong after a rotation; the
      // public document is writable without it, so retry keyless once.
      res = await send(false);
    }
    if (!res.ok) {
      const retryAt = parseRetry(res);
      if (res.status === 401 || res.status === 403) {
        syncBackoff(retryAt || Date.now() + 15 * 60 * 1000, "d1");
        throw new Error(res.status === 401
          ? "state key rejected" : "state write forbidden (key or origin)");
      }
      if (res.status === 429 || res.status >= 500) {
        syncBackoff(retryAt || 0, "d1");
        throw new Error(`state Worker unavailable (${res.status})`);
      }
      throw new Error(`state write failed (${res.status})`);
    }
    const data = await res.json().catch(() => null);
    if (data && data.doc && data.doc.overrides) {
      // The Worker merges by per-post timestamp, so adopt anything newer than
      // what this tab already has without clobbering edits made during the POST.
      const merged = mergeDocs({ overrides, __order: overrides.__order,
        __order_t: overrides.__order_t }, data.doc);
      adoptDoc(merged);
    }
    sync.d1Rev = Math.max(sync.d1Rev, revAtSend);
    sync.lastD1 = Date.now();
    sync.nextD1 = 0;
    saveSyncState();
    sync.lastError = "";
    saveSyncState();
  }

  async function pushGist(revAtSend, keepalive) {
    const body = JSON.stringify(syncDoc());
    const res = await nativeFetch(`https://api.github.com/gists/${GIST_ID}?t=${Date.now()}`, {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${cloudToken}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ files: { [GIST_FILE]: { content: body } } }),
      keepalive: !!keepalive,
    });
    if (!res.ok) {
      const retryAt = parseRetry(res);
      if (res.status === 401) {
        syncBackoff(Date.now() + 15 * 60 * 1000, "gist");
        throw new Error("gist token expired or lacks gist scope");
      }
      if (res.status === 403 || res.status === 429) {
        syncBackoff(retryAt || 0, "gist");
        throw new Error("GitHub rate limit; will retry");
      }
      throw new Error(`gist write failed (${res.status})`);
    }
    sync.gistRev = Math.max(sync.gistRev, revAtSend);
    sync.lastGist = Date.now();
    sync.nextGist = 0;
    sync.lastError = "";
    try {
      localStorage.setItem(GIST_CACHE_KEY, JSON.stringify({
        at: Date.now(), doc: syncDoc(),
      }));
    } catch (_) {}
    saveSyncState();
  }

  async function pushCloud(force, keepalive) {
    if (sync.inFlight) { scheduleFlush(); return; }
    const now = Date.now();
    const { pendingD1, pendingGist } = targetPending();
    if (!pendingD1 && !pendingGist) { sync.firstDirty = 0; return; }
    const d1Due = pendingD1 && now >= sync.nextD1;
    const gistDue = pendingGist && now >= sync.nextGist &&
      now - sync.lastGist >= (force ? SYNC.gistHideMin : SYNC.gistMin);
    if (!d1Due && !gistDue) { scheduleFlush(); return; }
    sync.inFlight = true;
    const revAtSend = sync.rev;
    const sig = docHash();
    let firstErr = null;
    try {
      if (d1Due) {
        if (sig === sync.d1Sig) {
          sync.d1Rev = Math.max(sync.d1Rev, revAtSend);
          sync.lastD1 = Date.now();
        } else {
          try { await pushD1(revAtSend, keepalive); sync.d1Sig = sig; }
          catch (err) {
            // Network failures do not set a retry time in pushD1; back off
            // here so a reload or a dead tab does not hammer the Worker.
            if (sync.nextD1 <= Date.now()) syncBackoff(0, "d1");
            firstErr = err;
          }
        }
      }
      if (gistDue) {
        if (sig === sync.gistSig) {
          sync.gistRev = Math.max(sync.gistRev, revAtSend);
          sync.lastGist = Date.now();
        } else {
          try { await pushGist(revAtSend, keepalive); sync.gistSig = sig; }
          catch (err) {
            if (sync.nextGist <= Date.now()) syncBackoff(0, "gist");
            firstErr = firstErr || err;
          }
        }
      }
      if (firstErr) syncError(firstErr);
      else syncOk();
    } finally {
      sync.inFlight = false;
      if (sync.rev > Math.max(sync.d1Rev, sync.gistRev)) scheduleFlush();
      else sync.firstDirty = 0;
    }
  }

  function splitMediaUrl(raw) {
    if (!raw || typeof raw !== "string") return null;
    const hf = raw.match(/^https?:\/\/huggingface\.co\/datasets\/[^/]+\/(ig-archive-[a-z0-9-]+)\/resolve\/[^/]+\/(.+)$/);
    if (hf) return { repo: hf[1], path: hf[2] };
    if (!MEDIA_PROXY) return null;
    try {
      const url = new URL(raw, location.href);
      if (url.origin !== new URL(MEDIA_PROXY).origin) return null;
      const m = url.pathname.match(/^\/(ig-archive-[a-z0-9-]+)\/(.+)$/);
      return m ? { repo: m[1], path: m[2] } : null;
    } catch (_) {
      return null;
    }
  }

  function mediaPrefix(path) {
    return MEDIA_PREFIXES.find((p) => path.startsWith(p)) || "";
  }

  async function loadMediaTokens(force) {
    if (!MEDIA_PROXY || !MEDIA_TOKEN_URL) return;
    if (!force && Object.keys(mediaTokens).length
      && Date.now() / 1000 < mediaTokenExp - 300) return;
    const combos = new Set();
    const consider = (post, u) => {
      const ref = splitMediaUrl(u);
      if (!ref || !MEDIA_REPOS.includes(ref.repo)) return;
      // A moved post may target the NSFW repo; mint that token too.
      const target = mediaRefFor(post, ref);
      const prefix = mediaPrefix(target.path);
      if (prefix && MEDIA_REPOS.includes(target.repo)) {
        combos.add(`${target.repo}|${prefix}`);
      }
    };
    posts.forEach((p) => {
      consider(p, p.thumb);
      consider(p, p.cover);
      consider(p, p.comments_url);
      (p.media || []).forEach((m) => { consider(p, m.url); consider(p, m.thumb); });
    });
    if (!combos.size) return;
    const results = await Promise.all([...combos].map(async (key) => {
      const [repo, prefix] = key.split("|");
      try {
        const res = await nativeFetch(
          `${MEDIA_TOKEN_URL}?repo=${encodeURIComponent(repo)}` +
          `&prefix=${encodeURIComponent(prefix)}&ttl=21600`,
          { cache: "no-store" });
        if (!res.ok) return null;
        const data = await res.json();
        if (!data || !data.token || !data.exp) return null;
        return { key, token: data.token, exp: Number(data.exp) };
      } catch (_) {
        return null;
      }
    }));
    let minExp = 0;
    results.filter(Boolean).forEach((r) => {
      mediaTokens[r.key] = { token: r.token, exp: r.exp };
      minExp = minExp ? Math.min(minExp, r.exp) : r.exp;
    });
    if (minExp) mediaTokenExp = minExp;
  }

  function proxiedMediaUrl(repo, path) {
    if (!MEDIA_PROXY) return "";
    const prefix = mediaPrefix(path);
    const rec = mediaTokens[`${repo}|${prefix}`];
    if (!rec || rec.exp * 1000 < Date.now()) return "";
    return `${MEDIA_PROXY}/${repo}/${path}?t=${encodeURIComponent(rec.token)}`;
  }

  // A post moved to the NSFW repo carries nsfw_repo + nsfw_paths in its
  // override.  Only recorded files are rewritten, so a partially failed move
  // keeps the files that are still on ig-archive-test.
  function mediaRefFor(post, ref) {
    const o = overrides[post.id];
    if (!o || !o.nsfw_repo) return ref;
    if (o.nsfw_paths && typeof o.nsfw_paths === "object") {
      const moved = o.nsfw_paths[ref.path];
      return moved ? { repo: o.nsfw_repo, path: moved } : ref;
    }
    if (o.nsfw === true) return { repo: o.nsfw_repo, path: ref.path };
    return ref;
  }

  function rewriteMediaUrl(post, raw) {
    const ref = splitMediaUrl(raw);
    if (!ref) return raw;
    const target = mediaRefFor(post, ref);
    const proxied = proxiedMediaUrl(target.repo, target.path);
    if (proxied) return proxied;
    // Fallback: direct HF URL.  Works while the dataset is still public.
    return `https://huggingface.co/datasets/shalevlau/${target.repo}/resolve/main/${target.path}`;
  }

  function rewritePostMedia(post) {
    if (post.thumb) post.thumb = rewriteMediaUrl(post, post.thumb);
    if (post.cover) post.cover = rewriteMediaUrl(post, post.cover);
    if (post.comments_url) post.comments_url = rewriteMediaUrl(post, post.comments_url);
    if (Array.isArray(post.media)) {
      post.media = post.media.map((m) => ({
        ...m,
        url: rewriteMediaUrl(post, m.url),
        thumb: m.thumb ? rewriteMediaUrl(post, m.thumb) : m.thumb,
      }));
    }
  }

  function applyOverride(post) {
    const o = overrides[post.id];
    if (!o) return;
    if (Array.isArray(o.categories)) {
      post.categories = o.categories;
      post.category = o.categories[0] || null;
    }
    if (o.notes !== undefined) post.notes = o.notes;
    if (Array.isArray(o.tags)) post.tags = o.tags;
  }

  function saveOverride(post, patch) {
    const prev = overrides[post.id] || {};
    const changed = Object.keys(patch)
      .some((key) => !sameValue(prev[key], patch[key]));
    if (!changed) return;  // a blur with the same text must not create a write
    overrides[post.id] = {
      ...prev,
      ...patch,
      t: Date.now(),
      ...(post.platform ? { platform: post.platform } : {}),
      ...(post.folder ? { folder: String(post.folder) } : {}),
    };
    localStorage.setItem(STORE, JSON.stringify(overrides));
    markDirty();
  }

  const json = (data, status = 200) => new Response(
    JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

  const cats = (item) => item.categories || (item.category ? [item.category] : []);

  function sameValue(a, b) {
    if (a === b) return true;
    try { return JSON.stringify(a) === JSON.stringify(b); } catch (_) { return false; }
  }

  function docHash() {
    const doc = syncDoc();
    delete doc.updated_at;
    const ov = {};
    Object.keys(doc.overrides || {}).sort().forEach((k) => {
      ov[k] = doc.overrides[k];
    });
    doc.overrides = ov;
    const text = JSON.stringify(doc);
    let h = 2166136261;
    for (let i = 0; i < text.length; i += 1) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return String(text.length) + ":" + (h >>> 0).toString(36);
  }

  function postKey(post) {
    return post.folder || post.pk || String(post.id);
  }

  function currentPlatform(params) {
    const raw = String((params && params.get("platform"))
      || new URLSearchParams(location.search).get("platform") || "").toLowerCase();
    return raw === "twitter" ? "x" : raw;
  }

  function registryPlatform(params) {
    const direct = platformKey(currentPlatform(params));
    if (direct) return direct;
    return platformFromSource(currentSource(params));
  }

  function sourceItems(source, platform) {
    if (!source || source === "all" || source === "__all__") {
      return platform
        ? posts.filter((p) => String(p.platform || "").toLowerCase() === platform)
        : posts.slice();
    }
    return posts.filter((p) => p.source === source || p.platform === source);
  }

  function registry(platform) {
    const key = platformKey(platform);
    const map = overrides.__category_registry;
    if (map && typeof map === "object" && !Array.isArray(map)) {
      if (!key) return unionPlatforms(map);
      if (Object.prototype.hasOwnProperty.call(map, key)) {
        return (Array.isArray(map[key]) ? map[key] : []).slice();
      }
      // The registry is authoritative: never fall back to another platform's
      // categories when this platform has an empty/missing entry.
      return [];
    }
    if (Array.isArray(overrides.__category_order)) {
      return overrides.__category_order.filter((c) => typeof c === "string");
    }
    if (Array.isArray(overrides.__categories)) {
      return overrides.__categories.filter((c) => typeof c === "string");
    }
    const names = [];
    posts.forEach((p) => {
      if (key && platformKey(p.platform) !== key) return;
      cats(p).forEach((c) => { if (!names.includes(c)) names.push(c); });
    });
    return names;
  }

  function saveRegistry(names, platform) {
    const key = platformKey(platform || registryPlatform());
    if (!key) return;
    const next = names.filter(Boolean);
    let map = overrides.__category_registry;
    let pt = overrides.__category_registry_platform_t || {};
    if (!map || typeof map !== "object" || Array.isArray(map)
      || !Object.keys(map).length) {
      const legacy = legacyCategories(overrides);
      if (legacy.length) {
        const migrated = migrateLegacyRegistry(overrides);
        map = migrated.platforms;
        pt = migrated.pt;
      } else {
        map = {};
      }
    }
    const prev = Array.isArray(map[key]) ? map[key] : [];
    if (next.length === prev.length && next.every((c, i) => c === prev[i])) return;
    map = { ...map, [key]: next };
    const now = Date.now();
    overrides.__category_registry = map;
    overrides.__category_registry_v = 2;
    overrides.__category_registry_t = Math.max(
      Number(overrides.__category_registry_t) || 0, now);
    overrides.__category_registry_platform_t = { ...pt, [key]: now };
    overrides.__categories = unionPlatforms(map);
    overrides.__category_order = overrides.__categories.slice();
    overrides.__cat_t = overrides.__category_registry_t;
    localStorage.setItem(STORE, JSON.stringify(overrides));
    markDirty();
  }

  function schedulePush() {
    markDirty();
  }

  function categoryCounts(items, platform) {
    const counts = {};
    registry(platform).forEach((c) => { counts[c] = 0; });
    items.forEach((p) => cats(p).forEach((c) => {
      counts[c] = (counts[c] || 0) + 1;
    }));
    return counts;
  }

  function filterCounts(items) {
    return {
      no_video: items.filter((p) => !p.is_video).length,
      no_image: items.filter((p) => !(p.media || []).some((m) => m.kind === "image")).length,
      no_ads: items.length,
      bad_owner: items.filter((p) => !p.owner).length,
      bad_cover: items.filter((p) => !p.cover).length,
      caption_empty: items.filter((p) => !(p.caption_full || p.caption || "").trim()).length,
      caption_error: items.filter((p) => /^(429|error)/i.test((p.caption_full || p.caption || "").trim())).length,
      bad_shortcode: items.filter((p) => !p.shortcode).length,
      comments_empty: items.filter((p) => !p.comments_url).length,
      comments_error: 0,
      still_saved: 0,
      already_unsaved: items.length,
      uncategorised: items.filter((p) => !cats(p).length).length,
    };
  }

  function facetsFor(source, platform) {
    const key = platformKey(platform) || platformFromSource(source);
    const items = sourceItems(source, key);
    const counts = categoryCounts(items, key);
    const names = registry(key);
    const catList = names.map((name) => ({ name, count: counts[name] || 0 }));
    const tagCounts = {};
    const suggestedCounts = {};
    const postTagCounts = {};
    items.forEach((p) => {
      (p.tags || []).forEach((t) => {
        tagCounts[t] = (tagCounts[t] || 0) + 1;
      });
      if (p.suggested_category) {
        suggestedCounts[p.suggested_category] =
          (suggestedCounts[p.suggested_category] || 0) + 1;
      }
      (p.hashtags || []).forEach((t) => {
        postTagCounts[t] = (postTagCounts[t] || 0) + 1;
      });
    });
    const asList = (counts) => Object.entries(counts)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([name, count]) => ({ name, count }));
    return {
      total: items.length,
      videos: items.filter((p) => p.is_video).length,
      ads: items.filter((p) => p.is_ad).length,
      filter_counts: filterCounts(items),
      categories: catList,
      category_list: catList,
      tags: asList(tagCounts),
      suggested: asList(suggestedCounts),
      post_tags: asList(postTagCounts),
      presets: names.length ? names : (db.facets.presets || []),
      platform: key || "all",
      saved: { still_saved: 0, already_unsaved: items.length },
      stats: { comments: items.reduce((n, p) => n + (p.comments_count || 0), 0) },
    };
  }

  const SOURCE_ALIASES = {
    twitter: "x",
    instagram: "shalevfrfr",
  };

  function currentSource(params) {
    const raw = (params && params.get("source"))
      || new URLSearchParams(location.search).get("source") || "";
    return SOURCE_ALIASES[raw] || raw;
  }

  function filtered(params) {
    let items = posts.slice();
    const source = currentSource(params);
    const platform = currentPlatform(params);
    if (source && source !== "all" && source !== "__all__") {
      items = items.filter((p) => p.source === source || p.platform === source);
    } else if (platform) {
      items = items.filter(
        (p) => String(p.platform || "").toLowerCase() === platform);
    }
    const q = (params.get("q") || "").toLowerCase();
    const category = params.get("category") || "";
    const suggested = params.get("suggested") || "";
    const tag = params.get("tag") || "";
    const owner = params.get("owner") || "";
    if (q) {
      items = items.filter((p) => (
        (p.caption_full || "").toLowerCase().includes(q)
        || (p.owner || "").toLowerCase().includes(q)
        || (p.shortcode || "").toLowerCase().includes(q)
        || (p.notes || "").toLowerCase().includes(q)
        || (p.vision_caption || "").toLowerCase().includes(q)
        || (p.transcript || "").toLowerCase().includes(q)
        || (p.tags || []).some((t) => t.toLowerCase().includes(q))
      ));
    }
    if (suggested) items = items.filter((p) => p.suggested_category === suggested);
    if (category && category !== "all") items = items.filter((p) => cats(p).includes(category));
    if (tag) items = items.filter((p) => (p.tags || []).includes(tag));
    if (owner) items = items.filter((p) => p.owner === owner);
    const flags = new Set((params.get("flags") || "").split(",").filter(Boolean));
    if (flags.size) {
      items = items.filter((p) => {
        const media = p.media || [];
        if (flags.has("no_video") && p.is_video) return false;
        if (flags.has("no_image") && media.some((m) => m.kind === "image")) return false;
        if (flags.has("no_ads") && p.is_ad) return false;
        if (flags.has("bad_owner") && p.owner) return false;
        if (flags.has("bad_cover") && p.cover) return false;
        if (flags.has("caption_empty") && (p.caption_full || p.caption || "").trim()) return false;
        if (flags.has("caption_error") && !/^(429|error)/i.test((p.caption_full || "").trim())) return false;
        if (flags.has("bad_shortcode") && p.shortcode) return false;
        if (flags.has("comments_empty") && p.comments_url) return false;
        if (flags.has("comments_error") && !p.comments_url) return false;
        if (flags.has("still_saved") && p.unsaved) return false;
        if (flags.has("already_unsaved") && !p.unsaved) return false;
        if (flags.has("uncategorised") && cats(p).length) return false;
        return true;
      });
    }
    const only = (params.get("categories") || "").split(",").filter(Boolean);
    if (only.length) items = items.filter((p) => only.some((c) => cats(p).includes(c)));
    return items;
  }

  function sorted(items, sort) {
    if (sort === "newest") {
      return items.sort((a, b) => (b.taken_at || "").localeCompare(a.taken_at || "") || b.id - a.id);
    }
    if (sort === "oldest") {
      return items.sort((a, b) => {
        if (!a.taken_at && !b.taken_at) return a.id - b.id;
        if (!a.taken_at) return 1;
        if (!b.taken_at) return -1;
        return a.taken_at.localeCompare(b.taken_at) || a.id - b.id;
      });
    }
    if (sort === "likes") return items.sort((a, b) => (b.likes || 0) - (a.likes || 0));
    if (sort === "uncategorised") {
      return items.sort((a, b) => {
        const ac = cats(a).length ? 1 : 0;
        const bc = cats(b).length ? 1 : 0;
        return ac - bc || a.id - b.id;
      });
    }
    if (sort === "random") return items.sort(() => Math.random() - 0.5);
    const order = Array.isArray(overrides.__order) ? overrides.__order : [];
    if (order.length) {
      const rank = new Map(order.map((id, i) => [Number(id), i]));
      return items.sort((a, b) =>
        (rank.has(a.id) ? rank.get(a.id) : 1e9)
        - (rank.has(b.id) ? rank.get(b.id) : 1e9) || a.id - b.id);
    }
    // No manual order yet: use the saved-feed order the manifest carries from
    // the archive DB, so the online-only site shows the same order without a
    // local server.  Posts without it sort last by id.
    const savedRank = (p) => {
      const n = Number(p.order);
      return Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER;
    };
    return items.sort((a, b) => savedRank(a) - savedRank(b) || a.id - b.id);
  }

  function urgentFlush() {
    const { pendingD1, pendingGist } = targetPending();
    if (!pendingD1 && !pendingGist) return;
    const gistFresh = Date.now() - sync.lastGist < SYNC.gistHideMin;
    pushCloud(!gistFresh, true);
  }

  // The online-only build has no local server.  Block links to the local
  // downloader dashboard so a dead 127.0.0.1 tab does not look like the
  // archive is broken; everything else (media, state) is remote.
  document.addEventListener("click", (event) => {
    const anchor = event.target && event.target.closest
      ? event.target.closest('a[href*="127.0.0.1"], a[href*="//localhost"]')
      : null;
    if (!anchor) return;
    event.preventDefault();
    event.stopPropagation();
    alert("That opens the local downloader dashboard, which only exists on "
      + "the machine that runs the archiver. The gallery itself is fully "
      + "online: media is on Hugging Face and edits sync to workers.dev.");
  }, true);

  // The hosted build has no local downloader dashboard.  web/app.js still
  // points the live iframe at 127.0.0.1:8766, which a https page cannot load;
  // hide it and leave a read-only explanation instead of a broken frame.
  function maskLocalDashboard() {
    const wrap = document.getElementById("liveFrameWrap");
    if (wrap && !wrap.classList.contains("hidden")) {
      const frame = document.getElementById("liveFrame");
      if (frame) frame.removeAttribute("src");
      wrap.classList.add("hidden");
    }
    const note = document.getElementById("liveNote");
    if (note && /127\.0\.0\.1|local 8766|local 8766 dashboard/i.test(
      note.textContent || "")) {
      note.textContent = "Live download progress is produced by the local "
        + "archiver. This hosted gallery shows the finished archive; "
        + "categories, notes and order are fully online.";
    }
  }
  if (typeof MutationObserver === "function" && document.body) {
    new MutationObserver(maskLocalDashboard).observe(document.body, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ["class", "src"],
    });
  }
  window.addEventListener("load", maskLocalDashboard);

  // Refresh signed media URLs before they expire.  Images already on screen
  // stay cached; new videos / lazy thumbnails use the fresh token.
  if (MEDIA_PROXY && MEDIA_TOKEN_URL) {
    setInterval(() => {
      loadMediaTokens(true).then(() => posts.forEach(rewritePostMedia)).catch(() => {});
    }, 4 * 3600 * 1000);
  }

  window.fetch = async (input, options = {}) => {
    const requestUrl = typeof input === "string" ? input : input.url;
    const url = new URL(requestUrl, document.baseURI);
    const marker = url.pathname.indexOf("/api/");
    if (marker < 0) return nativeFetch(input, options);
    if (!db) {
      db = await ready;
      posts = db.posts;
      window.__STATIC_POSTS = posts;
      posts.forEach(applyOverride);
      // Autosave flush/listeners stay unconditional; the old export/import/
      // cloud-sync buttons and their panel are injected by the UI layer now.
      window.addEventListener("pagehide", urgentFlush);
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") urgentFlush();
      });
      setInterval(() => {
        if (sync.rev > Math.max(sync.d1Rev, sync.gistRev)) pushCloud(false);
      }, 60000);
    }
    const api = url.pathname.slice(marker + 5);
    const method = (options.method || "GET").toUpperCase();

    if (api === "edits") {
      if (method !== "POST") return json({ error: "method not allowed" }, 405);
      const headers = new Headers(options.headers || {});
      headers.set("Content-Type", "application/json");
      const remote = await galleryJson("/api/edits", null, {
        method: "POST",
        headers,
        body: options.body,
        keepalive: !!options.keepalive,
      }, options.keepalive ? 0 : 8000);
      if (remote) return json(remote);
      return json({ error: "gallery edits endpoint unavailable" }, 503);
    }
    if (api === "accounts") {
      if (method === "GET") {
        const remote = await galleryJson("/api/accounts", url.searchParams, {}, 3000);
        if (remote && Array.isArray(remote.accounts)) return json(remote);
      }
      if (window.SAVED_ACCOUNTS) return json(window.SAVED_ACCOUNTS);
      return json({ accounts: [{
        id: "instagram-static", platform: "instagram", label: "Instagram",
        handle: "", total: posts.length, videos: posts.filter((p) => p.is_video).length,
      }], active: "instagram-static" });
    }
    if (api === "facets") {
      const remote = await galleryJson("/api/facets", url.searchParams, {}, 3000);
      if (remote && (Array.isArray(remote.category_list)
        || Array.isArray(remote.categories))) return json(remote);
      return json(facetsFor(currentSource(url.searchParams), currentPlatform(url.searchParams)));
    }
    if (api === "categories") {
      const src = currentSource(url.searchParams);
      let payload = {};
      if (method === "POST") {
        try { payload = JSON.parse(options.body || "{}"); } catch (_) {}
      }
      const platform = platformKey(payload.platform) || registryPlatform(url.searchParams);
      const adoptRemoteRegistry = (remote) => {
        const reg = remote && remote.registry;
        if (!reg || !reg.platforms || typeof reg.platforms !== "object") return;
        overrides.__category_registry = reg.platforms;
        overrides.__category_registry_v = 2;
        overrides.__category_registry_t = reg.t || Date.now();
        if (reg.pt) overrides.__category_registry_platform_t = reg.pt;
        overrides.__categories = unionPlatforms(reg.platforms);
        overrides.__category_order = overrides.__categories.slice();
        overrides.__cat_t = overrides.__category_registry_t;
        localStorage.setItem(STORE, JSON.stringify(overrides));
      };
      if (method === "GET") {
        const remote = await galleryJson("/api/categories", url.searchParams, {}, 3000);
        if (remote && Array.isArray(remote.category_list)) {
          adoptRemoteRegistry(remote);
          return json(remote);
        }
        const f = facetsFor(src, platform);
        return json({
          platform: platform || "all",
          registry: registry(platform),
          categories: f.category_list,
          category_list: f.category_list,
          uncategorised: f.filter_counts.uncategorised,
        });
      }
      if (method === "POST") {
        if (platform && galleryReady()) {
          const remote = await galleryJson("/api/categories", url.searchParams, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...payload, platform, source: src }),
          }, 8000);
          if (remote && remote.ok) {
            adoptRemoteRegistry(remote);
            return json(remote);
          }
        }
        const op = String(payload.op || "");
        const name = String(payload.name || "").trim();
        let names = registry(platform).slice();
        const updatePosts = (fn) => {
          posts.forEach((p) => {
            const matchesPlatform = platform
              ? platformKey(p.platform) === platform
              : (!src || p.source === src || p.platform === src);
            if (!matchesPlatform) return;
            const before = cats(p);
            const next = fn(before);
            if (sameValue(before, next)) return;  // leave untouched posts alone
            p.categories = next;
            p.category = next[0] || null;
            overrides[p.id] = { ...(overrides[p.id] || {}), categories: next,
              category: next[0] || null, t: Date.now() };
          });
        };
        if (op === "add" && name && !names.includes(name)) names.push(name);
        if (op === "rename" && name) {
          const next = String(payload.new_name || "").trim();
          if (next && next !== name) {
            names = names.map((c) => c === name ? next : c);
            updatePosts((values) => values.map((c) => c === name ? next : c));
          }
        }
        if (op === "remove" && name) {
          names = names.filter((c) => c !== name);
          updatePosts((values) => values.filter((c) => c !== name));
        }
        if (op === "reorder") {
          const wanted = Array.isArray(payload.names) ? payload.names.map(String) : [];
          names = wanted.filter((c) => names.includes(c))
            .concat(names.filter((c) => !wanted.includes(c)));
        }
        saveRegistry(names, platform);
        localStorage.setItem(STORE, JSON.stringify(overrides));
        const f = facetsFor(src, platform);
        return json({
          ok: true,
          platform: platform || "all",
          registry: names,
          categories: f.category_list,
          category_list: f.category_list,
          uncategorised: f.filter_counts.uncategorised,
        });
      }
      return json({ error: "method not allowed" }, 405);
    }
    if (api === "cloud") {
      if (method === "POST") {
        let payload = {};
        try { payload = JSON.parse(options.body || "{}"); } catch (_) {}
        const op = String(payload.op || "push");
        if (op === "pull") {
          await loadCloud(true);
          posts.forEach(applyOverride);
        } else if (op === "push") {
          await pushCloud(true);
        }
      }
      const provider = cloudToken && d1Url ? "gist+d1"
        : cloudToken ? "gist" : d1Url ? "d1" : "local";
      const { pendingD1, pendingGist } = targetPending();
      return json({
        enabled: true, provider, online: !!(cloudToken || d1Url),
        pending: pendingD1 || pendingGist,
        revision: sync.rev, d1_revision: sync.d1Rev, gist_revision: sync.gistRev,
        last_push: sync.lastOk || "",
        last_error: sync.lastError || "",
        next_retry: Math.min(
          ...([sync.nextD1, sync.nextGist].filter((t) => t > 0)), 0) || 0,
        intervals: { d1: SYNC.min, gist: SYNC.gistMin },
      });
    }
    if (api === "live") {
      const items = sourceItems(currentSource(url.searchParams));
      return json({ kind: "static",
        stats: { total: items.length, videos: items.filter((p) => p.is_video).length,
          comments: items.reduce((n, p) => n + (p.comments_count || 0), 0),
          still_saved: 0, already_unsaved: items.length },
        progress: {}, job: {}, log: [], actions: [], dashboard: null, verify: {} });
    }
    if (api === "posts") {
      const params = url.searchParams;
      const remote = await galleryJson("/api/posts", params, {}, 4000);
      if (remote && Array.isArray(remote.items)) {
        // The gallery Worker returns direct Hugging Face URLs.  Once the
        // datasets are gated those 401, so rewrite every item through the
        // signed media proxy (same rewrite the static manifest gets).
        if (MEDIA_PROXY) {
          await loadMediaTokens().catch(() => {});
          remote.items.forEach(rewritePostMedia);
        }
        return json(remote);
      }
      const all = sorted(filtered(params), params.get("sort") || "manual");
      const offset = Number(params.get("offset") || 0);
      const limit = Math.min(Number(params.get("limit") || 60), 120);
      return json({ items: all.slice(offset, offset + limit), total: all.length, offset });
    }
    let m = api.match(/^post\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      if (!post) return json({ error: "not found" }, 404);
      const out = { ...post, caption: post.caption_full || post.caption || "", media: post.media || [] };
      if (method === "GET") {
        const remote = await galleryJson(`/api/post/${m[1]}`, url.searchParams, {}, 3000);
        if (remote && remote.id !== undefined) {
          if (MEDIA_PROXY) {
            await loadMediaTokens().catch(() => {});
            rewritePostMedia(remote);
          }
          return json(remote);
        }
      }
      if (method === "POST") {
        try {
          const payload = JSON.parse(options.body || "{}");
          const patch = {};
          if (payload.categories) {
            post.categories = payload.categories.filter(Boolean);
            post.category = post.categories[0] || null;
            patch.categories = post.categories;
          } else if (payload.category) {
            post.categories = [...new Set([...cats(post), payload.category])];
            post.category = post.categories[0] || null;
            patch.categories = post.categories;
          }
          if (payload.notes !== undefined) {
            post.notes = payload.notes;
            patch.notes = payload.notes;
          }
          if (payload.tags !== undefined) {
            post.tags = payload.tags;
            patch.tags = payload.tags;
          }
          saveOverride(post, patch);
          // Mirror the edit through the gallery Worker (batched endpoint);
          // the local + state push above remains the fallback.
          galleryEdit([{
            id: Number(post.id),
            patch,
            t: Date.now(),
          }], sync.d1Rev).catch(() => {});
        } catch (_) {}
        return json({ ok: true });
      }
      if (url.searchParams.get("comments") === "1" && post.comments_url) {
        const res = await nativeFetch(post.comments_url);
        out.comments = res.ok ? await res.json() : [];
      } else out.comments = [];
      if (!Array.isArray(out.comments)) out.comments = [];
      out.comments_data = {
        items: out.comments.slice(0, 30),
        total: out.comments.length,
        hasMore: out.comments.length > 30,
        offset: 0,
      };
      return json(out);
    }
    m = api.match(/^comments\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      if (!post || !post.comments_url) return json({ items: [], total: 0, hasMore: false });
      const res = await nativeFetch(post.comments_url);
      const all = res.ok ? await res.json() : [];
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Math.min(Number(url.searchParams.get("limit") || 30), 100);
      return json({ items: all.slice(offset, offset + limit), total: all.length,
        hasMore: offset + limit < all.length, offset });
    }
    if (api.startsWith("open_folder/")) return json({ ok: false });
    if (api === "bulk_category") {
      try {
        const payload = JSON.parse(options.body || "{}");
        const category = String(payload.category || "").trim();
        const platform = platformKey(payload.platform) || registryPlatform(url.searchParams);
        const clearing = !category || payload.clear;
        const workerOps = [];
        if (clearing) {
          (payload.ids || []).forEach((id) => {
            const post = posts.find((p) => p.id === Number(id));
            if (!post) return;
            post.categories = [];
            post.category = null;
            saveOverride(post, { categories: [] });
            workerOps.push({ id: Number(post.id), patch: { categories: [] }, t: Date.now() });
          });
          if (workerOps.length) galleryEdit(workerOps, sync.d1Rev).catch(() => {});
          return json({ ok: true });
        }
        if (category && platform) {
          const names = registry(platform);
          if (!names.includes(category)) {
            saveRegistry([...names, category], platform);
            galleryJson("/api/categories", url.searchParams, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ op: "add", platform, name: category }),
            }, 6000).catch(() => {});
          }
        }
        (payload.ids || []).forEach((id) => {
          const post = posts.find((p) => p.id === Number(id));
          if (!post || !category) return;
          if (cats(post).includes(category)) return;
          post.categories = [...new Set([...cats(post), category])];
          post.category = post.categories[0] || null;
          saveOverride(post, { categories: post.categories });
          workerOps.push({
            id: Number(post.id), patch: { categories: post.categories }, t: Date.now(),
          });
        });
        if (workerOps.length) galleryEdit(workerOps, sync.d1Rev).catch(() => {});
      } catch (_) {}
      return json({ ok: true });
    }
    if (api === "reorder") {
      try {
        const payload = JSON.parse(options.body || "{}");
        const id = Number(payload.id);
        const order = Array.isArray(overrides.__order) ? overrides.__order.slice() : [];
        if (!order.length) order.push(...sorted(posts.slice(), "manual").map((p) => p.id));
        const from = order.indexOf(id);
        if (from >= 0) order.splice(from, 1);
        let at = order.length;
        if (payload.before_id) {
          const idx = order.indexOf(Number(payload.before_id));
          if (idx >= 0) at = idx;
        } else if (payload.after_id) {
          const idx = order.indexOf(Number(payload.after_id));
          if (idx >= 0) at = idx + 1;
        }
        order.splice(at, 0, id);
        overrides.__order = order;
        overrides.__order_t = Date.now();
        localStorage.setItem(STORE, JSON.stringify(overrides));
        schedulePush();
      } catch (_) {}
      return json({ ok: true });
    }
    m = api.match(/^media\/(\d+)\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      const item = post && post.media[Number(m[2])];
      return item ? Response.redirect(new URL(item.url, document.baseURI).href, 302)
        : json({ error: "no media" }, 404);
    }
    m = api.match(/^thumb\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      return post && post.thumb
        ? Response.redirect(new URL(post.thumb, document.baseURI).href, 302)
        : json({ error: "no thumb" }, 404);
    }
    m = api.match(/^cover\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      return post && post.cover
        ? Response.redirect(new URL(post.cover, document.baseURI).href, 302)
        : json({ error: "no cover" }, 404);
    }
    return json({ error: "not found" }, 404);
  };
})();
