// The /wiki explorer: the learnings wiki drawn as a link graph on a canvas.
// Included as <script src="/static/wiki-graph.js"></script>; like log-view.js its
// contract is page-supplied mounts — <canvas id="graph">, <aside id="detail">,
// <input id="search">, the filter checkboxes, and the buttons named in bind()
// below. The page owns all CSS; this file emits structure and class names.
//
// Everything user-visible goes in through textContent or canvas fillText. Page
// titles and summaries are written by a model out of fetched web content, so
// there is no innerHTML in this file.
//
// Canvas, not SVG: 388 nodes and ~930 edges want pan and zoom on a phone, and
// 388 <g> elements with transforms do not stay smooth there. No library — the
// project has no runtime dependencies, and the layout below is 60 lines.
//
// Three decisions that are not obvious from the code:
//
//   - The layout is plain O(n²) repulsion, no quadtree. 388 nodes is 75k pairs
//     per tick, about a millisecond; a Barnes-Hut tree would be more code than
//     the whole simulation and save nothing at this size.
//   - It runs a fixed number of ticks and then FREEZES, redrawing only on
//     interaction. A permanent requestAnimationFrame loop on a page left open on
//     a phone is a battery drain for a picture that stopped moving.
//   - Filters remove nodes from the simulation, not just from the drawing.
//     Hiding the 100 dated logs has to let the remaining 288 spread into the
//     space, or the filter changes what you see without changing what you can
//     read.
(() => {
  const KINDS = ["concept", "log", "lens", "project"];

  // Matches the dark palette in map.html — wiki cyan is that page's own colour
  // for this vault, so the two views name the same thing the same way.
  const COLOR = {
    concept: "#22d3ee",
    log: "#4b5675",
    lens: "#d98d4a",
    project: "#6f8fd9",
  };

  const TICKS = 300;           // enough to settle at this size; measured, not guessed
  // Ticks per animation frame. One tick per frame is 300 frames — five seconds
  // of watching a vault crawl into shape before it can be read. A tick over 288
  // visible nodes is ~41k pairs and costs well under a millisecond, so four fit
  // in a frame with room to spare and the settle lands near a second.
  const TICKS_PER_FRAME = 4;
  const MIN_ZOOM = 0.15;
  const MAX_ZOOM = 6;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function radius(node) {
    return 3.5 + Math.sqrt(node.deg) * 1.9;
  }

  // --- pure graph helpers ---------------------------------------------------- //

  // A deterministic start beats Math.random: the same vault lays out the same
  // way twice, so a page you found in the corner last time is still there.
  function seedPositions(nodes) {
    nodes.forEach((n, i) => {
      const angle = i * 2.39996;              // golden angle, an even spiral
      const r = 12 * Math.sqrt(i + 1);
      n.x = Math.cos(angle) * r;
      n.y = Math.sin(angle) * r;
      n.vx = 0;
      n.vy = 0;
    });
    return nodes;
  }

  function neighbours(edges, count) {
    const out = Array.from({ length: count }, () => []);
    edges.forEach(([a, b]) => { out[a].push(b); out[b].push(a); });
    return out;
  }

  // Which node indices survive the current filter state. `kinds` is a Set of the
  // kinds left on; `orphansOnly` narrows to nodes nothing links to, which is how
  // you find what a bad ingest stranded.
  function visibleNodes(nodes, kinds, orphansOnly) {
    const keep = new Set();
    nodes.forEach((n, i) => {
      if (!kinds.has(n.kind)) return;
      if (orphansOnly && n.deg > 0) return;
      keep.add(i);
    });
    return keep;
  }

  function visibleEdges(edges, keep) {
    return edges.filter(([a, b]) => keep.has(a) && keep.has(b));
  }

  function searchMatches(nodes, query) {
    const q = (query || "").trim().toLowerCase();
    if (!q) return [];
    return nodes
      .map((n, i) => [n, i])
      .filter(([n]) => n.id.toLowerCase().includes(q) || n.title.toLowerCase().includes(q))
      .map(([, i]) => i);
  }

  // Two ceilings that keep the simulation stable rather than merely tidy.
  //
  // Repulsion is an inverse-square law, so two nodes that land almost on top of
  // each other produce an enormous force. Unclamped, one frame throws them to the
  // far edge, the bounding-box fit then shrinks the whole vault to a speck, and
  // the picture is ruined by two pages.
  //
  // MAX_FORCE is set high on purpose. Repulsion reaches 30 at a separation of
  // about five units — exactly where crowded nodes most need to push apart — so a
  // low ceiling does not stabilise the layout, it collapses the core into an
  // unreadable clump (measured: at 30 this vault's hubs fused into one blob). At
  // 200 the clamp engages only below ~2 units, the pathological case it exists
  // for, and MAX_STEP does the actual stabilising: it caps how far any node moves
  // in one tick, which is what lets several ticks run per frame without the
  // layout exploding.
  const MAX_FORCE = 200;
  const MAX_STEP = 24;

  // One simulation step over the visible subgraph. Repulsion is all-pairs,
  // attraction runs along edges, and a weak pull toward the origin keeps
  // disconnected clusters from drifting off the canvas forever.
  function tick(nodes, edges, keep, alpha) {
    const live = [...keep];
    for (let i = 0; i < live.length; i++) {
      const a = nodes[live[i]];
      for (let j = i + 1; j < live.length; j++) {
        const b = nodes[live[j]];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = (i - j) * 0.01 + 0.01; dy = 0.01; d2 = dx * dx + dy * dy; }
        if (d2 > 250000) continue;            // far enough apart to ignore
        // Repulsion scales with how connected the pair is. A uniform constant
        // tuned to separate ordinary pages leaves this vault's hubs — claude-code
        // and agentos carry ~50 edges each, pulling hard on everything — welded
        // into one blob at the centre with their labels stacked on top of each
        // other. Weighting by degree is what pushes that core open.
        const weight = 1 + (a.deg + b.deg) * 0.06;
        const force = Math.min(1100 * weight / d2, MAX_FORCE);
        const d = Math.sqrt(d2);
        const fx = (dx / d) * force, fy = (dy / d) * force;
        a.vx -= fx; a.vy -= fy;
        b.vx += fx; b.vy += fy;
      }
    }
    for (const [ai, bi] of edges) {
      const a = nodes[ai], b = nodes[bi];
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const force = (d - 58) * 0.032;
      const fx = (dx / d) * force, fy = (dy / d) * force;
      a.vx += fx; a.vy += fy;
      b.vx -= fx; b.vy -= fy;
    }
    for (const i of keep) {
      const n = nodes[i];
      n.vx -= n.x * 0.008;
      n.vy -= n.y * 0.008;
      const step = Math.hypot(n.vx, n.vy) * alpha;
      const scale = step > MAX_STEP ? MAX_STEP / step : 1;
      n.x += n.vx * alpha * scale;
      n.y += n.vy * alpha * scale;
      n.vx *= 0.82;
      n.vy *= 0.82;
    }
  }

  // --- the page -------------------------------------------------------------- //

  function bind() {
    const canvas = document.getElementById("graph");
    const detail = document.getElementById("detail");
    const search = document.getElementById("search");
    const status = document.getElementById("graphStatus");
    const relayout = document.getElementById("relayout");
    const orphansBox = document.getElementById("orphansOnly");
    const ctx = canvas.getContext("2d");

    let nodes = [], edges = [], adj = [], keep = new Set(), liveEdges = [];
    let selected = null, hover = null;
    let view = { x: 0, y: 0, k: 1 };
    let ticksLeft = 0, raf = null;
    // Set the moment the user pans, zooms or drags. The auto-fit below runs
    // while the layout settles, and yanking the view back under someone who has
    // started exploring is worse than an imperfect frame.
    let userMoved = false;
    // A node the camera should stay on — set by a /wiki?page=<slug> deep link,
    // by search, and by following a link chip. It is re-applied every frame
    // while the layout settles rather than once at the end, because the node is
    // still moving until then. Deferring it to the end also made the whole
    // feature depend on the animation actually finishing, and it does not always
    // finish: a browser stops delivering animation frames to a tab that is not
    // visible, so a /wiki link opened in a background tab settled part-way and
    // then simply never centred. Applying it every frame works whenever frames
    // stop. Cleared as soon as the user pans, zooms, or drags.
    let autoFocus = null;

    const kindBoxes = {};
    KINDS.forEach((k) => { kindBoxes[k] = document.getElementById(`kind-${k}`); });

    function activeKinds() {
      return new Set(KINDS.filter((k) => !kindBoxes[k] || kindBoxes[k].checked));
    }

    function resize() {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(rect.width * dpr));
      canvas.height = Math.max(1, Math.round(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function toWorld(px, py) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: (px - rect.left - rect.width / 2 - view.x) / view.k,
        y: (py - rect.top - rect.height / 2 - view.y) / view.k,
      };
    }

    function nodeAt(px, py) {
      const p = toWorld(px, py);
      let best = null, bestD = Infinity;
      for (const i of keep) {
        const n = nodes[i];
        const dx = n.x - p.x, dy = n.y - p.y;
        const d = Math.sqrt(dx * dx + dy * dy);
        // Generous on touch: a 5px dot is not a tap target.
        if (d < Math.max(radius(n) + 6 / view.k, 11 / view.k) && d < bestD) {
          best = i; bestD = d;
        }
      }
      return best;
    }

    function lit() {
      if (selected === null) return null;
      return new Set([selected, ...adj[selected]]);
    }

    function draw() {
      const rect = canvas.getBoundingClientRect();
      ctx.save();
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.translate(rect.width / 2 + view.x, rect.height / 2 + view.y);
      ctx.scale(view.k, view.k);

      const focus = lit();
      ctx.lineWidth = 1 / view.k;
      for (const [a, b] of liveEdges) {
        const on = focus && (focus.has(a) && focus.has(b));
        ctx.strokeStyle = focus ? (on ? "#e3b341" : "#171b2e") : "#1c2136";
        ctx.beginPath();
        ctx.moveTo(nodes[a].x, nodes[a].y);
        ctx.lineTo(nodes[b].x, nodes[b].y);
        ctx.stroke();
      }

      for (const i of keep) {
        const n = nodes[i];
        const dim = focus && !focus.has(i);
        ctx.globalAlpha = dim ? 0.16 : 1;
        ctx.fillStyle = COLOR[n.kind] || COLOR.concept;
        ctx.beginPath();
        ctx.arc(n.x, n.y, radius(n), 0, Math.PI * 2);
        ctx.fill();
        if (i === selected || i === hover) {
          ctx.strokeStyle = "#e8eaf2";
          ctx.lineWidth = 2 / view.k;
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;

      // Labels only where they can be read: zoomed in, or on a hub, or on what
      // is selected. All 388 at once is an unreadable smear.
      ctx.font = `${11 / view.k}px -apple-system, BlinkMacSystemFont, sans-serif`;
      ctx.textAlign = "center";
      for (const i of keep) {
        const n = nodes[i];
        const show = i === selected || i === hover || view.k > 1.4 ||
                     (view.k > 0.9 && n.deg >= 20);
        if (!show) continue;
        if (focus && !focus.has(i)) continue;
        ctx.fillStyle = "#aab1c7";
        ctx.fillText(n.id, n.x, n.y + radius(n) + 12 / view.k);
      }
      ctx.restore();
    }

    function run() {
      if (raf) cancelAnimationFrame(raf);
      const step = () => {
        raf = null;
        if (ticksLeft <= 0) { frameCamera(); draw(); return; }
        for (let i = 0; i < TICKS_PER_FRAME && ticksLeft > 0; i++) {
          tick(nodes, liveEdges, keep, Math.max(0.12, ticksLeft / TICKS));
          ticksLeft -= 1;
        }
        frameCamera();
        draw();
        raf = requestAnimationFrame(step);
      };
      step();
    }

    function applyFilters(restart) {
      keep = visibleNodes(nodes, activeKinds(), orphansBox && orphansBox.checked);
      liveEdges = visibleEdges(edges, keep);
      if (selected !== null && !keep.has(selected)) select(null);
      status.textContent = `${keep.size} of ${nodes.length} pages · ${liveEdges.length} links`;
      if (restart) { ticksLeft = Math.round(TICKS * 0.6); run(); } else { draw(); }
    }

    // Make node `i` visible if a filter is hiding it, by switching its kind back
    // on. The detail panel lists every page a page links to — hiding some of them
    // would misreport the wiki — so a chip has to be able to lead somewhere. Most
    // hub pages link to dated reviews, which are off by default, so without this
    // the commonest chip in the panel is a dead button.
    function reveal(i) {
      if (keep.has(i)) return true;
      const box = kindBoxes[nodes[i].kind];
      if (!box || (orphansBox && orphansBox.checked && nodes[i].deg > 0)) return false;
      box.checked = true;
      applyFilters(false);
      return keep.has(i);
    }

    function select(i) {
      selected = i;
      detail.textContent = "";
      if (i === null) {
        detail.appendChild(el("p", "hint",
          "Tap a page to see what it links to. Pinch or scroll to zoom, drag to pan."));
        draw();
        return;
      }
      const n = nodes[i];
      detail.appendChild(el("h2", null, n.title));
      const meta = el("p", "meta");
      meta.appendChild(el("span", `kind kind-${n.kind}`, n.kind));
      meta.appendChild(el("span", "slug", n.id));
      if (n.updated) meta.appendChild(el("span", "updated", n.updated));
      detail.appendChild(meta);
      if (n.summary) detail.appendChild(el("p", "summary", n.summary));

      const links = adj[i];
      detail.appendChild(el("p", "links-label",
        links.length ? `Links (${links.length})` : "Links to nothing, and nothing links here."));
      const chips = el("div", "chips");
      links.slice().sort((a, b) => nodes[b].deg - nodes[a].deg).forEach((j) => {
        const chip = el("button", "chip" + (keep.has(j) ? "" : " is-hidden"), nodes[j].id);
        chip.type = "button";
        if (!keep.has(j)) chip.title = `hidden by the ${nodes[j].kind} filter — click to show`;
        chip.addEventListener("click", () => {
          if (!reveal(j)) return;
          autoFocus = j;
          centerOn(j);
          select(j);
        });
        chips.appendChild(chip);
      });
      detail.appendChild(chips);

      const read = el("button", "read", "Read page");
      read.type = "button";
      const body = el("pre", "page-body");
      body.hidden = true;
      read.addEventListener("click", async () => {
        if (!body.hidden) { body.hidden = true; read.textContent = "Read page"; return; }
        read.textContent = "…";
        try {
          const data = await (await fetch(`/api/wiki/page/${encodeURIComponent(n.id)}`)).json();
          body.textContent = data.error ? `Could not read this page: ${data.error}` : data.content;
        } catch (e) {
          body.textContent = "Could not read this page.";
        }
        body.hidden = false;
        read.textContent = "Hide page";
      });
      detail.appendChild(read);
      detail.appendChild(body);
      draw();
    }

    // Frame whatever is currently visible. Without this the view opens at zoom 1
    // on a layout ~1500 units across, so you land inside the hairball with no
    // way to know which direction the rest of it is in.
    function fitView() {
      if (!keep.size) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const i of keep) {
        const n = nodes[i], r = radius(n);
        minX = Math.min(minX, n.x - r); maxX = Math.max(maxX, n.x + r);
        minY = Math.min(minY, n.y - r); maxY = Math.max(maxY, n.y + r);
      }
      const rect = canvas.getBoundingClientRect();
      const w = Math.max(maxX - minX, 1), h = Math.max(maxY - minY, 1);
      const k = Math.min(rect.width / w, rect.height / h) * 0.9;
      view.k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k));
      view.x = -((minX + maxX) / 2) * view.k;
      view.y = -((minY + maxY) / 2) * view.k;
    }

    function centerOn(i) {
      const n = nodes[i];
      view.k = Math.max(view.k, 1.6);
      view.x = -n.x * view.k;
      view.y = -n.y * view.k;
      draw();
    }

    // Where the camera goes on a frame the user hasn't taken control of: onto
    // the focused node if there is one, otherwise framing everything visible.
    // The layout expands as it relaxes, so a camera set once and left alone lets
    // the graph grow off the edges before it finishes.
    function frameCamera() {
      if (userMoved) return;
      if (autoFocus !== null && keep.has(autoFocus)) centerOn(autoFocus);
      else fitView();
    }

    // --- input ---------------------------------------------------------------- //

    let dragging = null, dragNode = null, last = null;

    canvas.addEventListener("pointerdown", (e) => {
      canvas.setPointerCapture(e.pointerId);
      last = { x: e.clientX, y: e.clientY };
      const hit = nodeAt(e.clientX, e.clientY);
      dragNode = hit;
      dragging = hit === null ? "pan" : "node";
    });

    canvas.addEventListener("pointermove", (e) => {
      if (!dragging) {
        const hit = nodeAt(e.clientX, e.clientY);
        if (hit !== hover) { hover = hit; canvas.style.cursor = hit === null ? "grab" : "pointer"; draw(); }
        return;
      }
      const dx = e.clientX - last.x, dy = e.clientY - last.y;
      last = { x: e.clientX, y: e.clientY };
      if (Math.abs(dx) > 1 || Math.abs(dy) > 1) userMoved = true;
      if (dragging === "pan") {
        view.x += dx; view.y += dy;
      } else {
        nodes[dragNode].x += dx / view.k;
        nodes[dragNode].y += dy / view.k;
        nodes[dragNode].vx = 0;
        nodes[dragNode].vy = 0;
      }
      draw();
    });

    function endDrag(e) {
      if (dragging === "node" && dragNode !== null &&
          Math.abs(e.clientX - last.x) < 4 && Math.abs(e.clientY - last.y) < 4) {
        select(dragNode === selected ? null : dragNode);
      } else if (dragging === "node") {
        select(dragNode);
      }
      dragging = null;
      dragNode = null;
    }
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", () => { dragging = null; dragNode = null; });

    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      userMoved = true;
      const before = toWorld(e.clientX, e.clientY);
      const k = view.k * Math.exp(-e.deltaY * 0.0016);
      view.k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k));
      const after = toWorld(e.clientX, e.clientY);
      view.x += (after.x - before.x) * view.k;
      view.y += (after.y - before.y) * view.k;
      draw();
    }, { passive: false });

    search.addEventListener("input", () => {
      const hits = searchMatches(nodes, search.value);
      if (hits.length && reveal(hits[0])) {
        autoFocus = hits[0];
        centerOn(hits[0]);
        select(hits[0]);
      }
    });

    KINDS.forEach((k) => {
      if (kindBoxes[k]) kindBoxes[k].addEventListener("change", () => applyFilters(true));
    });
    if (orphansBox) orphansBox.addEventListener("change", () => applyFilters(true));
    relayout.addEventListener("click", () => {
      // "Show me the whole shape again" — so drop both the pinned camera and the
      // user's own panning, or the relaid-out graph stays framed on one node.
      autoFocus = null;
      userMoved = false;
      seedPositions(nodes);
      ticksLeft = TICKS;
      run();
    });
    // A window listener is not enough. The canvas box also changes when the
    // detail panel does — selecting a page, or opening its full text — and on a
    // phone that panel sits BELOW the canvas and takes height from it. The
    // backing store then no longer matches the CSS box and the drawing tears: a
    // stale band of the previous frame across the bottom of the graph. Observing
    // the canvas itself catches every cause, window resizes included. Writing
    // canvas.width does not change its CSS size, so this cannot feed itself.
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => resize()).observe(canvas);
    } else {
      window.addEventListener("resize", resize);
    }

    (async () => {
      let graph;
      try {
        graph = await (await fetch("/api/wiki/graph")).json();
      } catch (e) {
        graph = { error: "Could not reach the wiki." };
      }
      if (graph.error) {
        status.textContent = graph.error;
        return;
      }
      nodes = seedPositions(graph.nodes);
      edges = graph.edges;
      adj = neighbours(edges, nodes.length);

      const dangling = document.getElementById("dangling");
      if (dangling && graph.dangling) {
        dangling.textContent = `${graph.dangling} broken link${graph.dangling === 1 ? "" : "s"}`;
        dangling.hidden = false;
      }

      applyFilters(false);
      resize();
      select(null);

      // A finding on /wiki/lint links here as /wiki?page=<slug>. Landing on the
      // whole graph and hunting for the page would make that link useless. The
      // panel fills straight away; the camera waits for the layout to settle.
      const wanted = new URLSearchParams(window.location.search).get("page");
      if (wanted) {
        // A dated log named by a lint finding is filtered out by default, so
        // reveal it rather than landing on a page that shows nothing.
        const i = nodes.findIndex((n) => n.id === wanted);
        if (i >= 0 && reveal(i)) { autoFocus = i; select(i); }
      }

      ticksLeft = TICKS;
      run();
    })();
  }

  const api = { seedPositions, neighbours, visibleNodes, visibleEdges,
                searchMatches, radius, tick, KINDS, COLOR };
  if (typeof window !== "undefined") window.WrenWikiGraph = api;
  if (typeof document !== "undefined" && document.getElementById("graph")) bind();
})();
