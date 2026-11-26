// Chinese reading shown right under every stored review, wherever the review appears.
// Pages call ReviewZh.markup(review) next to the original text; a page-wide watcher then asks the server for the
// Chinese of every placeholder on screen (cached ones come back at once, missing ones are translated ten at a time).
(() => {
  const cache = new Map(), failed = new Map();
  let busy = false, timer = null;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  const join = item => [item.title_zh, item.zh].filter(Boolean).join("\n\n");
  const usable = id => typeof id === "string" && /^[0-9a-f]{16}$/.test(id);

  // review: {id, text, title?, zh?}. A review without a library id shows only the Chinese it already carries.
  function markup(review, className = "") {
    if (!review || (!String(review.text || "").trim() && !String(review.title || "").trim())) return "";
    const cls = `review-zh${className ? ` ${className}` : ""}`;
    const id = review.id;
    if (!usable(id)) return review.zh ? `<p class="${cls}" lang="zh-CN">${esc(review.zh)}</p>` : "";
    if (cache.has(id)) return cache.get(id) ? `<p class="${cls}" lang="zh-CN" data-review-zh="${esc(id)}">${esc(cache.get(id))}</p>` : "";
    if (review.zh && (!review.title || review.title_zh)) { cache.set(id, join(review)); return `<p class="${cls}" lang="zh-CN" data-review-zh="${esc(id)}">${esc(join(review))}</p>`; }
    if (review.zh && !review.title) return `<p class="${cls}" lang="zh-CN" data-review-zh="${esc(id)}">${esc(review.zh)}</p>`;
    if (window.WorkbenchGuest) return "";
    const error = failed.get(id);
    schedule();
    return `<p class="${cls}${error ? " is-failed" : " is-pending"}" lang="zh-CN" data-review-zh="${esc(id)}"${error ? "" : " data-review-zh-pending"}>${error ? failedMarkup(error) : "中文翻译中…"}</p>`;
  }

  const failedMarkup = error => `中文翻译没成功${error ? `（${esc(error)}）` : ""} <button type="button" class="subtle compact" data-review-zh-retry>重试</button>`;

  function paint(id) {
    document.querySelectorAll(`[data-review-zh="${id}"]`).forEach(node => {
      if (cache.has(id)) {
        const text = cache.get(id);
        node.classList.remove("is-pending", "is-failed");
        node.removeAttribute("data-review-zh-pending");
        if (text) node.textContent = text; else node.remove();
      } else if (failed.has(id)) {
        node.classList.remove("is-pending");
        node.classList.add("is-failed");
        node.removeAttribute("data-review-zh-pending");
        node.innerHTML = failedMarkup(failed.get(id));
      }
    });
  }

  function schedule(delay = 30) {
    if (timer || busy) return;
    timer = setTimeout(() => { timer = null; void drain(); }, delay);
  }

  async function drain() {
    if (busy || typeof api !== "function") return;
    const ids = [...new Set([...document.querySelectorAll("[data-review-zh-pending]")].map(node => node.dataset.reviewZh))]
      .filter(id => usable(id) && !failed.has(id));
    ids.forEach(id => { if (cache.has(id)) paint(id); });
    const wanted = ids.filter(id => !cache.has(id)).slice(0, 200);
    if (!wanted.length) return;
    busy = true;
    let again = false;
    try {
      const result = await api("/api/reviews/translations/lookup", { method: "POST", body: { ids: wanted } });
      Object.entries(result.items || {}).forEach(([id, item]) => { cache.set(id, join(item)); paint(id); });
      (result.failed || []).forEach(id => { failed.set(id, result.error || ""); paint(id); });
      const pending = new Set(result.pending || []);
      // Not in the review library any more: nothing to translate, drop the placeholder.
      wanted.forEach(id => { if (!cache.has(id) && !failed.has(id) && !pending.has(id)) { cache.set(id, ""); paint(id); } });
      again = pending.size > 0;
    } catch (error) {
      wanted.forEach(id => { failed.set(id, error.message || ""); paint(id); });
    } finally {
      busy = false;
    }
    // New placeholders may have appeared while this request ran, and the rest of the page still needs translating.
    if (again || document.querySelector("[data-review-zh-pending]")) schedule(again ? 30 : 300);
  }

  document.addEventListener("click", event => {
    const button = event.target.closest?.("[data-review-zh-retry]");
    if (!button) return;
    const node = button.closest("[data-review-zh]");
    const id = node?.dataset.reviewZh;
    if (!id) return;
    failed.delete(id);
    document.querySelectorAll(`[data-review-zh="${id}"]`).forEach(item => {
      item.classList.remove("is-failed");
      item.classList.add("is-pending");
      item.setAttribute("data-review-zh-pending", "");
      item.textContent = "中文翻译中…";
    });
    schedule(0);
  });

  // The Chinese already on this page for a review, for searches that look at it too.
  window.ReviewZh = { markup, text: id => cache.get(id) || "", refresh: () => schedule(0) };
})();
