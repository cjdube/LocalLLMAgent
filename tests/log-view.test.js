/**
 * Tests for chat/static/log-view.js — the /logs viewer's rendering.
 *
 * Like nav.js and run-chart.js it's a plain <script> in an IIFE, so the source
 * is re-run with `new Function` per test. It exports its pure helpers on
 * window.WrenLogView, which is what most of these exercise; the page wiring is
 * covered through the mounts at the bottom.
 *
 * The assertions that matter are about *not showing markup*: log content is
 * untrusted (it carries fetched page titles, URLs, and model output), so the
 * highlighter must produce text nodes, never parsed HTML. A regression there is
 * invisible in a screenshot and is the reason this file exists.
 */

const fs = require("fs");
const path = require("path");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "log-view.js"), "utf8");

function entry(overrides = {}) {
  return {
    offset: 0,
    ts: "2026-08-05 05:15:04,666",
    level: "info",
    msg: "hello",
    dropped_chars: 0,
    extra: [],
    dropped_lines: 0,
    boundary: null,
    context: false,
    ...overrides,
  };
}

// Re-runs the source against whatever is currently mounted. It must NOT reset
// the DOM: the page-wiring tests below mount their markup first, and the script
// binds to it on load exactly as the real <script> tag does.
function load() {
  new Function(SOURCE)();
  return window.WrenLogView;
}

beforeEach(() => {
  delete window.WrenLogView;
  document.body.innerHTML = "";
});

// --- highlighting --------------------------------------------------------- //

describe("highlight", () => {
  test("splits key=value into a dim key and a coloured value", () => {
    const frag = load().highlight("ollama_chat model=gemma4:26b-mlx eval_tokens=60");
    const host = document.createElement("div");
    host.appendChild(frag);

    expect([...host.querySelectorAll(".tok-key")].map((n) => n.textContent))
      .toEqual(["model=", "eval_tokens="]);
    expect([...host.querySelectorAll(".tok-val")].map((n) => n.textContent))
      .toEqual(["gemma4:26b-mlx", "60"]);
    expect(host.textContent).toBe("ollama_chat model=gemma4:26b-mlx eval_tokens=60");
  });

  test("splits a tool call at the result arrow", () => {
    const host = document.createElement("div");
    host.appendChild(load().highlight('tool_call fetch_strava({"d": 1}) -> {"ok": true}'));

    expect(host.querySelector(".tok-result").textContent).toBe('{"ok": true}');
    expect(host.querySelector(".tok-arrow").textContent).toBe(" → ");
  });

  test("does not treat key=value inside a result as a key", () => {
    // The result half is one span; only the head is scanned for k=v, so a URL
    // query string in a fetched result can't shatter into dozens of spans.
    const host = document.createElement("div");
    host.appendChild(load().highlight("fetch_webpage -> https://x.com/a?utm_source=b&ref=c"));
    expect(host.querySelectorAll(".tok-key").length).toBe(0);
  });

  test("leaves a plain message untouched", () => {
    const host = document.createElement("div");
    host.appendChild(load().highlight("Starting morning brief run"));
    expect(host.textContent).toBe("Starting morning brief run");
    expect(host.children.length).toBe(0);
  });

  test("renders markup in log text as literal characters", () => {
    const nasty = '<img src=x onerror=alert(1)> model=<script>evil</script>';
    const host = document.createElement("div");
    host.appendChild(load().highlight(nasty));

    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector("script")).toBeNull();
    expect(host.textContent).toBe(nasty);
  });
});

// --- JSON payloads -------------------------------------------------------- //

describe("prettyJson", () => {
  test("pretty-prints the payload after a tool-call arrow", () => {
    const out = load().prettyJson('fetch -> {"a":1,"b":[2,3]}');
    expect(out).toBe('{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}');
  });

  test("returns null for truncated JSON rather than throwing", () => {
    // The server caps a message at 4000 chars, so a big payload arrives as
    // invalid JSON by construction — the common case, not an error case.
    expect(load().prettyJson('fetch -> {"a":1,"b":[2,3')).toBeNull();
  });

  test("returns null when there is no payload", () => {
    expect(load().prettyJson("Starting morning brief run")).toBeNull();
  });
});

// --- entries -------------------------------------------------------------- //

describe("renderEntry", () => {
  test("shows the time only — the date lives on the day divider", () => {
    const row = load().renderEntry(entry());
    expect(row.querySelector(".log-time").textContent).toBe("05:15:04");
  });

  test("carries the level as a class so the gutter rail can colour it", () => {
    const row = load().renderEntry(entry({ level: "error" }));
    expect(row.className).toContain("lvl-error");
    expect(row.querySelector(".log-level").textContent).toBe("error");
  });

  test("abbreviates warning to warn so the level column stays aligned", () => {
    const row = load().renderEntry(entry({ level: "warning" }));
    expect(row.querySelector(".log-level").textContent).toBe("warn");
  });

  test("falls back to info styling for an unknown level", () => {
    const row = load().renderEntry(entry({ level: "notice" }));
    expect(row.className).toContain("lvl-info");
  });

  test("marks a context row so the page can dim it", () => {
    expect(load().renderEntry(entry({ context: true })).className).toContain("is-context");
  });

  test("a plain short entry has no fold control", () => {
    expect(load().renderEntry(entry()).querySelector(".log-fold")).toBeNull();
  });

  test("continuation lines collapse behind a fold and expand on click", () => {
    const row = load().renderEntry(entry({
      msg: "Drafted entry:",
      extra: ["## Daily Log", "- one", "- two"],
    }));
    const fold = row.querySelector(".log-fold");
    expect(fold.textContent).toBe("▸ 3 more lines");
    expect(row.querySelector(".log-body")).toBeNull();

    fold.click();
    expect(row.querySelector(".log-body").textContent).toContain("- two");
    expect(fold.getAttribute("aria-expanded")).toBe("true");

    fold.click();
    expect(row.querySelector(".log-body")).toBeNull();
  });

  test("a truncated line says how much was not read", () => {
    const row = load().renderEntry(entry({ msg: "x".repeat(4000), dropped_chars: 42683 }));
    expect(row.querySelector(".log-fold").textContent).toContain("42,683 chars not shown");

    row.querySelector(".log-fold").click();
    expect(row.querySelector(".log-body").textContent).toContain("42,683 more characters");
  });

  test("only the first line of a multi-line message goes on the row", () => {
    const row = load().renderEntry(entry({ msg: "head\ntail" }));
    expect(row.querySelector(".log-msg").textContent).toBe("head");
  });
});

// --- stream --------------------------------------------------------------- //

describe("renderStream", () => {
  const stream = (entries) => {
    const host = document.createElement("div");
    host.appendChild(load().renderStream(entries));
    return host;
  };

  test("renders newest first — the server sends time order", () => {
    const host = stream([
      entry({ offset: 0, ts: "2026-08-05 05:15:00,000", msg: "older" }),
      entry({ offset: 1, ts: "2026-08-05 05:15:02,000", msg: "newer" }),
    ]);
    expect([...host.querySelectorAll(".log-msg")].map((n) => n.textContent))
      .toEqual(["newer", "older"]);
  });

  test("day dividers stay above their own day, newest day first", () => {
    const host = stream([
      entry({ offset: 0, ts: "2026-08-04 23:59:00,000", msg: "yesterday" }),
      entry({ offset: 1, ts: "2026-08-05 00:01:00,000", msg: "today" }),
    ]);
    const rows = [...host.children].map((n) => n.textContent);
    expect(rows[0]).toBe("2026-08-05");
    expect(rows[1]).toContain("today");
    expect(rows[2]).toBe("2026-08-04");
    expect(rows[3]).toContain("yesterday");
  });

  test("a closed run gets one header above its block, carrying the duration", () => {
    const host = stream([
      entry({ offset: 0, ts: "2026-08-05 05:15:00,000", boundary: "start", msg: "Starting run" }),
      entry({ offset: 1, ts: "2026-08-05 05:15:02,000", msg: "middle" }),
      entry({ offset: 2, ts: "2026-08-05 05:15:04,400", boundary: "end", msg: "run complete" }),
    ]);
    const marks = [...host.querySelectorAll(".log-run")];
    expect(marks.length).toBe(1);
    expect(marks[0].textContent).toBe("run 05:15:00 · 4.4s");

    const order = [...host.children].map((n) => n.className.split(" ")[0]);
    expect(order).toEqual(["log-day", "log-run", "log-entry", "log-entry", "log-entry"]);
  });

  test("a run still open at the newest entry says so, at the top", () => {
    const host = stream([
      entry({ offset: 0, ts: "2026-08-05 05:15:00,000", boundary: "start", msg: "Starting run" }),
      entry({ offset: 1, ts: "2026-08-05 05:15:02,000", msg: "still going" }),
    ]);
    expect(host.querySelector(".log-run").textContent).toBe("run 05:15:00 · running");
    expect(host.children[1].className).toContain("log-run");
  });

  test("a run whose start fell on an older page says only what it knows", () => {
    const host = stream([entry({ offset: 0, ts: "2026-08-05 05:15:04,400", boundary: "end" })]);
    expect(host.querySelector(".log-run").textContent).toBe("run ended 05:15:04");
  });

  test("a daemon log with no run marks gets day dividers only", () => {
    const host = stream([entry({ offset: 0 }), entry({ offset: 1 })]);
    expect(host.querySelectorAll(".log-run").length).toBe(0);
    expect(host.querySelectorAll(".log-entry").length).toBe(2);
  });
});

describe("dedupeDays", () => {
  function stack(...blocks) {
    const host = document.createElement("div");
    const view = load();
    blocks.forEach((b) => host.appendChild(view.renderStream(b)));
    return { host, view };
  }

  test("drops the repeated divider where two pages meet on the same day", () => {
    const { host, view } = stack(
      [entry({ offset: 9, ts: "2026-08-05 05:15:41,000", msg: "new" })],
      [entry({ offset: 6, ts: "2026-08-05 05:15:09,000", msg: "old" })],
    );
    expect(host.querySelectorAll(".log-day").length).toBe(2);
    view.dedupeDays(host);
    expect([...host.querySelectorAll(".log-day")].map((n) => n.textContent))
      .toEqual(["2026-08-05"]);
    expect(host.querySelectorAll(".log-entry").length).toBe(2);
  });

  test("keeps a divider that genuinely opens a different day", () => {
    const { host, view } = stack(
      [entry({ offset: 9, ts: "2026-08-05 00:01:00,000" })],
      [entry({ offset: 6, ts: "2026-08-04 23:59:00,000" })],
    );
    view.dedupeDays(host);
    expect([...host.querySelectorAll(".log-day")].map((n) => n.textContent))
      .toEqual(["2026-08-05", "2026-08-04"]);
  });

  test("a day that recurs after another day is kept", () => {
    // Not reachable from a single ordered log, but dedupeDays must compare with
    // the divider immediately above rather than with every one it has seen.
    const { host, view } = stack(
      [entry({ offset: 9, ts: "2026-08-05 00:01:00,000" })],
      [entry({ offset: 6, ts: "2026-08-04 23:59:00,000" })],
      [entry({ offset: 3, ts: "2026-08-05 00:00:00,000" })],
    );
    view.dedupeDays(host);
    expect(host.querySelectorAll(".log-day").length).toBe(3);
  });
});

describe("pairRuns", () => {
  test("keys a closed run on its end, so the header lands on top when reversed", () => {
    const runs = load().pairRuns([
      entry({ offset: 10, boundary: "start" }),
      entry({ offset: 20 }),
      entry({ offset: 30, boundary: "end" }),
    ]);
    expect([...runs.keys()]).toEqual([30]);
    expect(runs.get(30).start.offset).toBe(10);
  });

  test("keys an open run on the newest entry, not on its start", () => {
    const runs = load().pairRuns([
      entry({ offset: 10, boundary: "start" }),
      entry({ offset: 20 }),
    ]);
    expect([...runs.keys()]).toEqual([20]);
    expect(runs.get(20).end).toBeNull();
  });

  test("handles back-to-back runs in one page", () => {
    const runs = load().pairRuns([
      entry({ offset: 10, boundary: "start" }),
      entry({ offset: 20, boundary: "end" }),
      entry({ offset: 30, boundary: "start" }),
      entry({ offset: 40, boundary: "end" }),
    ]);
    expect([...runs.keys()]).toEqual([20, 40]);
    expect(runs.get(40).start.offset).toBe(30);
  });
});

// --- minimap and summary -------------------------------------------------- //

describe("renderMinimap", () => {
  test("colours each bucket by the worst level in it", () => {
    const entries = Array.from({ length: 10 }, () => entry());
    entries[7].level = "error";
    const bar = load().renderMinimap(entries, 5);
    const cells = [...bar.querySelectorAll(".map-cell")];

    expect(cells.length).toBe(5);
    expect(cells[3].className).toContain("lvl-error");
    expect(cells[0].className).toContain("lvl-info");
  });

  test("never exceeds the requested cell count on a large page", () => {
    const entries = Array.from({ length: 803 }, () => entry());
    expect(load().renderMinimap(entries, 60).querySelectorAll(".map-cell").length)
      .toBeLessThanOrEqual(60);
  });

  test("an empty page renders an empty bar rather than throwing", () => {
    expect(load().renderMinimap([]).querySelectorAll(".map-cell").length).toBe(0);
  });
});

describe("summary", () => {
  const base = { size: 662081, counts: { info: 782, warning: 19, error: 2 } };

  test("names the window read, not the file, on a partial scan", () => {
    const text = load().summary({
      ...base, scanned: { from: 150095, to: 662081, entries: 628, complete: false },
    });
    expect(text).toContain("782 info");
    expect(text).toContain("19 warn");
    expect(text).toContain("last 500 KB of 647 KB");
  });

  test("says whole file when the scan reached the start", () => {
    const text = load().summary({
      ...base, scanned: { from: 0, to: 662081, entries: 803, complete: true },
    });
    expect(text).toContain("whole file");
  });

  test("reports an empty window without inventing counts", () => {
    const text = load().summary({
      size: 0, counts: {}, scanned: { from: 0, to: 0, entries: 0, complete: true },
    });
    expect(text).toContain("no entries");
  });
});

// --- page wiring ---------------------------------------------------------- //

describe("page wiring", () => {
  function mount(live = false) {
    document.body.innerHTML = `
      <select id="logPick"></select>
      <select id="logStreamPick"></select>
      <select id="logLevel"><option value="" selected>all levels</option>
        <option value="error">errors only</option></select>
      <input id="logSearch" type="search">
      <input id="logLive" type="checkbox" ${live ? "checked" : ""}>
      <div id="logMap"></div><p id="logNote"></p>
      <div id="logStream"></div>
      <button id="logOlder" hidden></button>`;
  }

  function page(entries, extra = {}) {
    return {
      key: "wren", stream: "log", path: "wren.log", size: 1000, entries,
      counts: { info: entries.length }, matched: entries.length,
      scanned: { from: 0, to: 1000, entries: entries.length, complete: true, skipped: false },
      next_before: null, next_after: 1000, ...extra,
    };
  }

  const CATALOGUE = {
    logs: [
      { key: "morning_brief", display_name: "Morning Brief", is_daemon: false,
        streams: { log: {}, stdout: {} } },
      { key: "wren", display_name: "Wren Chat Server", is_daemon: true,
        streams: { log: {} } },
    ],
  };

  function stubFetch(entries, extra = {}) {
    const calls = [];
    global.fetch = jest.fn((url) => {
      calls.push(url);
      const body = url.indexOf("entries") === -1 ? CATALOGUE : page(entries, extra);
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    });
    return calls;
  }

  const settle = () => new Promise((r) => setTimeout(r, 0));

  test("opens the chat server's log first — the one with no other way in", async () => {
    mount();
    const calls = stubFetch([entry()]);
    load();
    await settle();

    expect(document.getElementById("logPick").value).toBe("wren");
    expect(calls.some((u) => u.includes("key=wren"))).toBe(true);
    expect(document.querySelectorAll("#logStream .log-entry").length).toBe(1);
  });

  test("disables the stream picker when a log has only one stream", async () => {
    mount();
    stubFetch([entry()]);
    load();
    await settle();
    expect(document.getElementById("logStreamPick").disabled).toBe(true);
  });

  test("shows load-older only when the server offers a cursor", async () => {
    mount();
    stubFetch([entry()], { next_before: 4096 });
    load();
    await settle();
    expect(document.getElementById("logOlder").hidden).toBe(false);
  });

  test("a failed read reports it instead of leaving a blank page", async () => {
    mount();
    global.fetch = jest.fn((url) => (url.startsWith("/api/logs?") || url === "/api/logs"
      ? Promise.resolve({ ok: true, json: () => Promise.resolve({ logs: [
          { key: "wren", display_name: "Wren", is_daemon: true, streams: { log: {} } }] }) })
      : Promise.resolve({ ok: false, status: 500 })));
    load();
    await settle();
    expect(document.getElementById("logStream").textContent).toContain("Could not read");
  });

  test("does nothing when the page has no stream mount", () => {
    document.body.innerHTML = "<div>unrelated page</div>";
    global.fetch = jest.fn();
    load();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test("load older appends at the bottom — down is back in time", async () => {
    mount();
    let call = 0;
    global.fetch = jest.fn((url) => {
      if (url.indexOf("entries") === -1) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(CATALOGUE) });
      }
      call += 1;
      const body = call === 1
        ? page([entry({ offset: 500, msg: "recent" })], { next_before: 400 })
        : page([entry({ offset: 100, msg: "ancient" })], { next_before: null });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    });
    load();
    await settle();

    document.getElementById("logOlder").click();
    await settle();

    const msgs = [...document.querySelectorAll("#logStream .log-msg")].map((n) => n.textContent);
    expect(msgs).toEqual(["recent", "ancient"]);
    expect(document.getElementById("logOlder").hidden).toBe(true);
  });
});

describe("live tail", () => {
  beforeEach(() => { jest.useFakeTimers(); });
  afterEach(() => { jest.useRealTimers(); });

  function mountLive() {
    document.body.innerHTML = `
      <select id="logPick"></select><select id="logStreamPick"></select>
      <select id="logLevel"><option value="" selected></option></select>
      <input id="logSearch" type="search">
      <input id="logLive" type="checkbox" checked>
      <div id="logMap"></div><p id="logNote"></p>
      <div id="logStream"></div><button id="logOlder" hidden></button>`;
  }

  const CATALOGUE = {
    logs: [{ key: "wren", display_name: "Wren", is_daemon: true, streams: { log: {} } }],
  };

  function pageBody(entries, extra = {}) {
    return {
      key: "wren", stream: "log", path: "wren.log", size: 1000, entries,
      counts: {}, matched: entries.length,
      scanned: { from: 0, to: 1000, entries: entries.length, complete: true, skipped: false },
      next_before: null, next_after: 1000, ...extra,
    };
  }

  // Fake timers freeze the macrotask queue, so awaited fetches only advance when
  // the microtask queue is drained by hand. bind() chains several (catalogue →
  // load → render), hence the loop rather than a couple of awaits.
  const settle = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

  test("polls with the after cursor and puts new entries on top", async () => {
    mountLive();
    const urls = [];
    global.fetch = jest.fn((url) => {
      urls.push(url);
      if (url.indexOf("entries") === -1) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(CATALOGUE) });
      }
      const body = url.indexOf("after=") === -1
        ? pageBody([entry({ offset: 10, msg: "first" })], { next_after: 500 })
        : pageBody([entry({ offset: 500, msg: "just arrived" })], { next_after: 900 });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    });

    load();
    await settle();
    await settle();

    jest.advanceTimersByTime(4000);
    await settle();
    await settle();

    expect(urls.some((u) => u.includes("after=500"))).toBe(true);
    expect([...document.querySelectorAll("#logStream .log-msg")].map((n) => n.textContent))
      .toEqual(["just arrived", "first"]);
  });

  test("an empty poll changes nothing on the page", async () => {
    mountLive();
    global.fetch = jest.fn((url) => {
      if (url.indexOf("entries") === -1) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(CATALOGUE) });
      }
      const body = url.indexOf("after=") === -1
        ? pageBody([entry({ offset: 10, msg: "first" })], { next_after: 500 })
        : pageBody([], { next_after: 500 });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    });

    load();
    await settle();
    await settle();
    jest.advanceTimersByTime(8000);
    await settle();
    await settle();

    expect(document.querySelectorAll("#logStream .log-entry").length).toBe(1);
  });

  test("unchecking live stops the polling", async () => {
    mountLive();
    const urls = [];
    global.fetch = jest.fn((url) => {
      urls.push(url);
      const body = url.indexOf("entries") === -1 ? CATALOGUE
        : pageBody([entry({ offset: 10 })], { next_after: 500 });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    });

    load();
    await settle();
    await settle();

    const box = document.getElementById("logLive");
    box.checked = false;
    box.dispatchEvent(new Event("change"));
    const seen = urls.length;

    jest.advanceTimersByTime(20000);
    await settle();
    expect(urls.length).toBe(seen);
  });

  test("a failed poll leaves the page standing", async () => {
    mountLive();
    let first = true;
    global.fetch = jest.fn((url) => {
      if (url.indexOf("entries") === -1) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(CATALOGUE) });
      }
      if (first) {
        first = false;
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(pageBody([entry({ offset: 10, msg: "kept" })],
                                               { next_after: 500 })),
        });
      }
      return Promise.resolve({ ok: false, status: 500 });
    });

    load();
    await settle();
    await settle();
    jest.advanceTimersByTime(4000);
    await settle();
    await settle();

    expect([...document.querySelectorAll("#logStream .log-msg")].map((n) => n.textContent))
      .toEqual(["kept"]);
  });
});
