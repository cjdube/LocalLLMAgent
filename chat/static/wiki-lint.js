// The /wiki/lint view: renders the structural audit of the learnings vault.
// Included as <script src="/static/wiki-lint.js"></script>; like log-view.js and
// nav.js its contract is page-supplied mounts — <div id="lintSections">,
// <div id="lintSummary">, <input id="lintFilter">, and the two buttons named in
// bind() below. The page owns all CSS; this file emits structure and class names.
//
// Class names to style: .lint-section with .is-clean / .is-open, .lint-head,
// .lint-count, .lint-items, .lint-item, .lint-page, .lint-text, .lint-actions,
// .lint-peek, .lint-fixes, .lint-error.
//
// Everything user-visible goes in through textContent. A finding quotes page
// titles and citation strings written by a model out of web content, and a peek
// shows a wiki page verbatim, so there is no innerHTML anywhere in this file.
//
// Two rendering decisions worth keeping:
//
//   - Clean sections still render, collapsed, reading "0 — clean". The command
//     line prints only what it found, which leaves you unable to tell a check
//     that passed from a check that was never run.
//   - A finding's leading "<slug>.md" becomes the row's handle. Every check in
//     the sibling repo writes findings that way, so the slug is parseable
//     without the server having to send it separately — and it is what makes a
//     finding openable instead of merely readable.
(() => {
  // Sections whose findings apply_safe_fixes can act on. The button is pointless
  // when neither has anything, and offering it anyway invites a click that
  // writes to the vault and reports "no mechanical fixes needed".
  const FIXABLE_SECTIONS = ["Broken and self links", "Index integrity"];

  // Findings open with the page they are about: "orphan.md is an orphan — …".
  const LEADING_PAGE = /^([A-Za-z0-9._-]+)\.md\b/;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;   // textContent only
    return e;
  }

  function pageOf(finding) {
    const m = LEADING_PAGE.exec(finding || "");
    return m ? m[1] : null;
  }

  function countFindings(sections) {
    return Object.values(sections || {}).reduce((n, items) => n + items.length, 0);
  }

  function fixableCount(sections) {
    return FIXABLE_SECTIONS.reduce(
      (n, name) => n + ((sections && sections[name]) || []).length, 0);
  }

  // The filter is a plain substring match over the finding text, case-folded.
  // A section keeps only its matches; a section left with none is hidden
  // entirely rather than shown as clean, which would be a lie.
  function filterSections(sections, query) {
    const q = (query || "").trim().toLowerCase();
    if (!q) return sections;
    const out = {};
    for (const [name, items] of Object.entries(sections || {})) {
      const hits = items.filter((f) => f.toLowerCase().includes(q));
      if (hits.length) out[name] = hits;
    }
    return out;
  }

  function summaryText(result) {
    const n = countFindings(result.sections);
    const pages = `${result.pages} page${result.pages === 1 ? "" : "s"} checked`;
    if (!n) return `${pages} · no structural problems`;
    return `${pages} · ${n} finding${n === 1 ? "" : "s"}`;
  }

  // --- rendering ----------------------------------------------------------- //

  function renderItem(finding, onPeek) {
    const row = el("li", "lint-item");
    const slug = pageOf(finding);
    if (slug) {
      const handle = el("span", "lint-page", slug);
      row.appendChild(handle);
      row.appendChild(el("span", "lint-text", finding.slice(slug.length + 3).trim()));
      const actions = el("span", "lint-actions");
      const peek = el("button", "peek", "peek");
      peek.type = "button";
      const graph = el("a", "graph", "graph");
      graph.href = `/wiki?page=${encodeURIComponent(slug)}`;
      actions.appendChild(peek);
      actions.appendChild(graph);
      row.appendChild(actions);
      const pane = el("pre", "lint-peek");
      pane.hidden = true;
      row.appendChild(pane);
      peek.addEventListener("click", () => onPeek(slug, pane, peek));
    } else {
      row.appendChild(el("span", "lint-text", finding));
    }
    return row;
  }

  function renderSection(name, items, opts) {
    const box = el("section", "lint-section" + (items.length ? "" : " is-clean"));
    const head = el("button", "lint-head");
    head.type = "button";
    head.appendChild(el("span", "lint-name", name));
    head.appendChild(el("span", "lint-count", items.length ? String(items.length) : "0 — clean"));
    box.appendChild(head);

    const list = el("ul", "lint-items");
    items.forEach((f) => list.appendChild(renderItem(f, opts.onPeek)));
    box.appendChild(list);

    // Findings open, clean sections closed: what is wrong should be readable
    // without a click, and what is right only needs to be countable.
    const open = items.length > 0;
    box.classList.toggle("is-open", open);
    list.hidden = !open;
    head.addEventListener("click", () => {
      const nowOpen = !box.classList.contains("is-open");
      box.classList.toggle("is-open", nowOpen);
      list.hidden = !nowOpen;
    });
    return box;
  }

  function renderSections(mount, sections, opts) {
    mount.textContent = "";
    const entries = Object.entries(sections || {});
    if (!entries.length) {
      mount.appendChild(el("p", "lint-error", "Nothing matched that filter."));
      return;
    }
    entries.forEach(([name, items]) =>
      mount.appendChild(renderSection(name, items, opts)));
  }

  function renderFixes(mount, fixes) {
    mount.textContent = "";
    if (!fixes || !fixes.length) {
      mount.appendChild(el("p", null, "No mechanical fixes were needed."));
      return;
    }
    const list = el("ul");
    fixes.forEach((c) => list.appendChild(el("li", null, c)));
    mount.appendChild(el("p", null, `Applied ${fixes.length} fix${fixes.length === 1 ? "" : "es"}:`));
    mount.appendChild(list);
  }

  // --- page wiring --------------------------------------------------------- //

  function bind() {
    const summary = document.getElementById("lintSummary");
    const mount = document.getElementById("lintSections");
    const filter = document.getElementById("lintFilter");
    const recheck = document.getElementById("lintRecheck");
    const fixBtn = document.getElementById("lintFix");
    const fixLog = document.getElementById("lintFixLog");

    let current = null;   // the last good payload

    async function peek(slug, pane, button) {
      if (!pane.hidden) { pane.hidden = true; button.textContent = "peek"; return; }
      button.textContent = "…";
      let body;
      try {
        const resp = await fetch(`/api/wiki/page/${encodeURIComponent(slug)}`);
        const data = await resp.json();
        body = data.error ? `Could not read this page: ${data.error}` : data.content;
      } catch (e) {
        body = "Could not read this page.";
      }
      pane.textContent = body;
      pane.hidden = false;
      button.textContent = "hide";
    }

    function draw() {
      if (!current) return;
      renderSections(mount, filterSections(current.sections, filter.value), { onPeek: peek });
    }

    function show(result) {
      if (result.error) {
        current = null;
        summary.textContent = "";
        mount.textContent = "";
        mount.appendChild(el("p", "lint-error", result.error));
        fixBtn.hidden = true;
        return;
      }
      current = result;
      summary.textContent = summaryText(result);
      fixBtn.hidden = fixableCount(result.sections) === 0;
      draw();
    }

    async function load(url, opts) {
      summary.textContent = "Checking…";
      try {
        show(await (await fetch(url, opts)).json());
      } catch (e) {
        show({ error: "Could not reach the lint." });
      }
    }

    recheck.addEventListener("click", () => { fixLog.textContent = ""; load("/api/wiki/lint"); });
    filter.addEventListener("input", draw);

    fixBtn.addEventListener("click", async () => {
      const n = current ? fixableCount(current.sections) : 0;
      const ok = window.confirm(
        `Apply the safe fixes to ${n} finding${n === 1 ? "" : "s"}?\n\n` +
        "This writes to the vault. It strips self-links from wiki pages and " +
        "de-links dead entries in index.md. Nothing else is touched — orphans, " +
        "bad dates and invented citations are left for you.");
      if (!ok) return;
      fixBtn.disabled = true;
      await load("/api/wiki/lint/fix", { method: "POST" });
      if (current) renderFixes(fixLog, current.fixes);
      fixBtn.disabled = false;
    });

    load("/api/wiki/lint");
  }

  const api = { pageOf, countFindings, fixableCount, filterSections, summaryText,
                renderSection, renderSections, renderFixes, FIXABLE_SECTIONS };
  if (typeof window !== "undefined") window.WrenWikiLint = api;
  if (typeof document !== "undefined" && document.getElementById("lintSections")) bind();
})();
