# Run-duration charts

The **Run duration** section at the top of `/dashboard`: one small chart per
scheduled task, plotting how long each of its runs took.

## Why duration, and not reliability

The obvious dashboard chart is a success rate, and in this data it carries no
information. Across 26 days and 205 runs there is exactly one failure
(`calendar_colorizer`, 2026-07-25 17:00). A reliability chart would be a solid
green block forever.

The variance is all in *how long*, and the existing dot-strip actively hides it —
ten green dots look the same whether a run took eight seconds or twenty-five
minutes:

| Task | runs | median | max | spread |
|---|---|---|---|---|
| `strava_download` | 37 | 9.9s | 968.8s | 98× |
| `morning_brief` | 32 | 47.7s | 1531.1s | 32× |
| `ai_chat_learnings` | 27 | 167.5s | 3366.6s | 20× |
| `calendar_colorizer` | 33 | 25.8s | 54.1s | 2.1× |
| `daily_synthesis` | 6 | 45.9s | 49.7s | 1.1× |

`ai_chat_learnings` ran for 56 minutes on 2026-07-25 and reported success. Those
tails are the local model — a cold load, or a generation running long — and they
are exactly the class of thing that goes silent for weeks under
"degrade, don't crash". The bottom two rows are the control: the chart
discriminates rather than showing noise everywhere.

## No model, no new persistence

The whole path is deterministic:

```
logs/*.log  →  parse_runs()  →  run_stats()  →  /api/run_stats  →  SVG coords
             (regex parsing)   (window+stats)    (JSON)            (arithmetic)
```

Nothing here calls a model — not the local Gemma, not a cloud backend. This is
the "deterministic Python owns structure" rule from AGENTS.md applied to axes:
scales and path coordinates are structure. Nothing new is written to disk
either; the series is parsed from logs the tasks already produce.

Rendering is hand-rolled SVG in `chat/static/run-chart.js`. A CDN charting
library would mean a local-first dashboard that can't draw itself offline, and
vendoring one is 50–200KB of third-party JS in a repo with no build step. For
one chart type, ~150 lines is the cheaper trade. If this grows to four or five
chart types, revisit — [uPlot](https://github.com/leeoniya/uPlot) vendored
locally would be the option to weigh.

## Two scale decisions

**Log y**, specifically `log10(d + 1)`. Durations span three orders of magnitude
*within a single task*, so on a linear axis every point but the spike sits on
the floor. The `+ 1` is not cosmetic: `log_inspector` genuinely completes in
0.0s, and `log10(0)` is `-Infinity`, which lands in an SVG attribute and draws
an empty chart with no error anywhere.

**x is run index, not time** — one point per run, evenly spaced, oldest left.
Cadence is irregular (`starred_installed` ran three times in one day), and a
real time axis bunches those into an unreadable clump. Each point's tooltip
carries the real timestamp, and the section caption says which axis you're
looking at. If you ever want cadence itself charted, that's a different chart,
not a change to this one.

## Unfinished runs

A run that started and never closed has no duration. It stays in the series with
`duration_s: None` — "started and never finished" is worth seeing — but it is
excluded from the median/max and counted separately, surfacing as
`· 2 unfinished` in the cell caption. A task with *nothing but* unfinished runs
says so rather than rendering an empty box.

## Two bounds: a time window and a point cap

`?days=` defaults to 30 and clamps to 1–365. On top of that, each series is
capped at its **30 most recent runs**.

The cap is about legibility, and the threshold is arithmetic rather than taste:
the plot area is 268 viewBox units wide and the dots are 4.8 across, so past
~57 points they overlap into a smear that still renders and still says nothing.
Nothing today comes close — the busiest task is ~40 runs in 30 days — but
`days` is a request parameter, and the only thing keeping a 30-second poller's
86,400 runs out of here is the accident that interval pollers are classified as
daemons. Degrading into an unreadable-but-correct chart is exactly the quiet
kind of failure this codebase keeps getting bitten by.

Two consequences worth knowing:

- **A busy task's chart spans fewer days than `days`.** The two bounds don't
  agree and the tighter one wins, so different cells can cover different
  periods. Point tooltips carry the real dates. `total` reports the pre-cap
  count so the caption can say `212 of 232 runs · newest 30 per chart` instead
  of claiming a window the drawing doesn't cover.
- **Trimming moves the median.** Every statistic describes the runs actually
  returned, never the discarded ones — a caption must not cite a max that isn't
  drawn. When the cap dropped `strava_download`'s oldest ten runs, its reported
  median went 9.9s → 1.9s, because those ten were the pre-speedup era.

Both are downstream of one rule: the numbers describe the picture.

## Known limit: history is capped by log rotation

The real ceiling on `days` is what's on disk. `_rotated_log_paths` reads
`<task>.log` plus `.log.1/.2/.3`, so history older than three rotations is gone.
Separately, the `.bak` files sitting in `logs/` (`weekly_learnings.log.bak` is
368KB, `strava_download.log.bak` 105KB) don't match that pattern and are
invisible to the parser — there is more history on disk than any chart currently
shows.

## Where it lives

- `chat/insights.py` — `run_stats(days, limit, now)`, the windowed and capped
  per-task series. Returns runs **oldest first**, the reverse of `parse_runs`,
  because that's the order a chart plots.
- `chat/routes_dashboard.py` — `GET /api/run_stats`. `?days=` is clamped rather
  than validated: a nonsense window should narrow the chart, not 400 the page.
- `chat/static/run-chart.js` — the renderer, drivable two ways:
  - **Auto** — a page supplies `#runChart` (the grid) and optionally
    `#runChartHint` (the caption), and the file fetches `/api/run_stats` and
    draws itself. Nothing to call.
  - **Driven** — the page owns the data and the grouping and calls
    `WrenRunCharts.render(mount, tasks, days)` once per mount, plus
    `WrenRunCharts.caption(tasks, days, limit)` for the same caption string.
    The dashboard uses this: one grid per agent, drawn inside that agent's box,
    so a chart sits with the agent that owns the job. A page with no `#runChart`
    never fetches — the driven caller already has the data.
- `chat/static/dashboard.html` — the agent boxes, the fold behaviour, and all
  the CSS. The chart line and card tint read `--ag` / `--ag-bg` off the
  enclosing `.house`, so a chart is drawn in its owner's colour.

## Testing notes

- `run_stats` → `tests/test_insights.py`, with `now` pinned so the window is
  deterministic. No timezone conversion is involved (see the docstring): both
  the log timestamps and `now` are naive-local, so pinning `TIMEZONE` isn't what
  these tests need — pinning `now` is.
- The renderer → `tests/run-chart.test.js` (jest/jsdom); run `npm test`. Most of
  those tests pin arithmetic rather than appearance, because a NaN coordinate
  renders as an empty chart and reports nothing: zero-second runs, a single run
  (no interval to divide by), and runs that all took the same time (no range)
  each have a case.
