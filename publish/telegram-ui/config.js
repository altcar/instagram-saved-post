/* Runtime configuration for the statically hosted Telegram UI.
 *
 * The UI is on GitHub Pages; the messages and media are read from the
 * tg-archiver FastAPI server on your own computer. Start it with:
 *
 *     cd tg-archiver && ./run.sh 8799
 *
 * Change the URL with ?local_api=http://127.0.0.1:8799 or by setting
 * localStorage tg_api_base.
 */
(() => {
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("local_api") || params.get("api");
  if (fromQuery) localStorage.setItem("tg_api_base", fromQuery);
  window.TG_API_BASE = localStorage.getItem("tg_api_base") || "http://127.0.0.1:8799";
  window.TG_LOCAL_ONLY = params.get("online") !== "1";
})();
