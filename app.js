(() => {
  "use strict";
  const cfg = window.SAVED_HUB || { backends: {}, titles: {}, subtitles: {} };
  const $ = (id) => document.getElementById(id);
  const routes = [
    { id: "instagram", label: "Instagram", icon: "instagram" },
    { id: "rednote", label: "RedNote", icon: "rednote" },
    { id: "threads", label: "Threads", icon: "threads" },
    { id: "twitter", label: "X / Twitter", icon: "twitter" },
    { id: "reddit", label: "Reddit", icon: "reddit" },
  ];

  const icon = (name) => {
    if (name === "instagram") {
      return `<svg viewBox="0 0 24 24"><rect x="3.2" y="3.2" width="17.6" height="17.6" rx="5"/><circle cx="12" cy="12" r="4.1"/><circle cx="17.2" cy="6.8" r="1.1" fill="currentColor" stroke="none"/></svg>`;
    }
    if (name === "rednote") {
      return `<svg viewBox="0 0 24 24"><path d="M6 3.8h12v16.4l-6-3.6-6 3.6z"/><path d="M9.5 8.5h5M9.5 12h5"/></svg>`;
    }
    if (name === "threads") {
      return `<svg viewBox="0 0 24 24"><path d="M17.5 8.2c-.5-2.7-2.3-4.2-5.2-4.2-3.4 0-5.8 2.4-5.8 8s2.4 8 6 8c2.8 0 4.6-1.5 4.6-3.6 0-2-1.6-3.4-4.1-3.4-2.3 0-3.7 1.2-3.7 2.8 0 1.4 1.1 2.4 2.7 2.4 2.2 0 3.6-1.5 3.9-4"/><path d="M6.5 6.5h11"/></svg>`;
    }
    if (name === "twitter") {
      return `<svg viewBox="0 0 24 24"><path d="M4 4l16 16M20 4L4 20"/></svg>`;
    }
    if (name === "telegram") {
      return `<svg viewBox="0 0 24 24"><path d="M21 4 3 11l5 2 2 6 3-4 5 3z"/><path d="m8 13 8-6"/></svg>`;
    }
    if (name === "reddit") {
      return `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.6"/><circle cx="9.2" cy="12.6" r="1" fill="currentColor" stroke="none"/><circle cx="14.8" cy="12.6" r="1" fill="currentColor" stroke="none"/><path d="M9 15.4c.9.8 1.9 1.1 3 1.1s2.1-.3 3-1.1"/><path d="M12 3.4v3.1M12 3.4l4-1.1"/></svg>`;
    }
    return `<svg viewBox="0 0 24 24"><path d="M4 20V9M10 20V4M16 20v-7M22 20H2"/></svg>`;
  };

  const siteBase = new URL(
    cfg.siteBase || window.SAVED_BASE_URL || ".", location.href).href;
  const params = new URLSearchParams(location.search);
  const sitePath = new URL(siteBase).pathname.replace(/\/+$/, "");
  const relPath = sitePath && location.pathname.startsWith(sitePath)
    ? location.pathname.slice(sitePath.length) : location.pathname;
  const path = relPath.replace(/\/+$/, "") || "/";
  const pathId = path === "/" ? "" : path.replace(/^\/+/, "").split("/")[0];
  const routeIds = routes.map((r) => r.id);
  const platformToRoute = {};
  Object.entries(cfg.platforms || {}).forEach(([route, platform]) => {
    platformToRoute[platform] = route;
  });
  // GitHub Pages serves this site from /instagram-saved-post/, so path based
  // routes never reach the app. Query routing (?platform=...) works anywhere.
  const platformParam = params.get("platform") || "";
  const sourceParam = params.get("source") || "";
  const active = routeIds.includes(platformParam) ? platformParam
    : routeIds.includes(pathId) ? pathId
      : (platformToRoute[sourceParam] && routeIds.includes(platformToRoute[sourceParam]))
        ? platformToRoute[sourceParam] : "instagram";
  const baseRaw = (cfg.backends || {})[active] ||
    (active === "telegram" ? "http://127.0.0.1:8799/" : "about:blank");
  const base = new URL(baseRaw, siteBase).href;
  const platform = (cfg.platforms || {})[active] || null;
  const defaultSource = (cfg.defaultSources || {})[active] || "";
  let source = params.get("source") || defaultSource;

  $("socialBar").innerHTML = routes.map((r) => `
    <a class="acct ${r.id === active ? "on" : ""}" href="?platform=${r.id}" title="${r.label}">
      ${icon(r.icon)}<span class="railLabel">${r.label}</span>
    </a>`).join("") +
    `<span class="spacer"></span>
     <a href="?platform=${active}" title="Reload hub route" onclick="location.reload();return false;">
       <svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>
       <span class="railLabel">Reload</span>
     </a>`;

  document.title = `${(cfg.titles || {})[active] || "Saved Archive"} · Saved Archive`;
  $("hubTitle").textContent = (cfg.titles || {})[active] || active;
  $("hubSub").textContent = (cfg.subtitles || {})[active] || "";

  const frame = $("hubFrame");
  const open = $("hubOpen");
  let accountSelect = null;

  function targetUrl(src) {
    if (!src) return base;
    if (!platform) return base;
    const sep = base.includes("?") ? "&" : "?";
    // The combined view must stay inside this platform, so it is passed
    // through explicitly; per-account sources already imply it.
    const extra = src === "all" ? `&platform=${encodeURIComponent(platform)}` : "";
    return `${base}${sep}source=${encodeURIComponent(src)}${extra}`;
  }

  function applySource(src) {
    source = src || defaultSource;
    frame.src = targetUrl(source);
    open.href = frame.src;
    const url = new URL(location.href);
    if (source) url.searchParams.set("source", source);
    else url.searchParams.delete("source");
    history.replaceState(null, "", url.pathname + url.search);
  }

  if (platform) {
    accountSelect = document.createElement("select");
    accountSelect.id = "hubAccount";
    accountSelect.className = "hubSelect";
    accountSelect.title = "Switch account";
    $("hubActions").prepend(accountSelect);
    accountSelect.onchange = () => applySource(
      accountSelect.value === "__all__" ? "all" : accountSelect.value);
  }

  applySource(source);
  if (platform && cfg.accountsApi) {
    fetch(new URL(cfg.accountsApi, siteBase).href, { cache: "no-store" })
      .then((res) => res.ok ? res.json() : null)
      .then((data) => {
        if (!data || !Array.isArray(data.accounts)) return;
        const options = data.accounts.filter((a) =>
          String(a.platform || "").toLowerCase() === platform);
        if (!options.length) { accountSelect.classList.add("hidden"); return; }
        const combinedTotal = options.reduce(
          (sum, a) => sum + (Number(a.total) || 0), 0);
        const allOption = options.length > 1
          ? `<option value="__all__">All accounts · ${combinedTotal.toLocaleString()}</option>`
          : "";
        // Instagram rows are the archive accounts themselves, so show the
        // handle exactly like the local hub; other platforms keep their label.
        const optionName = (a) => String(a.platform || "").toLowerCase() === "instagram"
          ? (a.handle || a.id) : (a.label || a.id);
        accountSelect.innerHTML = allOption + options.map((a) => `
          <option value="${String(a.id).replace(/"/g, "&quot;")}">
            ${optionName(a)}${a.total != null ? " · " + Number(a.total).toLocaleString() : ""}
          </option>`).join("");
        const isAll = source === "all" && allOption;
        if (!isAll && !options.some((a) => a.id === source)) source = options[0].id;
        accountSelect.value = isAll ? "__all__" : source;
        applySource(source);
      })
      .catch(() => { accountSelect.classList.add("hidden"); });
  }

  $("hubReload").onclick = () => {
    const src = frame.src;
    frame.src = "about:blank";
    setTimeout(() => { frame.src = src; }, 60);
  };

  /* ---------------- last updated ---------------- */
  // A quiet "last updated" replaces the old export/import/cloud-sync buttons:
  // backups are automatic, the timestamp is the only thing worth showing.
  const relTime = (value) => {
    if (!value) return "";
    const then = typeof value === "number" ? value
      : Date.parse(value);
    if (!then || isNaN(then)) return "";
    const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (secs < 5) return "just now";
    if (secs < 60) return secs + "s ago";
    const mins = Math.round(secs / 60);
    if (mins < 60) return mins + "m ago";
    const hours = Math.round(mins / 60);
    if (hours < 24) return hours + "h ago";
    return new Date(then).toLocaleDateString(undefined,
      { month: "short", day: "numeric" });
  };

  const stateApi = (() => {
    if (cfg.stateApi) return new URL(cfg.stateApi, siteBase).href;
    if (!cfg.accountsApi) return "";
    try {
      const api = new URL(cfg.accountsApi, siteBase);
      if (api.pathname.endsWith("/api/accounts")) {
        return api.origin + "/api/cloud";
      }
    } catch (_) {}
    // The static site build has no local API; its personal state lives in the
    // public Worker document, whose updated_at is the online timestamp.
    if (location.protocol === "https:") {
      return "https://ig-archive-state.hheylau.workers.dev/?id=default";
    }
    return "";
  })();

  let lastStateAt = 0;
  const paintUpdated = () => {
    const el = $("hubUpdated");
    if (!el) return;
    el.textContent = lastStateAt ? "updated " + relTime(lastStateAt) : "";
    el.title = lastStateAt
      ? "Personal state last changed " + new Date(lastStateAt).toLocaleString()
      : "Personal state updates automatically";
  };

  async function loadLastUpdated() {
    if (!stateApi) { paintUpdated(); return; }
    try {
      const res = await fetch(stateApi, { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const stamp = data.last_updated
        || (data.doc && (data.doc.updated_at || data.updated_at))
        || data.updated_at || "";
      const parsed = typeof stamp === "number" ? stamp : Date.parse(stamp);
      if (parsed) lastStateAt = Math.max(lastStateAt, parsed);
      paintUpdated();
    } catch (_) {}
  }
  loadLastUpdated();
  setInterval(loadLastUpdated, 20000);
  setInterval(paintUpdated, 5000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadLastUpdated();
  });
})();
