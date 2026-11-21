/* Static replacement for webapp.py's /api/* endpoints on GitHub Pages. */
(() => {
  window.__STATIC = true;
  const nativeFetch = window.fetch.bind(window);
  const ready = nativeFetch("manifest.json").then((r) => r.json());
  let db = null;
  let posts = [];

  const json = (data, status = 200) => new Response(
    JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

  const cats = (item) => item.categories || (item.category ? [item.category] : []);

  function filtered(params) {
    let items = posts.slice();
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
    }
    const api = url.pathname.slice(marker + 5);
    const method = (options.method || "GET").toUpperCase();

    if (api === "facets") return json(db.facets);
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
          if (payload.categories) {
            post.categories = payload.categories.filter(Boolean);
            post.category = post.categories[0] || null;
          } else if (payload.category) {
            post.categories = [...new Set([...cats(post), payload.category])];
            post.category = post.categories[0] || null;
          }
          if (payload.notes !== undefined) post.notes = payload.notes;
          if (payload.tags !== undefined) post.tags = payload.tags;
        } catch (_) {}
        return json({ ok: true });
      }
      if (url.searchParams.get("comments") === "1" && post.comments_url) {
        const res = await nativeFetch(post.comments_url);
        out.comments = res.ok ? await res.json() : [];
      } else out.comments = [];
      return json(out);
    }
    if (api.startsWith("open_folder/")) return json({ ok: false });
    if (api === "bulk_category" || api === "reorder") return json({ ok: true });
    m = api.match(/^media\/(\d+)\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      const item = post && post.media[Number(m[2])];
      return item ? Response.redirect(item.url, 302) : json({ error: "no media" }, 404);
    }
    m = api.match(/^thumb\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      return post ? Response.redirect(post.thumb, 302) : json({ error: "no thumb" }, 404);
    }
    m = api.match(/^cover\/(\d+)$/);
    if (m) {
      const post = posts.find((p) => p.id === Number(m[1]));
      return post && post.cover ? Response.redirect(post.cover, 302) : json({ error: "no cover" }, 404);
    }
    return json({ error: "not found" }, 404);
  };
})();
