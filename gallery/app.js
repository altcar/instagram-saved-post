(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const isRednotePath = location.pathname.replace(/\/+$/, "") === "/rednote";
  const staticMode = !!window.__STATIC;
  const embedded = params.get("embed") === "1";

  const state = {
    source: params.get("source") || (isRednotePath ? "rednote-main" : "instagram-main"),
    q: "", category: "", tagStates: new Map(), owner: "", sort: "manual", view: "grid",
    filters: new Set(), onlyCats: new Set(),
    offset: 0, limit: 60, total: 0, items: [], loading: false, done: false,
    selected: new Set(),
    accounts: [], account: null, services: [],
    hubBase: (window.SAVED_BASE_URL || "").replace(/\/+$/, ""),
  };
  // Personal-tag chips cycle: off -> include -> exclude -> off.
  const tagMode = (name) => state.tagStates.get(name) || "off";
  const activeTags = (mode) =>
    [...state.tagStates].filter(([, m]) => m === mode).map(([name]) => name);
  let dragIds = null;
  let saveTimer = null;
  let searchTimer = null;
  let rectStart = null;
  let rectMoved = false;
  let suppressClick = false;
  let liveTimer = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const fmtNum = (n) => new Intl.NumberFormat(undefined, { notation: "compact" }).format(n || 0);
  const fmtInt = (n) => Number(n || 0).toLocaleString();
  const fmtDate = (s) => {
    if (!s) return "";
    const d = new Date(s);
    return isNaN(d) ? String(s) : d.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric" });
  };
  function linkify(text) {
    return esc(text || "").replace(/https?:\/\/[^\s<]+/g, (raw) => {
      let url = raw;
      let trailing = "";
      const match = raw.match(/([),.;!?]+)$/);
      if (match) { trailing = match[1]; url = raw.slice(0, -trailing.length); }
      const isPost = /threads\.(com|net)\/(?:[^\s]+\/post\/|share\/)|instagram\.com\/(p|reel)\//i.test(url);
      const label = isPost ? "linked post ↗" : url;
      return `<a class="cLink ${isPost ? "postLink" : ""}" href="${url}"
        target="_blank" rel="noopener" title="${url}">${label}</a>${trailing}`;
    });
  }
  const platformName = (p) => ({
    instagram: "Instagram", rednote: "RedNote", xiaohongshu: "RedNote",
    threads: "Threads", twitter: "X / Twitter", x: "X / Twitter",
  }[String(p || "").toLowerCase()] || String(p || "Source"));

  function apiUrl(url) {
    if (!url.startsWith("/api/") || staticMode) return url;
    if (url.includes("source=")) return url;
    return url + (url.includes("?") ? "&" : "?") + "source=" + encodeURIComponent(state.source);
  }

  async function api(url, opts) {
    const res = await fetch(apiUrl(url), opts);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  function sourceHref(id) {
    const account = (state.accounts || []).find((a) => a.id === id);
    const platform = String((account || {}).platform || "instagram").toLowerCase();
    if (state.hubBase && !embedded) {
      const route = platform === "rednote" ? "rednote"
        : platform === "threads" ? "threads"
        : (platform === "twitter" || platform === "x") ? "twitter" : "instagram";
      return `${state.hubBase}/${route}?source=${encodeURIComponent(id)}`;
    }
    if (staticMode) {
      return (location.pathname.includes("/gallery") ? "?" : "gallery/?")
        + "source=" + encodeURIComponent(id);
    }
    const prefix = location.pathname.startsWith("/gallery") ? "/gallery/" : "/";
    return `${prefix}?source=${encodeURIComponent(id)}`;
  }

  /* ---------------- account registry ---------------- */
  function accountIcon(account) {
    const platform = String(account.platform || "").toLowerCase();
    if (platform === "instagram") {
      return `<svg viewBox="0 0 24 24"><rect x="3.2" y="3.2" width="17.6" height="17.6" rx="5"/><circle cx="12" cy="12" r="4.1"/><circle cx="17.2" cy="6.8" r="1.1" fill="currentColor" stroke="none"/></svg>`;
    }
    if (platform === "rednote" || platform === "xiaohongshu") {
      return `<svg viewBox="0 0 24 24"><path d="M6 3.8h12v16.4l-6-3.6-6 3.6z"/><path d="M9.5 8.5h5M9.5 12h5"/></svg>`;
    }
    if (platform === "telegram") {
      return `<svg viewBox="0 0 24 24"><path d="M21 4 3 11l5 2 2 6 3-4 5 3z"/><path d="m8 13 8-6"/></svg>`;
    }
    if (platform === "twitter" || platform === "x") {
      return `<svg viewBox="0 0 24 24"><path d="M4 4l16 16M20 4L4 20"/></svg>`;
    }
    return `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M4 12h16M12 4c3 3 3 13 0 16M12 4c-3 3-3 13 0 16"/></svg>`;
  }

  function renderSocialBar() {
    const nav = $("socialBar");
    if (!nav) return;
    if (!state.accounts.length) {
      nav.innerHTML = `<a class="acct on" href="gallery/" title="Saved Archive"><span class="ico">${accountIcon({ platform: "instagram" })}</span></a>`;
      return;
    }
    const links = state.accounts.map((a) => `
      <a class="acct ${a.id === state.source ? "on" : ""}" href="${sourceHref(a.id)}"
         title="${esc(a.label || platformName(a.platform))}${a.handle ? " · @" + esc(a.handle) : ""}${a.total != null ? " · " + fmtInt(a.total) + " posts" : ""}">
        <span class="ico">${accountIcon(a)}</span>
      </a>`).join("");
    const serviceLinks = (state.services || []).map((s) => `
      <a class="acct service" href="${esc(s.href || "#")}" title="${esc(s.label || platformName(s.platform))}"
         ${s.external ? 'target="_blank" rel="noopener"' : ""}>
        <span class="ico">${accountIcon(s)}</span>
      </a>`).join("");
    if (staticMode) {
      nav.innerHTML = links + serviceLinks;
      return;
    }
    const serviceRows = (state.services || []).map((s) => `
      <a class="acctRow" href="${esc(s.href || "#")}"
         ${s.external ? 'target="_blank" rel="noopener"' : ""}>
        <span class="acctIco">${esc((s.label || platformName(s.platform)).slice(0, 2).toUpperCase())}</span>
        <span class="acctTxt"><b>${esc(s.label || platformName(s.platform))}</b>
          <span>${esc(platformName(s.platform))} · separate service</span></span>
        <span class="acctN">↗</span>
      </a>`).join("");
    const rows = state.accounts.map((a) => {
      const managed = a.managed === "pipeline" || a.managed === "builtin";
      return `
      <a class="acctRow ${a.id === state.source ? "on" : ""}" href="${sourceHref(a.id)}">
        <span class="acctIco">${esc((a.label || platformName(a.platform)).slice(0, 2).toUpperCase())}</span>
        <span class="acctTxt"><b>${esc(a.label || platformName(a.platform))}</b>
          <span>${esc(platformName(a.platform))}${a.handle ? " · @" + esc(a.handle) : ""}${a.enabled === false ? " · sync off" : ""}${a.error ? " · " + esc(a.error) : ""}</span></span>
        <span class="acctN">${a.total == null ? "" : fmtNum(a.total)}</span>
        ${managed ? '<span class="acctN" title="Managed by accounts.py / built in">•</span>'
          : `<button class="link acctDel" data-id="${esc(a.id)}" title="Remove this account">✕</button>`}
      </a>`;
    }).join("");
    nav.innerHTML = links + serviceLinks + `
      <details class="acctMenu" id="acctMenu">
        <summary title="Switch account or platform">⋯</summary>
        <div class="acctPanel">
          <h3>Accounts &amp; platforms</h3>
          ${rows}
          ${serviceRows ? `<h3>Other services</h3>${serviceRows}` : ""}
          <form class="acctForm" id="acctForm">
            <h3 style="margin-top:6px">Add account</h3>
            <div class="row">
              <input name="label" placeholder="Label (e.g. Work Instagram)" required>
              <select name="platform"><option value="instagram">Instagram</option><option value="rednote">RedNote</option></select>
            </div>
            <input name="handle" placeholder="Handle / nickname (optional)">
            <input name="path" placeholder="Archive path (optional)">
            <button class="btn small" type="submit">Add account</button>
          </form>
        </div>
      </details>`;
    const form = $("acctForm");
    if (form) {
      form.onsubmit = async (e) => {
        e.preventDefault();
        const data = new FormData(form);
        const platform = String(data.get("platform") || "instagram");
        const payload = {
          op: "add",
          platform,
          label: String(data.get("label") || "").trim(),
          handle: String(data.get("handle") || "").trim(),
        };
        const path = String(data.get("path") || "").trim();
        if (path) {
          if (platform === "rednote") payload.root = path;
          else { payload.db = path.replace(/\/+$/, "") + "/archive.db"; payload.media = path.replace(/\/+$/, "") + "/posts"; payload.cache = path.replace(/\/+$/, "") + "/.cache"; }
        }
        try {
          const result = await api("/api/accounts", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const added = (result.accounts || []).find((a) => a.label === payload.label);
          location.href = sourceHref(added ? added.id : state.source);
        } catch (err) {
          alert("Could not add account: " + err.message);
        }
      };
    }
    nav.querySelectorAll(".acctDel").forEach((btn) => {
      btn.onclick = async (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (state.accounts.length < 2) { alert("Keep at least one account."); return; }
        if (!confirm("Remove this account from the switcher? Archive files are not touched.")) return;
        await api("/api/accounts", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ op: "remove", id: btn.dataset.id }),
        });
        if (btn.dataset.id === state.source) location.href = "/";
        else location.reload();
      };
    });
  }

  async function loadAccounts() {
    if (staticMode) {
      const doc = window.SAVED_ACCOUNTS || {};
      state.accounts = Array.isArray(doc.accounts) ? doc.accounts : [];
      state.services = [];
      state.hubBase = "";
      if (state.source !== "all" && !state.accounts.some((a) => a.id === state.source)) {
        const aliases = { twitter: "x", instagram: "shalevfrfr" };
        const alias = aliases[state.source];
        const match = alias && state.accounts.find((a) => a.id === alias);
        const first = state.accounts[0];
        state.source = match ? match.id : (first ? first.id : "shalevfrfr");
      }
      state.account = state.accounts.find((a) => a.id === state.source) || null;
      return;
    }
    try {
      const data = await api("/api/accounts");
      state.accounts = data.accounts || [];
      state.services = data.services || [];
      state.hubBase = data.hub || "";
      if (state.source !== "all" && !state.accounts.some((a) => a.id === state.source)) {
        const wanted = isRednotePath
          ? state.accounts.find((a) => String(a.platform).toLowerCase() === "rednote")
          : state.accounts.find((a) => String(a.platform).toLowerCase() === "instagram");
        state.source = (wanted || state.accounts[0] || {}).id || state.source;
      }
      state.account = state.accounts.find((a) => a.id === state.source) || null;
    } catch (_) {
      state.accounts = [];
      state.account = null;
    }
  }

  function paintSourceLabels() {
    const account = state.account || {};
    const label = state.source === "all"
      ? "All accounts"
      : (account.label || platformName(account.platform));
    const name = $("sourceName");
    if (name) {
      name.textContent = [label, account.handle ? "@" + account.handle : ""]
        .filter(Boolean).join(" · ");
    }
    document.title = `${label} saved · Saved Archive`;
    const liveTitle = $("liveTitle");
    if (liveTitle) liveTitle.textContent = `Live dashboard · ${label}`;
  }

  /* ---------------- facets / categories ---------------- */
  async function loadFacets() {
    const f = await api("/api/facets");
    $("total").textContent = fmtInt(f.total);
    const expected = $("expected");
    if (expected) expected.textContent = fmtInt(f.expected == null ? f.total : f.expected);
    $("videos").textContent = fmtInt(f.videos);
    window.__PRESETS = f.presets || [];
    const catList = (f.category_list || f.categories || []).map((c) => ({ name: c.name, count: c.count || 0 }));
    window.__CATS = catList;

    const filterCounts = f.filter_counts || {};
    document.querySelectorAll("[data-filter]").forEach((box) => {
      const flag = box.dataset.filter;
      const label = box.closest("label");
      if (!label) return;
      let badge = label.querySelector(".filterCount");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "filterCount";
        label.appendChild(badge);
      }
      const n = filterCounts[flag];
      badge.textContent = n === undefined ? "" : `(${fmtInt(n)})`;
    });

    const cats = [
      { name: "", label: "All posts", count: f.total, tools: false },
      ...catList.map((c) => ({ name: c.name, label: c.name, count: c.count, tools: true })),
    ];
    if (state.category && !cats.some((c) => c.name === state.category)) state.category = "";

    $("cats").innerHTML = cats.map((c) => `
      <div class="cat ${state.category === c.name ? "on" : ""}" data-cat="${esc(c.name)}"
           title="${c.name ? "Click to filter · drop posts to categorise" : "Show every saved post"}">
        <span class="catName">${esc(c.label)}</span>
        <span class="catN">${fmtNum(c.count)}</span>
        ${c.tools ? `<span class="catTools">
          <button data-act="up" title="Move up">▲</button>
          <button data-act="down" title="Move down">▼</button>
          <button data-act="rename" title="Rename">✎</button>
          <button data-act="del" class="del" title="Delete category">✕</button>
        </span>` : ""}
      </div>`).join("");
    $("resetFilter").classList.toggle("hidden", !state.category);

    $("onlyCats").innerHTML = catList.map((c) => `
      <label><input type="checkbox" data-only-cat="${esc(c.name)}"
        ${state.onlyCats.has(c.name) ? "checked" : ""}> only ${esc(c.name)} <b>${fmtNum(c.count)}</b></label>
    `).join("");
    $("onlyCats").querySelectorAll("input[data-only-cat]").forEach((box) => {
      box.onchange = () => {
        if (box.checked) state.onlyCats.add(box.dataset.onlyCat);
        else state.onlyCats.delete(box.dataset.onlyCat);
        reset();
      };
    });

    $("cats").querySelectorAll(".cat").forEach((el) => {
      el.onclick = (e) => {
        if (e.target.closest(".catTools")) return;
        state.category = state.category === el.dataset.cat ? "" : el.dataset.cat;
        state.tagStates.clear();
        reset();
        loadFacets();
      };
      el.addEventListener("dragover", (e) => {
        if (!el.dataset.cat) return;
        if (!dragIds && !state.selected.size) return;
        e.preventDefault();
        el.classList.add("over");
      });
      el.addEventListener("dragleave", () => el.classList.remove("over"));
      el.addEventListener("drop", async (e) => {
        e.preventDefault();
        el.classList.remove("over");
        if (!el.dataset.cat) return;
        const ids = dragIds && dragIds.length ? dragIds : [...state.selected];
        if (ids.length) await assignCategory(el.dataset.cat, ids);
      });
      el.querySelectorAll("[data-act]").forEach((btn) => {
        btn.onclick = (e) => categoryTool(e, el.dataset.cat, btn.dataset.act);
      });
    });

    $("tags").innerHTML = f.tags.map((t) => {
      const mode = tagMode(t.name);
      return `
      <span class="tag ${mode === "in" ? "on" : mode === "out" ? "out" : ""}"
        data-tag="${esc(t.name)}" data-mode="${mode}"
        role="button" aria-pressed="${mode !== "off"}"
        title="Filter by #${esc(t.name)}: click to include, click again to exclude, click again to clear">#${esc(t.name)} <b>${fmtNum(t.count)}</b></span>`;
    }).join("") || '<span class="hint">No personal tags yet.</span>';
    $("tags").querySelectorAll(".tag").forEach((el) => {
      el.onclick = () => {
        const mode = tagMode(el.dataset.tag);
        if (mode === "off") state.tagStates.set(el.dataset.tag, "in");
        else if (mode === "in") state.tagStates.set(el.dataset.tag, "out");
        else state.tagStates.delete(el.dataset.tag);
        reset();
        loadFacets();
      };
    });
    $("clearTag").classList.toggle("hidden", !state.tagStates.size);
    $("clearSel").classList.toggle("hidden", !state.selected.size);
  }

  async function categoryTool(e, name, action) {
    e.preventDefault();
    e.stopPropagation();
    if (!name) return;
    const names = (window.__CATS || []).map((c) => c.name);
    if (action === "rename") {
      const next = prompt(`Rename category "${name}"`, name);
      if (!next || !next.trim() || next.trim() === name) return;
      await api("/api/categories", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "rename", name, new_name: next.trim() }),
      });
      if (state.category === name) state.category = next.trim();
      state.onlyCats.delete(name);
      state.onlyCats.add(next.trim());
    } else if (action === "del") {
      if (!confirm(`Delete category "${name}"?\nPosts stay in the archive; only the filing is removed.`)) return;
      await api("/api/categories", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "remove", name }),
      });
      if (state.category === name) state.category = "";
      state.onlyCats.delete(name);
    } else {
      const index = names.indexOf(name);
      const swap = action === "up" ? index - 1 : index + 1;
      if (index < 0 || swap < 0 || swap >= names.length) return;
      [names[index], names[swap]] = [names[swap], names[index]];
      await api("/api/categories", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ op: "reorder", names }),
      });
    }
    await loadFacets();
    reset();
  }

  $("resetFilter").onclick = () => { state.category = ""; reset(); loadFacets(); };
  document.querySelectorAll("[data-filter]").forEach((box) => {
    box.onchange = () => {
      if (box.checked) state.filters.add(box.dataset.filter);
      else state.filters.delete(box.dataset.filter);
      reset();
    };
  });
  $("clearFilters").onclick = () => {
    state.filters.clear();
    state.onlyCats.clear();
    document.querySelectorAll("[data-filter]").forEach((b) => { b.checked = false; });
    $("filterMenu").open = false;
    reset();
    loadFacets();
  };
  $("addCat").onclick = async () => {
    const name = prompt("New category name");
    if (!name || !name.trim()) return;
    await api("/api/categories", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op: "add", name: name.trim() }),
    });
    await loadFacets();
  };

  function setOwner(owner) {
    state.owner = owner || "";
    state.q = "";
    $("search").value = "";
    $("ownerFilter").classList.toggle("hidden", !owner);
    if (owner) {
      $("ownerName").textContent = "@" + owner;
      const account = state.account || {};
      $("ownerName").href = String(account.platform || "").toLowerCase() === "rednote"
        ? "https://www.rednote.com/search_result?keyword=" + encodeURIComponent(owner)
        : "https://www.instagram.com/" + encodeURIComponent(owner) + "/";
      $("ownerName").target = "_blank";
    }
    reset();
  }
  $("clearOwner").onclick = () => setOwner("");

  async function assignCategory(category, ids) {
    if (!ids.length) return;
    const uncatFilter = state.filters.has("uncategorised");
    const uncategorisedIds = ids.filter((id) => {
      const item = state.items.find((x) => x.id === id);
      return item ? itemCategories(item).length === 0 : false;
    });
    await api("/api/bulk_category", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, category }),
    });
    ids.forEach((id) => {
      const item = state.items.find((x) => x.id === id);
      if (item) {
        item.categories = [...new Set([...(item.categories || []), category])];
        item.category = item.category || category;
        item.confidence = 1;
      }
      const node = document.querySelector(`.card[data-id="${id}"]`);
      if (node && item) paintCategoryChip(node, item);
    });
    // In "uncategorised only" the post no longer belongs in the current view.
    // Remove it immediately instead of waiting for the next server reload.
    if (uncatFilter) dropFromUncategorisedView(uncategorisedIds);
    clearSelection();
    loadFacets();
  }

  /* ---------------- listing ---------------- */
  function reset() {
    state.offset = 0;
    state.items = [];
    state.done = false;
    $("grid").innerHTML = "";
    $("empty").classList.add("hidden");
    load(true);
  }

  async function load(initial) {
    if (state.loading || (state.done && !initial)) return;
    state.loading = true;
    const query = new URLSearchParams({
      offset: state.offset, limit: state.limit, q: state.q,
      category: state.category,
      // Multiple chips compose: includes are ANDed, excludes are ANDed.
      // Each tag has a single state, so exclude always wins for one tag.
      tag: activeTags("in").join(","),
      ntag: activeTags("out").join(","),
      sort: state.sort, owner: state.owner,
      flags: [...state.filters].join(","),
      categories: [...state.onlyCats].join(","),
    });
    try {
      const data = await api("/api/posts?" + query);
      state.total = data.total;
      state.items.push(...data.items);
      state.offset += data.items.length;
      state.done = state.offset >= data.total || data.items.length === 0;
      appendCards(data.items);
      $("count").textContent = `${fmtInt(state.total)} post${state.total === 1 ? "" : "s"}`;
      $("shown").textContent = fmtInt(state.items.length);
      $("empty").classList.toggle("hidden", state.total !== 0);
      $("grid").classList.toggle("manual", state.sort === "manual" && state.view !== "post");
      $("hint").style.visibility = state.sort === "manual" ? "visible" : "hidden";
    } catch (e) {
      console.error(e);
      $("count").textContent = "Could not load posts";
    } finally {
      state.loading = false;
    }
  }

  function mediaOf(item) {
    if (item.media && item.media.length) return item.media;
    return [];
  }

  function showTextThumb(node, item, show) {
    const text = node.querySelector(".thumbText");
    const img = node.querySelector(".thumb");
    if (!text || !img) return;
    if (!show) {
      node.classList.remove("textThumb");
      text.hidden = true;
      text.textContent = "";
      img.hidden = false;
      return;
    }
    const body = String(item.caption_full || item.caption || item.title || "").trim();
    if (!body && !item.owner) {
      text.hidden = true;
      return;
    }
    node.classList.add("textThumb");
    text.hidden = false;
    text.innerHTML = `
      <div class="ttOwner">@${esc(item.owner || "threads")}</div>
      <div class="ttBody">${esc(body || "No caption")}</div>
      <div class="ttMeta">no media · text post</div>`;
    img.hidden = true;
  }

  function updateMedia(node, item, index) {
    const media = mediaOf(item);
    const img = node.querySelector(".thumb");
    const vid = node.querySelector(".cardVideo");
    const play = node.querySelector(".play");
    const dots = node.querySelector(".dots");
    vid.hidden = true;
    vid.pause();
    vid.removeAttribute("src");
    delete vid.dataset.mediaUrl;
    if (!media.length) {
      node._mediaIndex = 0;
      node.classList.remove("multi");
      dots.innerHTML = "";
      play.style.display = "none";
      showTextThumb(node, item, true);
      return;
    }
    const safe = Math.max(0, Math.min(index, media.length - 1));
    node._mediaIndex = safe;
    const current = media[safe];
    const usable = current && (current.thumb || current.url);
    if (!usable) {
      node.classList.remove("multi");
      dots.innerHTML = "";
      play.style.display = "none";
      showTextThumb(node, item, true);
      return;
    }
    showTextThumb(node, item, false);
    if (current.thumb) img.src = current.thumb;
    else img.src = current.url;
    play.style.display = current.kind === "video" ? "block" : "none";
    node.classList.toggle("multi", media.length > 1);
    dots.innerHTML = media.length > 1
      ? media.map((m, i) => `<i class="${i === safe ? "on" : ""}" data-i="${i}"></i>`).join("")
      : "";
    dots.querySelectorAll("i").forEach((dot) => {
      dot.onclick = (e) => { e.stopPropagation(); updateMedia(node, item, Number(dot.dataset.i)); };
    });
  }

  function changeMedia(node, delta) {
    const item = node._item;
    if (!item) return;
    const media = mediaOf(item);
    if (!media.length) return;
    updateMedia(node, item, ((node._mediaIndex || 0) + delta + media.length) % media.length);
  }

  function resetCardMedia(node) {
    if (!node || !node._item) return;
    const vid = node.querySelector(".cardVideo");
    if (vid) {
      vid.pause();
      vid.removeAttribute("src");
      vid.load();
      vid.hidden = true;
      vid.controls = false;
    }
    const img = node.querySelector(".thumb");
    if (img) img.hidden = false;
    const play = node.querySelector(".play");
    if (play) {
      play.style.display = "";
      play.classList.remove("playing");
      play.title = "Play";
    }
    updateMedia(node, node._item, 0);
  }

  function syncCardPlayButton(node, playing) {
    const play = node.querySelector(".play");
    if (!play) return;
    play.classList.toggle("playing", !!playing);
    play.style.display = "block";
    play.title = playing ? "Restart video" : "Play video";
    play.setAttribute("aria-label", playing ? "Restart video" : "Play video");
  }

  function playCardVideo(node) {
    const item = node._item;
    if (!item) return;
    const media = mediaOf(item);
    const current = media[node._mediaIndex || 0];
    if (!current || current.kind !== "video" || !current.url) return;
    const img = node.querySelector(".thumb");
    const vid = node.querySelector(".cardVideo");
    const sameSource = vid.dataset.mediaUrl === current.url;
    img.hidden = true;
    vid.hidden = false;
    if (!sameSource) {
      vid.src = current.url;
      vid.dataset.mediaUrl = current.url;
    }
    vid.controls = true;
    vid.autoplay = true;
    vid.muted = true;
    if (sameSource && (!vid.paused || vid.ended)) {
      try { vid.currentTime = 0; } catch (_) {}
    }
    vid.play().catch(() => {});
    syncCardPlayButton(node, true);
  }

  function itemCategories(item) {
    if (Array.isArray(item.categories) && item.categories.length) return item.categories;
    return item.category ? [item.category] : [];
  }

  function dropFromUncategorisedView(ids) {
    if (!state.filters.has("uncategorised")) return 0;
    const wanted = new Set(ids.map(Number));
    let removed = 0;
    state.items = state.items.filter((item) => {
      if (!wanted.has(item.id)) return true;
      const node = document.querySelector(`.card[data-id="${item.id}"]`);
      if (node) node.remove();
      state.selected.delete(item.id);
      removed += 1;
      return false;
    });
    if (removed) {
      state.total = Math.max(0, state.total - removed);
      state.offset = Math.max(0, state.offset - removed);
      $("count").textContent = `${fmtInt(state.total)} post${state.total === 1 ? "" : "s"}`;
      $("shown").textContent = fmtInt(state.items.length);
      $("empty").classList.toggle("hidden", state.total !== 0);
      updateSelectBar();
      if (!state.done) load(false);
    }
    return removed;
  }

  function paintCategoryChip(node, item) {
    const chip = node.querySelector(".catChip");
    if (!chip) return;
    const cats = itemCategories(item);
    if (!cats.length) {
      chip.classList.remove("on");
      chip.textContent = "";
      return;
    }
    const pct = item.confidence ? ` ${Math.round(item.confidence * 100)}%` : "";
    chip.textContent = cats.slice(0, 2).join(" · ")
      + (cats.length > 2 ? ` +${cats.length - 2}` : "") + pct;
    chip.classList.add("on");
  }

  function cardEl(item) {
    const node = $("cardTpl").content.firstElementChild.cloneNode(true);
    node.dataset.id = item.id;
    node._item = item;
    node.classList.toggle("video", item.is_video);
    node.classList.toggle("notes", !!item.notes);
    node.classList.toggle("selected", state.selected.has(item.id));
    updateMedia(node, item, 0);

    const owner = node.querySelector(".ownerLink");
    if (item.owner) {
      owner.hidden = false;
      owner.textContent = "@" + item.owner;
      owner.href = item.owner_url || "#";
      owner.onclick = (e) => { e.stopPropagation(); setOwner(item.owner); };
    } else {
      owner.hidden = true;
      owner.textContent = "";
      owner.removeAttribute("href");
      owner.onclick = null;
    }

    const box = node.querySelector(".pickBox");
    box.checked = state.selected.has(item.id);
    box.onclick = (e) => e.stopPropagation();
    box.onchange = () => toggleSelect(item.id, node, box.checked);

    const caption = node.querySelector(".caption");
    caption.textContent = item.caption || item.title || "";

    node.querySelector(".likes").textContent = item.likes ? "♥ " + fmtNum(item.likes) : "";
    node.querySelector(".comments").textContent = "💬 " + fmtNum(item.comments_count || 0);
    node.querySelector(".date").textContent = fmtDate(item.taken_at);
    const thumbImg = node.querySelector(".thumb");
    thumbImg.addEventListener("error", () => showTextThumb(node, item, true));
    thumbImg.addEventListener("load", () => {
      if (item.media && item.media.length) showTextThumb(node, item, false);
    });

    paintCategoryChip(node, item);

    node.querySelector(".cardTags").innerHTML =
      (item.tags || []).slice(0, 3).map((t) => `<span>#${esc(t)}</span>`).join("");

    node.querySelector(".play").onclick = (e) => { e.stopPropagation(); playCardVideo(node); };
    node.querySelector(".prev").onclick = (e) => { e.stopPropagation(); changeMedia(node, -1); };
    node.querySelector(".next").onclick = (e) => { e.stopPropagation(); changeMedia(node, 1); };

    const cardVideo = node.querySelector(".cardVideo");
    cardVideo.addEventListener("play", () => syncCardPlayButton(node, true));
    cardVideo.addEventListener("pause", () => syncCardPlayButton(node, false));
    cardVideo.addEventListener("ended", () => syncCardPlayButton(node, false));

    const thumbWrap = node.querySelector(".thumbWrap");
    let touchX = null;
    thumbWrap.addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
    thumbWrap.addEventListener("touchend", (e) => {
      if (touchX == null) return;
      const dx = e.changedTouches[0].clientX - touchX;
      if (Math.abs(dx) > 35) changeMedia(node, dx < 0 ? 1 : -1);
      touchX = null;
    });

    node.addEventListener("click", (e) => {
      if (suppressClick) { suppressClick = false; return; }
      if (e.target.closest(".ownerRow") || e.target.closest(".navBtn")
          || e.target.closest(".play") || e.target.closest("input")) return;
      resetCardMedia(node);
      openPost(item.id);
    });

    attachDrag(node);
    return node;
  }

  function appendCards(items) {
    const grid = $("grid");
    items.forEach((item) => grid.appendChild(cardEl(item)));
  }

  function renderAll() {
    const grid = $("grid");
    grid.className = "grid " + state.view + (state.sort === "manual" && state.view !== "post" ? " manual" : "");
    grid.innerHTML = "";
    state.items.forEach((item) => grid.appendChild(cardEl(item)));
    grid.classList.toggle("manual", state.sort === "manual" && state.view !== "post");
  }

  /* ---------------- selection ---------------- */
  function toggleSelect(id, node, checked) {
    const on = checked === undefined ? !state.selected.has(id) : checked;
    if (on) state.selected.add(id);
    else state.selected.delete(id);
    node.classList.toggle("selected", on);
    const box = node.querySelector(".pickBox");
    if (box) box.checked = on;
    updateSelectBar();
  }
  function clearSelection() {
    state.selected.clear();
    document.querySelectorAll(".card.selected").forEach((n) => {
      n.classList.remove("selected");
      const box = n.querySelector(".pickBox");
      if (box) box.checked = false;
    });
    updateSelectBar();
  }
  function updateSelectBar() {
    $("selCount").textContent = state.selected.size;
    $("selectBar").classList.toggle("hidden", state.selected.size === 0);
    $("clearSel").classList.toggle("hidden", state.selected.size === 0);
  }
  $("clearSel").onclick = clearSelection;
  const grid = $("grid");
  grid.addEventListener("mousedown", (e) => {
    if (e.target.closest(".card")) return;
    rectStart = { x: e.clientX, y: e.clientY };
    rectMoved = false;
    const box = $("selectBox");
    box.style.left = e.clientX + "px";
    box.style.top = e.clientY + "px";
    box.style.width = "0px";
    box.style.height = "0px";
    box.classList.remove("hidden");
  });
  window.addEventListener("mousemove", (e) => {
    if (!rectStart) return;
    if (Math.abs(e.clientX - rectStart.x) > 3 || Math.abs(e.clientY - rectStart.y) > 3) rectMoved = true;
    const box = $("selectBox");
    const x = Math.min(e.clientX, rectStart.x);
    const y = Math.min(e.clientY, rectStart.y);
    const w = Math.abs(e.clientX - rectStart.x);
    const h = Math.abs(e.clientY - rectStart.y);
    box.style.left = x + "px"; box.style.top = y + "px";
    box.style.width = w + "px"; box.style.height = h + "px";
    const rect = { left: x, top: y, right: x + w, bottom: y + h };
    document.querySelectorAll(".card").forEach((card) => {
      const r = card.getBoundingClientRect();
      const hit = !(r.right < rect.left || r.left > rect.right || r.bottom < rect.top || r.top > rect.bottom);
      card.classList.toggle("selected", hit || state.selected.has(Number(card.dataset.id)));
    });
  });
  window.addEventListener("mouseup", () => {
    if (!rectStart) return;
    const moved = rectMoved;
    rectStart = null;
    rectMoved = false;
    $("selectBox").classList.add("hidden");
    document.querySelectorAll(".card.selected").forEach((card) => {
      state.selected.add(Number(card.dataset.id));
      const box = card.querySelector(".pickBox");
      if (box) box.checked = true;
    });
    updateSelectBar();
    if (moved) {
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 120);
    }
  });
  grid.addEventListener("click", (e) => {
    if (suppressClick) { suppressClick = false; return; }
    if (e.target === grid && !rectMoved) clearSelection();
  });

  /* ---------------- drag ---------------- */
  function attachDrag(node) {
    node.draggable = true;
    node.addEventListener("dragstart", (e) => {
      const id = Number(node.dataset.id);
      dragIds = state.selected.has(id) ? [...state.selected] : [id];
      if (!state.selected.has(id)) {
        state.selected.clear();
        document.querySelectorAll(".card.selected").forEach((n) => n.classList.remove("selected"));
      }
      node.classList.add("drag");
      suppressClick = true;
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", JSON.stringify(dragIds)); } catch (_) {}
    });
    node.addEventListener("dragend", () => {
      node.classList.remove("drag");
      document.querySelectorAll(".card.over").forEach((n) => n.classList.remove("over"));
      dragIds = null;
      updateSelectBar();
      setTimeout(() => { suppressClick = false; }, 120);
    });
    node.addEventListener("dragover", (e) => {
      if (state.sort === "manual" && dragIds && dragIds.length === 1) {
        e.preventDefault();
        node.classList.add("over");
      }
    });
    node.addEventListener("dragleave", () => node.classList.remove("over"));
    node.addEventListener("drop", async (e) => {
      if (!(state.sort === "manual" && dragIds && dragIds.length === 1)) return;
      e.preventDefault();
      node.classList.remove("over");
      const targetId = Number(node.dataset.id);
      if (dragIds[0] === targetId) return;
      await api("/api/reorder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: dragIds[0], before_id: targetId }),
      });
      dragIds = null;
      reset();
    });
  }

  /* ---------------- detail drawer ---------------- */
  async function openPost(id) {
    try {
      const post = await api(`/api/post/${id}?comments=1&limit=20`);
      renderDrawer(post);
      $("drawer").classList.add("open");
      $("drawer").setAttribute("aria-hidden", "false");
      $("scrim").classList.remove("hidden");
    } catch (err) {
      console.error(err);
    }
  }

  function closeDrawer() {
    document.querySelectorAll("#drawer video").forEach((video) => video.pause());
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("scrim").classList.add("hidden");
  }

  function tagHtml(tags) {
    return (tags || []).map((t) => `<span>#${esc(t)}</span>`).join("") || '<span class="hint">none</span>';
  }

  function renderDrawer(post) {
    const platform = String(state.account?.platform || "").toLowerCase();
    $("drawer").classList.toggle("threads", ["threads", "twitter", "x"].includes(platform));
    $("dPermalink").href = post.permalink || "#";
    $("dPermalink").textContent = platform === "rednote"
      ? "Open on RedNote ↗"
      : (platform === "threads" ? "Open on Threads ↗" : "Open on Instagram ↗");
    const postCats = Array.isArray(post.categories) && post.categories.length
      ? [...post.categories]
      : (post.category ? [post.category] : []);
    const catNames = (window.__CATS || []).map((c) => c.name);
    for (const c of postCats) if (!catNames.includes(c)) catNames.push(c);
    const catOptions = ["", ...catNames]
      .map((c) => `<option value="${esc(c)}">${esc(c || "— add category —")}</option>`)
      .join("") + '<option value="__new">+ new category…</option>';

    let media = "";
    if (post.media && post.media.length) {
      media = `<div class="dGalleryWrap">
        <div class="dGallery" id="gallery">` + post.media.map((m, i) => {
        if (m.kind === "video") {
          return `<video class="gItem" data-i="${i}" data-kind="video" controls playsinline
            webkit-playsinline preload="metadata" poster="${m.thumb || post.cover}" src="${m.url}" muted></video>`;
        }
        return `<img class="gItem" data-i="${i}" data-kind="image" src="${m.url}" alt="">`;
      }).join("") + `</div>
        <button class="gPlay hidden" id="gPlay" type="button" title="Play / restart video" aria-label="Play or restart video">▶</button>
      </div>`;
      if (post.media.length > 1) {
        media += `<div class="gNav">
          <button class="btn" id="gPrev">‹</button>
          <span class="gCount" id="gCount">1 / ${post.media.length}</span>
          <button class="btn" id="gNext">›</button>
        </div>`;
      }
    } else {
      media = '<div class="hint" style="padding:20px 0">No media file for this post — showing caption and comments.</div>';
    }

    const owner = post.owner
      ? `<a href="${esc(post.owner_url || "#")}" target="_blank" rel="noopener">@${esc(post.owner)}</a>`
      : '<span class="hint">creator unavailable</span>';
    const pct = post.category_confidence ? ` · ${Math.round(post.category_confidence * 100)}%` : "";
    const folderLink = post.permalink
      ? `<a href="${esc(post.permalink)}" target="_blank" rel="noopener">${esc(post.shortcode || "post")}</a>`
      : esc(post.shortcode || "");

    const threadsMode = ["threads", "twitter", "x"].includes(platform);
    const layoutOpen = threadsMode
      ? '<div class="threadsLayout"><div class="threadsMain">' : "";
    const layoutSplit = threadsMode
      ? '</div><aside class="threadsComments">' : "";
    const layoutClose = threadsMode ? "</aside></div>" : "";

    $("drawerBody").innerHTML = `
      ${layoutOpen}
      ${media}
      <div class="dTitle">${owner}</div>
      <div class="dMeta">
        <span>${fmtDate(post.taken_at)}</span>
        <span>♥ ${fmtNum(post.likes)}</span>
        <span>💬 ${fmtNum(post.comments_count || 0)} comments</span>
        ${post.typename ? `<span>${esc(post.typename)}</span>` : ""}
        ${post.category ? `<span>${esc(post.category)}${pct}</span>` : ""}
        ${post.location ? `<span>📍 ${esc(post.location)}</span>` : ""}
        <span>${(post.media || []).length} file${(post.media || []).length === 1 ? "" : "s"}</span>
      </div>
      <div class="dCaption">${esc(post.caption || post.title || "")}</div>

      <div class="dSection">Categories <span id="savedCat" class="saved">saved</span></div>
      <div class="dTags" id="postCats">
        ${postCats.map((c) => `<span>${esc(c)} <button class="catRemove" data-cat="${esc(c)}" title="Remove">×</button></span>`).join("") || '<span class="hint">none</span>'}
      </div>
      <div class="dField" style="margin-top:8px"><select id="catSelect">${catOptions}</select></div>

      <div class="dSection">Notes <span id="savedNotes" class="saved">saved</span></div>
      <div class="dField"><textarea id="notes" placeholder="Why did you save this? What to use it for…">${esc(post.notes || "")}</textarea></div>

      <div class="dSection">Personal tags <span id="savedTags" class="saved">saved</span></div>
      <div class="dTags" id="dTags">${tagHtml(post.tags)}</div>
      <div class="dField" style="margin-top:8px">
        <input id="tagInput" placeholder="comma separated, e.g. lighting, tutorial, watch-later" value="${esc((post.tags || []).join(", "))}">
      </div>

      <div class="dSection">Details</div>
      <div class="dMeta">
        <span>${folderLink}</span>
        <span>${esc(post.folder || "")}</span>
        ${post.permalink ? `<a href="${esc(post.permalink)}" target="_blank" rel="noopener">post URL ↗</a>` : ""}
        <button class="btn small" id="openFolder" title="Open in file manager">Open folder</button>
        ${post.vision_caption ? `<span>vision: ${esc(post.vision_caption)}</span>` : ""}
      </div>

      ${layoutSplit}
      <div class="dSection commentsHead">Comments <span id="commentCount" class="commentTotal">—</span></div>
      <div id="comments" class="commentsPanel"></div>
      <button class="btn small hidden" id="moreComments" style="margin-top:8px">Load more comments</button>
      ${layoutClose}
    `;

    const gallery = $("gallery");
    if (gallery) {
      const items = [...gallery.querySelectorAll(".gItem")];
      const count = $("gCount");
      const playBtn = $("gPlay");
      let active = -1;

      const playActiveVideo = () => {
        const el = items[active];
        if (!el || el.tagName !== "VIDEO") return;
        el.muted = true;
        el.autoplay = true;
        el.playsInline = true;
        const attempt = el.play();
        if (attempt && attempt.catch) attempt.catch(() => {});
      };

      const updatePlayButton = () => {
        if (!playBtn) return;
        const el = items[active];
        const isVideo = !!(el && el.tagName === "VIDEO");
        playBtn.classList.toggle("hidden", !isVideo);
        playBtn.textContent = el && !el.paused ? "↻" : "▶";
        playBtn.title = el && !el.paused ? "Restart video" : "Play video";
      };

      const setActive = () => {
        const next = Math.max(0, Math.min(items.length - 1,
          Math.round(gallery.scrollLeft / Math.max(1, gallery.clientWidth))));
        const changed = next !== active;
        active = next;
        if (count) count.textContent = `${active + 1} / ${items.length}`;
        items.forEach((el, j) => {
          if (el.tagName === "VIDEO") {
            if (j === active) {
              if (changed) {
                try { el.currentTime = 0; } catch (_) {}
              }
              playActiveVideo();
            } else {
              el.pause();
            }
          }
        });
        updatePlayButton();
      };

      gallery.addEventListener("scroll", () => {
        clearTimeout(gallery.__t);
        gallery.__t = setTimeout(setActive, 120);
      });

      items.forEach((el) => {
        el.addEventListener("click", () => {
          if (el.tagName !== "VIDEO") return;
          if (el.paused) el.play().catch(() => {});
          else el.pause();
          updatePlayButton();
        });
        if (el.tagName === "VIDEO") {
          el.addEventListener("play", updatePlayButton);
          el.addEventListener("pause", updatePlayButton);
          el.addEventListener("ended", updatePlayButton);
        }
      });

      if (playBtn) {
        playBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const el = items[active];
          if (!el || el.tagName !== "VIDEO") return;
          try { el.currentTime = 0; } catch (_) {}
          el.muted = true;
          el.play().catch(() => {});
          updatePlayButton();
        });
      }

      const go = (delta) => {
        const target = Math.max(0, Math.min(items.length - 1, active + delta));
        gallery.scrollTo({ left: target * gallery.clientWidth, behavior: "smooth" });
        clearTimeout(gallery.__t);
        gallery.__t = setTimeout(setActive, 260);
      };
      if ($("gPrev")) $("gPrev").onclick = () => go(-1);
      if ($("gNext")) $("gNext").onclick = () => go(1);

      requestAnimationFrame(() => {
        setActive();
        playActiveVideo();
      });
    }

    if ($("openFolder") && staticMode) {
      $("openFolder").hidden = true;
    } else if ($("openFolder")) {
      $("openFolder").onclick = async () => {
        $("openFolder").textContent = "Opening…";
        try {
          const result = await api(`/api/open_folder/${post.id}`);
          $("openFolder").textContent = result.ok ? "Opened folder" : "Folder missing";
        } catch (_) {
          $("openFolder").textContent = "Could not open";
        }
        setTimeout(() => { if ($("openFolder")) $("openFolder").textContent = "Open folder"; }, 2000);
      };
    }

    const paintPostCats = () => {
      const wrap = $("postCats");
      if (!wrap) return;
      wrap.innerHTML = postCats.map((c) => `
        <span>${esc(c)} <button class="catRemove" data-cat="${esc(c)}" title="Remove">×</button></span>
      `).join("") || '<span class="hint">none</span>';
      wrap.querySelectorAll(".catRemove").forEach((btn) => {
        btn.onclick = async () => {
          const cat = btn.dataset.cat;
          const next = postCats.filter((c) => c !== cat);
          postCats.splice(0, postCats.length, ...next);
          await savePost(post.id, { categories: postCats });
          flash("savedCat");
          updateLocal(post.id, { categories: [...postCats], category: postCats[0] || null });
          paintPostCats();
          await loadFacets();
        };
      });
    };
    paintPostCats();

    $("catSelect").onchange = async (e) => {
      let value = e.target.value;
      if (value === "__new") {
        const next = prompt("New category name");
        value = next ? next.trim() : "";
      }
      if (!value) { e.target.value = ""; return; }
      if (!postCats.includes(value)) postCats.push(value);
      await savePost(post.id, { categories: postCats });
      flash("savedCat");
      updateLocal(post.id, { categories: [...postCats], category: postCats[0] || null });
      paintPostCats();
      e.target.value = "";
      dropFromUncategorisedView([post.id]);
      await loadFacets();
    };

    $("notes").oninput = () => {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(async () => {
        await savePost(post.id, { notes: $("notes").value });
        flash("savedNotes");
        updateLocal(post.id, { notes: $("notes").value });
        loadCloudStatus();
      }, 700);
    };

    const saveTags = async () => {
      const tags = $("tagInput").value.split(",").map((t) => t.trim()).filter(Boolean);
      await savePost(post.id, { tags });
      flash("savedTags");
      $("dTags").innerHTML = tagHtml(tags);
      updateLocal(post.id, { tags });
      await loadFacets();
    };
    $("tagInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveTags(); } });
    $("tagInput").addEventListener("blur", saveTags);

    let commentOffset = 0;
    const commentMediaHtml = (media) => {
      if (!media || !media.length) return "";
      return `<div class="commentMedia">` + media.map((m) => (
        m.kind === "video"
          ? `<video class="cMedia" controls preload="metadata" playsinline src="${m.url}"></video>`
          : `<a class="cMediaLink" href="${m.url}" target="_blank" rel="noopener">
               <img class="cMedia" loading="lazy" src="${m.url}" alt=""></a>`
      )).join("") + `</div>`;
    };
    const commentHtml = (c, isReply = false) => {
      const replies = (c.replies || []).map((r) => `
        <div class="comment reply">
          <div class="cTop"><b>@${esc(r.owner || "?")}</b>
            <span class="when">${fmtDate(r.created_at)}</span>
            ${r.likes ? `<span class="when">♥ ${fmtInt(r.likes)}</span>` : ""}</div>
          <div class="cText">${linkify(r.text || "")}</div>
          ${commentMediaHtml(r.media)}
        </div>`).join("");
      return `<div class="comment ${isReply ? "reply" : ""}">
        <div class="cTop"><b>@${esc(c.owner || "?")}</b>
          <span class="when">${fmtDate(c.created_at)}</span>
          ${c.likes ? `<span class="when">♥ ${fmtInt(c.likes)}</span>` : ""}
          ${c.reply_total ? `<span class="when">↳ ${fmtInt(c.reply_total)} replies</span>` : ""}</div>
        <div class="cText">${linkify(c.text || "")}</div>
        ${commentMediaHtml(c.media)}
        ${replies}
      </div>`;
    };
    const renderComments = (items, append) => {
      const box = $("comments");
      if (!box) return;
      if (!items.length && !append) {
        box.innerHTML = '<div class="commentsEmpty">No comments stored for this post.</div>';
        return;
      }
      box.insertAdjacentHTML("beforeend", items.map((c) => commentHtml(c)).join(""));
    };
    const data = post.comments_data || { items: [], total: 0, hasMore: false };
    commentOffset = (data.items || []).length;
    renderComments(data.items || [], false);
    const commentCount = $("commentCount");
    if (commentCount) {
      commentCount.textContent = `${fmtInt(data.total || 0)} comment${Number(data.total) === 1 ? "" : "s"}`;
    }
    if ($("moreComments") && data.hasMore) {
      $("moreComments").classList.remove("hidden");
      $("moreComments").onclick = async () => {
        $("moreComments").disabled = true;
        try {
          const next = await api(`/api/comments/${post.id}?offset=${commentOffset}&limit=20`);
          commentOffset += (next.items || []).length;
          renderComments(next.items || [], true);
          if (!next.hasMore) $("moreComments").classList.add("hidden");
        } finally {
          $("moreComments").disabled = false;
        }
      };
    }
  }

  async function savePost(id, payload) {
    await api(`/api/post/${id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    loadCloudStatus();
  }

  function flash(id) {
    const el = $(id);
    if (!el) return;
    el.classList.add("on");
    setTimeout(() => el.classList.remove("on"), 1200);
  }

  function updateLocal(id, patch) {
    const item = state.items.find((x) => x.id === id);
    if (!item) return;
    Object.assign(item, patch);
    const node = document.querySelector(`.card[data-id="${id}"]`);
    if (!node) return;
    node.classList.toggle("notes", !!item.notes);
    paintCategoryChip(node, item);
    node.querySelector(".cardTags").innerHTML =
      (item.tags || []).slice(0, 3).map((t) => `<span>#${esc(t)}</span>`).join("");
  }

  /* ---------------- live dashboard (native port of port 8766) ---------------- */
  const UNSAVE_CRITERIA = [
    ["comments", "comments"],
    ["caption", "caption"],
    ["cover", "cover"],
    ["owner", "ownerLink"],
    ["shortcode", "post shortcode"],
    ["postid", "Instagram post id"],
    ["adcarousel", "non-video ad carousel"],
    ["ads", "all ads"],
  ];
  const UNSAVE_KEY = "ig_unsave_criteria_v1";
  const LIVE_LOG_KEY = "ig_live_log_name";
  let liveUnsaveCriteria = {};
  let liveUnsavePreview = "";
  let liveLogName = localStorage.getItem(LIVE_LOG_KEY) || "download";
  let liveLogCatalogKey = "";
  let liveLogLast = "";
  let liveBusy = false;

  try { liveUnsaveCriteria = JSON.parse(localStorage.getItem(UNSAVE_KEY) || "{}"); } catch (_) {}

  function fmtDuration(seconds) {
    if (seconds == null) return "—";
    let value = Math.max(0, Math.round(seconds));
    const h = Math.floor(value / 3600);
    const m = Math.floor((value % 3600) / 60);
    const s = value % 60;
    return (h ? h + "h " : "") + (h || m ? m + "m " : "") + s + "s";
  }
  const fmtRate = (rate) => rate > 0 ? rate.toFixed(2) + " posts/s" : "—";

  function liveCard(label, value, sub, cls = "") {
    return `<div class="liveCard"><div class="k">${esc(label)}</div>
      <div class="v ${cls}">${esc(value)}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ""}</div>`;
  }

  function jobDetail(job) {
    const count = Number(job.count || 0);
    const b = job.breakdown || {};
    let detail = `success ${fmtInt(b.success ?? count)}`;
    if (job.name === "comments") {
      detail += ` · with comments ${fmtInt(b.with_comments || 0)} · no comments ${fmtInt(b.no_comments || 0)} · errors/429 ${fmtInt(b.errors || 0)}`;
    } else if (job.name === "ads") {
      detail += ` · non-ads ${fmtInt(b.non_ads || 0)} · errors ${fmtInt(b.errors || 0)}`;
    } else if (job.name === "download") {
      detail += ` · folders ${fmtInt(count)} · missing media ${fmtInt(b.missing_media || 0)}`;
    } else if (job.name === "covers") {
      detail += ` · missing ${fmtInt(b.missing || 0)}`;
    } else if (job.name === "unsave") {
      detail += ` · errors/rate-limited ${fmtInt(b.errors || 0)} · remaining ${fmtInt(b.remaining || 0)}`;
      if (b.ineligible) detail += ` · still saved (not eligible) ${fmtInt(b.ineligible)}`;
    } else {
      detail += ` · n/a ${fmtInt(b.not_applicable || 0)}`;
    }
    return detail;
  }

  function dashTask(job) {
    const count = Number(job.count || 0);
    const total = Number(job.total || 0);
    const pct = total ? Math.min(100, count / total * 100) : 0;
    const status = String(job.status || "stopped");
    const statusClass = ["verified", "running", "paused"].includes(status) ? status : "";
    const detail = jobDetail(job);
    if (job.name === "unsave") {
      const boxes = UNSAVE_CRITERIA.map(([key, label]) =>
        `<label><input type="checkbox" data-crit="${key}" ${liveUnsaveCriteria[key] ? "checked" : ""}> ${label}</label>`
      ).join("");
      return `<div class="dashTask" style="grid-column:1/-1">
        <div class="dashTaskTop"><span>${esc(job.label)}</span><b>${fmtInt(count)} / ${fmtInt(total)}</b></div>
        <div class="liveBar"><i style="width:${pct.toFixed(1)}%"></i></div>
        <div class="s">${pct.toFixed(1)}% · rate ${fmtRate(job.rate)} · eta ${fmtDuration(job.eta)} <span class="dashStatus ${statusClass}">${esc(status)}</span></div>
        <div class="s">${esc(detail)}</div>
        <div class="dashCriteria">${boxes}</div>
        <div class="liveActions">
          <button class="btn small" data-dash-unsave="preview" ${job._startable === false ? "disabled" : ""}>Validate selected</button>
          <button class="btn small" data-dash-unsave="run" ${job._startable === false ? "disabled" : ""}>Unsave matching</button>
          ${status === "running" && job._startable !== false ? '<button class="btn small" data-dash-unsave="pause">Pause</button>' : ""}
        </div>
        <div class="s" id="dashUnsavePreview">${esc(liveUnsavePreview)}</div>
      </div>`;
    }
    const buttonLabel = status === "running" ? "Pause" : (status === "paused" ? "Resume" : "Start");
    const action = status === "running" ? "pause" : "start";
    return `<div class="dashTask">
      <div class="dashTaskTop"><span>${esc(job.label)}</span><b>${fmtInt(count)} / ${fmtInt(total)}</b></div>
      <div class="liveBar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="s">${pct.toFixed(1)}% · rate ${fmtRate(job.rate)} · eta ${fmtDuration(job.eta)} <span class="dashStatus ${statusClass}">${esc(status)}</span></div>
      <div class="s">${esc(detail)}</div>
      <div class="liveActions">
        <button class="btn small" data-dash-job="${esc(job.name)}" data-dash-action="${action}" ${job.available && job._startable !== false ? "" : "disabled"}>${buttonLabel}</button>
      </div>
    </div>`;
  }

  function ensureDashShell(panel) {
    if ($("dashHero")) return;
    panel.innerHTML = `
      <div class="livePanel" id="liveSummary"></div>
      <div class="dashHero" id="dashHero"></div>
      <div class="livePanel dashCards" id="dashCards"></div>
      <div class="dashSectionHead">Pipeline tasks <span>classify · comments · covers · unsave · ads</span></div>
      <div class="dashTasks" id="dashTasks"></div>
      <div class="dashLogHead">
        <span>progress logs</span>
        <span class="dashLogControls">
          <select id="dashLogSelect" class="dashLogSelect"></select>
          <span class="hint" id="dashUpdated">—</span>
        </span>
      </div>
      <pre class="dashLog" id="dashLog"></pre>
      <div class="hint" id="dashPaths"></div>`;
  }

  function renderSimpleLive(panel, d) {
    const stats = d.stats || {};
    const progress = d.progress || {};
    const actions = d.actions || [];
    panel.innerHTML = [
      liveCard("Archived", fmtInt(stats.total), `${fmtInt(stats.videos || 0)} videos`),
      liveCard("Still saved", fmtInt(stats.still_saved), "in the live saved list"),
      liveCard("Already unsaved", fmtInt(stats.already_unsaved), "kept in this archive"),
      liveCard("Comments", fmtInt(stats.comments), "posts with saved comments"),
      stats.missing_media != null ? liveCard("Missing media", fmtInt(stats.missing_media), "posts incomplete") : "",
      stats.media_files != null && d.kind === "rednote" ? liveCard("Media files", fmtInt(stats.media_files), "on disk") : "",
      `<div class="liveCard liveWide">
        <div class="k">${esc(progress.kind || "job")} ${d.job && d.job.running ? "· running" : "· idle"}</div>
        <div class="liveBar"><i style="width:${Number(progress.percent || 0)}%"></i></div>
        <div class="s">${fmtInt(progress.done || 0)} / ${fmtInt(progress.total || 0)}
          ${progress.current ? " · " + esc(progress.current) : ""}
          ${progress.message ? " · " + esc(progress.message) : ""}</div>
        ${actions.length ? `<div class="liveActions">${actions.map((a) =>
          `<button class="btn small" data-live-job="${esc(a.type)}">${esc(a.label)}</button>`).join("")}</div>` : ""}
        ${(d.log || []).length ? `<pre class="liveLog">${esc((d.log || []).join("\n"))}</pre>` : ""}
      </div>`,
    ].join("");
    panel.querySelectorAll("[data-live-job]").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm(`Start "${btn.textContent}"?`)) return;
        btn.disabled = true;
        try {
          await api("/api/live/job", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type: btn.dataset.liveJob }),
          });
        } catch (err) {
          alert("Could not start job: " + err.message);
        }
        setTimeout(renderLive, 400);
      };
    });
  }

  function updateDashShell(d) {
    const total = d.completed_total ?? d.reference_total;
    const folders = Number(d.folders || 0);
    const pct = total ? Math.min(100, folders / total * 100) : 0;
    const status = String(d.status || "stopped");
    const dot = status === "running" ? "run" : (status === "complete" ? "done" : "stop");
    const position = d.position
      ? `list position ${fmtInt(d.position)}${d.shortcode ? " · " + esc(d.shortcode) : ""}`
      : "waiting for first post…";
    $("dashHero").innerHTML = `
      <div class="dashTop">
        <div>
          <div class="dashBig">${fmtInt(folders)} <span>posts saved</span></div>
          <div class="s">${position}</div>
        </div>
        <div class="dashRight">
          <div class="s"><span class="dashDot ${dot}"></span>${esc(status)}${d.pid ? " · pid " + fmtInt(d.pid) : ""}</div>
          <div class="s">elapsed <b>${fmtDuration(d.elapsed)}</b></div>
          <div class="s">rate <b>${fmtRate(d.rate)}</b></div>
          <div class="s">eta <b>${fmtDuration(d.eta)}</b></div>
        </div>
      </div>
      <div class="liveBar dashProgress ${status === "running" && !total ? "indet" : ""}" id="dashBarWrap">
        <i style="width:${pct.toFixed(1)}%"></i>
      </div>
      <div class="dashBarMeta">
        <span>${total ? pct.toFixed(1) + "%" : "in progress"}</span>
        <span>${total ? `of ${fmtInt(total)} ${d.completed_total ? "total" : "(previous run)"}` : "total unknown"}</span>
      </div>`;
    $("dashCards").innerHTML = [
      liveCard("Downloaded", fmtInt(d.folders), "unique folders", "ok"),
      liveCard("On disk", d.size_human || "0 B",
        d.completed_total ? `${fmtInt(d.completed_total)} in metadata` : "media folders"),
      liveCard("Failures", fmtInt(d.failed), "logged errors", d.failed ? "bad" : "ok"),
      liveCard("Current", d.shortcode || "—", d.position ? "#" + fmtInt(d.position) : ""),
    ].join("");
    $("dashTasks").innerHTML = (d.jobs || []).map((job) => dashTask({
      ...job, _startable: d.startable !== false,
    })).join("");
    $("dashPaths").textContent = `${d.data_dir || ""}  ·  ${d.log_path || ""}` +
      (d.startable === false ? "  ·  read-only: jobs managed by accounts.py / run_accounts.py" : "");
    $("dashUpdated").textContent = "updated " + new Date().toLocaleTimeString();

    const key = (d.logs || []).map((x) => x.name + (x.exists ? "+" : "-")).join("|");
    const select = $("dashLogSelect");
    if (key !== liveLogCatalogKey) {
      liveLogCatalogKey = key;
      select.innerHTML = '<option value="all">all logs</option>' +
        (d.logs || []).map((x) =>
          `<option value="${esc(x.name)}">${esc(x.name)}${x.exists ? "" : " (missing)"}</option>`).join("");
      if (![...select.options].some((o) => o.value === liveLogName)) liveLogName = "download";
      select.value = liveLogName;
    }
    updateDashLog(d);
  }

  async function updateDashLog(d) {
    const el = $("dashLog");
    if (!el) return;
    try {
      if (liveLogName === "download") {
        const text = (d.tail || []).join("\n");
        if (text !== liveLogLast) {
          liveLogLast = text;
          el.textContent = text;
          el.scrollTop = el.scrollHeight;
        }
      } else {
        const result = await api(`/api/dashboard/log?name=${encodeURIComponent(liveLogName)}&lines=160`);
        el.textContent = (result.lines || []).join("\n");
        el.scrollTop = el.scrollHeight;
      }
    } catch (_) {}
  }

  async function apiWithTimeout(url, ms = 25000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    try {
      return await api(url, { signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  async function renderLive() {
    if (liveBusy) return;
    liveBusy = true;
    try {
    const panel = $("livePanel");
    if (!panel) return;
    if (panel.dataset.source !== state.source) {
      panel.innerHTML = "";
      panel.dataset.source = state.source;
      liveLogCatalogKey = "";
      liveLogLast = "";
    }
    if (!panel.querySelector("#dashHero") && !panel.querySelector(".liveCard")) {
      panel.innerHTML = `<div class="liveCard liveWide"><div class="k">Live dashboard</div>
        <div class="s">Loading download, pipeline and log status…</div></div>`;
    }
    const platform = String((state.account || {}).platform || "").toLowerCase();
    let dash = null;
    if (platform === "instagram") {
      try { dash = await apiWithTimeout("/api/dashboard", 25000); } catch (_) { dash = null; }
    }
    if (dash) {
      ensureDashShell(panel);
      const summary = $("liveSummary");
      if (summary) { summary.innerHTML = ""; summary.style.display = "none"; }
      updateDashShell(dash);
      const wrap = $("liveFrameWrap");
      wrap.classList.add("hidden");
      const open = $("liveOpen");
      open.classList.remove("hidden");
      open.href = "http://127.0.0.1:8766/";
      $("liveNote").textContent = dash.startable === false
        ? "Read-only live status: this account is managed by accounts.py / run_accounts.py."
        : "Live download, comments and unsave progress for this account.";
      return;
    }
    let d;
    try {
      d = await api("/api/live");
    } catch (err) {
      panel.innerHTML = `<div class="liveCard liveWide"><div class="k">Live status</div>
        <div class="s">Could not read live progress: ${esc(err.message)}</div></div>`;
      return;
    }
    renderSimpleLive(panel, d);
    const wrap = $("liveFrameWrap");
    if (d.dashboard) {
      wrap.classList.remove("hidden");
      const frame = $("liveFrame");
      if (frame.getAttribute("src") !== d.dashboard) frame.src = d.dashboard;
      const open = $("liveOpen");
      open.classList.remove("hidden");
      open.href = d.dashboard;
    } else {
      wrap.classList.add("hidden");
      $("liveOpen").classList.add("hidden");
    }
    const verify = d.verify || {};
    if (verify.required && verify.url) {
      $("liveNote").innerHTML = `<a class="link" href="${esc(verify.url)}" target="_blank" rel="noopener">Verification required — open ↗</a>`;
    } else {
      $("liveNote").textContent = "Live download, comments and unsave progress for this account.";
    }
    } finally {
      liveBusy = false;
    }
  }

  function liveCriteria() {
    return UNSAVE_CRITERIA.map(([key]) => key).filter((key) => liveUnsaveCriteria[key]);
  }

  async function runDashJob(name, action) {
    if (!confirm(`${action === "pause" ? "Pause" : action === "start" ? "Start" : action} "${name}"?`)) return;
    try {
      const result = await api("/api/dashboard/job", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, action }),
      });
      if (!result.ok) alert(result.message || "Job did not start");
    } catch (err) {
      alert("Job request failed: " + err.message);
    }
    setTimeout(renderLive, 400);
  }

  async function runUnsave(action) {
    const selected = liveCriteria();
    if (!selected.length) {
      liveUnsavePreview = "Select at least one checkbox.";
      const node = $("dashUnsavePreview");
      if (node) node.textContent = liveUnsavePreview;
      return;
    }
    if (action === "preview") {
      liveUnsavePreview = "Validating…";
      const node = $("dashUnsavePreview");
      if (node) node.textContent = liveUnsavePreview;
      try {
        const result = await api("/api/dashboard/unsave/preview", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ criteria: selected.join(",") }),
        });
        if (result.error) liveUnsavePreview = "Error: " + result.error;
        else {
          const failures = Object.entries(result.failures || {})
            .map(([key, value]) => key + " " + value).join(" · ");
          liveUnsavePreview = `matched ${fmtInt(result.matched)} / ${fmtInt(result.scanned)} · ${failures || "all pass"}`;
        }
      } catch (_) {
        liveUnsavePreview = "Preview failed.";
      }
      const out = $("dashUnsavePreview");
      if (out) out.textContent = liveUnsavePreview;
      return;
    }
    if (action === "pause") {
      await runDashJob("unsave", "pause");
      return;
    }
    if (!confirm("Unsave matching posts using: " + selected.join(", ") + "?")) return;
    try {
      const result = await api("/api/dashboard/unsave/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ criteria: selected.join(",") }),
      });
      liveUnsavePreview = result.ok ? "Unsave started." : "Error: " + (result.error || "unknown");
    } catch (_) {
      liveUnsavePreview = "Unsave request failed.";
    }
    const out = $("dashUnsavePreview");
    if (out) out.textContent = liveUnsavePreview;
    setTimeout(renderLive, 600);
  }

  const livePanelEl = $("livePanel");
  if (livePanelEl) {
    livePanelEl.addEventListener("change", (e) => {
      const box = e.target.closest("input[data-crit]");
      if (box) {
        liveUnsaveCriteria[box.dataset.crit] = box.checked;
        localStorage.setItem(UNSAVE_KEY, JSON.stringify(liveUnsaveCriteria));
        return;
      }
      if (e.target.id === "dashLogSelect") {
        liveLogName = e.target.value;
        localStorage.setItem(LIVE_LOG_KEY, liveLogName);
        updateDashLog({ tail: [] });
      }
    });
    livePanelEl.addEventListener("click", (e) => {
      const job = e.target.closest("[data-dash-job]");
      if (job) {
        runDashJob(job.dataset.dashJob, job.dataset.dashAction);
        return;
      }
      const unsave = e.target.closest("[data-dash-unsave]");
      if (unsave) runUnsave(unsave.dataset.dashUnsave);
    });
  }

  function startLive() {
    renderLive();
    clearInterval(liveTimer);
    liveTimer = setInterval(() => { if (state.view === "live") renderLive(); }, 2000);
  }
  function stopLive() {
    clearInterval(liveTimer);
    liveTimer = null;
  }
  if ($("liveRefresh")) $("liveRefresh").onclick = renderLive;

  /* ---------------- cloud sync badge ---------------- */
  async function loadCloudStatus() {
    const el = $("syncState");
    if (!el || staticMode) return;
    try {
      const d = await api("/api/cloud");
      if (!d.enabled || d.provider === "none") { el.textContent = ""; return; }
      el.classList.toggle("err", !!d.last_error);
      el.innerHTML = `edits sync: <b>${esc(d.provider)}</b>${d.online ? " online" : " (local mirror)"}
        ${d.last_push ? " · " + esc(d.last_push) : ""}${d.last_error ? " · " + esc(d.last_error) : ""}`;
      el.title = d.online
        ? "Click to push now · shift-click to pull the online copy"
        : "Local mirror. Configure data/cloud.json with gist, github or d1 for online storage.";
      el.onclick = async (e) => {
        try {
          const result = await api("/api/cloud", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ op: e.shiftKey ? "pull" : "push" }),
          });
          if (e.shiftKey && result.applied) { await loadFacets(); reset(); }
          await loadCloudStatus();
        } catch (err) { alert("Sync failed: " + err.message); }
      };
    } catch (_) { el.textContent = ""; }
  }

  /* ---------------- UI wiring ---------------- */
  $("search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = e.target.value.trim();
      if (state.q) {
        state.owner = "";
        $("ownerFilter").classList.add("hidden");
        $("ownerName").textContent = "";
      }
      reset();
    }, 300);
  });
  $("sort").addEventListener("change", (e) => { state.sort = e.target.value; reset(); });
  $("clearTag").onclick = () => { state.tagStates.clear(); reset(); loadFacets(); };
  $("views").querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      state.view = btn.dataset.view;
      $("views").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      const live = state.view === "live";
      $("grid").classList.toggle("hidden", live);
      $("sentinel").classList.toggle("hidden", live);
      $("empty").classList.add("hidden");
      $("liveView").classList.toggle("hidden", !live);
      if (live) startLive();
      else { stopLive(); renderAll(); }
    };
  });
  $("closeDrawer").onclick = closeDrawer;
  $("scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeDrawer(); clearSelection(); }
  });

  const io = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && !state.done && state.view !== "live") load(false);
  }, { rootMargin: "600px" });
  io.observe($("sentinel"));

  (async () => {
    if (embedded) {
      document.body.classList.add("embed");
      const social = $("socialBar");
      if (social) social.remove();
    }
    const initialSource = state.source;
    // Paint the gallery immediately; the account switcher can load alongside.
    try { await loadFacets(); } catch (err) { console.error(err); }
    load(true);
    await loadAccounts();
    renderSocialBar();
    paintSourceLabels();
    if (!staticMode && state.source !== initialSource) {
      try { await loadFacets(); } catch (err) { console.error(err); }
      reset();
    }
    loadCloudStatus();
  })();
})();
