/* Static replacement for webapp.py's /api/* endpoints on GitHub Pages. */
(() => {
  window.__STATIC = true;
  const nativeFetch = window.fetch.bind(window);
  const ready = nativeFetch("manifest.json?nc=20261123", { cache: "no-store" })
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
  let cloudTimer = null;
  let overrides = {};
  try { overrides = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (_) {}
  let db = null;
  let posts = [];

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
        if (Array.isArray(assign[key])) patch.categories = assign[key];
        if (typeof notes[key] === "string") patch.notes = notes[key];
        if (Array.isArray(tags[key])) patch.tags = tags[key];
        if (meta[key]) {
          if (typeof meta[key].notes === "string") patch.notes = meta[key].notes;
          if (Array.isArray(meta[key].tags)) patch.tags = meta[key].tags;
        }
        if (Object.keys(patch).length) {
          out.overrides[post.id] = { ...(out.overrides[post.id] || {}), ...patch };
        }
      });
    });
    return out;
  }

  async function loadCloud() {
    let localDoc = null;
    try {
      const local = await nativeFetch(`static_overrides.json?nc=${Date.now()}`,
        { cache: "no-store" });
      if (local.ok) localDoc = await local.json();
    } catch (_) {}
    const browserLocal = { ...overrides };
    if (localDoc && localDoc.overrides) {
      overrides = { ...localDoc.overrides, ...overrides };
    }
    if (localDoc && Array.isArray(localDoc.categories)) {
      const merged = Array.isArray(overrides.__categories) ? overrides.__categories.slice() : [];
      localDoc.categories.forEach((c) => { if (!merged.includes(c)) merged.push(c); });
      overrides.__categories = merged;
      overrides.__category_order = merged;
    }
    if (!GIST_ID) {
      localStorage.setItem(STORE, JSON.stringify(overrides));
      return;
    }
    try {
      const headers = cloudToken ? { Authorization: `Bearer ${cloudToken}` } : {};
      const res = await nativeFetch(`https://api.github.com/gists/${GIST_ID}?t=${Date.now()}`, {
        headers, cache: "no-store" });
      if (!res.ok) return;
      const gist = await res.json();
      const files = gist.files || {};
      let remote = null;
      const direct = files[GIST_FILE] && files[GIST_FILE].content;
      if (direct) {
        try { remote = JSON.parse(direct); } catch (_) {}
      }
      if (!remote && files["cloud_state.json"] && files["cloud_state.json"].content) {
        try {
          remote = stateToOverrides(JSON.parse(files["cloud_state.json"].content));
        } catch (_) {}
      }
      if (!remote) return;
      if (remote.overrides) {
        overrides = { ...overrides, ...remote.overrides, ...browserLocal };
      }
      if (Array.isArray(remote.categories)) {
        const merged = Array.isArray(overrides.__categories) ? overrides.__categories.slice() : [];
        remote.categories.forEach((c) => { if (!merged.includes(c)) merged.push(c); });
        overrides.__categories = merged;
        overrides.__category_order = merged;
      }
      localStorage.setItem(STORE, JSON.stringify(overrides));
    } catch (_) {
      localStorage.setItem(STORE, JSON.stringify(overrides));
    }
  }

  function pushCloud() {
    const body = JSON.stringify({ overrides, updated_at: new Date().toISOString() });
    if (GIST_ID && cloudToken) {
      nativeFetch(`https://api.github.com/gists/${GIST_ID}`, {
        method: "PATCH",
        headers: {
          Authorization: `Bearer ${cloudToken}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ files: { [GIST_FILE]: { content: body } } }),
      }).catch(() => {});
      localStorage.setItem("ig_cloud_last_push", new Date().toISOString());
      return;
    }
    if (d1Url) {
      nativeFetch(d1Url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overrides }),
      }).catch(() => {});
      localStorage.setItem("ig_cloud_last_push", new Date().toISOString());
      return;
    }
    // No cloud credentials configured: localStorage remains authoritative.
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
    overrides[post.id] = { ...(overrides[post.id] || {}), ...patch };
    localStorage.setItem(STORE, JSON.stringify(overrides));
    if (cloudTimer) clearTimeout(cloudTimer);
    cloudTimer = setTimeout(() => { cloudTimer = null; pushCloud(); }, 1500);
  }

  const json = (data, status = 200) => new Response(
    JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

  const cats = (item) => item.categories || (item.category ? [item.category] : []);

  function postKey(post) {
    return post.folder || post.pk || String(post.id);
  }

  function sourceItems(source) {
    if (!source) return posts.slice();
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
    overrides.__category_order = names.filter(Boolean);
    overrides.__categories = names.filter(Boolean);
    localStorage.setItem(STORE, JSON.stringify(overrides));
    schedulePush();
  }

  function schedulePush() {
    if (cloudTimer) clearTimeout(cloudTimer);
    cloudTimer = setTimeout(() => { cloudTimer = null; pushCloud(); }, 1200);
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
    if (source) {
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
        cloudBtn.textContent = "Cloud sync";
        cloudBtn.title = "Set gist token / D1 fallback for online category and note persistence";
        Object.assign(cloudBtn.style, {
          position: "fixed", right: "18px", bottom: "98px", zIndex: "80",
          padding: "9px 13px", borderRadius: "10px", border: "1px solid #2b364b",
          background: "#121722", color: "#e9edf6", cursor: "pointer", fontSize: "12px",
        });
        cloudBtn.onclick = () => {
          const token = prompt("GitHub token with gist scope (stored only in this browser)", cloudToken);
          if (token === null) return;
          cloudToken = token.trim();
          localStorage.setItem("ig_cloud_token", cloudToken);
          const d1 = prompt("D1 fallback URL (Worker endpoint, optional)", d1Url);
          if (d1 !== null) {
            d1Url = d1.trim();
            localStorage.setItem("ig_cloud_d1", d1Url);
          }
          loadCloud().then(() => { posts.forEach(applyOverride); location.reload(); });
        };
        document.body.appendChild(cloudBtn);
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
              const next = fn(cats(p));
              p.categories = next;
              p.category = next[0] || null;
              overrides[p.id] = { ...(overrides[p.id] || {}), categories: next };
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
      const provider = cloudToken ? "gist" : (d1Url ? "d1" : "local");
      return json({ enabled: true, provider, online: !!(cloudToken || d1Url),
        last_push: localStorage.getItem("ig_cloud_last_push") || "" });
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
