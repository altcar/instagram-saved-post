/* Static replacement for webapp.py's /api/* endpoints on GitHub Pages. */
(() => {
  window.__STATIC = true;
  const nativeFetch = window.fetch.bind(window);
  const ready = nativeFetch("manifest.json?nc=202611232012", { cache: "no-store" })
    .then((r) => r.json())
    .then(async (data) => {
      posts = data.posts || [];
      window.__STATIC_POSTS = posts;
      await loadCloud();
      posts.forEach(applyOverride);
      return data;
    });
  const STORE = "ig_static_overrides_v1";
  const CLOUD = window.SAVED_CLOUD || {};
  const GIST_ID = CLOUD.gistId || "5dc24371e7371776648695269b30cfd2";
  const GIST_FILE = CLOUD.gistFile || "static_overrides.json";
  let cloudToken = localStorage.getItem("ig_cloud_token") || CLOUD.token || "";
  let d1Url = localStorage.getItem("ig_cloud_d1") || CLOUD.d1Url || "";
  let d1Key = localStorage.getItem("ig_cloud_d1_key") || "";
  let overrides = {};
  try { overrides = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (_) {}

  // ---- write-behind sync -------------------------------------------------
  // Local storage is always written first, so the UI never waits on a network
  // round trip.  Cloud writes are coalesced and rate limited: D1 at most once
  // per 30s, the gist at most once per 5 minutes (one commit per write), plus
  // one urgent flush when the tab is hidden.  A change made while typing is
  // therefore at most one write, no matter how many keystrokes it took.
  const SYNC = {
    idle: 5000,        // save 5s after the last edit
    min: 30000,        // never more than one D1 write per 30s
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
    paintSyncButton();
  }

  function targetPending() {
    const pendingD1 = !!d1Url && sync.rev > sync.d1Rev;
    const pendingGist = !!cloudToken && !!GIST_ID && sync.rev > sync.gistRev;
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

  function syncBackoff(until, provider) {
    const base = until || (Date.now() + sync.backoff);
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
    paintSyncButton();
    refreshSyncPanel();
  }

  function syncError(err) {
    sync.lastError = String(err && err.message || err || "sync failed");
    paintSyncButton();
    refreshSyncPanel();
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
    const at = Number(a.__cat_t || 0);
    const bt = Number(b.__cat_t || 0);
    let order;
    if (at > bt) order = a.__category_order || a.__categories || [];
    else if (bt > at) order = b.__category_order || b.__categories || [];
    else {
      order = [...(a.__category_order || a.__categories || [])];
      (b.__category_order || b.__categories || []).forEach((c) => {
        if (!order.includes(c)) order.push(c);
      });
    }
    const out = { ...a, ...b, overrides: merged };
    out.__categories = order.filter(Boolean);
    out.__category_order = out.__categories;
    out.__cat_t = Math.max(at, bt);
    out.__updated_at = new Date().toISOString();
    return out;
  }

  function stateToOverrides(state) {
    const out = { overrides: {}, categories: [] };
    const byKey = {};
    posts.forEach((p) => {
      byKey[p.folder || p.pk || String(p.id)] = p;
    });
    const sources = (state && state.sources) || {};
    Object.values(sources).forEach((src) => {
      (src.categories || []).forEach((c) => {
        if (!out.categories.includes(c)) out.categories.push(c);
      });
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
          out.overrides[post.id] = { ...(out.overrides[post.id] || {}), ...patch };
        }
      });
    });
    return out;
  }

  async function loadCloud(force) {
    let doc = { ...overrides };
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

    // D1 is the fast primary. One read per page load is cheap and keeps
    // different devices in sync even when the gist is rate limited.
    if (d1Url) {
      try {
        const headers = d1Key ? { "X-Archive-Key": d1Key } : {};
        const res = await nativeFetch(`${d1Url}${d1Url.includes("?") ? "&" : "?"}id=default`,
          { headers, cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          if (data && data.doc) doc = mergeDocs(doc, data.doc);
        }
      } catch (_) {}
    }

    if (!GIST_ID) {
      overrides = doc.overrides || {};
      overrides.__categories = doc.__categories || [];
      overrides.__category_order = overrides.__categories;
      localStorage.setItem(STORE, JSON.stringify(overrides));
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
    overrides = doc.overrides || {};
    overrides.__categories = doc.__categories || [];
    overrides.__category_order = overrides.__category_order || overrides.__categories;
    overrides.__cat_t = doc.__cat_t || overrides.__cat_t || 0;
    localStorage.setItem(STORE, JSON.stringify(overrides));
  }

  function syncDoc() {
    return {
      overrides,
      __categories: overrides.__categories || overrides.__category_order || [],
      __category_order: overrides.__category_order || overrides.__categories || [],
      __cat_t: overrides.__cat_t || 0,
      updated_at: new Date().toISOString(),
    };
  }

  async function pushD1(revAtSend, keepalive) {
    const res = await nativeFetch(d1Url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(d1Key ? { "X-Archive-Key": d1Key } : {}),
      },
      body: JSON.stringify({ doc: syncDoc(), rev: revAtSend }),
      keepalive: !!keepalive,
    });
    if (!res.ok) {
      const retryAt = parseRetry(res);
      if (res.status === 401 || res.status === 403) {
        syncBackoff(retryAt || Date.now() + 15 * 60 * 1000, "d1");
        throw new Error(res.status === 401 ? "D1 key rejected" : "D1 write forbidden");
      }
      if (res.status === 429 || res.status >= 500) {
        syncBackoff(retryAt || 0, "d1");
        throw new Error(`D1 temporarily unavailable (${res.status})`);
      }
      throw new Error(`D1 write failed (${res.status})`);
    }
    const data = await res.json().catch(() => null);
    if (data && data.doc && data.doc.overrides) {
      // The Worker merges by per-post timestamp, so adopt anything newer than
      // what this tab already has without clobbering edits made during the POST.
      const merged = mergeDocs({ overrides }, data.doc);
      overrides = { ...merged.overrides,
        __categories: merged.__categories,
        __category_order: merged.__category_order,
        __cat_t: merged.__cat_t };
      localStorage.setItem(STORE, JSON.stringify(overrides));
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
    if (!pendingD1 && !pendingGist) { sync.firstDirty = 0; paintSyncButton(); return; }
    const d1Due = pendingD1 && now >= sync.nextD1;
    const gistDue = pendingGist && now >= sync.nextGist &&
      now - sync.lastGist >= (force ? SYNC.gistHideMin : SYNC.gistMin);
    if (!d1Due && !gistDue) { scheduleFlush(); return; }
    sync.inFlight = true;
    paintSyncButton();
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
          catch (err) { firstErr = err; }
        }
      }
      if (gistDue) {
        if (sig === sync.gistSig) {
          sync.gistRev = Math.max(sync.gistRev, revAtSend);
          sync.lastGist = Date.now();
        } else {
          try { await pushGist(revAtSend, keepalive); sync.gistSig = sig; }
          catch (err) { firstErr = firstErr || err; }
        }
      }
      if (firstErr) syncError(firstErr);
      else syncOk();
    } finally {
      sync.inFlight = false;
      if (sync.rev > Math.max(sync.d1Rev, sync.gistRev)) scheduleFlush();
      else sync.firstDirty = 0;
      paintSyncButton();
      refreshSyncPanel();
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

  function sourceItems(source) {
    if (!source || source === "all" || source === "__all__") return posts.slice();
    return posts.filter((p) => p.source === source || p.platform === source);
  }

  function registry() {
    if (Array.isArray(overrides.__category_order)) {
      return overrides.__category_order.filter((c) => typeof c === "string");
    }
    if (Array.isArray(overrides.__categories)) {
      return overrides.__categories.filter((c) => typeof c === "string");
    }
    const names = [];
    posts.forEach((p) => cats(p).forEach((c) => { if (!names.includes(c)) names.push(c); }));
    return names;
  }

  function saveRegistry(names) {
    const next = names.filter(Boolean);
    const prev = overrides.__category_order || overrides.__categories || [];
    if (next.length === prev.length && next.every((c, i) => c === prev[i])) return;
    overrides.__category_order = next;
    overrides.__categories = next;
    overrides.__cat_t = Date.now();
    localStorage.setItem(STORE, JSON.stringify(overrides));
    markDirty();
  }

  function schedulePush() {
    markDirty();
  }

  function categoryCounts(items) {
    const counts = {};
    registry().forEach((c) => { counts[c] = 0; });
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

  function facetsFor(source) {
    const items = sourceItems(source);
    const counts = categoryCounts(items);
    const names = registry();
    const catList = names.map((name) => ({ name, count: counts[name] || 0 }));
    const tagCounts = {};
    items.forEach((p) => (p.tags || []).forEach((t) => {
      tagCounts[t] = (tagCounts[t] || 0) + 1;
    }));
    return {
      total: items.length,
      videos: items.filter((p) => p.is_video).length,
      ads: 0,
      filter_counts: filterCounts(items),
      categories: catList,
      category_list: catList,
      tags: Object.entries(tagCounts)
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([name, count]) => ({ name, count })),
      presets: names.length ? names : (db.facets.presets || []),
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
    if (source && source !== "all" && source !== "__all__") {
      items = items.filter((p) => p.source === source || p.platform === source);
    }
    const q = (params.get("q") || "").toLowerCase();
    const category = params.get("category") || "";
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
    return items.sort((a, b) => a.id - b.id);
  }

  function fmtWait(ms) {
    const s = Math.max(1, Math.round(ms / 1000));
    return s < 60 ? `${s}s` : `${Math.round(s / 60)}m`;
  }

  function syncLabel() {
    const { pendingD1, pendingGist } = targetPending();
    const pending = pendingD1 || pendingGist;
    if (sync.inFlight) return "☁ Saving…";
    if (!cloudToken && !d1Url) return "☁ Local only";
    const now = Date.now();
    const d1Locked = pendingD1 && sync.nextD1 > now;
    const gistLocked = pendingGist && sync.nextGist > now;
    const unlocked = (pendingD1 && !d1Locked) || (pendingGist && !gistLocked);
    const locks = [d1Locked ? sync.nextD1 : 0, gistLocked ? sync.nextGist : 0]
      .filter((t) => t > 0);
    if (locks.length && !unlocked) {
      return `☁ Retry ${fmtWait(Math.min(...locks) - now)}`;
    }
    if (pending) return "☁ Pending…";
    return "☁ Saved";
  }

  function paintSyncButton() {
    const el = document.getElementById("cloudSync");
    if (!el) return;
    el.textContent = syncLabel();
    const { pendingD1, pendingGist } = targetPending();
    el.title = !cloudToken && !d1Url
      ? "Edits are saved in this browser. Click to configure gist/D1 cloud sync."
      : `${pendingD1 || pendingGist ? "Waiting to save" : "All edits saved"}`
        + ` · D1 at most 1 write/30s · gist at most 1 commit/5min`
        + (sync.lastError ? ` · ${sync.lastError}` : "");
    el.classList.toggle("cloudErr", !!sync.lastError);
  }

  function refreshSyncPanel() {
    const box = document.getElementById("cloudPanelState");
    if (!box) return;
    const { pendingD1, pendingGist } = targetPending();
    const provider = cloudToken && d1Url ? "gist + D1"
      : cloudToken ? "gist" : d1Url ? "D1" : "this browser only";
    const rows = [
      `Storage: <b>${provider}</b>`,
      `Status: <b>${syncLabel().replace("☁ ", "")}</b>`,
      pendingD1 || pendingGist ? "Unsaved: <b>yes</b> — will retry automatically" : "Unsaved: <b>no</b>",
      sync.lastError ? `Last error: <b style="color:#f08a8a">${sync.lastError}</b>` : "",
      `Gist: one commit per ≤5 min · D1: one write per ≤30 s`,
    ].filter(Boolean);
    box.innerHTML = rows.map((r) => `<div style="margin:3px 0">${r}</div>`).join("");
    paintSyncButton();
  }

  function configureSync() {
    const token = prompt("GitHub token with gist scope (blank = local only for gist)",
      cloudToken ? "••••••••" : "");
    if (token === null) return;
    if (!token.startsWith("••••")) {
      cloudToken = token.trim();
      localStorage.setItem("ig_cloud_token", cloudToken);
    }
    const d1 = prompt("D1 Worker endpoint URL (optional)", d1Url);
    if (d1 === null) return;
    d1Url = d1.trim();
    localStorage.setItem("ig_cloud_d1", d1Url);
    const key = prompt("D1 archive key (optional, stored in this browser)",
      d1Key ? "••••••••" : "");
    if (key !== null && !key.startsWith("••••")) {
      d1Key = key.trim();
      localStorage.setItem("ig_cloud_d1_key", d1Key);
    }
    sync.nextD1 = 0;
    sync.nextGist = 0;
    sync.backoff = SYNC.retryMin;
    loadCloud().then(() => {
      posts.forEach(applyOverride);
      refreshSyncPanel();
      pushCloud(false);
    });
  }

  function openSyncPanel() {
    const existing = document.getElementById("cloudPanel");
    if (existing) { existing.remove(); return; }
    const panel = document.createElement("div");
    panel.id = "cloudPanel";
    Object.assign(panel.style, {
      position: "fixed", right: "18px", bottom: "140px", zIndex: "81",
      width: "300px", padding: "13px", borderRadius: "12px",
      border: "1px solid #2b364b", background: "#121722", color: "#e9edf6",
      boxShadow: "0 12px 40px rgba(0,0,0,.45)", fontSize: "12px", lineHeight: "1.45",
    });
    panel.innerHTML = `
      <div style="font-weight:600;margin-bottom:7px">Cloud sync</div>
      <div id="cloudPanelState"></div>
      <div style="display:flex;gap:6px;margin-top:10px">
        <button id="cloudNow" style="flex:1;padding:6px 8px;border-radius:8px;border:1px solid #2b364b;background:#1b2333;color:#e9edf6;cursor:pointer">Sync now</button>
        <button id="cloudPull" style="flex:1;padding:6px 8px;border-radius:8px;border:1px solid #2b364b;background:#1b2333;color:#e9edf6;cursor:pointer">Pull</button>
        <button id="cloudConfig" style="flex:1;padding:6px 8px;border-radius:8px;border:1px solid #2b364b;background:#1b2333;color:#e9edf6;cursor:pointer">Configure…</button>
        <button id="cloudClose" style="padding:6px 8px;border-radius:8px;border:1px solid #2b364b;background:#1b2333;color:#e9edf6;cursor:pointer">✕</button>
      </div>
      <div style="margin-top:9px;color:#9aa7bd">Edits save instantly in this browser. D1 gets at most one write every 30s; the gist gets at most one commit every 5 minutes. Manual sync forces both.</div>`;
    document.body.appendChild(panel);
    refreshSyncPanel();
    document.getElementById("cloudNow").onclick = async () => {
      await pushCloud(true);
      refreshSyncPanel();
    };
    document.getElementById("cloudPull").onclick = async () => {
      await nativeFetch("/api/cloud", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "pull" }),
      }).catch(() => {});
      location.reload();
    };
    document.getElementById("cloudConfig").onclick = configureSync;
    document.getElementById("cloudClose").onclick = () => panel.remove();
  }

  function urgentFlush() {
    const { pendingD1, pendingGist } = targetPending();
    if (!pendingD1 && !pendingGist) return;
    const gistFresh = Date.now() - sync.lastGist < SYNC.gistHideMin;
    pushCloud(!gistFresh, true);
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
      if (!document.getElementById("exportEdits")) {
        const btn = document.createElement("button");
        btn.id = "exportEdits";
        btn.textContent = "Export edits";
        btn.title = "Download a small JSON file with your category/note/tag edits";
        Object.assign(btn.style, {
          position: "fixed", right: "18px", bottom: "18px", zIndex: "80",
          padding: "9px 13px", borderRadius: "10px", border: "1px solid #2b364b",
          background: "#121722", color: "#e9edf6", cursor: "pointer", fontSize: "12px",
        });
        btn.onclick = () => {
          const blob = new Blob([JSON.stringify(overrides, null, 2)], { type: "application/json" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = "instagram-archive-edits.json";
          a.click();
          URL.revokeObjectURL(a.href);
        };
        document.body.appendChild(btn);
        const importBtn = document.createElement("button");
        importBtn.id = "importEdits";
        importBtn.textContent = "Import edits";
        importBtn.title = "Load a previously exported edits JSON file";
        Object.assign(importBtn.style, {
          position: "fixed", right: "18px", bottom: "58px", zIndex: "80",
          padding: "9px 13px", borderRadius: "10px", border: "1px solid #2b364b",
          background: "#121722", color: "#e9edf6", cursor: "pointer", fontSize: "12px",
        });
        importBtn.onclick = () => {
          const input = document.createElement("input");
          input.type = "file";
          input.accept = "application/json,.json";
          input.onchange = async () => {
            const file = input.files && input.files[0];
            if (!file) return;
            try {
              const incoming = JSON.parse(await file.text());
              if (!incoming || typeof incoming !== "object") return;
              overrides = { ...overrides, ...incoming };
              localStorage.setItem(STORE, JSON.stringify(overrides));
              posts.forEach(applyOverride);
              location.reload();
            } catch (err) {
              alert("Could not import edits: " + err.message);
            }
          };
          input.click();
        };
        document.body.appendChild(importBtn);
        const cloudBtn = document.createElement("button");
        cloudBtn.id = "cloudSync";
        cloudBtn.title = "Cloud sync status and settings";
        Object.assign(cloudBtn.style, {
          position: "fixed", right: "18px", bottom: "98px", zIndex: "80",
          padding: "9px 13px", borderRadius: "10px", border: "1px solid #2b364b",
          background: "#121722", color: "#e9edf6", cursor: "pointer", fontSize: "12px",
        });
        cloudBtn.onclick = () => openSyncPanel();
        document.body.appendChild(cloudBtn);
        paintSyncButton();
        window.addEventListener("pagehide", urgentFlush);
        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState === "hidden") urgentFlush();
        });
        setInterval(() => {
          if (sync.rev > Math.max(sync.d1Rev, sync.gistRev)) pushCloud(false);
        }, 60000);
      }
    }
    const api = url.pathname.slice(marker + 5);
    const method = (options.method || "GET").toUpperCase();

    if (api === "accounts") {
      if (window.SAVED_ACCOUNTS) return json(window.SAVED_ACCOUNTS);
      return json({ accounts: [{
        id: "instagram-static", platform: "instagram", label: "Instagram",
        handle: "", total: posts.length, videos: posts.filter((p) => p.is_video).length,
      }], active: "instagram-static" });
    }
    if (api === "facets") {
      return json(facetsFor(currentSource(url.searchParams)));
    }
    if (api === "categories") {
      const src = currentSource(url.searchParams);
      if (method === "POST") {
        let payload = {};
        try { payload = JSON.parse(options.body || "{}"); } catch (_) {}
        const op = String(payload.op || "");
        const name = String(payload.name || "").trim();
        let names = registry().slice();
        const updatePosts = (fn) => {
          posts.forEach((p) => {
            if (!src || p.source === src || p.platform === src) {
              const before = cats(p);
              const next = fn(before);
              if (sameValue(before, next)) return;  // leave untouched posts alone
              p.categories = next;
              p.category = next[0] || null;
              overrides[p.id] = { ...(overrides[p.id] || {}), categories: next,
                category: next[0] || null, t: Date.now() };
            }
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
        saveRegistry(names);
        localStorage.setItem(STORE, JSON.stringify(overrides));
        return json({ categories: facetsFor(src).category_list,
          uncategorised: facetsFor(src).filter_counts.uncategorised });
      }
      const f = facetsFor(currentSource(url.searchParams));
      return json({ categories: f.category_list, uncategorised: f.filter_counts.uncategorised });
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
        if (category) {
          const names = registry();
          if (!names.includes(category)) saveRegistry([...names, category]);
        }
        (payload.ids || []).forEach((id) => {
          const post = posts.find((p) => p.id === Number(id));
          if (!post || !category) return;
          if (cats(post).includes(category)) return;
          post.categories = [...new Set([...cats(post), category])];
          post.category = post.categories[0] || null;
          saveOverride(post, { categories: post.categories });
        });
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
