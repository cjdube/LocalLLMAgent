/**
 * Tests for chat/static/run-chart.js — the dashboard's run-duration charts.
 *
 * Like nav.js and chat-dock.js it's a plain <script> wrapped in an IIFE that
 * runs on load and binds to page-supplied mounts, so each test rebuilds the
 * markup, stubs fetch, and re-runs the source with `new Function`.
 *
 * The network is never touched. Most of these pin the arithmetic rather than
 * the appearance: a NaN coordinate renders as an *empty chart with no error
 * anywhere*, which is the failure mode this file exists to prevent.
 */

const fs = require("fs");
const path = require("path");

const CHART_SRC = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "run-chart.js"), "utf8");

// The renderer's contract with a page: a required grid mount and an optional
// caption. dashboard.html supplies both.
const MARKUP = `
  <span id="runChartHint"></span>
  <div id="runChart">Loading…</div>
`;

// Let the renderer's awaited fetch/.json() settle before asserting.
const settle = () => new Promise((r) => setTimeout(r, 0));

function run(secs, status = "success", start = "2026-07-25 06:00:00,000") {
  return { id: start, start, status, duration_s: secs };
}

function task(overrides) {
  const runs = overrides.runs || [];
  const timed = runs.filter((r) => typeof r.duration_s === "number")
    .map((r) => r.duration_s).sort((a, b) => a - b);
  return {
    key: "brief",
    display_name: "Brief",
    runs,
    count: runs.length,
    unfinished: runs.length - timed.length,
    failures: runs.filter((r) => r.status === "failure").length,
    median_s: timed.length ? timed[Math.floor(timed.length / 2)] : null,
    max_s: timed.length ? timed[timed.length - 1] : null,
    ...overrides,
  };
}

async function render(payload) {
  global.fetch = jest.fn(() => Promise.resolve({ json: async () => payload }));
  document.body.innerHTML = MARKUP;
  new Function(CHART_SRC)();
  await settle();
}

const mount = () => document.getElementById("runChart");
const cells = () => document.querySelectorAll(".chart-cell");
const points = () => [...document.querySelectorAll("circle")];
const coords = () =>
  points().flatMap((c) => [Number(c.getAttribute("cx")), Number(c.getAttribute("cy"))]);

describe("rendering", () => {
  test("draws one cell per task, with a point per timed run", async () => {
    await render({
      days: 30,
      tasks: [
        task({ key: "a", display_name: "Alpha", runs: [run(5), run(9), run(600)] }),
        task({ key: "b", display_name: "Bravo", runs: [run(1), run(2)] }),
      ],
    });
    expect(cells()).toHaveLength(2);
    expect(document.querySelectorAll(".chart-cell")[0].querySelectorAll("circle"))
      .toHaveLength(3);
    expect(points()).toHaveLength(5);
    expect(global.fetch).toHaveBeenCalledWith("/api/run_stats");
  });

  test("names each task and captions its median and max", async () => {
    await render({ days: 30, tasks: [task({ runs: [run(8), run(10), run(600)] })] });
    expect(document.querySelector(".chart-name").textContent).toBe("Brief");
    // 600s renders compactly as minutes — the caption shares a row with the name.
    expect(document.querySelector(".chart-stat").textContent).toBe("med 10s · max 10m");
  });

  test("a failed run is marked, a successful one is not", async () => {
    await render({
      days: 30,
      tasks: [task({ runs: [run(5), run(54, "failure"), run(6)] })],
    });
    const classes = points().map((c) => c.getAttribute("class"));
    expect(classes).toEqual(["pt", "pt fail", "pt"]);
  });

  test("each point carries its timestamp, duration and status as a tooltip", async () => {
    await render({
      days: 30,
      tasks: [task({ runs: [run(54.1, "failure", "2026-07-25 17:00:01,657")] })],
    });
    expect(points()[0].querySelector("title").textContent)
      .toBe("2026-07-25 17:00 · 54s · failure");
  });

  test("fills the caption mount when the page supplies one", async () => {
    await render({
      days: 14,
      tasks: [task({ key: "a", runs: [run(5)] }), task({ key: "b", runs: [run(5), run(6)] })],
    });
    const hint = document.getElementById("runChartHint").textContent;
    expect(hint).toContain("3 runs");
    expect(hint).toContain("last 14 days");
  });

  test("the caption names the trim when the server capped a series", async () => {
    // Otherwise it would claim "last 30 days" over a chart that only covers the
    // most recent 30 runs of a busier task — a window the drawing can't support.
    await render({
      days: 30,
      limit: 30,
      tasks: [
        task({ key: "a", runs: [run(5), run(6)], total: 40 }),
        task({ key: "b", runs: [run(7)], total: 1 }),
      ],
    });
    const hint = document.getElementById("runChartHint").textContent;
    expect(hint).toContain("3 of 41 runs");
    expect(hint).toContain("newest 30 per chart");
    expect(hint).not.toContain("last 30 days");
  });

  test("nothing capped leaves the window claim intact", async () => {
    await render({
      days: 30,
      limit: 30,
      tasks: [task({ runs: [run(5), run(6)], total: 2 })],
    });
    const hint = document.getElementById("runChartHint").textContent;
    expect(hint).toContain("2 runs");
    expect(hint).toContain("last 30 days");
    expect(hint).not.toContain("newest");
  });
});

describe("scale arithmetic", () => {
  // Every case here would otherwise emit NaN or Infinity into an SVG attribute,
  // which draws nothing and reports nothing.
  test("a 0.0s run plots at a real coordinate, not -Infinity", async () => {
    await render({ days: 30, tasks: [task({ runs: [run(0), run(0.1), run(3)] })] });
    expect(coords().every(Number.isFinite)).toBe(true);
  });

  test("a single run plots at a real coordinate", async () => {
    await render({ days: 30, tasks: [task({ runs: [run(42)] })] });
    expect(points()).toHaveLength(1);
    expect(coords().every(Number.isFinite)).toBe(true);
  });

  test("runs that all took the same time plot at a real coordinate", async () => {
    await render({ days: 30, tasks: [task({ runs: [run(7), run(7), run(7)] })] });
    expect(coords().every(Number.isFinite)).toBe(true);
    // No range to spread over — they sit on one flat line.
    const ys = points().map((c) => Number(c.getAttribute("cy")));
    expect(new Set(ys).size).toBe(1);
  });

  test("the polyline has one finite pair per point", async () => {
    await render({ days: 30, tasks: [task({ runs: [run(0), run(5), run(900)] })] });
    const pts = document.querySelector(".chart-line").getAttribute("points").split(" ");
    expect(pts).toHaveLength(3);
    expect(pts.every((p) => p.split(",").every((n) => Number.isFinite(Number(n))))).toBe(true);
  });

  test("the biggest run sits above the smallest", async () => {
    await render({ days: 30, tasks: [task({ runs: [run(10), run(1000)] })] });
    const [small, big] = points().map((c) => Number(c.getAttribute("cy")));
    expect(big).toBeLessThan(small);   // SVG y grows downward
  });
});

describe("degrading", () => {
  test("an unfinished run is counted in the caption but never plotted", async () => {
    await render({
      days: 30,
      tasks: [task({ runs: [run(5), { id: "x", start: "2026-07-25 06:00:00,000",
                                      status: "running", duration_s: null }] })],
    });
    expect(points()).toHaveLength(1);
    expect(document.querySelector(".chart-stat").textContent).toContain("1 unfinished");
  });

  test("a task with only unfinished runs says so instead of drawing an empty box", async () => {
    await render({
      days: 30,
      tasks: [task({ runs: [{ id: "x", start: "2026-07-25 06:00:00,000",
                              status: "running", duration_s: null }] })],
    });
    expect(document.querySelectorAll("svg")).toHaveLength(0);
    expect(document.querySelector(".chart-empty").textContent).toBe("1 unfinished, none timed");
  });

  test("a task with no runs in the window says so", async () => {
    await render({ days: 7, tasks: [task({ runs: [] })] });
    expect(document.querySelector(".chart-empty").textContent)
      .toBe("no runs in the last 7 days");
  });

  test("no tasks at all leaves a message, not a blank panel", async () => {
    await render({ days: 30, tasks: [] });
    expect(mount().textContent).toBe("No scheduled tasks to chart.");
  });

  test("an expired session (401 JSON body) is reported, not left as Loading", async () => {
    await render({ error: "not authenticated" });
    expect(mount().textContent).toContain("not authenticated");
    expect(mount().textContent).not.toContain("Loading");
  });

  test("a dropped connection is reported rather than thrown", async () => {
    global.fetch = jest.fn(() => Promise.reject(new Error("network down")));
    document.body.innerHTML = MARKUP;
    expect(() => new Function(CHART_SRC)()).not.toThrow();
    await settle();
    expect(mount().textContent).toContain("network down");
  });

  test("a page without the grid mount does not throw", () => {
    global.fetch = jest.fn(() => Promise.resolve({ json: async () => ({}) }));
    document.body.innerHTML = `<div>no mount here</div>`;
    expect(() => new Function(CHART_SRC)()).not.toThrow();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test("a page without the caption mount still renders the charts", async () => {
    global.fetch = jest.fn(() =>
      Promise.resolve({ json: async () => ({ days: 30, tasks: [task({ runs: [run(5)] })] }) }));
    document.body.innerHTML = `<div id="runChart"></div>`;
    new Function(CHART_SRC)();
    await settle();
    expect(cells()).toHaveLength(1);
  });
});

// The dashboard draws one grid per agent inside that agent's box, which the
// single #runChart mount cannot express — so the renderer also exposes
// render(mount, tasks, days). These pin that second contract: a page driving it
// supplies both the mounts and the data, and the file must not fetch anything.
describe("driven by the page", () => {
  function load(markup = `<div>no auto mount</div>`) {
    global.fetch = jest.fn(() => Promise.resolve({ json: async () => ({}) }));
    document.body.innerHTML = markup;
    new Function(CHART_SRC)();
    return window.WrenRunCharts;
  }

  test("exposes render and caption even when the page has no auto mount", () => {
    const api = load();
    expect(typeof api.render).toBe("function");
    expect(typeof api.caption).toBe("function");
    // The whole point of the driven path: the page already has the data.
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test("draws into the mount it is handed, not into #runChart", () => {
    const api = load(`<div id="runChart"></div><div id="mine"></div>`);
    const mine = document.getElementById("mine");
    api.render(mine, [task({ runs: [run(5), run(9)] })], 30);
    // Both halves: the given mount filled AND the auto mount left alone. A test
    // that checked only the first would pass on a renderer that ignored its
    // argument and drew everywhere.
    expect(mine.querySelectorAll(".chart-cell")).toHaveLength(1);
    expect(mine.querySelectorAll("circle")).toHaveLength(2);
    expect(document.getElementById("runChart").querySelectorAll(".chart-cell")).toHaveLength(0);
  });

  test("two mounts each get only their own tasks", () => {
    const api = load(`<div id="a"></div><div id="b"></div>`);
    api.render(document.getElementById("a"),
      [task({ key: "w1", display_name: "Wren job", runs: [run(5)] })], 30);
    api.render(document.getElementById("b"),
      [task({ key: "s1", display_name: "Scribe job", runs: [run(5)] }),
       task({ key: "s2", display_name: "Scribe job 2", runs: [run(5)] })], 30);
    const names = (id) => [...document.getElementById(id).querySelectorAll(".chart-name")]
      .map((n) => n.textContent);
    expect(names("a")).toEqual(["Wren job"]);
    expect(names("b")).toEqual(["Scribe job", "Scribe job 2"]);
  });

  test("re-rendering a mount replaces its grid rather than stacking a second", () => {
    // loadTasks() runs again every time a "Run now" finishes, into the same box.
    const api = load(`<div id="mine"></div>`);
    const mine = document.getElementById("mine");
    api.render(mine, [task({ runs: [run(5)] })], 30);
    api.render(mine, [task({ runs: [run(5)] })], 30);
    expect(mine.querySelectorAll(".chart-grid")).toHaveLength(1);
    expect(mine.querySelectorAll(".chart-cell")).toHaveLength(1);
  });

  test("an agent with no charted tasks says so instead of leaving a blank box", () => {
    const api = load();
    const mine = document.createElement("div");
    api.render(mine, [], 30);
    expect(mine.textContent).toContain("No scheduled tasks to chart");
  });

  test("caption returns the same string the auto path writes to the hint", () => {
    const api = load();
    const tasks = [task({ runs: [run(5), run(6)] })];
    expect(api.caption(tasks, 30, 40)).toBe(
      "2 runs · last 30 days · one point per run, oldest left · log scale");
    // A trimmed series must say so — 30 days would be a claim the chart cannot
    // support.
    expect(api.caption([task({ runs: [run(5)], count: 40, total: 500 })], 30, 40))
      .toContain("40 of 500 runs · newest 40 per chart");
  });
});
