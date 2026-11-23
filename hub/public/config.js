/* Saved Archive hub backend map.
 *
 * Local defaults point at the local services. For a workers.dev deployment,
 * copy this file with the public backend URLs (Render/Fly/VPS/Cloudflare
 * Tunnel) and deploy the public/ folder as Workers Assets.
 *
 * The gallery URLs deliberately omit `source`; the hub account selector adds
 * it from accountsApi. `?embed=1` hides the gallery's own social rail.
 */
window.SAVED_HUB = {
  accountsApi: "http://127.0.0.1:8770/api/accounts",
  backends: {
    instagram: "http://127.0.0.1:8770/?embed=1",
    rednote: "http://127.0.0.1:8770/rednote?embed=1",
    threads: "http://127.0.0.1:8770/?embed=1",
    twitter: "http://127.0.0.1:8770/?embed=1",
    telegram: "http://127.0.0.1:8799/",
    progress: "http://127.0.0.1:8766/",
  },
  platforms: {
    instagram: "instagram",
    rednote: "rednote",
    threads: "threads",
    twitter: "twitter",
    telegram: null,
    progress: null,
  },
  defaultSources: {
    instagram: "",
    rednote: "rednote-main",
    threads: "threads",
    twitter: "twitter",
  },
  titles: {
    instagram: "Instagram saved",
    rednote: "RedNote saved",
    threads: "Threads saved",
    twitter: "X / Twitter saved",
    telegram: "Telegram archive",
    progress: "Downloader progress",
  },
  subtitles: {
    instagram: "standard gallery · filters · categories · live",
    rednote: "same gallery UI · RedNote source",
    threads: "same gallery UI · Threads source",
    twitter: "same gallery UI · X source",
    telegram: "message archive · existing Telegram UI",
    progress: "standalone downloader dashboard",
  },
};
