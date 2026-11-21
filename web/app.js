(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    q: "", category: "all", tag: "", sort: "manual", view: "grid",
    offset: 0, limit: 60, total: 0, items: [], loading: false, done: false,
  };
  let dragId = null;
  let saveTimer = null;
  let searchTimer = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const fmtNum = (n) => new Intl.NumberFormat(undefined, { notation: "compact" }).format(n || 0);
  const fmtDate = (s) => {
    if (!s) return "";
    const d = new Date(s);
    return isNaN(d) ? s : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };

  async function api(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  /* ---------------- facets ---------------- */
  async function loadFacets() {
    const f = await api("/api/facets");
    $("total").textContent = f.total.toLocaleString();
    $("videos").textContent = f.videos.toLocaleString();

    const cats = [{ name: "all", count: f.total }, ...f.categories];
    $("cats").innerHTML = cats.map((c) => `
      <div class="cat ${state.category === c.name ? "on" : ""}" data-cat="${esc(c.name)}">
        <span>${esc(c.name === "all" ? "All posts" : c.name)}</span><b>${fmtNum(c.count)}</b>
      </div>`).join("");
    $("cats").querySelectorAll(".cat").forEach((el) => {
      el.onclick = () => { state.category = el.dataset.cat; state.tag = ""; reset(); loadFacets(); };
    });

    $("tags").innerHTML = f.tags.map((t) => `
      <span class="tag ${state.tag === t.name ? "on" : ""}" data-tag="${esc(t.name)}">#${esc(t.name)} <b>${fmtNum(t.count)}</b></span>
    `).join("") || '<span class="hint">No personal tags yet.</span>';
    $("tags").querySelectorAll(".tag").forEach((el) => {
      el.onclick = () => {
        state.tag = state.tag === el.dataset.tag ? "" : el.dataset.tag;
        reset(); loadFacets();
      };
    });
    $("clearTag").classList.toggle("hidden", !state.tag);
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
    const params = new URLSearchParams({
      offset: state.offset, limit: state.limit, q: state.q,
      category: state.category, tag: state.tag, sort: state.sort,
    });
    try {
      const data = await api("/api/posts?" + params);
      state.total = data.total;
      state.items.push(...data.items);
      state.offset += data.items.length;
      state.done = state.offset >= data.total || data.items.length === 0;
      appendCards(data.items);
      $("count").textContent = `${state.total.toLocaleString()} post${state.total === 1 ? "" : "s"}`;
      $("shown").textContent = state.items.length.toLocaleString();
      $("empty").classList.toggle("hidden", state.total !== 0);
      $("grid").classList.toggle("manual", state.sort === "manual" && state.view !== "post");
      $("hint").style.visibility = state.sort === "manual" ? "visible" : "hidden";
    } catch (e) {
      console.error(e);
    } finally {
      state.loading = false;
    }
  }

  function cardEl(item) {
    const node = $("cardTpl").content.firstElementChild.cloneNode(true);
    node.dataset.id = item.id;
    node.classList.toggle("video", item.is_video);
    node.classList.toggle("notes", !!item.notes);
    node.querySelector(".thumb").src = item.thumb;
    node.querySelector(".thumb").alt = item.caption || item.shortcode;
    node.querySelector(".owner").textContent = item.owner ? "@" + item.owner : "unknown";
    node.querySelector(".caption").textContent = item.caption || "";
    node.querySelector(".likes").textContent = item.likes ? "♥ " + fmtNum(item.likes) : "";
    node.querySelector(".date").textContent = fmtDate(item.taken_at);
    const chip = node.querySelector(".catChip");
    if (item.category) { chip.textContent = item.category; chip.classList.add("on"); }
    const tags = node.querySelector(".cardTags");
    tags.innerHTML = (item.tags || []).slice(0, 3).map((t) => `<span>#${esc(t)}</span>`).join("");
    node.onclick = () => openPost(item.id);
    return node;
  }

  function appendCards(items) {
    const grid = $("grid");
    items.forEach((item) => {
      const node = cardEl(item);
      if (state.view === "list") node.classList.add("listCard");
      grid.appendChild(node);
      attachDrag(node);
    });
  }

  function renderAll() {
    const grid = $("grid");
    grid.className = "grid " + state.view + (state.sort === "manual" && state.view !== "post" ? " manual" : "");
    grid.innerHTML = "";
    state.items.forEach((item) => {
      const node = cardEl(item);
      grid.appendChild(node);
      attachDrag(node);
    });
    $("grid").classList.toggle("manual", state.sort === "manual" && state.view !== "post");
  }

  /* ---------------- drag reorder ---------------- */
  function attachDrag(node) {
    if (state.sort !== "manual") return;
    node.draggable = true;
    node.addEventListener("dragstart", (e) => {
      dragId = Number(node.dataset.id);
      node.classList.add("drag");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", String(dragId)); } catch (_) {}
    });
    node.addEventListener("dragend", () => {
      node.classList.remove("drag");
      document.querySelectorAll(".card.over").forEach((n) => n.classList.remove("over"));
    });
    node.addEventListener("dragover", (e) => {
      if (dragId == null) return;
      e.preventDefault();
      node.classList.add("over");
    });
    node.addEventListener("dragleave", () => node.classList.remove("over"));
    node.addEventListener("drop", async (e) => {
      e.preventDefault();
      node.classList.remove("over");
      const targetId = Number(node.dataset.id);
      if (!dragId || dragId === targetId) return;
      await api("/api/reorder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: dragId, before_id: targetId }),
      });
      dragId = null;
      reset();
    });
  }

  /* ---------------- detail drawer ---------------- */
  async function openPost(id) {
    const post = await api(`/api/post/${id}`);
    renderDrawer(post);
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    $("scrim").classList.remove("hidden");
  }

  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("scrim").classList.add("hidden");
  }

  function renderDrawer(post) {
    $("dPermalink").href = post.permalink;
    const tags = (post.tags || []).map((t) => `<span>#${esc(t)}</span>`).join("") || '<span class="hint">none</span>';
    const catOptions = ["", ...(window.__CATEGORIES || [])]
      .map((c) => `<option value="${esc(c)}" ${post.category === c ? "selected" : ""}>${esc(c || "— uncategorised —")}</option>`)
      .join("");

    let media = "";
    if (post.media.length) {
      media = `<div class="dGallery" id="gallery">` + post.media.map((m, i) => {
        if (m.kind === "video") {
          return `<video class="gItem" data-i="${i}" controls playsinline preload="metadata"
            poster="${post.cover}" src="${m.url}" muted ${i === 0 ? "autoplay" : ""}></video>`;
        }
        return `<img class="gItem" data-i="${i}" src="${m.url}" alt="">`;
      }).join("") + `</div>`;
      if (post.media.length > 1) {
        media += `<div class="gNav">
          <button class="btn" id="gPrev">‹</button>
          <span class="gCount" id="gCount">1 / ${post.media.length}</span>
          <button class="btn" id="gNext">›</button>
        </div>`;
      }
    }

    $("drawerBody").innerHTML = `
      ${media}
      <div class="dTitle">@${esc(post.owner || "unknown")}</div>
      <div class="dMeta">
        <span>${fmtDate(post.taken_at)}</span>
        <span>♥ ${fmtNum(post.likes)}</span>
        <span>${esc(post.typename || "")}</span>
        ${post.category ? `<span>${esc(post.category)}${post.category_confidence ? " · " + Math.round(post.category_confidence * 100) + "%" : ""}</span>` : ""}
        ${post.location ? `<span>📍 ${esc(post.location)}</span>` : ""}
        <span>${post.media.length} file${post.media.length === 1 ? "" : "s"}</span>
      </div>
      <div class="dCaption">${esc(post.caption)}</div>

      <div class="dSection">Category <span id="savedCat" class="saved">saved</span></div>
      <div class="dField"><select id="catSelect">${catOptions}</select></div>

      <div class="dSection">Notes <span id="savedNotes" class="saved">saved</span></div>
      <div class="dField"><textarea id="notes" placeholder="Why did you save this? What to use it for…">${esc(post.notes)}</textarea></div>

      <div class="dSection">Personal tags <span id="savedTags" class="saved">saved</span></div>
      <div class="dTags" id="dTags">${tags}</div>
      <div class="dField" style="margin-top:8px">
        <input id="tagInput" placeholder="comma separated, e.g. lighting, tutorial, watch-later" value="${esc((post.tags || []).join(", "))}">
      </div>

      <div class="dSection">Comments <button class="btn" id="loadComments">Load comments</button></div>
      <div id="comments"><span class="hint">Loaded on demand to keep browsing light.</span></div>

      <div class="dSection">Details</div>
      <div class="dMeta">
        <span>${esc(post.shortcode)}</span>
        <span>${esc(post.folder || "")}</span>
        ${post.vision_caption ? `<span>vision: ${esc(post.vision_caption)}</span>` : ""}
      </div>
    `;

    const gallery = $("gallery");
    if (gallery) {
      const items = [...gallery.querySelectorAll(".gItem")];
      const count = $("gCount");
      const setActive = () => {
        const i = Math.min(items.length - 1,
          Math.round(gallery.scrollLeft / Math.max(1, gallery.clientWidth)));
        if (count) count.textContent = `${i + 1} / ${items.length}`;
        items.forEach((el, j) => {
          if (el.tagName === "VIDEO") {
            if (j === i) el.play().catch(() => {});
            else el.pause();
          }
        });
      };
      gallery.addEventListener("scroll", () => {
        clearTimeout(gallery.__t);
        gallery.__t = setTimeout(setActive, 120);
      });
      if ($("gPrev")) $("gPrev").onclick = () =>
        gallery.scrollBy({ left: -gallery.clientWidth, behavior: "smooth" });
      if ($("gNext")) $("gNext").onclick = () =>
        gallery.scrollBy({ left: gallery.clientWidth, behavior: "smooth" });
      setActive();
      const first = items[0];
      if (first && first.tagName === "VIDEO") first.play().catch(() => {});
    }

    $("catSelect").onchange = async (e) => {
      await savePost(post.id, { category: e.target.value });
      flash("savedCat");
      updateLocal(post.id, { category: e.target.value });
    };

    $("notes").oninput = () => {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(async () => {
        await savePost(post.id, { notes: $("notes").value });
        flash("savedNotes");
        updateLocal(post.id, { notes: $("notes").value });
      }, 700);
    };

    const saveTags = async () => {
      const tags = $("tagInput").value.split(",").map((t) => t.trim()).filter(Boolean);
      await savePost(post.id, { tags });
      flash("savedTags");
      $("dTags").innerHTML = tags.map((t) => `<span>#${esc(t)}</span>`).join("") || '<span class="hint">none</span>';
      updateLocal(post.id, { tags });
      loadFacets();
    };
    $("tagInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveTags(); } });
    $("tagInput").addEventListener("blur", saveTags);

    $("loadComments").onclick = async () => {
      $("loadComments").disabled = true;
      const full = await api(`/api/post/${post.id}?comments=1`);
      const list = full.comments || [];
      $("comments").innerHTML = list.length
        ? list.map((c) => `<div class="comment"><b>@${esc(c.owner || "?")}</b>
            <span class="when">${fmtDate(c.created_at)}</span><br>${esc(c.text || "")}</div>`).join("")
        : '<span class="hint">No comments stored for this post.</span>';
    };
  }

  async function savePost(id, payload) {
    await api(`/api/post/${id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
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
    const chip = node.querySelector(".catChip");
    if (item.category) { chip.textContent = item.category; chip.classList.add("on"); }
    else chip.classList.remove("on");
    node.querySelector(".cardTags").innerHTML =
      (item.tags || []).slice(0, 3).map((t) => `<span>#${esc(t)}</span>`).join("");
  }

  /* ---------------- UI wiring ---------------- */
  $("search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = e.target.value.trim(); reset(); }, 300);
  });
  $("sort").addEventListener("change", (e) => { state.sort = e.target.value; reset(); });
  $("clearTag").onclick = () => { state.tag = ""; reset(); loadFacets(); };
  $("views").querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      state.view = btn.dataset.view;
      $("views").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      renderAll();
    };
  });
  $("closeDrawer").onclick = closeDrawer;
  $("scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  const io = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && !state.done) load(false);
  }, { rootMargin: "600px" });
  io.observe($("sentinel"));

  (async () => {
    try {
      const f = await api("/api/facets");
      window.__CATEGORIES = f.presets || [];
    } catch (_) { window.__CATEGORIES = []; }
    await loadFacets();
    addEventListener("resize", () => {});
    load(true);
  })();
})();
