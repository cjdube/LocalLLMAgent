# Run-duration charts

The **Run duration** section at the bottom of `/dashboard`: one small chart per
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
the "deterministic Python owns structure" rule from CLAUDE.md applied to axes:
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

## Known limit: the window is capped by log rotation

`?days=` defaults to 30 and clamps to 1–365, but the real ceiling is what's on
disk. `_rotated_log_paths` reads `<task>.log` plus `.log.1/.2/.3`, so history
older than three rotations is gone. Separately, the `.bak` files sitting in
`logs/` (`weekly_learnings.log.bak` is 368KB, `strava_download.log.bak` 105KB)
don't match that pattern and are invisible to the parser — there is more history
on disk than any chart currently shows.

## Where it lives

- `chat/insights.py` — `run_stats(days, now)`, the windowed per-task series.
  Returns runs **oldest first**, the reverse of `parse_runs`, because that's the
  order a chart plots.
- `chat/routes_dashboard.py` — `GET /api/run_stats`. `?days=` is clamped rather
  than validated: a nonsense window should narrow the chart, not 400 the page.
- `chat/static/run-chart.js` — the renderer. Contract is two page-supplied
  mounts, `#runChart` (required) and `#runChartHint` (optional caption).
- `chat/static/dashboard.html` — the section markup and all the CSS.

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
