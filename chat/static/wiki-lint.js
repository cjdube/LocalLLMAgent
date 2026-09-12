// The /wiki/lint view: renders the structural audit of the learnings vault.
// Included as <script src="/static/wiki-lint.js"></script>; like log-view.js and
// nav.js its contract is page-supplied mounts — <div id="lintSections">,
// <div id="lintSummary">, <input id="lintFilter">, and the two buttons named in
// bind() below. The page owns all CSS; this file emits structure and class names.
//
// Class names to style: .lint-section with .is-clean / .is-open, .lint-head,
// .lint-count, .lint-help with .lint-help-what / .lint-help-fix, .lint-items,
// .lint-item, .lint-page, .lint-text, .lint-actions, .lint-peek, .lint-fixes,
// .lint-error.
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
  // when none of them has anything, and offering it anyway invites a click that
  // writes to the vault and reports "no mechanical fixes needed".
  //
  // "Escaped text" belongs here and was missing until 2026-09-12: the sibling's
  // apply_safe_fixes has decoded it since 2026-08-21, so a vault whose only
  // damage was escaped text hid the one button that would have repaired it.
  // Keep this list equal to the three fixes that function applies, no fewer.
  const FIXABLE_SECTIONS = ["Broken and self links", "Index integrity", "Escaped text"];

  // What each check looks for and what to do about a hit, shown in a collapsed
  // expander on the category card. The categories themselves are owned by the
  // sibling repo (~/Projects/ObsidianWikiAgent, wiki_lint.py:structural_findings),
  // so this map can fall behind it: a name with no entry here renders no
  // expander rather than an empty one, and tests/wiki-lint.test.js pins the ten
  // that exist today. Wording is grounded in each check_* docstring over there.
  const HELP = {
    "Broken and self links": {
      what: "A [[wiki-link]] points at a page that does not exist, or a page links to itself.",
      fix: "Create the missing page, correct the spelling, or drop the link. " +
           "Self-links are mechanical — \u201cApply safe fixes\u201d strips them for you.",
    },
    "Orphan pages": {
      what: "Nothing links to this page. Being listed in index.md does not count — a table " +
            "of contents is not the same as being reachable from related work. Dated logs are exempt.",
      fix: "Link it from a page it belongs beside, or decide it never earned its own page " +
           "and fold the material into one that did.",
    },
    "Index integrity": {
      what: "index.md and the vault disagree: a page missing from the index, an index link to a " +
            "page that was deleted, a section heading written twice, or pages in the Unfiled " +
            "backlog. Unfiled should always be zero, so any count at all is drift.",
      fix: "Dead index links are mechanical — \u201cApply safe fixes\u201d de-links them. A twin " +
           "heading or an Unfiled backlog means a heading was edited by hand; restore it and re-file.",
    },
    "Source coverage": {
      what: "A dated source still in raw/ is marked ingested but produced no dated page of its own, " +
            "which every dated capture is required to earn.",
      fix: "Treat it as material that may have been lost. Read the source, find what never " +
           "reached the vault, and write the dated page.",
    },
    "Page format": {
      what: "The page breaks the format RULES.md requires — the title only repeats the slug, a date " +
            "is a placeholder or in the future, or a citation names a source file that does not exist.",
      fix: "Titles and dates are a hand edit. An invented citation means the model made the source " +
           "up: check the claim against a real source, or take it out.",
    },
    "Misspelled slugs": {
      what: "The filename is one character off its own title — a page titled \u2018Ollama Thread " +
            "Wedges\u2019 filed as olloma-thread-wedges.md. No search for the real spelling finds it.",
      fix: "Rename the file to the right slug, then repoint every link to it, index.md included.",
    },
    "Duplicate titles": {
      what: "Two pages carry the same title, which makes them one page written twice.",
      fix: "Merge them, keep the better slug, and repoint the links. Two pages on one concept under " +
           "different titles are a judgment call and are left to the --deep pass instead.",
    },
    "Template twins": {
      what: "Two pages are character-identical apart from what names them — one template filled in twice.",
      fix: "Decide which one is real. The other is either a mistake or a page that was never " +
           "actually written.",
    },
    "Lens integrity": {
      what: "The page describes itself as an evaluation lens but no longer carries the " +
            "`lens: true` frontmatter marker that makes it one, so evaluate_against cannot use it.",
      fix: "Put `lens: true` back in the frontmatter, or reword the page if it is not a lens any more.",
    },
    "Escaped text": {
      what: "JSON escaping was left in the prose as literal text — a backslash where a quote belongs, " +
            "\\u2019 where a curly apostrophe does. Obsidian renders the backslashes.",
      fix: "Mechanical — \u201cApply safe fixes\u201d decodes it back into the prose it " +
           "damaged. That one rewrites a page body, so read the change log it prints.",
    },
  };

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

  // The category's own explanation. Closed by default, and its open state is
  // owned by the caller: draw() rebuilds every section on each filter keystroke,
  // so an expander that tracked its own state would snap shut as you type.
  function renderHelp(name, opts) {
    const text = HELP[name];
    if (!text) return null;
    const open = (opts && opts.openHelp) || null;
    const box = el("details", "lint-help");
    box.appendChild(el("summary", null, "What does this mean?"));
    box.appendChild(el("p", "lint-help-what", text.what));
    box.appendChild(el("p", "lint-help-fix", text.fix));
    if (open) {
      box.open = open.has(name);
      box.addEventListener("toggle", () => {
        if (box.open) open.add(name); else open.delete(name);
      });
    }
    return box;
  }

  function renderSection(name, items, opts) {
    const box = el("section", "lint-section" + (items.length ? "" : " is-clean"));
    const head = el("button", "lint-head");
    head.type = "button";
    head.appendChild(el("span", "lint-name", name));
    head.appendChild(el("span", "lint-count", items.length ? String(items.length) : "0 — clean"));
    box.appendChild(head);

    // Below the head, not inside it: .lint-head is a <button> and a <details>
    // cannot nest in one. Clean sections get the expander too — what a passing
    // check guards is the thing you most need it to tell you.
    const help = renderHelp(name, opts);
    if (help) box.appendChild(help);

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

    let current = null;               // the last good payload
    const openHelp = new Set();       // which explanations the reader has opened

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
      renderSections(mount, filterSections(current.sections, filter.value),
                     { onPeek: peek, openHelp });
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
        "This writes to the vault. It strips self-links from wiki pages, " +
        "de-links dead entries in index.md, and decodes escaped text back into " +
        "the prose it damaged — that last one rewrites the body of a page you " +
        "wrote. Nothing else is touched: orphans, bad dates and invented " +
        "citations are left for you.");
      if (!ok) return;
      fixBtn.disabled = true;
      await load("/api/wiki/lint/fix", { method: "POST" });
      if (current) renderFixes(fixLog, current.fixes);
      fixBtn.disabled = false;
    });

    load("/api/wiki/lint");
  }

  const api = { pageOf, countFindings, fixableCount, filterSections, summaryText,
                renderSection, renderSections, renderFixes, FIXABLE_SECTIONS, HELP };
  if (typeof window !== "undefined") window.WrenWikiLint = api;
  if (typeof document !== "undefined" && document.getElementById("lintSections")) bind();
})();
