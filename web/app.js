(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    q: "", category: "", tag: "", owner: "", sort: "manual", view: "grid",
    offset: 0, limit: 60, total: 0, items: [], loading: false, done: false,
    selected: new Set(),
    customCats: JSON.parse(localStorage.getItem("ig_custom_cats") || "[]"),
  };
  let dragIds = null;
  let saveTimer = null;
  let searchTimer = null;
  let rectStart = null;
  let rectMoved = false;
  let suppressClick = false;

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

  /* ---------------- facets / categories ---------------- */
  async function loadFacets() {
    const f = await api("/api/facets");
    $("total").textContent = f.total.toLocaleString();
    $("videos").textContent = f.videos.toLocaleString();
    window.__PRESETS = f.presets || [];

    const map = new Map();
    for (const c of f.categories) map.set(c.name, c.count);
    const adsCount = f.ads ?? map.get("ad") ?? 0;
    const cats = [
      { name: "ad", count: adsCount },
      ...state.customCats
        .filter((name) => name !== "ad")
        .map((name) => ({ name, count: map.get(name) || 0 })),
    ];

    $("cats").innerHTML = cats.map((c) => `
      <div class="cat ${state.category === c.name ? "on" : ""}" data-cat="${esc(c.name)}"
           title="Click to filter · drop posts to categorise">
        <span>${esc(c.name === "ad" ? "Ads (real Instagram ads)" : c.name)}</span>
        <b>${fmtNum(c.count)}</b>
      </div>`).join("");
    $("resetFilter").classList.toggle("hidden", !state.category);

    $("cats").querySelectorAll(".cat").forEach((el) => {
      el.onclick = () => {
        state.category = state.category === el.dataset.cat ? "" : el.dataset.cat;
        state.tag = "";
        reset();
        loadFacets();
      };
      el.addEventListener("dragover", (e) => {
        if (el.dataset.cat === "all") return;
        if (!dragIds && !state.selected.size) return;
        e.preventDefault();
        el.classList.add("over");
      });
      el.addEventListener("dragleave", () => el.classList.remove("over"));
      el.addEventListener("drop", async (e) => {
        e.preventDefault();
        el.classList.remove("over");
        if (el.dataset.cat === "all") return;
        const ids = dragIds && dragIds.length ? dragIds : [...state.selected];
        if (ids.length) await assignCategory(el.dataset.cat, ids);
      });
    });

    $("tags").innerHTML = f.tags.map((t) => `
      <span class="tag ${state.tag === t.name ? "on" : ""}" data-tag="${esc(t.name)}">#${esc(t.name)} <b>${fmtNum(t.count)}</b></span>
    `).join("") || '<span class="hint">No personal tags yet.</span>';
    $("tags").querySelectorAll(".tag").forEach((el) => {
      el.onclick = () => {
        state.tag = state.tag === el.dataset.tag ? "" : el.dataset.tag;
        reset();
        loadFacets();
      };
    });
    $("clearTag").classList.toggle("hidden", !state.tag);
    $("clearSel").classList.toggle("hidden", !state.selected.size);
  }

  $("resetFilter").onclick = () => { state.category = ""; reset(); loadFacets(); };
  $("addCat").onclick = () => {
    const name = prompt("New category name");
    if (!name) return;
    const clean = name.trim().toLowerCase();
    if (!clean) return;
    if (!state.customCats.includes(clean)) state.customCats.push(clean);
    localStorage.setItem("ig_custom_cats", JSON.stringify(state.customCats));
    loadFacets();
  };

  function setOwner(owner) {
    state.owner = owner || "";
    state.q = "";
    $("search").value = "";
    $("ownerFilter").classList.toggle("hidden", !owner);
    if (owner) {
      $("ownerName").textContent = "@" + owner;
      $("ownerName").href = "https://www.instagram.com/" + owner + "/";
    }
    reset();
  }
  $("clearOwner").onclick = () => setOwner("");

  async function assignCategory(category, ids) {
    if (!ids.length) return;
    await api("/api/bulk_category", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, category }),
    });
    ids.forEach((id) => {
      const item = state.items.find((x) => x.id === id);
      if (item) { item.category = category; item.confidence = 1; }
      const node = document.querySelector(`.card[data-id="${id}"]`);
      if (node) {
        const chip = node.querySelector(".catChip");
        if (category) { chip.textContent = category + " 100%"; chip.classList.add("on"); }
        else chip.classList.remove("on");
      }
    });
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
    const params = new URLSearchParams({
      offset: state.offset, limit: state.limit, q: state.q,
      category: state.category, tag: state.tag, sort: state.sort, owner: state.owner,
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

  function mediaOf(item) {
    if (item.media && item.media.length) return item.media;
    return [{ i: 0, kind: item.is_video ? "video" : "image", thumb: item.thumb, url: `/api/media/${item.id}/0` }];
  }

  function updateMedia(node, item, index) {
    const media = mediaOf(item);
    const safe = Math.max(0, Math.min(index, media.length - 1));
    node._mediaIndex = safe;
    const current = media[safe];
    const img = node.querySelector(".thumb");
    const vid = node.querySelector(".cardVideo");
    const play = node.querySelector(".play");
    vid.hidden = true;
    vid.pause();
    vid.removeAttribute("src");
    img.hidden = false;
    img.src = current.thumb;
    play.style.display = current.kind === "video" ? "block" : "none";
    node.classList.toggle("multi", media.length > 1);
    const dots = node.querySelector(".dots");
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
    updateMedia(node, item, ((node._mediaIndex || 0) + delta + media.length) % media.length);
  }

  function playCardVideo(node) {
    const item = node._item;
    if (!item) return;
    const media = mediaOf(item);
    const current = media[node._mediaIndex || 0];
    if (!current || current.kind !== "video") return;
    const img = node.querySelector(".thumb");
    const vid = node.querySelector(".cardVideo");
    img.hidden = true;
    vid.hidden = false;
    vid.src = current.url;
    vid.controls = true;
    vid.autoplay = true;
    vid.muted = true;
    vid.play().catch(() => {});
    node.querySelector(".play").style.display = "none";
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
    owner.textContent = item.owner ? "@" + item.owner : "unknown";
    owner.href = item.owner_url || (item.owner ? `https://www.instagram.com/${item.owner}/` : "#");
    owner.onclick = (e) => { e.stopPropagation(); if (item.owner) setOwner(item.owner); };

    const box = node.querySelector(".pickBox");
    box.checked = state.selected.has(item.id);
    box.onclick = (e) => e.stopPropagation();
    box.onchange = () => toggleSelect(item.id, node, box.checked);

    const caption = node.querySelector(".caption");
    caption.textContent = item.caption || "";

    node.querySelector(".likes").textContent = item.likes ? "♥ " + fmtNum(item.likes) : "";
    node.querySelector(".date").textContent = fmtDate(item.taken_at);

    const chip = node.querySelector(".catChip");
    if (item.category) {
      const pct = item.confidence ? ` ${Math.round(item.confidence * 100)}%` : "";
      chip.textContent = item.category + pct;
      chip.classList.add("on");
    }

    node.querySelector(".cardTags").innerHTML =
      (item.tags || []).slice(0, 3).map((t) => `<span>#${esc(t)}</span>`).join("");

    node.querySelector(".play").onclick = (e) => { e.stopPropagation(); playCardVideo(node); };
    node.querySelector(".prev").onclick = (e) => { e.stopPropagation(); changeMedia(node, -1); };
    node.querySelector(".next").onclick = (e) => { e.stopPropagation(); changeMedia(node, 1); };

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
          || e.target.closest(".play") || e.target.closest(".cardVideo")
          || e.target.closest("input")) return;
      openPost(item.id);
    });

    attachDrag(node);
    return node;
  }

  function appendCards(items) {
    const grid = $("grid");
    items.forEach((item) => {
      const node = cardEl(item);
      grid.appendChild(node);
    });
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
      setTimeout(() => { suppressClick = false; }, 200);
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", JSON.stringify(dragIds)); } catch (_) {}
    });
    node.addEventListener("dragend", () => {
      node.classList.remove("drag");
      document.querySelectorAll(".card.over").forEach((n) => n.classList.remove("over"));
      dragIds = null;
      updateSelectBar();
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
    const catOptions = ["", ...(window.__PRESETS || [...new Set([...state.customCats, "other"])])]
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

    const owner = post.owner
      ? `<a href="https://www.instagram.com/${esc(post.owner)}/" target="_blank" rel="noopener">@${esc(post.owner)}</a>`
      : "unknown";
    const pct = post.category_confidence ? ` · ${Math.round(post.category_confidence * 100)}%` : "";
    const folderLink = post.folder
      ? `<a href="${esc(post.permalink)}" target="_blank" rel="noopener">${esc(post.shortcode)}</a>`
      : esc(post.shortcode);

    $("drawerBody").innerHTML = `
      ${media}
      <div class="dTitle">${owner}</div>
      <div class="dMeta">
        <span>${fmtDate(post.taken_at)}</span>
        <span>♥ ${fmtNum(post.likes)}</span>
        <span>${esc(post.typename || "")}</span>
        ${post.category ? `<span>${esc(post.category)}${pct}</span>` : ""}
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
        <span>${folderLink}</span>
        <span>${esc(post.folder || "")}</span>
        <a href="${esc(post.permalink)}" target="_blank" rel="noopener">post URL ↗</a>
        <button class="btn small" id="openFolder" title="Open in file manager">Open folder</button>
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

    if ($("openFolder")) {
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
    if (item.category) {
      const pct = item.confidence ? ` ${Math.round(item.confidence * 100)}%` : "";
      chip.textContent = item.category + pct;
      chip.classList.add("on");
    } else chip.classList.remove("on");
    node.querySelector(".cardTags").innerHTML =
      (item.tags || []).slice(0, 3).map((t) => `<span>#${esc(t)}</span>`).join("");
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
  $("clearTag").onclick = () => { state.tag = ""; reset(); loadFacets(); };
  $("views").querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      state.view = btn.dataset.view;
      $("views").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      const live = state.view === "live";
      $("grid").classList.toggle("hidden", live);
      $("sentinel").classList.toggle("hidden", live);
      $("empty").classList.add("hidden");
      $("liveView").classList.toggle("hidden", !live);
      if (!live) renderAll();
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
    try {
      const f = await api("/api/facets");
      window.__PRESETS = f.presets || [];
    } catch (_) { window.__PRESETS = []; }
    await loadFacets();
    load(true);
  })();
})();
