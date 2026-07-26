// Run-duration charts for the dashboard: one small chart per scheduled task,
// plotting how long each of its runs took.
//
// Included as <script src="/static/run-chart.js"></script>. Like nav.js, its
// contract is page-supplied mounts: <div id="runChart"> (required — the grid)
// and <span id="runChartHint"> (optional — the caption). The page owns all CSS;
// this file emits structure and class names only.
//
// Class names to style: .chart-grid, .chart-cell, .chart-head with
// .chart-name / .chart-stat, .chart-empty, and inside the SVG .chart-svg,
// .chart-median, .chart-line, and .pt with .pt.fail for a failed run.
//
// Hand-rolled SVG rather than a charting library on purpose. A CDN dependency
// would mean this local-first dashboard can't draw itself offline, and
// vendoring one is ~50-200KB of third-party JS in a repo with no build step —
// both are a lot to pay for one chart type. No model is involved either: the
// series is parsed from log files by chat/insights.py and the coordinates are
// arithmetic. Deterministic code owns structure; that includes axes.
//
// Two scale decisions worth keeping:
//
//   Log y. Durations span three orders of magnitude WITHIN one task —
//   strava_download's median is 9.9s and its max 968.8s, an Ollama cold load
//   rather than a different job. On a linear axis every point but the spike
//   sits on the floor and the chart says nothing. Durations can legitimately be
//   0.0s (log_inspector's), so the scale is log10(d + 1), which puts zero at
//   zero instead of at -Infinity.
//
//   x is run index, not time — one point per run, evenly spaced, oldest left.
//   Cadence is irregular (starred_installed ran three times in one day), and a
//   real time axis would bunch those into an unreadable clump. The per-point
//   tooltip carries the actual timestamp, and the caption says so.
(() => {
  const mount = document.getElementById("runChart");
  if (!mount) return;                                    // degrade, don't throw

  const W = 280, H = 72;                                 // cell viewBox
  const PAD = { top: 8, right: 6, bottom: 8, left: 6 };
  const PLOT_W = W - PAD.left - PAD.right;
  const PLOT_H = H - PAD.top - PAD.bottom;

  function svgEl(tag, attrs) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    return e;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;        // textContent, never innerHTML
    return e;
  }

  // Compact enough for a 70px-wide caption: 3.2s / 48s / 26m.
  function fmtDur(s) {
    if (s >= 60) return Math.round(s / 60) + "m";
    if (s < 10) return (Math.round(s * 10) / 10) + "s";
    return Math.round(s) + "s";
  }

  function plot(runs, task) {
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img" });
    const label = svgEl("title", {});
    label.textContent =
      `${task.display_name}: ${runs.length} run durations, oldest first, log scale`;
    svg.appendChild(label);

    const ys = runs.map((r) => Math.log10(r.duration_s + 1));
    const lo = Math.min.apply(null, ys);
    const span = Math.max.apply(null, ys) - lo;

    // A single run has no interval to divide by, and a task whose runs all took
    // the same time has no range — both would otherwise produce NaN
    // coordinates, which render as an empty chart with no error anywhere.
    const x = (i) => PAD.left + (runs.length === 1 ? PLOT_W / 2 : (i / (runs.length - 1)) * PLOT_W);
    const y = (v) => (span === 0 ? PAD.top + PLOT_H / 2 : PAD.top + PLOT_H * (1 - (v - lo) / span));

    if (task.median_s !== null && task.median_s !== undefined) {
      const medY = y(Math.log10(task.median_s + 1)).toFixed(1);
      svg.appendChild(svgEl("line", {
        class: "chart-median", x1: PAD.left, x2: W - PAD.right, y1: medY, y2: medY,
      }));
    }

    svg.appendChild(svgEl("polyline", {
      class: "chart-line",
      points: runs.map((r, i) => `${x(i).toFixed(1)},${y(ys[i]).toFixed(1)}`).join(" "),
    }));

    runs.forEach((r, i) => {
      const dot = svgEl("circle", {
        class: r.status === "failure" ? "pt fail" : "pt",
        cx: x(i).toFixed(1), cy: y(ys[i]).toFixed(1), r: 2.4,
      });
      const tip = svgEl("title", {});
      // start is "YYYY-MM-DD HH:MM:SS,mmm" — trim to the minute.
      tip.textContent = `${r.start.slice(0, 16)} · ${fmtDur(r.duration_s)} · ${r.status}`;
      dot.appendChild(tip);
      svg.appendChild(dot);
    });

    return svg;
  }

  function renderCell(task, days) {
    const cell = el("div", "chart-cell");
    const head = el("div", "chart-head");
    const name = el("span", "chart-name", task.display_name);
    name.title = task.display_name;   // the page truncates long names to one line
    head.appendChild(name);

    const timed = task.runs.filter((r) => typeof r.duration_s === "number");
    if (timed.length) {
      let stat = `med ${fmtDur(task.median_s)} · max ${fmtDur(task.max_s)}`;
      if (task.unfinished) stat += ` · ${task.unfinished} unfinished`;
      head.appendChild(el("span", "chart-stat", stat));
    }
    cell.appendChild(head);

    // An unfinished run has no duration to plot, so a task with nothing but
    // unfinished runs says so rather than rendering an empty box.
    if (!timed.length) {
      cell.appendChild(el("div", "chart-empty", task.unfinished
        ? `${task.unfinished} unfinished, none timed`
        : `no runs in the last ${days} days`));
      return cell;
    }
    cell.appendChild(plot(timed, task));
    return cell;
  }

  async function load() {
    let data;
    try {
      const resp = await fetch("/api/run_stats");
      data = await resp.json();
    } catch (err) {
      // A dropped connection or an error page rejects here. Say so — an
      // unhandled rejection would leave "Loading…" on screen forever.
      mount.textContent = `Couldn't load run history (${err.message}).`;
      return;
    }
    // fetch() does not reject on 401; an expired session arrives as a JSON
    // error body.
    if (!data || data.error) {
      mount.textContent = "Couldn't load run history: " + ((data && data.error) || "no data");
      return;
    }

    const tasks = data.tasks || [];
    mount.textContent = "";
    if (!tasks.length) {
      mount.appendChild(el("div", "chart-empty", "No scheduled tasks to chart."));
      return;
    }

    const grid = el("div", "chart-grid");
    tasks.forEach((task) => grid.appendChild(renderCell(task, data.days)));
    mount.appendChild(grid);

    const hint = document.getElementById("runChartHint");
    if (hint) {
      const runs = tasks.reduce((n, t) => n + t.count, 0);
      hint.textContent =
        `${runs} runs · last ${data.days} days · one point per run, oldest left · log scale`;
    }
  }

  load();
})();
