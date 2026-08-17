/**
 * Tests for chat/static/wiki-graph.js — the /wiki explorer's logic.
 *
 * The pure parts only: filtering, neighbour lookup, search, sizing, and that the
 * layout converges. Pixels are not asserted — a canvas drawing is checked by
 * looking at it, and jsdom has no real 2D context to look at.
 *
 * Like log-view.js it's a plain <script> in an IIFE, so the source is re-run with
 * `new Function` per test and exports its helpers on window.WrenWikiGraph.
 */

const fs = require("fs");
const path = require("path");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "wiki-graph.js"), "utf8");

function load() {
  new Function(SOURCE)();
  return window.WrenWikiGraph;
}

function node(id, kind = "concept", deg = 0) {
  return { id, title: id.replace(/-/g, " "), summary: "", kind, updated: "", deg };
}

// a — b — c, plus a stranded d and a daily log.
function fixture() {
  return {
    nodes: [
      node("agentos", "concept", 2),
      node("ollama", "concept", 2),
      node("claude-code", "concept", 2),
      node("stranded", "concept", 0),
      node("daily-chrome-2026-08-16", "log", 1),
      node("ai-slop", "lens", 1),
    ],
    edges: [[0, 1], [1, 2], [4, 0], [5, 2]],
  };
}

beforeEach(() => {
  delete window.WrenWikiGraph;
  document.body.innerHTML = "";
});

// --- neighbours -------------------------------------------------------------- //

describe("neighbours", () => {
  test("is symmetric — the graph is undirected on screen", () => {
    const { nodes, edges } = fixture();
    const adj = load().neighbours(edges, nodes.length);
    expect(adj[0].sort()).toEqual([1, 4]);
    expect(adj[1].sort()).toEqual([0, 2]);
  });

  test("a page nothing links to has an empty list, not a hole", () => {
    const { nodes, edges } = fixture();
    expect(load().neighbours(edges, nodes.length)[3]).toEqual([]);
  });
});

// --- filters ------------------------------------------------------------------ //

describe("visibleNodes / visibleEdges", () => {
  test("hiding daily logs drops those nodes and their edges", () => {
    const api = load();
    const { nodes, edges } = fixture();
    const keep = api.visibleNodes(nodes, new Set(["concept", "lens", "project"]), false);
    expect([...keep].sort()).toEqual([0, 1, 2, 3, 5]);
    // The log's edge to agentos goes with it.
    expect(api.visibleEdges(edges, keep)).toEqual([[0, 1], [1, 2], [5, 2]]);
  });

  test("an edge survives only when BOTH ends are visible", () => {
    const api = load();
    const { nodes, edges } = fixture();
    const keep = api.visibleNodes(nodes, new Set(["lens"]), false);
    expect(api.visibleEdges(edges, keep)).toEqual([]);
  });

  test("orphans-only finds what nothing links to", () => {
    const api = load();
    const { nodes } = fixture();
    const keep = api.visibleNodes(nodes, new Set(api.KINDS), true);
    expect([...keep]).toEqual([3]);
  });

  test("turning every kind off leaves an empty graph, not everything", () => {
    const api = load();
    const { nodes } = fixture();
    expect(api.visibleNodes(nodes, new Set(), false).size).toBe(0);
  });
});

// --- search -------------------------------------------------------------------- //

describe("searchMatches", () => {
  test("matches slug or title, case-insensitively", () => {
    const api = load();
    const { nodes } = fixture();
    expect(api.searchMatches(nodes, "OLLAMA")).toEqual([1]);
    expect(api.searchMatches(nodes, "claude code")).toEqual([2]);   // the title
  });

  test("an empty query matches nothing rather than everything", () => {
    const api = load();
    expect(api.searchMatches(fixture().nodes, "   ")).toEqual([]);
  });
});

// --- sizing --------------------------------------------------------------------- //

describe("radius", () => {
  test("grows with degree but sub-linearly, so a hub stays on screen", () => {
    const api = load();
    const small = api.radius({ deg: 1 });
    const hub = api.radius({ deg: 52 });
    expect(hub).toBeGreaterThan(small);
    expect(hub).toBeLessThan(small * 5);
  });

  test("a node nothing links to is still visible", () => {
    expect(load().radius({ deg: 0 })).toBeGreaterThan(2);
  });
});

// --- layout ----------------------------------------------------------------------- //

describe("layout", () => {
  test("seeding is deterministic, so the same vault lays out the same way twice", () => {
    const api = load();
    const a = api.seedPositions(fixture().nodes);
    const b = api.seedPositions(fixture().nodes);
    expect(a.map((n) => [n.x, n.y])).toEqual(b.map((n) => [n.x, n.y]));
  });

  test("no two seeded nodes start on the same point", () => {
    const api = load();
    const seen = new Set(api.seedPositions(fixture().nodes).map((n) => `${n.x},${n.y}`));
    expect(seen.size).toBe(6);
  });

  test("linked pages end up closer than unlinked ones", () => {
    const api = load();
    const { nodes, edges } = fixture();
    api.seedPositions(nodes);
    const keep = new Set(nodes.map((_, i) => i));
    for (let t = 0; t < 400; t++) api.tick(nodes, edges, keep, 0.4);

    const dist = (i, j) => Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
    expect(dist(0, 1)).toBeLessThan(dist(0, 3));   // agentos–ollama vs the stranded page
  });

  test("the layout settles rather than flying apart", () => {
    const api = load();
    const { nodes, edges } = fixture();
    api.seedPositions(nodes);
    const keep = new Set(nodes.map((_, i) => i));
    for (let t = 0; t < 500; t++) api.tick(nodes, edges, keep, 0.4);

    for (const n of nodes) {
      expect(Number.isFinite(n.x)).toBe(true);
      expect(Number.isFinite(n.y)).toBe(true);
      expect(Math.hypot(n.x, n.y)).toBeLessThan(4000);
    }
  });

  test("two nodes seeded on top of each other are pushed apart, not made NaN", () => {
    const api = load();
    const nodes = [node("a"), node("b")].map((n) => ({ ...n, x: 5, y: 5, vx: 0, vy: 0 }));
    const keep = new Set([0, 1]);
    for (let t = 0; t < 40; t++) api.tick(nodes, [], keep, 0.4);
    expect(Number.isFinite(nodes[0].x)).toBe(true);
    expect(Math.hypot(nodes[0].x - nodes[1].x, nodes[0].y - nodes[1].y)).toBeGreaterThan(0);
  });

  test("a filtered-out node is not moved", () => {
    const api = load();
    const { nodes, edges } = fixture();
    api.seedPositions(nodes);
    const hidden = { x: nodes[4].x, y: nodes[4].y };
    const keep = new Set([0, 1, 2, 3, 5]);
    for (let t = 0; t < 50; t++) api.tick(nodes, api.visibleEdges(edges, keep), keep, 0.4);
    expect([nodes[4].x, nodes[4].y]).toEqual([hidden.x, hidden.y]);
  });
});

// --- colours ------------------------------------------------------------------------ //

test("every kind has a colour", () => {
  const api = load();
  api.KINDS.forEach((k) => expect(api.COLOR[k]).toMatch(/^#[0-9a-f]{6}$/i));
});
