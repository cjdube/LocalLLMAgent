// The /logs viewer: turns the entry list from /api/logs/entries into a readable
// stream. Included as <script src="/static/log-view.js"></script>; like nav.js
// and run-chart.js its contract is page-supplied mounts — <select id="logPick">,
// <div id="logStream">, and the filter controls named in bind() below. The page
// owns all CSS; this file emits structure and class names only.
//
// Class names to style: .log-day, .log-run, .log-entry with .lvl-<level> and
// .is-context, .log-time, .log-level, .log-msg, .log-fold, .log-body, .log-note.
//
// Everything user-visible goes in through textContent. Log content is untrusted
// by definition — it is full of URLs, page titles, and model output fetched from
// the web — so there is no innerHTML anywhere in this file, and the highlighter
// below builds spans as DOM nodes rather than as markup strings.
//
// Three rendering decisions come straight from what these logs actually contain:
//
//   - The date moves to a divider row, leaving the time on the entry. Every line
//     starts with the same 23-character stamp otherwise, which is 23 characters
//     of noise on every row of an 800-entry page.
//   - Long messages and continuation blocks fold. ~31% of lines here are
//     continuations (a drafted digest, a traceback) and the longest single line
//     on record is 46,683 chars, so "show it all" is not an option.
//   - The value half of `k=v` and the result half of `name(args) -> result` are
//     coloured, because that is the grammar the loggers in agent/loop.py and
//     tasks/ actually emit. Highlighting those two shapes is what makes a line
//     like `ollama_chat model=… prompt_tokens=5362 eval_tokens=60` scannable.
(() => {
  const LEVELS = ["debug", "info", "warning", "error", "critical"];

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // --- message highlighting ------------------------------------------------ //

  // Splits on the two shapes the loggers emit, in one pass:
  //   key=value            -> .tok-key + .tok-val
  //   ' -> '               -> .tok-arrow, everything after it is .tok-result
  // Anything unmatched stays plain. Returns a DocumentFragment.
  function highlight(msg) {
    const frag = document.createDocumentFragment();
    const arrow = msg.indexOf(" -> ");
    const head = arrow === -1 ? msg : msg.slice(0, arrow);

    const re = /([A-Za-z_][\w.]*)=(\S+)/g;
    let last = 0;
    let m;
    while ((m = re.exec(head)) !== null) {
      if (m.index > last) frag.appendChild(document.createTextNode(head.slice(last, m.index)));
      frag.appendChild(el("span", "tok-key", m[1] + "="));
      frag.appendChild(el("span", "tok-val", m[2]));
      last = m.index + m[0].length;
    }
    if (last < head.length) frag.appendChild(document.createTextNode(head.slice(last)));

    if (arrow !== -1) {
      frag.appendChild(el("span", "tok-arrow", " → "));
      frag.appendChild(el("span", "tok-result", msg.slice(arrow + 4)));
    }
    return frag;
  }

  // Pretty-print a JSON payload if it parses, else hand back the raw text.
  // A truncated message (the server caps at 4000 chars) is invalid JSON by
  // construction, so failing to parse is the normal case, not an error case.
  function prettyJson(text) {
    const start = text.search(/[[{]/);
    if (start === -1) return null;
    try {
      return JSON.stringify(JSON.parse(text.slice(start)), null, 2);
    } catch (e) {
      return null;
    }
  }

  // --- entry rendering ----------------------------------------------------- //

  function foldable(entry) {
    return entry.extra.length > 0 || entry.dropped_chars > 0 || prettyJson(entry.msg) !== null;
  }

  function foldLabel(entry) {
    const parts = [];
    if (entry.extra.length) parts.push(`${entry.extra.length} more lines`);
    if (entry.dropped_lines) parts.push(`${entry.dropped_lines.toLocaleString()} lines not shown`);
    if (entry.dropped_chars) parts.push(`${entry.dropped_chars.toLocaleString()} chars not shown`);
    if (!parts.length) parts.push("expand");
    return parts.join(" · ");
  }

  function body(entry) {
    const box = el("pre", "log-body");
    const pretty = prettyJson(entry.msg);
    const chunks = [];
    if (pretty) chunks.push(pretty);
    if (entry.extra.length) chunks.push(entry.extra.join("\n"));
    if (entry.dropped_chars) {
      chunks.push(`… ${entry.dropped_chars.toLocaleString()} more characters in this line, not read`);
    }
    if (entry.dropped_lines) {
      chunks.push(`… ${entry.dropped_lines.toLocaleString()} more lines in this entry, not read`);
    }
    box.textContent = chunks.join("\n\n");
    return box;
  }

  function renderEntry(entry) {
    const level = LEVELS.includes(entry.level) ? entry.level : "info";
    const row = el("div", `log-entry lvl-${level}` + (entry.context ? " is-context" : ""));
    row.appendChild(el("span", "log-time", entry.ts.slice(11, 19)));
    row.appendChild(el("span", "log-level", level === "warning" ? "warn" : level));

    const msg = el("span", "log-msg");
    // The first line only — the fold owns everything below it.
    msg.appendChild(highlight(entry.msg.split("\n")[0]));
    row.appendChild(msg);

    if (foldable(entry)) {
      const fold = el("button", "log-fold", "▸ " + foldLabel(entry));
      fold.setAttribute("aria-expanded", "false");
      let open = null;
      fold.addEventListener("click", () => {
        if (open) {
          open.remove();
          open = null;
          fold.textContent = "▸ " + foldLabel(entry);
          fold.setAttribute("aria-expanded", "false");
          return;
        }
        open = body(entry);
        row.appendChild(open);
        fold.textContent = "▾ " + foldLabel(entry);
        fold.setAttribute("aria-expanded", "true");
      });
      row.appendChild(fold);
    }
    return row;
  }

  // --- stream assembly ----------------------------------------------------- //

  function fmtDur(a, b) {
    const ms = Date.parse(b.replace(" ", "T").replace(",", ".")) -
               Date.parse(a.replace(" ", "T").replace(",", "."));
    if (!isFinite(ms) || ms < 0) return "";
    return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  // Pairs each run's start with its end, keyed by the offset of the entry the
  // run's HEADER should sit above once the stream is reversed.
  //
  // Pairing has to happen forwards — a start is only known to be open until an
  // end shows up — but rendering is backwards, and in reverse the first entry of
  // a run block is its END. So a closed run is keyed on its end, and a run still
  // open at the newest entry is keyed on that entry, which puts both headers at
  // the top of their block. A run whose start fell on an older page pairs with
  // null and says only what it knows.
  function pairRuns(entries) {
    const runs = new Map();
    let open = null;
    entries.forEach((entry) => {
      if (entry.boundary === "start") {
        open = entry;
      } else if (entry.boundary === "end") {
        runs.set(entry.offset, { start: open, end: entry });
        open = null;
      }
    });
    if (open && entries.length) runs.set(entries[entries.length - 1].offset, { start: open, end: null });
    return runs;
  }

  function runHeader(run) {
    if (!run.end) return el("div", "log-run", `run ${run.start.ts.slice(11, 19)} · running`);
    if (!run.start) return el("div", "log-run", `run ended ${run.end.ts.slice(11, 19)}`);
    return el("div", "log-run",
      `run ${run.start.ts.slice(11, 19)} · ${fmtDur(run.start.ts, run.end.ts)}`);
  }

  // NEWEST FIRST. The server returns time order because pairing runs and
  // attaching continuation lines are both natural forwards; the reversal is
  // here, at the last possible moment, so only this function has to think in
  // two directions.
  //
  // Day dividers and run headers still sit ABOVE the rows they introduce, which
  // is the whole reason this isn't just a reversed loop over renderEntry.
  function renderStream(entries) {
    const runs = pairRuns(entries);
    const frag = document.createDocumentFragment();
    let day = null;

    entries.slice().reverse().forEach((entry) => {
      const entryDay = entry.ts.slice(0, 10);
      if (entryDay !== day) {
        day = entryDay;
        frag.appendChild(el("div", "log-day", entryDay));
      }
      const run = runs.get(entry.offset);
      if (run) frag.appendChild(runHeader(run));
      frag.appendChild(renderEntry(entry));
    });
    return frag;
  }

  // Drops a day divider that repeats the one above it.
  //
  // renderStream works on one page at a time and can't see its neighbours, so
  // both directions produce a repeat where two pages meet on the same day: the
  // live tail prepends a block that reopens today, and "load older" appends a
  // block that reopens the day already showing above it. Reconciling here keeps
  // renderStream a pure function of its own entries.
  function dedupeDays(container) {
    let last = null;
    [...container.children].forEach((node) => {
      if (!node.classList.contains("log-day")) return;
      if (node.textContent === last) node.remove();
      else last = node.textContent;
    });
  }

  // A one-bar overview of the scanned window: each cell is a bucket of entries
  // coloured by the worst level in it, so a bad patch in an 800-entry page is
  // visible without scrolling. 60 cells regardless of page size — past that they
  // stop being separable and the bar stops saying anything.
  function renderMinimap(entries, cells = 60) {
    const bar = el("div", "log-map");
    if (!entries.length) return bar;
    const per = Math.ceil(entries.length / cells);
    for (let i = 0; i < entries.length; i += per) {
      const slice = entries.slice(i, i + per);
      let worst = "info";
      slice.forEach((e) => {
        if (LEVELS.indexOf(e.level) > LEVELS.indexOf(worst)) worst = e.level;
      });
      const cell = el("span", `map-cell lvl-${worst}`);
      cell.title = `${slice[0].ts.slice(11, 19)} – ${slice[slice.length - 1].ts.slice(11, 19)}`;
      bar.appendChild(cell);
    }
    return bar;
  }

  function summary(data) {
    const counts = data.counts || {};
    const bits = LEVELS.filter((l) => counts[l])
      .map((l) => `${counts[l].toLocaleString()} ${l === "warning" ? "warn" : l}`);
    // Say what was read, not what the file holds. The server scans a fixed
    // window backwards from the end, so "3 errors" would otherwise read as a
    // claim about a file it only partly looked at.
    const scope = data.scanned.complete
      ? `whole file (${Math.round(data.size / 1024).toLocaleString()} KB)`
      : `last ${Math.round((data.scanned.to - data.scanned.from) / 1024).toLocaleString()} KB of ${Math.round(data.size / 1024).toLocaleString()} KB`;
    return `${bits.join(" · ") || "no entries"} in ${scope}`;
  }

  window.WrenLogView = { highlight, prettyJson, renderEntry, renderStream, renderMinimap,
                         summary, foldLabel, pairRuns, dedupeDays };

  // --- page wiring --------------------------------------------------------- //

  function bind() {
    const stream = document.getElementById("logStream");
    if (!stream) return;                                  // degrade, don't throw

    const pick = document.getElementById("logPick");
    const streamPick = document.getElementById("logStreamPick");
    const levelPick = document.getElementById("logLevel");
    const search = document.getElementById("logSearch");
    const mapMount = document.getElementById("logMap");
    const note = document.getElementById("logNote");
    const older = document.getElementById("logOlder");
    const liveBox = document.getElementById("logLive");
    let catalogue = [];
    let oldest = null;
    let newest = null;
    let timer = null;

    async function api(url) {
      const res = await fetch(url, { headers: { "Accept": "application/json" } });
      if (!res.ok) throw new Error(`${res.status}`);
      return res.json();
    }

    function params() {
      const q = new URLSearchParams({ key: pick.value, stream: streamPick.value });
      if (levelPick.value) q.set("level", levelPick.value);
      if (search.value.trim()) q.set("q", search.value.trim());
      return q;
    }

    function syncStreams() {
      const entry = catalogue.find((l) => l.key === pick.value);
      streamPick.textContent = "";
      if (!entry) return;
      Object.keys(entry.streams).forEach((name) => {
        const opt = el("option", null, name === "log" ? "structured log" : "launchd stdout");
        opt.value = name;
        streamPick.appendChild(opt);
      });
      streamPick.disabled = Object.keys(entry.streams).length < 2;
    }

    function setNote(data) {
      if (!note) return;
      const filtered = (levelPick.value || search.value.trim())
        ? ` · ${data.matched.toLocaleString()} shown by filter` : "";
      note.textContent = summary(data) + filtered;
    }

    async function load() {
      try {
        const data = await api("/api/logs/entries?" + params().toString());
        stream.textContent = "";
        if (mapMount) {
          mapMount.textContent = "";
          mapMount.appendChild(renderMinimap(data.entries));
        }
        stream.appendChild(renderStream(data.entries));
        oldest = data.next_before;
        newest = data.next_after;
        if (older) older.hidden = !oldest;
        setNote(data);
        if (!data.entries.length) {
          stream.appendChild(el("p", "log-note", "Nothing matches in the window read."));
        }
      } catch (e) {
        stream.textContent = "";
        stream.appendChild(el("p", "log-note", "Could not read that log."));
      }
    }

    // Older entries append at the BOTTOM now: newest-first means scrolling down
    // is going back in time, so that is where the next page belongs.
    async function loadOlder() {
      if (!oldest) return;
      const q = params();
      q.set("before", oldest);
      try {
        const data = await api("/api/logs/entries?" + q.toString());
        stream.appendChild(renderStream(data.entries));
        dedupeDays(stream);
        oldest = data.next_before;
        if (older) older.hidden = !oldest;
      } catch (e) {
        if (older) older.hidden = true;
      }
    }

    // The live tail. New entries go on TOP, which is where the eye already is,
    // and the scroll position is held steady so reading something further down
    // isn't yanked around every time a line arrives.
    async function poll() {
      if (newest === null || document.hidden) return;
      const q = params();
      q.set("after", newest);
      let data;
      try {
        data = await api("/api/logs/entries?" + q.toString());
      } catch (e) {
        return;                        // a failed poll is not a failed page
      }
      newest = data.next_after;
      if (!data.entries.length) return;

      const before = document.documentElement.scrollHeight;
      const anchored = window.scrollY;
      stream.insertBefore(renderStream(data.entries), stream.firstChild);
      dedupeDays(stream);
      const grew = document.documentElement.scrollHeight - before;
      if (anchored > 0 && grew > 0) window.scrollTo(0, anchored + grew);
    }

    function setLive(on) {
      clearInterval(timer);
      timer = on ? setInterval(poll, 4000) : null;
    }

    let debounce;
    function reload() {
      oldest = null;
      newest = null;
      clearTimeout(debounce);
      debounce = setTimeout(load, 150);
    }

    pick.addEventListener("change", () => { syncStreams(); reload(); });
    streamPick.addEventListener("change", reload);
    levelPick.addEventListener("change", reload);
    search.addEventListener("input", reload);
    if (older) older.addEventListener("click", loadOlder);
    if (liveBox) {
      liveBox.addEventListener("change", () => setLive(liveBox.checked));
      setLive(liveBox.checked);
    }

    (async () => {
      try {
        catalogue = (await api("/api/logs")).logs;
      } catch (e) {
        stream.appendChild(el("p", "log-note", "Could not load the log list."));
        return;
      }
      catalogue.forEach((entry) => {
        const opt = el("option", null,
          `${entry.display_name}${entry.is_daemon ? " (always on)" : ""}`);
        opt.value = entry.key;
        pick.appendChild(opt);
      });
      // The chat server's log is the one with no other way in — the dashboard's
      // run drawer refuses daemons — so it opens first.
      const wren = catalogue.find((l) => l.key === "wren");
      pick.value = wren ? wren.key : (catalogue[0] && catalogue[0].key);
      syncStreams();
      load();
    })();
  }

  if (typeof document !== "undefined" && document.getElementById("logStream")) bind();
})();
