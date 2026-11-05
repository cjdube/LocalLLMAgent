// The /wiki/review view: every vault page sorted into groups, marked by hand.
// Included as <script src="/static/wiki-review.js"></script>; like wiki-lint.js
// it holds no data, because /static has no auth check. The data comes from
// GET /api/wiki/review. Summaries come from web sources, so every string from
// the API goes through esc() before it reaches innerHTML.
(() => {
  const FAMILY_NOTES = {
    "Dated logs": "A cut here removes old daily pages only. New pages still arrive each day.",
    "Your own projects": "Pages here describe your own work. Other pages link to many of them, so look at the links number before you cut.",
  };

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => n.toLocaleString("en-US");

  // A page override wins over its group's verdict.
  function isCut(mark, name) {
    const override = ((mark || {}).pages || {})[name];
    return override ? override === "cut" : (mark || {}).verdict === "cut";
  }

  function cutList(groups, marks) {
    return groups.flatMap((g) => g.pages.filter((n) => isCut(marks[g.id], n)));
  }

  window.WrenWikiReview = { esc, isCut, cutList };

  const list = document.getElementById("list");
  if (!list) return;                                    // degrade, don't throw

  let DATA = { groups: [] }, P = {}, marks = {}, biggest = 1;
  let filter = "all", query = "";
  const open = new Set();
  const queues = {};

  const mark = (id) => marks[id] || { verdict: null, pages: {} };
  const isThin = (n) => P[n][2] <= 1 && P[n][3] <= 1;
  const matches = (name) => !query || name.toLowerCase().includes(query) || P[name][0].toLowerCase().includes(query);

  function pageRow(g, name) {
    const [summary, , sources, links, date] = P[name];
    const cut = isCut(marks[g.id], name);
    return `<div class="p ${cut ? "is-cut" : ""}">
      <input type="checkbox" data-g="${esc(g.id)}" data-p="${esc(name)}" ${cut ? "checked" : ""} aria-label="Cut ${esc(name)}">
      <div><div class="n">${esc(name)}</div><div class="s">${esc(summary)}</div></div>
      <div class="m">${sources} src · ${links} links · ${esc(date)}</div>
    </div>`;
  }

  function groupHtml(g) {
    const m = mark(g.id);
    const shown = query ? g.pages.filter(matches) : g.pages;
    const words = g.pages.reduce((a, n) => a + P[n][1], 0);
    const newest = g.pages.reduce((a, n) => (P[n][4] > a ? P[n][4] : a), "");
    const thin = g.family === "Dated logs" ? "" : `<span>${g.pages.filter(isThin).length} thin</span>`;
    const overrides = Object.keys(m.pages || {}).length;
    const isOpen = open.has(g.id) || !!query;
    const v = (key, label) => `<button class="v ${key}" type="button" data-g="${esc(g.id)}" data-v="${key}" aria-pressed="${m.verdict === key}">${label}</button>`;
    return `<article class="group" data-group="${esc(g.id)}">
      <div class="top">
        <div><h3>${esc(g.title)}</h3><p class="line">${esc(g.line)}</p></div>
        <div class="votes" role="group" aria-label="Mark ${esc(g.title)}">${v("keep", "Keep")}${v("unsure", "Not sure")}${v("cut", "Don't care")}</div>
      </div>
      <div class="size" aria-hidden="true"><i style="width:${(g.pages.length / biggest * 100).toFixed(1)}%"></i></div>
      <div class="meta"><span>${g.pages.length} pages</span><span>${num(words)} words</span>${thin}<span>newest ${esc(newest)}</span>${overrides ? `<span>${overrides} set by hand</span>` : ""}</div>
      <button class="more" type="button" data-more="${esc(g.id)}" aria-expanded="${isOpen}">${isOpen ? "Hide pages" : "Show pages"}</button>
      ${isOpen ? `<div class="pages">${shown.map((n) => pageRow(g, n)).join("")}</div>` : ""}
    </article>`;
  }

  function visible(g) {
    const verdict = mark(g.id).verdict;
    if (filter === "none" && verdict) return false;
    if (!["all", "none"].includes(filter) && verdict !== filter) return false;
    if (query && !g.pages.some(matches) && !g.title.toLowerCase().includes(query)) return false;
    return true;
  }

  function renderList() {
    const families = [...new Set(DATA.groups.map((g) => g.family))];
    let html = "";
    for (const fam of families) {
      const groups = DATA.groups.filter((g) => g.family === fam && visible(g));
      if (!groups.length) continue;
      html += `<h2>${esc(fam)}</h2>`;
      if (FAMILY_NOTES[fam]) html += `<p class="fam-note">${esc(FAMILY_NOTES[fam])}</p>`;
      html += groups.map(groupHtml).join("");
    }
    list.innerHTML = html || `<p class="empty">No groups match. Clear the search or pick "All".</p>`;
  }

  function renderTally() {
    const cut = cutList(DATA.groups, marks);
    document.getElementById("t-marked").textContent = DATA.groups.filter((g) => mark(g.id).verdict).length;
    document.getElementById("t-pages").textContent = num(cut.length);
    document.getElementById("t-words").textContent = num(cut.reduce((a, n) => a + P[n][1], 0));
  }

  // Re-rendering replaces the row under the finger, so put focus and each
  // open list's scroll position back where they were.
  function rerender() {
    const a = document.activeElement;
    const key = a && a.dataset ? { g: a.dataset.g, v: a.dataset.v, p: a.dataset.p, more: a.dataset.more } : null;
    const tops = new Map([...document.querySelectorAll(".pages")]
      .map((el) => [el.closest(".group").dataset.group, el.scrollTop]));
    renderList();
    renderTally();
    for (const el of document.querySelectorAll(".pages")) {
      const top = tops.get(el.closest(".group").dataset.group);
      if (top !== undefined) el.scrollTop = top;
    }
    if (!key || !(key.g || key.more)) return;
    const candidates = key.more ? document.querySelectorAll(".more")
      : key.v ? document.querySelectorAll(".v") : document.querySelectorAll(".p input");
    const el = [...candidates].find((c) => (key.more ? c.dataset.more === key.more
      : c.dataset.g === key.g && (key.v ? c.dataset.v === key.v : c.dataset.p === key.p)));
    if (el) el.focus();
  }

  function setStatus(text, isErr) {
    const s = document.getElementById("status");
    s.textContent = text;
    s.className = isErr ? "err" : "";
  }

  // One queue per group, so two quick taps on a group reach the server in order.
  function save(id) {
    const body = JSON.stringify(mark(id));
    queues[id] = (queues[id] || Promise.resolve()).then(async () => {
      const resp = await fetch(`/api/wiki/review/marks/${encodeURIComponent(id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (!resp.ok) throw new Error(`status ${resp.status}`);
      setStatus("Saved.");
    }).catch((e) => setStatus(`The last mark did not save (${e.message}). Reload this page, then mark it again.`, true));
  }

  async function copyCutList() {
    const names = cutList(DATA.groups, marks);
    if (!names.length) { setStatus("No pages are marked to cut."); return; }
    try {
      await navigator.clipboard.writeText(names.join("\n"));
      setStatus(`Copied ${num(names.length)} page names.`);
    } catch (e) {
      setStatus("Your browser did not allow the copy.", true);
    }
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.id === "copyCut") {
      copyCutList();
    } else if (b.dataset.f) {
      filter = b.dataset.f;
      document.querySelectorAll(".chip[data-f]").forEach((c) => c.setAttribute("aria-pressed", c === b));
      rerender();
    } else if (b.dataset.more) {
      const id = b.dataset.more;
      if (open.has(id)) open.delete(id); else open.add(id);
      rerender();
    } else if (b.dataset.v) {
      const id = b.dataset.g, m = mark(id);
      marks[id] = { verdict: m.verdict === b.dataset.v ? null : b.dataset.v, pages: m.pages || {} };
      rerender();
      save(id);
    }
  });

  document.addEventListener("change", (e) => {
    const box = e.target;
    if (!box.dataset || !box.dataset.p) return;
    const id = box.dataset.g, name = box.dataset.p, m = mark(id);
    const pages = { ...(m.pages || {}) };
    // A tick that agrees with the group's verdict is no override at all.
    if (box.checked === (m.verdict === "cut")) delete pages[name];
    else pages[name] = box.checked ? "cut" : "keep";
    marks[id] = { verdict: m.verdict || null, pages };
    rerender();
    save(id);
  });

  let timer;
  document.getElementById("q").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { query = e.target.value.trim().toLowerCase(); renderList(); }, 150);
  });

  (async () => {
    try {
      const resp = await fetch("/api/wiki/review");
      const body = await resp.json();
      if (!resp.ok || body.error) throw new Error(body.error || `status ${resp.status}`);
      DATA = body;
      P = body.pages;
      marks = body.marks;
      biggest = Math.max(1, ...DATA.groups.map((g) => g.pages.length));
    } catch (e) {
      setStatus(`The vault could not be loaded (${e.message}).`, true);
      return;
    }
    document.getElementById("t-groups").textContent = DATA.groups.length;
    document.getElementById("lede").textContent =
      `Your vault has ${num(Object.keys(P).length)} pages, sorted into ${DATA.groups.length} groups, so you can make one choice for many pages at a time.`;
    if (DATA.sorted_at) {
      document.getElementById("fine").textContent += ` Groups last sorted ${DATA.sorted_at}. A newer page shows in Unsorted.`;
    }
    setStatus("Your marks save as you go.");
    renderList();
    renderTally();
  })();
})();
