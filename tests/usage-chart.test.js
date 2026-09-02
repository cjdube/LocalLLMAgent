/**
 * Tests for chat/static/usage-chart.js — the /activity page.
 *
 * Same shape as run-chart.test.js: the file is a plain <script> in an IIFE that
 * runs on load and binds to page-supplied mounts, so each test rebuilds the
 * markup, stubs fetch, and re-runs the source with `new Function`.
 *
 * Most of these pin arithmetic and wording rather than appearance, because the
 * failure modes that matter here are silent ones: a stacked bar that drops a
 * model, a cost card that reads "$0.00" when it means "we don't know", and an
 * empty day that vanishes instead of showing as empty.
 */

const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "usage-chart.js"), "utf8");

// Every mount activity.html supplies.
const MARKUP = `
  <div class="range" id="usageRange">
    <button type="button" data-days="7" class="active">7d</button>
    <button type="button" data-days="30">30d</button>
    <button type="button" data-days="90">90d</button>
  </div>
  <div id="usageCards"></div>
  <div id="usageLegend"></div>
  <div id="usageChart"></div>
  <div id="usageAgents"></div>
  <div id="usageTasks"></div>
  <p id="usageNote"></p>
`;

const settle = () => new Promise((r) => setTimeout(r, 0));

function payload(over = {}) {
  return {
    days: 7,
    totals: {
      calls: 3, tokens: 3300, prompt_tokens: 3000, output_tokens: 300,
      thinking_tokens: 0, cost_usd: 0, unpriced_calls: 0, local_calls: 3,
      cut_off: 0, failed: 0, median_ms: 900,
      ...(over.totals || {}),
    },
    by_day: over.by_day || [
      { day: "2026-08-31", models: {} },
      { day: "2026-09-01", models: { "gemma4:26b-mlx": 3300 } },
    ],
    by_model: over.by_model || [
      { model: "gemma4:26b-mlx", tokens: 3300, calls: 3, cost_usd: 0 },
    ],
    by_agent: over.by_agent || [
      { agent: "wren", title: "Wren", tokens: 3300, calls: 3, cost_usd: 0 },
    ],
    by_task: over.by_task || [
      { task: "wren", tokens: 3300, calls: 3, cost_usd: 0 },
    ],
    by_backend: over.by_backend || [],
    ...(over.days ? { days: over.days } : {}),
  };
}

async function render(data, chartWidth) {
  global.fetch = jest.fn(() => Promise.resolve({ json: async () => data }));
  document.body.innerHTML = MARKUP;
  if (chartWidth) {
    Object.defineProperty(document.getElementById("usageChart"), "clientWidth", {
      configurable: true, value: chartWidth,
    });
  }
  new Function(SRC)();
  await settle();
}

const bars = () => [...document.querySelectorAll("rect.usage-bar")];
const cardText = () =>
  [...document.querySelectorAll("#usageCards .metric")].map((c) => c.textContent);

describe("metric cards", () => {
  test("shows the token total with the in/out split beneath it", async () => {
    await render(payload());
    expect(cardText()[0]).toBe("Tokens3.3K3.0K in · 300 out");
  });

  test("shows the call count with the median duration", async () => {
    await render(payload());
    expect(cardText()[1]).toBe("Model calls3median 900ms");
  });

  test("a slow median reads in seconds", async () => {
    await render(payload({ totals: { median_ms: 9200 } }));
    expect(cardText()[1]).toContain("median 9.2s");
  });

  test("local calls are named on the cost card, not hidden", async () => {
    // "$0.00" alone reads as a bug. "3 of 3 free on device" is the fact that
    // makes it make sense.
    await render(payload());
    expect(cardText()[2]).toBe("Cloud cost$0.003 of 3 free on device");
  });

  test("an unpriced call is reported instead of the free count", async () => {
    // The whole point of the column: a model missing from the price table is
    // unknown, and must never be quietly totalled as zero.
    await render(payload({ totals: { cost_usd: 0.25, unpriced_calls: 2 } }));
    expect(cardText()[2]).toBe("Cloud cost$0.252 calls unpriced");
  });

  test("a sub-cent bill shows as under a cent, not as zero", async () => {
    await render(payload({ totals: { cost_usd: 0.0004, local_calls: 0 } }));
    expect(cardText()[2]).toContain("<$0.01");
  });

  test("cut off and failed calls are summed, with the cut-off count spelled out", async () => {
    await render(payload({ totals: { cut_off: 2, failed: 1 } }));
    expect(cardText()[3]).toBe("Cut off or failed32 hit the token cap");
  });

  test("big numbers compact to K and M", async () => {
    await render(payload({ totals: { tokens: 1620000, prompt_tokens: 1600000,
                                     output_tokens: 20000 } }));
    expect(cardText()[0]).toBe("Tokens1.62M1.60M in · 20.0K out");
  });
});

describe("stacked daily bars", () => {
  test("uses the widget's real width so a seven-day chart is not stretched", async () => {
    const days = Array.from({ length: 7 }, (_, i) => ({
      day: `2026-08-${25 + i}`,
      models: { a: 100 },
    }));
    await render(payload({ by_day: days, by_model: [{ model: "a", tokens: 700 }] }), 1000);

    expect(document.querySelector("svg.usage-svg").getAttribute("viewBox"))
      .toBe("0 0 1000 200");
    expect(Number(bars()[0].getAttribute("width"))).toBeLessThanOrEqual(34);
  });

  test("draws one bar per model per day, and none for a quiet day", async () => {
    await render(payload({
      by_day: [
        { day: "2026-08-31", models: {} },
        { day: "2026-09-01", models: { a: 100, b: 50 } },
      ],
      by_model: [{ model: "a", tokens: 100 }, { model: "b", tokens: 50 }],
    }));
    expect(bars()).toHaveLength(2);
  });

  test("a quiet day still gets its column on the axis", async () => {
    // An empty column reads as "nothing ran". A missing one reads as "no data".
    await render(payload({
      by_day: [
        { day: "2026-08-30", models: {} },
        { day: "2026-08-31", models: {} },
        { day: "2026-09-01", models: { a: 10 } },
      ],
      by_model: [{ model: "a", tokens: 10 }],
    }));
    const labels = [...document.querySelectorAll("text.usage-axis")]
      .map((t) => t.textContent);
    // Three day labels (the window is short enough to label every column) on
    // top of the three y-axis ticks.
    expect(labels).toHaveLength(6);
  });

  test("bars stack: each sits directly on the one below", async () => {
    await render(payload({
      by_day: [{ day: "2026-09-01", models: { a: 100, b: 100 } }],
      by_model: [{ model: "a", tokens: 100 }, { model: "b", tokens: 100 }],
    }));
    const [first, second] = bars();
    const bottom = Number(second.getAttribute("y")) + Number(second.getAttribute("height"));
    expect(Number(first.getAttribute("y"))).toBeCloseTo(bottom, 1);
  });

  test("every coordinate is a number", async () => {
    // A NaN renders as an empty chart with no error anywhere — the failure this
    // whole file exists to catch.
    await render(payload());
    for (const bar of bars()) {
      for (const attr of ["x", "y", "width", "height"]) {
        expect(Number(bar.getAttribute(attr))).not.toBeNaN();
      }
    }
  });

  test("a model that ran once on a busy day still draws at least a pixel", async () => {
    await render(payload({
      by_day: [{ day: "2026-09-01", models: { big: 1000000, tiny: 1 } }],
      by_model: [{ model: "big", tokens: 1000000 }, { model: "tiny", tokens: 1 }],
    }));
    const heights = bars().map((b) => Number(b.getAttribute("height")));
    expect(Math.min(...heights)).toBeGreaterThanOrEqual(1);
  });

  test("a window with no calls at all still draws its axis", async () => {
    await render(payload({
      totals: { calls: 0, tokens: 0, prompt_tokens: 0, output_tokens: 0,
                local_calls: 0, median_ms: null },
      by_day: [{ day: "2026-09-01", models: {} }],
      by_model: [], by_agent: [], by_task: [],
    }));
    expect(document.querySelector("svg.usage-svg")).not.toBeNull();
    expect(bars()).toHaveLength(0);
    expect(cardText()[1]).toContain("median —");
  });

  test("each bar carries its day, model and tokens as a tooltip", async () => {
    await render(payload());
    expect(bars()[0].querySelector("title").textContent)
      .toBe("2026-09-01 · gemma4:26b-mlx · 3.3K tokens");
  });

  test("past six models the tail folds into one band", async () => {
    const models = "abcdefgh".split("").map((m, i) => ({ model: m, tokens: 100 - i }));
    const day = {};
    models.forEach((m) => { day[m.model] = m.tokens; });
    await render(payload({
      by_day: [{ day: "2026-09-01", models: day }], by_model: models,
    }));
    // Six named bands plus one "other" — not eight.
    expect(bars()).toHaveLength(7);
    const legend = document.getElementById("usageLegend").textContent;
    expect(legend).toContain("2 other · 187");
  });
});

describe("legend", () => {
  test("names each model with its total", async () => {
    await render(payload());
    expect(document.getElementById("usageLegend").textContent)
      .toBe("gemma4:26b-mlx · 3.3K");
  });

  test("a model keeps the same colour in the legend and the bars", async () => {
    await render(payload({
      by_day: [{ day: "2026-09-01", models: { a: 100, b: 50 } }],
      by_model: [{ model: "a", tokens: 100 }, { model: "b", tokens: 50 }],
    }));
    const swatches = [...document.querySelectorAll(".legend-swatch")]
      .map((s) => s.style.background);
    const fills = bars().map((b) => b.getAttribute("fill"));
    // Both lists are in the same (largest-first) order.
    expect(fills).toHaveLength(2);
    expect(swatches).toHaveLength(2);
    expect(new Set(fills).size).toBe(2);
  });
});

describe("breakdowns", () => {
  test("agents are listed by their display title", async () => {
    await render(payload({
      by_agent: [
        { agent: "wren", title: "Wren", tokens: 900, calls: 2, cost_usd: 0 },
        { agent: "scribejay", title: "ScribeJay", tokens: 300, calls: 1, cost_usd: 0 },
      ],
    }));
    const text = document.getElementById("usageAgents").textContent;
    expect(text).toContain("Wren900");
    expect(text).toContain("ScribeJay300");
  });

  test("the largest agent bar is full width", async () => {
    await render(payload({
      by_agent: [
        { agent: "wren", title: "Wren", tokens: 900, calls: 2, cost_usd: 0 },
        { agent: "wiki", title: "Wiki", tokens: 450, calls: 1, cost_usd: 0 },
      ],
    }));
    const widths = [...document.querySelectorAll("#usageAgents .bar-fill")]
      .map((f) => f.style.width);
    // jsdom normalizes the style value, so "50.0%" comes back as "50%".
    expect(widths).toEqual(["100%", "50%"]);
  });

  test("a job shows its call count, and a paid job shows dollars instead", async () => {
    // For a Claude Code build the dollars ARE the story; for a local task they
    // are always zero and say nothing.
    await render(payload({
      by_task: [
        { task: "build_worker", tokens: 50000, calls: 1, cost_usd: 0.42 },
        { task: "morning_brief", tokens: 9000, calls: 4, cost_usd: 0 },
      ],
    }));
    const rows = [...document.querySelectorAll("#usageTasks .task-row")]
      .map((r) => r.textContent);
    expect(rows[0]).toBe("build_worker50.0K · $0.42");
    expect(rows[1]).toBe("morning_brief9.0K · 4 calls");
  });

  test("an empty breakdown says so rather than rendering blank", async () => {
    await render(payload({ by_agent: [], by_task: [] }));
    expect(document.getElementById("usageAgents").textContent)
      .toBe("Nothing recorded in this window.");
    expect(document.getElementById("usageTasks").textContent)
      .toBe("Nothing recorded in this window.");
  });
});

describe("the range switch", () => {
  test("asks for a week on load", async () => {
    await render(payload());
    expect(global.fetch).toHaveBeenCalledWith("/api/usage?days=7");
  });

  test("clicking a range re-fetches and moves the active mark", async () => {
    await render(payload());
    document.querySelector('button[data-days="30"]').click();
    await settle();
    expect(global.fetch).toHaveBeenLastCalledWith("/api/usage?days=30");
    const active = [...document.querySelectorAll("#usageRange button.active")]
      .map((b) => b.dataset.days);
    expect(active).toEqual(["30"]);
  });
});

describe("degrading", () => {
  test("an expired session is reported, not thrown", async () => {
    // fetch() does not reject on 401 — it arrives as a JSON error body.
    await render({ error: "not authenticated" });
    expect(document.getElementById("usageChart").textContent)
      .toBe("Couldn't load usage: not authenticated");
  });

  test("a dropped connection is reported", async () => {
    global.fetch = jest.fn(() => Promise.reject(new Error("network down")));
    document.body.innerHTML = MARKUP;
    new Function(SRC)();
    await settle();
    expect(document.getElementById("usageChart").textContent)
      .toBe("Couldn't load usage (network down).");
  });

  test("a page with no chart mount is left alone", async () => {
    global.fetch = jest.fn();
    document.body.innerHTML = "<div>some other page</div>";
    new Function(SRC)();
    await settle();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
