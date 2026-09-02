// The /activity view: how many tokens each agent spent, on which model, on what.
//
// Included as <script src="/static/usage-chart.js"></script>. It owns the whole
// page below the header: it fetches /api/usage, fills the metric cards, draws
// the stacked daily bars, and lists the per-agent and per-job breakdowns. The
// page supplies the mounts and all the CSS; this emits structure and class
// names only, the same contract run-chart.js has.
//
// Mounts it expects: #usageCards, #usageLegend, #usageChart, #usageAgents,
// #usageTasks, #usageNote, and #usageRange for the 7/30/90 buttons.
//
// Hand-rolled SVG rather than a charting library, for the same two reasons
// run-chart.js gives: a CDN dependency means this local-first dashboard cannot
// draw itself offline, and vendoring one is ~50-200KB of third-party JS in a
// repo with no build step. A stacked bar is arithmetic.
//
// One scale decision worth keeping, and it is the opposite of run-chart.js's:
// the y axis here is LINEAR. Durations span three orders of magnitude within
// one task, which is why that chart uses log — but daily token totals do not,
// and the whole point of this chart is that a bar twice as tall cost twice as
// much. A log axis would flatten exactly the spike worth seeing.
(() => {
  const FALLBACK_W = 640, H = 200;
  const PAD = { top: 10, right: 8, bottom: 22, left: 44 };
  const PLOT_H = H - PAD.top - PAD.bottom;
  const MAX_BAR = 34;

  // Fixed order, assigned to models by total size, so a model keeps its colour
  // as long as its rank holds. Local first: gemma is the bar that dominates
  // every honest week here, and it should read as the calm default rather than
  // as an alert.
  const SERIES = ["#3a76c4", "#d9743a", "#3f9d7c", "#8c6bb1", "#b3627f", "#6b7280"];
  const OTHER = "#9ca3af";
  // Past this many models the legend stops being a legend, so the tail is
  // folded into one "other" band rather than generating more hues.
  const MAX_SERIES = 6;

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

  // 8 / 940 / 12.4K / 1.62M — a token count is never interesting to the digit.
  function fmtTokens(n) {
    n = n || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "K";
    return String(n);
  }

  function fmtCost(usd) {
    if (typeof usd !== "number") return "—";
    // Sub-cent totals are real and worth showing as themselves: "$0.00" beside
    // a week of Gemini calls reads as a bug, not as "almost nothing".
    if (usd > 0 && usd < 0.01) return "<$0.01";
    return "$" + usd.toFixed(2);
  }

  function fmtDur(ms) {
    if (typeof ms !== "number") return "—";
    if (ms >= 1000) return (ms / 1000).toFixed(1) + "s";
    return Math.round(ms) + "ms";
  }

  // "2026-09-01" -> "Mon 1". The window is at most a year and the axis has room
  // for a handful of labels, so the year never earns its space.
  function fmtDay(iso) {
    const d = new Date(iso + "T00:00:00");
    if (isNaN(d)) return iso.slice(5);
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
  }

  // Which models get their own colour, biggest first, tail folded into "other".
  function seriesFor(byModel) {
    const named = byModel.slice(0, MAX_SERIES).map((m) => m.model);
    const colors = {};
    named.forEach((name, i) => { colors[name] = SERIES[i]; });
    return { named, colors, hasOther: byModel.length > MAX_SERIES };
  }

  function bandFor(models, series) {
    // Collapse a day's per-model totals onto the legend's bands, so a day whose
    // seventh model appeared only that day still stacks into "other" rather
    // than vanishing from a column whose height is supposed to be the total.
    const bands = {};
    for (const name in models) {
      const key = series.named.indexOf(name) >= 0 ? name : "other";
      bands[key] = (bands[key] || 0) + models[name];
    }
    return bands;
  }

  function renderChart(mount, byDay, series) {
    mount.textContent = "";
    const days = byDay || [];
    const stacks = days.map((d) => bandFor(d.models || {}, series));
    const totals = stacks.map((b) => Object.values(b).reduce((a, n) => a + n, 0));
    const peak = Math.max.apply(null, totals.concat([0]));
    // Match SVG units to the rendered widget so preserveAspectRatio="none"
    // remains responsive without widening bars and text on a desktop card.
    const width = mount.clientWidth || FALLBACK_W;
    const plotW = width - PAD.left - PAD.right;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${width} ${H}`, class: "usage-svg", role: "img",
      preserveAspectRatio: "none",
    });
    const label = svgEl("title", {});
    label.textContent = `Tokens per day for the last ${days.length} days, stacked by model`;
    svg.appendChild(label);

    // A window with no calls at all still draws its axis and its empty columns:
    // "nothing ran" is a true and useful reading, where a blank box is not.
    const scale = peak > 0 ? PLOT_H / peak : 0;

    [0, 0.5, 1].forEach((f) => {
      const y = (PAD.top + PLOT_H * (1 - f)).toFixed(1);
      svg.appendChild(svgEl("line", {
        class: "usage-grid", x1: PAD.left, x2: width - PAD.right, y1: y, y2: y,
      }));
      const t = svgEl("text", { class: "usage-axis", x: PAD.left - 6, y: y, dy: "0.32em" });
      t.textContent = fmtTokens(Math.round(peak * f));
      svg.appendChild(t);
    });

    const slot = days.length ? plotW / days.length : plotW;
    const barW = Math.min(MAX_BAR, Math.max(3, slot * 0.62));

    days.forEach((day, i) => {
      const cx = PAD.left + slot * (i + 0.5);
      const x = (cx - barW / 2).toFixed(1);
      let base = PAD.top + PLOT_H;

      const bands = series.named.concat(series.hasOther ? ["other"] : []);
      bands.forEach((name) => {
        const value = stacks[i][name] || 0;
        if (!value) return;
        // Floor at 1px: a model that ran once in a busy day is a fact, and a
        // sub-pixel rect renders as nothing at all.
        const h = Math.max(1, value * scale);
        base -= h;
        const rect = svgEl("rect", {
          class: "usage-bar", x: x, y: base.toFixed(1),
          width: barW.toFixed(1), height: h.toFixed(1),
          fill: name === "other" ? OTHER : series.colors[name],
        });
        const tip = svgEl("title", {});
        tip.textContent = `${day.day} · ${name} · ${fmtTokens(value)} tokens`;
        rect.appendChild(tip);
        svg.appendChild(rect);
      });

      // Label roughly six columns however wide the window is, so 7 days labels
      // every column and 90 days labels one a fortnight instead of a smear.
      const every = Math.ceil(days.length / 6);
      if (i % every === 0 || i === days.length - 1) {
        const t = svgEl("text", {
          class: "usage-axis", x: cx.toFixed(1), y: H - 6, "text-anchor": "middle",
        });
        t.textContent = fmtDay(day.day);
        svg.appendChild(t);
      }
    });

    mount.appendChild(svg);
  }

  function renderCards(mount, totals) {
    mount.textContent = "";
    const free = totals.local_calls
      ? `${totals.local_calls} of ${totals.calls} free on device`
      : "no local calls";
    // The unpriced count rides with the cost, never folded into it: a model
    // missing from the price table is "unknown", not "free".
    const costNote = totals.unpriced_calls
      ? `${totals.unpriced_calls} calls unpriced`
      : free;
    const cards = [
      ["Tokens", fmtTokens(totals.tokens),
        `${fmtTokens(totals.prompt_tokens)} in · ${fmtTokens(totals.output_tokens)} out`],
      ["Model calls", String(totals.calls || 0), `median ${fmtDur(totals.median_ms)}`],
      ["Cloud cost", fmtCost(totals.cost_usd), costNote],
      ["Cut off or failed", String((totals.cut_off || 0) + (totals.failed || 0)),
        `${totals.cut_off || 0} hit the token cap`],
    ];
    cards.forEach(([label, value, note]) => {
      const card = el("div", "metric");
      card.appendChild(el("p", "metric-label", label));
      card.appendChild(el("p", "metric-value", value));
      card.appendChild(el("p", "metric-note", note));
      mount.appendChild(card);
    });
  }

  function renderLegend(mount, byModel, series) {
    mount.textContent = "";
    byModel.slice(0, MAX_SERIES).forEach((m) => {
      const item = el("span", "legend-item");
      const swatch = el("span", "legend-swatch");
      swatch.style.background = series.colors[m.model];
      item.appendChild(swatch);
      item.appendChild(el("span", null, `${m.model} · ${fmtTokens(m.tokens)}`));
      mount.appendChild(item);
    });
    if (series.hasOther) {
      const rest = byModel.slice(MAX_SERIES);
      const item = el("span", "legend-item");
      const swatch = el("span", "legend-swatch");
      swatch.style.background = OTHER;
      item.appendChild(swatch);
      const tokens = rest.reduce((n, m) => n + m.tokens, 0);
      item.appendChild(el("span", null, `${rest.length} other · ${fmtTokens(tokens)}`));
      mount.appendChild(item);
    }
  }

  function renderBars(mount, entries, nameKey) {
    mount.textContent = "";
    const peak = entries.reduce((n, e) => Math.max(n, e.tokens), 0);
    if (!entries.length) {
      mount.appendChild(el("p", "empty", "Nothing recorded in this window."));
      return;
    }
    entries.forEach((entry) => {
      const row = el("div", "bar-row");
      const head = el("div", "bar-head");
      head.appendChild(el("span", null, entry[nameKey] || entry.title));
      head.appendChild(el("span", "bar-value", fmtTokens(entry.tokens)));
      row.appendChild(head);
      const track = el("div", "bar-track");
      const fill = el("div", "bar-fill");
      fill.style.width = (peak ? (entry.tokens / peak) * 100 : 0).toFixed(1) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      mount.appendChild(row);
    });
  }

  function renderTasks(mount, tasks) {
    mount.textContent = "";
    if (!tasks.length) {
      mount.appendChild(el("p", "empty", "Nothing recorded in this window."));
      return;
    }
    tasks.slice(0, 8).forEach((t) => {
      const row = el("div", "task-row");
      row.appendChild(el("span", "task-name", t.task));
      // Cost where there is one, call count where there isn't: for a Claude
      // Code build the dollars ARE the story, and for a local task they are
      // always zero and say nothing.
      const detail = t.cost_usd > 0
        ? `${fmtTokens(t.tokens)} · ${fmtCost(t.cost_usd)}`
        : `${fmtTokens(t.tokens)} · ${t.calls} calls`;
      row.appendChild(el("span", "task-detail", detail));
      mount.appendChild(row);
    });
  }

  function render(data) {
    const byModel = data.by_model || [];
    const series = seriesFor(byModel);
    renderCards(document.getElementById("usageCards"), data.totals || {});
    renderLegend(document.getElementById("usageLegend"), byModel, series);
    renderChart(document.getElementById("usageChart"), data.by_day, series);
    renderBars(document.getElementById("usageAgents"), data.by_agent || [], "title");
    renderTasks(document.getElementById("usageTasks"), data.by_task || []);

    const note = document.getElementById("usageNote");
    if (note) {
      const t = data.totals || {};
      note.textContent =
        `${t.calls || 0} model calls over ${data.days} days · `
        + "local calls are free at the point of use; cloud costs are estimated "
        + "from a hand-maintained price table, except Claude Code runs, which "
        + "report what they were charged.";
    }
  }

  window.WrenUsage = { render, fmtTokens, fmtCost, fmtDur, seriesFor, bandFor };

  const mount = document.getElementById("usageChart");
  if (!mount) return;                                    // degrade, don't throw

  let days = 7;

  async function load() {
    let data;
    try {
      const resp = await fetch(`/api/usage?days=${days}`);
      data = await resp.json();
    } catch (err) {
      // A dropped connection or an error page rejects here. Say so — an
      // unhandled rejection would leave the page blank with nothing in it to
      // explain why.
      mount.textContent = `Couldn't load usage (${err.message}).`;
      return;
    }
    // fetch() does not reject on 401; an expired session arrives as a JSON
    // error body.
    if (!data || data.error) {
      mount.textContent = "Couldn't load usage: " + ((data && data.error) || "no data");
      return;
    }
    render(data);
  }

  const range = document.getElementById("usageRange");
  if (range) {
    range.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-days]");
      if (!btn) return;
      days = parseInt(btn.dataset.days, 10) || 7;
      range.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      load();
    });
  }

  load();
})();
