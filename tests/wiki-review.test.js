/**
 * Tests for chat/static/wiki-review.js — the /wiki/review view.
 *
 * Like wiki-lint.js it's a plain <script> in an IIFE, so the source is re-run
 * with `new Function` per test. Page summaries come from web sources, so the
 * escaping test matters most: a summary holding markup must stay text.
 */

const fs = require("fs");
const path = require("path");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "wiki-review.js"), "utf8");

function payload(extra = {}) {
  return {
    groups: [
      { id: "g1", family: "Topics", title: "First", line: "One.", pages: ["a", "b"] },
      { id: "g2", family: "Topics", title: "Second", line: "Two.", pages: ["c"] },
    ],
    pages: { a: ["sum a", 10, 1, 0, "2026-01-01"], b: ["sum b", 20, 3, 4, "2026-01-02"], c: ["sum c", 30, 2, 2, ""] },
    marks: {},
    sorted_at: "2026-01-03",
    ...extra,
  };
}

function mountPage() {
  document.body.innerHTML = `
    <p id="lede"></p><p id="fine"></p>
    <b id="t-marked"></b><span id="t-groups"></span><b id="t-pages"></b><b id="t-words"></b>
    <span id="status"></span>
    <button class="chip" data-f="all"></button>
    <button class="chip" data-f="cut"></button>
    <button class="chip" id="copyCut"></button>
    <input id="q">
    <div id="list"></div>`;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const text = (id) => document.getElementById(id).textContent;
const vote = (group, verdict) => document.querySelector(`.v[data-g="${group}"][data-v="${verdict}"]`);

// The script adds document-level listeners, which outlive a test.
const added = [];
const realAdd = document.addEventListener.bind(document);
document.addEventListener = (type, fn) => { added.push([type, fn]); realAdd(type, fn); };

async function start(data = payload(), putStatus = 200) {
  mountPage();
  global.fetch = jest.fn(async (url, opts) => {
    if (opts && opts.method === "PUT") return { ok: putStatus < 400, status: putStatus };
    return { ok: !data.error, status: data.error ? 404 : 200, json: async () => data };
  });
  new Function(SOURCE)();
  await flush();
}

afterEach(() => {
  for (const [type, fn] of added.splice(0)) document.removeEventListener(type, fn);
  document.body.innerHTML = "";
  delete global.fetch;
  delete window.WrenWikiReview;
});

describe("isCut and cutList", () => {
  beforeEach(() => new Function(SOURCE)());

  test("a page override beats the group verdict, both ways", () => {
    const { isCut } = window.WrenWikiReview;
    expect(isCut({ verdict: "cut", pages: { a: "keep" } }, "a")).toBe(false);
    expect(isCut({ verdict: "keep", pages: { a: "cut" } }, "a")).toBe(true);
    expect(isCut({ verdict: "cut", pages: {}, covers: ["b"] }, "b")).toBe(true);
    expect(isCut(undefined, "a")).toBe(false);
  });

  test("cutList lists only the pages marked to cut", () => {
    const { groups } = payload();
    const marks = { g1: { verdict: "cut", pages: { a: "keep" }, covers: ["a", "b"] }, g2: { verdict: "unsure", pages: {} } };
    expect(window.WrenWikiReview.cutList(groups, marks)).toEqual(["b"]);
  });

  test("a cut verdict skips a page that joined the group after it was set", () => {
    const { groups } = payload();
    const marks = { g1: { verdict: "cut", pages: {}, covers: ["a"] } };
    expect(window.WrenWikiReview.cutList(groups, marks)).toEqual(["a"]);
  });
});

describe("pages that joined after the mark", () => {
  test("the group says how many, and their boxes start unticked", async () => {
    await start(payload({ marks: { g1: { verdict: "cut", pages: {}, covers: ["a"] } } }));
    expect(document.querySelector('[data-group="g1"] .meta').textContent).toContain("1 new since marked");
    document.querySelector('[data-more="g1"]').click();
    expect(document.querySelector('input[data-p="b"]').checked).toBe(false);
    expect(text("t-pages")).toBe("1");
  });

  test("marking the group again covers every page in it now", async () => {
    await start(payload({ marks: { g1: { verdict: "cut", pages: {}, covers: ["a"] } } }));
    vote("g1", "cut").click();   // clears
    vote("g1", "cut").click();   // sets again
    await flush();
    expect(text("t-pages")).toBe("2");
    expect(document.querySelector('[data-group="g1"] .meta').textContent).not.toContain("new since marked");
  });

  test("ticking a page the verdict does not cover saves a cut override", async () => {
    await start(payload({ marks: { g1: { verdict: "cut", pages: {}, covers: ["a"] } } }));
    document.querySelector('[data-more="g1"]').click();
    const box = document.querySelector('input[data-p="b"]');
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(JSON.parse(global.fetch.mock.calls[1][1].body)).toEqual({ verdict: "cut", pages: { b: "cut" } });
  });
});

describe("the Move menu", () => {
  function unsorted() {
    return payload({
      groups: [
        { id: "g1", family: "Topics", title: "First", line: "One.", pages: ["a", "b"] },
        { id: "unsorted", family: "Not sorted yet", title: "Unsorted", line: "x", pages: ["c"] },
      ],
      move_to: [{ id: "g1", title: "First" }, { id: "g9", title: "<b>Ninth</b>" }],
    });
  }

  // The server's answer after the move: c now sits in g1.
  async function startAndMove(moveStatus) {
    await start(unsorted());
    const after = payload({
      groups: [{ id: "g1", family: "Topics", title: "First", line: "One.", pages: ["a", "b", "c"] }],
      move_to: unsorted().move_to,
    });
    const first = global.fetch;
    global.fetch = jest.fn(async (url, opts) => {
      if (opts && opts.method === "POST") return { ok: moveStatus < 400, status: moveStatus };
      return moveStatus < 400 ? { ok: true, status: 200, json: async () => after } : first(url, opts);
    });
    document.querySelector('[data-more="unsorted"]').click();
    const select = document.querySelector('select[data-move="c"]');
    select.value = "g1";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    await flush();
  }

  test("only an Unsorted page has the menu, and it lists move_to as text", async () => {
    await start(unsorted());
    document.querySelector('[data-more="g1"]').click();
    document.querySelector('[data-more="unsorted"]').click();
    const menus = document.querySelectorAll("select[data-move]");
    expect(menus).toHaveLength(1);
    expect(menus[0].dataset.move).toBe("c");
    expect([...menus[0].options].map((o) => o.textContent)).toEqual(["Move to…", "First", "<b>Ninth</b>"]);
  });

  test("choosing a group sends the move and shows the new list", async () => {
    await startAndMove(200);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("/api/wiki/review/move");
    expect(JSON.parse(opts.body)).toEqual({ page: "c", group: "g1" });
    expect(text("status")).toBe("Moved c to First.");
    expect(text("t-groups")).toBe("1");
    expect(document.querySelector("select[data-move]")).toBeNull();
  });

  test("a refused move says so and leaves the page in Unsorted", async () => {
    await startAndMove(404);
    expect(text("status")).toContain("did not finish");
    expect(document.getElementById("status").className).toBe("err");
    const select = document.querySelector('select[data-move="c"]');
    expect(select.value).toBe("");
  });

  test("choosing the blank option sends nothing", async () => {
    await start(unsorted());
    document.querySelector('[data-more="unsorted"]').click();
    const calls = global.fetch.mock.calls.length;
    document.querySelector("select[data-move]").dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(global.fetch.mock.calls).toHaveLength(calls);
  });
});

describe("the page", () => {
  test("renders groups, counts and the sort date from the API", async () => {
    await start();
    expect(global.fetch).toHaveBeenCalledWith("/api/wiki/review");
    expect(document.querySelectorAll(".group")).toHaveLength(2);
    expect(text("t-groups")).toBe("2");
    expect(text("lede")).toContain("3 pages");
    expect(text("fine")).toContain("2026-01-03");
  });

  test("saved marks show on load", async () => {
    await start(payload({ marks: { g1: { verdict: "cut", pages: {}, covers: ["a", "b"] } } }));
    expect(vote("g1", "cut").getAttribute("aria-pressed")).toBe("true");
    expect(text("t-marked")).toBe("1");
    expect(text("t-pages")).toBe("2");
    expect(text("t-words")).toBe("30");
  });

  test("markup in a summary or a title stays text", async () => {
    const evil = "<img src=x onerror=boom>";
    const data = payload();
    data.pages.a[0] = evil;
    data.groups[0].title = evil;
    await start(data);
    document.querySelector('[data-more="g1"]').click();
    expect(document.querySelector("#list img")).toBeNull();
    expect(document.querySelector(".p .s").textContent).toBe(evil);
    expect(document.querySelector("h3").textContent).toBe(evil);
  });

  test("a missing vault shows the error and renders no groups", async () => {
    await start({ error: "learnings vault not found" });
    expect(text("status")).toContain("learnings vault not found");
    expect(document.getElementById("status").className).toBe("err");
    expect(document.querySelectorAll(".group")).toHaveLength(0);
  });

  test("marking a group sends a PUT for that group", async () => {
    await start();
    vote("g1", "cut").click();
    await flush();
    const [url, opts] = global.fetch.mock.calls[1];
    expect(url).toBe("/api/wiki/review/marks/g1");
    expect(opts.method).toBe("PUT");
    expect(JSON.parse(opts.body)).toEqual({ verdict: "cut", pages: {} });
    expect(text("status")).toBe("Saved.");
    expect(text("t-pages")).toBe("2");
  });

  test("a second tap on the same verdict clears it", async () => {
    await start();
    vote("g1", "cut").click();
    vote("g1", "cut").click();
    await flush();
    expect(JSON.parse(global.fetch.mock.calls[2][1].body).verdict).toBeNull();
    expect(text("t-marked")).toBe("0");
  });

  test("a failed save says so", async () => {
    await start(payload(), 404);
    vote("g1", "cut").click();
    await flush();
    expect(text("status")).toContain("did not save");
    expect(document.getElementById("status").className).toBe("err");
  });

  test("unticking one page in a cut group saves a keep override", async () => {
    await start(payload({ marks: { g1: { verdict: "cut", pages: {}, covers: ["a", "b"] } } }));
    document.querySelector('[data-more="g1"]').click();
    const box = document.querySelector('input[data-p="a"]');
    box.checked = false;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(JSON.parse(global.fetch.mock.calls[1][1].body))
      .toEqual({ verdict: "cut", pages: { a: "keep" } });
    expect(text("t-pages")).toBe("1");
  });

  test("ticking a page back removes the override", async () => {
    await start(payload({ marks: { g1: { verdict: "cut", pages: { a: "keep" }, covers: ["a", "b"] } } }));
    document.querySelector('[data-more="g1"]').click();
    const box = document.querySelector('input[data-p="a"]');
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(JSON.parse(global.fetch.mock.calls[1][1].body).pages).toEqual({});
  });

  test("the filter hides groups and leaves the copy button alone", async () => {
    await start(payload({ marks: { g1: { verdict: "cut", pages: {}, covers: ["a", "b"] } } }));
    document.querySelector('[data-f="cut"]').click();
    expect(document.querySelectorAll(".group")).toHaveLength(1);
    expect(document.getElementById("copyCut").hasAttribute("aria-pressed")).toBe(false);
  });

  test("Copy cut list copies the names and sends nothing to the server", async () => {
    const writeText = jest.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await start(payload({ marks: { g1: { verdict: "cut", pages: { a: "keep" }, covers: ["a", "b"] }, g2: { verdict: "cut", pages: {}, covers: ["c"] } } }));
    document.getElementById("copyCut").click();
    await flush();
    expect(writeText).toHaveBeenCalledWith("b\nc");
    expect(text("status")).toBe("Copied 2 page names.");
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  test("Copy cut list with nothing marked copies nothing", async () => {
    const writeText = jest.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await start();
    document.getElementById("copyCut").click();
    await flush();
    expect(writeText).not.toHaveBeenCalled();
    expect(text("status")).toBe("No pages are marked to cut.");
  });

  test("a blocked copy says so", async () => {
    const writeText = jest.fn(async () => { throw new Error("denied"); });
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await start(payload({ marks: { g2: { verdict: "cut", pages: {}, covers: ["c"] } } }));
    document.getElementById("copyCut").click();
    await flush();
    expect(document.getElementById("status").className).toBe("err");
  });
});
