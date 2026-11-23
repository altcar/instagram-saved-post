/* D1-backed personal-state store for the Saved Archive static gallery.
 *
 * One row per document in the ig_archive_docs table.  The static site writes
 * this document at most once every 30 seconds; the Worker merges per-post
 * timestamps so two tabs/devices never overwrite each other's edits.
 *
 * Bindings: DB (D1), ARCHIVE_KEY (secret).
 */
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Archive-Key",
  "Access-Control-Max-Age": "86400",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

function mergeDocs(a, b) {
  const left = a || {};
  const right = b || {};
  const ao = left.overrides || {};
  const bo = right.overrides || {};
  const ov = {};
  const keys = new Set([...Object.keys(ao), ...Object.keys(bo)]);
  for (const key of keys) {
    if (key.startsWith("__")) continue;
    const x = ao[key];
    const y = bo[key];
    if (!x) { ov[key] = y; continue; }
    if (!y) { ov[key] = x; continue; }
    ov[key] = Number(y.t || 0) > Number(x.t || 0)
      ? { ...x, ...y } : { ...y, ...x };
  }
  const at = Number(left.__cat_t || 0);
  const bt = Number(right.__cat_t || 0);
  let order;
  if (at > bt) order = left.__category_order || left.__categories || [];
  else if (bt > at) order = right.__category_order || right.__categories || [];
  else {
    order = [...(left.__category_order || left.__categories || [])];
    for (const c of right.__category_order || right.__categories || []) {
      if (!order.includes(c)) order.push(c);
    }
  }
  const doc = { ...left, ...right, overrides: ov };
  doc.__categories = order.filter(Boolean);
  doc.__category_order = doc.__categories;
  doc.__cat_t = Math.max(at, bt);
  doc.updated_at = new Date().toISOString();
  return doc;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      const headers = { ...CORS };
      if (request.headers.get("Access-Control-Request-Private-Network") === "true") {
        headers["Access-Control-Allow-Private-Network"] = "true";
      }
      return new Response(null, { status: 204, headers });
    }
    if (url.pathname === "/health") return json({ ok: true });
    const key = request.headers.get("X-Archive-Key") || url.searchParams.get("key") || "";
    if (!env.ARCHIVE_KEY || key !== env.ARCHIVE_KEY) {
      return json({ error: "unauthorized" }, 401);
    }
    const id = (url.searchParams.get("id") || "default").slice(0, 64);
    try {
      if (request.method === "GET") {
        const row = await env.DB.prepare(
          "SELECT body, rev, updated_at FROM ig_archive_docs WHERE id = ?"
        ).bind(id).first();
        if (!row) return json({ doc: null, rev: 0 });
        return json({ doc: JSON.parse(row.body), rev: row.rev, updated_at: row.updated_at });
      }
      if (request.method === "POST") {
        const payload = await request.json();
        const incoming = payload.doc || payload;
        const row = await env.DB.prepare(
          "SELECT body, rev FROM ig_archive_docs WHERE id = ?"
        ).bind(id).first();
        const current = row ? JSON.parse(row.body) : { overrides: {} };
        const doc = mergeDocs(current, incoming);
        const rev = (row ? Number(row.rev) : 0) + 1;
        const now = Date.now();
        await env.DB.prepare(
          "INSERT INTO ig_archive_docs (id, body, rev, updated_at) VALUES (?, ?, ?, ?) " +
          "ON CONFLICT(id) DO UPDATE SET body = excluded.body, rev = excluded.rev, " +
          "updated_at = excluded.updated_at"
        ).bind(id, JSON.stringify(doc), rev, now).run();
        return json({ doc, rev, updated_at: now });
      }
      return json({ error: "method not allowed" }, 405);
    } catch (err) {
      return json({ error: String(err) }, 500);
    }
  },
};
