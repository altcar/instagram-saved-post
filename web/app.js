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
      { name: "", label: "All posts", count: f.total },
      { name: "ad", label: "Ads (real Instagram ads)", count: adsCount },
      ...state.customCats
        .filter((name) => name !== "ad")
        .map((name) => ({ name, label: name, count: map.get(name) || 0 })),
    ];

    $("cats").innerHTML = cats.map((c) => `
      <div class="cat ${state.category === c.name ? "on" : ""}" data-cat="${esc(c.name)}"
           title="${c.name ? "Click to filter · drop posts to categorise" : "Show every saved post"}">
        <span>${esc(c.label)}</span>
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
      if (item) {
        item.categories = [...new Set([...(item.categories || []), category])];
        item.category = item.category || category;
        item.confidence = 1;
      }
      const node = document.querySelector(`.card[data-id="${id}"]`);
      if (node && item) paintCategoryChip(node, item);
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
    delete vid.dataset.mediaUrl;
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
    // Return to the first media (the cover image), even if a carousel slide
    // or a video was showing when the card was clicked.
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
    if (!current || current.kind !== "video") return;
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
    // The button stays on top of the playing clip. Pressing it again restarts
    // from 0:00 instead of doing nothing.
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
      owner.href = item.owner_url || `https://www.instagram.com/${item.owner}/`;
      owner.onclick = (e) => { e.stopPropagation(); setOwner(item.owner); };
    } else {
      // Do not use a shortcode or the word "post" as a fake handle.
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
    caption.textContent = item.caption || "";

    node.querySelector(".likes").textContent = item.likes ? "♥ " + fmtNum(item.likes) : "";
    node.querySelector(".date").textContent = fmtDate(item.taken_at);

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
      // Clicking anywhere else, including a playing video, opens the detail
      // drawer and sends the card back to its cover image.
      resetCardMedia(node);
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
    const post = await api(`/api/post/${id}`);
    renderDrawer(post);
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    $("scrim").classList.remove("hidden");
  }

  function closeDrawer() {
    document.querySelectorAll("#gallery video").forEach((video) => video.pause());
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("scrim").classList.add("hidden");
  }

  function renderDrawer(post) {
    $("dPermalink").href = post.permalink;
    const tags = (post.tags || []).map((t) => `<span>#${esc(t)}</span>`).join("") || '<span class="hint">none</span>';
    const postCats = Array.isArray(post.categories) && post.categories.length
      ? post.categories
      : (post.category ? [post.category] : []);
    const catOptions = ["", ...(window.__PRESETS || [...new Set([...state.customCats, "other"])])]
      .map((c) => `<option value="${esc(c)}">${esc(c || "— add category —")}</option>`)
      .join("");

    let media = "";
    if (post.media.length) {
      media = `<div class="dGalleryWrap">
        <div class="dGallery" id="gallery">` + post.media.map((m, i) => {
        if (m.kind === "video") {
          return `<video class="gItem" data-i="${i}" data-kind="video" controls playsinline
            webkit-playsinline preload="metadata" poster="${post.cover}" src="${m.url}" muted></video>`;
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
    }

    const owner = post.owner
      ? `<a href="https://www.instagram.com/${esc(post.owner)}/" target="_blank" rel="noopener">@${esc(post.owner)}</a>`
      : '<span class="hint">creator unavailable</span>';
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

      <div class="dSection">Categories <span id="savedCat" class="saved">saved</span></div>
      <div class="dTags" id="postCats">
        ${postCats.map((c) => `<span>${esc(c)} <button class="catRemove" data-cat="${esc(c)}" title="Remove">×</button></span>`).join("") || '<span class="hint">none</span>'}
      </div>
      <div class="dField" style="margin-top:8px"><select id="catSelect">${catOptions}</select></div>

      <div class="dSection">Notes <span id="savedNotes" class="saved">saved</span></div>
      <div class="dField"><textarea id="notes" placeholder="Why did you save this? What to use it for…">${esc(post.notes)}</textarea></div>

      <div class="dSection">Personal tags <span id="savedTags" class="saved">saved</span></div>
      <div class="dTags" id="dTags">${tags}</div>
      <div class="dField" style="margin-top:8px">
        <input id="tagInput" placeholder="comma separated, e.g. lighting, tutorial, watch-later" value="${esc((post.tags || []).join(", "))}">
      </div>

      <div class="dSection">Details</div>
      <div class="dMeta">
        <span>${folderLink}</span>
        <span>${esc(post.folder || "")}</span>
        <a href="${esc(post.permalink)}" target="_blank" rel="noopener">post URL ↗</a>
        <button class="btn small" id="openFolder" title="Open in file manager">Open folder</button>
        ${post.vision_caption ? `<span>vision: ${esc(post.vision_caption)}</span>` : ""}
      </div>

      <div class="dSection">Comments <button class="btn small" id="loadComments">Refresh</button></div>
      <div id="comments"><span class="hint">Loading comments…</span></div>
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
          // Every press starts the current clip again from the beginning.
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

      // Wait for the drawer to get its real width before deciding which slide
      // is active and starting the first video.
      requestAnimationFrame(() => {
        setActive();
        playActiveVideo();
      });
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
          loadFacets();
        };
      });
    };
    paintPostCats();

    $("catSelect").onchange = async (e) => {
      const value = e.target.value;
      if (!value) return;
      if (!postCats.includes(value)) postCats.push(value);
      await savePost(post.id, { categories: postCats });
      flash("savedCat");
      updateLocal(post.id, { categories: [...postCats], category: postCats[0] || null });
      paintPostCats();
      e.target.value = "";
      loadFacets();
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

    const loadComments = async () => {
      if ($("loadComments")) $("loadComments").disabled = true;
      $("comments").innerHTML = '<span class="hint">Loading comments…</span>';
      try {
        const full = await api(`/api/post/${post.id}?comments=1`);
        const list = full.comments || [];
        $("comments").innerHTML = list.length
          ? list.map((c) => `<div class="comment"><b>@${esc(c.owner || "?")}</b>
              <span class="when">${fmtDate(c.created_at)}</span><br>${esc(c.text || "")}</div>`).join("")
          : '<span class="hint">No comments stored for this post.</span>';
      } catch (_) {
        $("comments").innerHTML = '<span class="hint">Could not load comments.</span>';
      } finally {
        if ($("loadComments")) $("loadComments").disabled = false;
      }
    };
    $("loadComments").onclick = loadComments;
    loadComments();
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
    paintCategoryChip(node, item);
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
