/**
 * Tests for chat/static/wiki-lint.js — the /wiki/lint view's rendering.
 *
 * Like log-view.js it's a plain <script> in an IIFE, so the source is re-run
 * with `new Function` per test. Pure helpers are exported on window.WrenWikiLint;
 * the page wiring is covered through the mounts at the bottom.
 *
 * Two things here are worth more than the rest. Clean sections must still
 * render — the command line prints only what it found, which is why you cannot
 * tell a check that passed from one that never ran. And findings are model-
 * written text quoting page titles and citations, so they must arrive as text
 * nodes: a finding containing markup must not become markup.
 */

const fs = require("fs");
const path = require("path");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "wiki-lint.js"), "utf8");

const SECTIONS = {
  "Broken and self links": ["a.md links to itself — remove the self-link."],
  "Orphan pages": ["lonely.md is an orphan — no other page links to it."],
  "Page format": [],
  "Duplicate titles": [],
};

function payload(extra = {}) {
  return { vault: "/v", pages: 388, sections: SECTIONS, fixes: [], ...extra };
}

function load() {
  new Function(SOURCE)();
  return window.WrenWikiLint;
}

function mountPage() {
  document.body.innerHTML = `
    <input id="lintFilter">
    <button id="lintRecheck"></button>
    <button id="lintFix" hidden></button>
    <div id="lintSummary"></div>
    <div id="lintFixLog"></div>
    <div id="lintSections"></div>`;
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  delete window.WrenWikiLint;
  document.body.innerHTML = "";
  delete global.fetch;
});

// --- parsing the finding text ---------------------------------------------- //

describe("pageOf", () => {
  test("pulls the slug out of a finding that opens with one", () => {
    expect(load().pageOf("olloma-thread-wedges.md is titled '# Ollama…'"))
      .toBe("olloma-thread-wedges");
  });

  test("handles a dated log slug", () => {
    expect(load().pageOf("daily-chrome-2026-08-16.md is an orphan."))
      .toBe("daily-chrome-2026-08-16");
  });

  test("returns null for a finding that names two pages first", () => {
    // check_duplicate_titles writes "a.md and b.md share the title …" — the
    // leading token is still a page, so this one DOES parse. The null case is a
    // finding that opens with prose.
    expect(load().pageOf("index.md links to [[gone]], which has no page")).toBe("index");
    expect(load().pageOf("Something went wrong")).toBeNull();
  });
});

describe("countFindings / fixableCount", () => {
  test("counts across sections", () => {
    expect(load().countFindings(SECTIONS)).toBe(2);
  });

  test("fixable counts only the sections apply_safe_fixes acts on", () => {
    const api = load();
    expect(api.fixableCount(SECTIONS)).toBe(1);      // the self-link, not the orphan
    expect(api.FIXABLE_SECTIONS).toEqual(["Broken and self links", "Index integrity"]);
  });

  test("a vault with only judgment calls has nothing to fix", () => {
    expect(load().fixableCount({ "Orphan pages": ["a.md is an orphan."] })).toBe(0);
  });
});

describe("filterSections", () => {
  test("an empty query passes everything through", () => {
    expect(load().filterSections(SECTIONS, "  ")).toBe(SECTIONS);
  });

  test("matches case-insensitively and drops sections left empty", () => {
    const out = load().filterSections(SECTIONS, "ORPHAN");
    expect(Object.keys(out)).toEqual(["Orphan pages"]);
  });

  test("a filter hides clean sections rather than showing them as clean", () => {
    const out = load().filterSections(SECTIONS, "self-link");
    expect(Object.keys(out)).toEqual(["Broken and self links"]);
  });
});

describe("summaryText", () => {
  test("says so plainly when the vault is clean", () => {
    expect(load().summaryText({ pages: 388, sections: { "Orphan pages": [] } }))
      .toBe("388 pages checked · no structural problems");
  });

  test("counts findings, singular and plural", () => {
    const api = load();
    expect(api.summaryText({ pages: 388, sections: SECTIONS })).toBe("388 pages checked · 2 findings");
    expect(api.summaryText({ pages: 1, sections: { a: ["x.md bad"] } })).toBe("1 page checked · 1 finding");
  });
});

// --- rendering -------------------------------------------------------------- //

describe("renderSections", () => {
  test("renders a clean section, collapsed, so you can see it was checked", () => {
    const api = load();
    const mount = document.createElement("div");
    api.renderSections(mount, SECTIONS, { onPeek: () => {} });

    const clean = [...mount.querySelectorAll(".lint-section.is-clean")];
    expect(clean.map((s) => s.querySelector(".lint-name").textContent))
      .toEqual(["Page format", "Duplicate titles"]);
    expect(clean[0].querySelector(".lint-count").textContent).toBe("0 — clean");
    expect(clean[0].querySelector(".lint-items").hidden).toBe(true);
  });

  test("sections with findings start open", () => {
    const api = load();
    const mount = document.createElement("div");
    api.renderSections(mount, SECTIONS, { onPeek: () => {} });
    const dirty = mount.querySelector(".lint-section:not(.is-clean)");
    expect(dirty.classList.contains("is-open")).toBe(true);
    expect(dirty.querySelector(".lint-items").hidden).toBe(false);
  });

  test("clicking a head toggles its list", () => {
    const api = load();
    const mount = document.createElement("div");
    api.renderSections(mount, SECTIONS, { onPeek: () => {} });
    const section = mount.querySelector(".lint-section");
    section.querySelector(".lint-head").click();
    expect(section.querySelector(".lint-items").hidden).toBe(true);
  });

  test("a finding becomes a slug handle plus its text, with a graph link", () => {
    const api = load();
    const mount = document.createElement("div");
    api.renderSections(mount, { S: ["lonely.md is an orphan — no other page links to it."] },
                       { onPeek: () => {} });
    const item = mount.querySelector(".lint-item");
    expect(item.querySelector(".lint-page").textContent).toBe("lonely");
    expect(item.querySelector(".lint-text").textContent)
      .toBe("is an orphan — no other page links to it.");
    expect(item.querySelector("a.graph").getAttribute("href")).toBe("/wiki?page=lonely");
  });

  test("a finding is text, never markup", () => {
    // Findings quote page titles and citations a model wrote out of fetched web
    // content. Rendering one as HTML would be an injection with a straight path
    // from a scraped page to this view.
    const api = load();
    const mount = document.createElement("div");
    api.renderSections(mount, { S: ["a.md cites '<img src=x onerror=alert(1)>', which is not a file"] },
                       { onPeek: () => {} });
    expect(mount.querySelector("img")).toBeNull();
    expect(mount.querySelector(".lint-text").textContent).toContain("<img src=x");
  });

  test("an over-narrow filter says nothing matched", () => {
    const api = load();
    const mount = document.createElement("div");
    api.renderSections(mount, {}, { onPeek: () => {} });
    expect(mount.textContent).toBe("Nothing matched that filter.");
  });
});

describe("renderFixes", () => {
  test("lists what was written", () => {
    const api = load();
    const mount = document.createElement("div");
    api.renderFixes(mount, ["a.md: removed 1 self-link", "index.md: de-linked 2 dead links"]);
    expect(mount.textContent).toContain("Applied 2 fixes");
    expect([...mount.querySelectorAll("li")].map((li) => li.textContent))
      .toEqual(["a.md: removed 1 self-link", "index.md: de-linked 2 dead links"]);
  });

  test("says so when there was nothing mechanical to do", () => {
    const api = load();
    const mount = document.createElement("div");
    api.renderFixes(mount, []);
    expect(mount.textContent).toBe("No mechanical fixes were needed.");
  });
});

// --- page wiring ------------------------------------------------------------ //

describe("the page", () => {
  test("loads findings and shows the fix button only when something is fixable", async () => {
    mountPage();
    global.fetch = jest.fn(async () => ({ json: async () => payload() }));
    load();
    await flush();

    expect(document.getElementById("lintSummary").textContent)
      .toBe("388 pages checked · 2 findings");
    expect(document.getElementById("lintFix").hidden).toBe(false);
    expect(global.fetch).toHaveBeenCalledWith("/api/wiki/lint", undefined);
  });

  test("hides the fix button when every finding needs judgment", async () => {
    mountPage();
    global.fetch = jest.fn(async () => ({
      json: async () => payload({ sections: { "Orphan pages": ["a.md is an orphan."] } }),
    }));
    load();
    await flush();
    expect(document.getElementById("lintFix").hidden).toBe(true);
  });

  test("a broken lint repo shows its error instead of a blank page", async () => {
    mountPage();
    global.fetch = jest.fn(async () => ({
      json: async () => ({ error: "wiki_lint.py not found (check WREN_WIKI_LINT_ROOT)" }),
    }));
    load();
    await flush();
    expect(document.querySelector(".lint-error").textContent).toContain("not found");
    expect(document.getElementById("lintFix").hidden).toBe(true);
  });

  test("an unreachable server is reported, not swallowed", async () => {
    mountPage();
    global.fetch = jest.fn(async () => { throw new Error("offline"); });
    load();
    await flush();
    expect(document.querySelector(".lint-error").textContent).toBe("Could not reach the lint.");
  });

  test("typing in the filter redraws without re-fetching", async () => {
    mountPage();
    global.fetch = jest.fn(async () => ({ json: async () => payload() }));
    load();
    await flush();

    const filter = document.getElementById("lintFilter");
    filter.value = "orphan";
    filter.dispatchEvent(new Event("input"));

    expect([...document.querySelectorAll(".lint-name")].map((n) => n.textContent))
      .toEqual(["Orphan pages"]);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  test("re-check re-fetches", async () => {
    mountPage();
    global.fetch = jest.fn(async () => ({ json: async () => payload() }));
    load();
    await flush();
    document.getElementById("lintRecheck").click();
    await flush();
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  test("declining the confirm writes nothing", async () => {
    mountPage();
    global.fetch = jest.fn(async () => ({ json: async () => payload() }));
    window.confirm = () => false;
    load();
    await flush();
    document.getElementById("lintFix").click();
    await flush();
    expect(global.fetch).toHaveBeenCalledTimes(1);            // the initial load only
  });

  test("confirming POSTs and reports what was written", async () => {
    mountPage();
    const calls = [];
    global.fetch = jest.fn(async (url, opts) => {
      calls.push([url, opts]);
      return { json: async () => payload({ fixes: ["a.md: removed 1 self-link"] }) };
    });
    window.confirm = () => true;
    load();
    await flush();
    document.getElementById("lintFix").click();
    await flush();

    expect(calls[1]).toEqual(["/api/wiki/lint/fix", { method: "POST" }]);
    expect(document.getElementById("lintFixLog").textContent)
      .toContain("a.md: removed 1 self-link");
  });

  test("peek reads the page and toggles closed again", async () => {
    mountPage();
    global.fetch = jest.fn(async (url) => ({
      json: async () => (url.startsWith("/api/wiki/page/")
        ? { name: "lonely", content: "# Lonely\n\nfull text" }
        : payload({ sections: { "Orphan pages": ["lonely.md is an orphan."] } })),
    }));
    load();
    await flush();

    const button = document.querySelector(".lint-actions button");
    button.click();
    await flush();
    const pane = document.querySelector(".lint-peek");
    expect(pane.hidden).toBe(false);
    expect(pane.textContent).toBe("# Lonely\n\nfull text");
    expect(button.textContent).toBe("hide");

    button.click();
    expect(pane.hidden).toBe(true);
    expect(button.textContent).toBe("peek");
  });
});
