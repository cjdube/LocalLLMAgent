/**
 * Tests for chat/static/settings-form.js — the only page in this repo that
 * writes configuration, and the only one served from chat/static/ that renders
 * a field for every setting Wren has.
 *
 * Like nav.js it's a plain <script> wrapped in an IIFE that runs on load and
 * binds to page-supplied mounts, so each test rebuilds those mounts, stubs
 * fetch, and re-runs the source with `new Function` for a clean render that
 * sidesteps the require cache.
 *
 * What is pinned here is what the script decides on its own, rather than what
 * the API decides (tests/test_routes_settings.py covers that):
 *
 *   * a secret gets no input at all, and a row the environment already answers
 *     is locked — both are rows where an edit would look accepted and change
 *     nothing;
 *   * a save sends only the fields that actually differ from what was loaded;
 *   * the three save banners are distinct, because "already in effect",
 *     "lands at the next run" and "restart the server" are three different
 *     things for the person reading them.
 */

const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "settings-form.js"), "utf8");

const RESTART_COMMAND = "launchctl kickstart -k gui/$UID/local.wren.wren";

// One row, shaped as chat/routes_settings.py:_row builds it.
const row = (over = {}) => ({
  key: "OLLAMA_MODEL",
  group: "Model",
  label: "Model",
  help: "Which Ollama model answers.",
  type: "str",
  applies: "restart",
  editable: true,
  reason: "",
  choices: [],
  minimum: null,
  maximum: null,
  default: "gemma4:26b-mlx",
  secret: false,
  source: "default",
  is_set: false,
  value: "gemma4:26b-mlx",
  ...over,
});

// A secret row has no `value` key at all — absent, not empty. Built by deleting
// it so a change to the default row above can never quietly put one back.
function secretRow(over = {}) {
  const r = row({ key: "NTFY_TOKEN", label: "ntfy token", secret: true,
                  is_set: true, applies: "live", ...over });
  delete r.value;
  return r;
}

const ROWS = [
  row(),
  row({ key: "OLLAMA_KEEP_ALIVE", label: "Keep alive", applies: "live",
        value: "10m", default: "10m" }),
  row({ key: "WREN_CHAT_SUMMARY_CHARS", label: "Summary chars", type: "int",
        applies: "restart", value: "1000", default: "1000",
        minimum: 200, maximum: 40000 }),
  row({ key: "WREN_MORNING_BRIEF_HOURS", label: "Brief window", type: "int",
        applies: "next_run", value: "48", default: "48" }),
  row({ key: "WREN_LLM_BACKEND", label: "Backend", applies: "restart",
        choices: ["ollama", "gemini"], value: "ollama", default: "ollama" }),
  row({ key: "WREN_CHAT_PORT", label: "Port", editable: false,
        reason: "the launchd plist binds this port", value: "8420" }),
  row({ key: "OLLAMA_HOST", label: "Ollama host", source: "env",
        is_set: true, value: "http://127.0.0.1:11434" }),
  secretRow(),
];

const payload = (over = {}) => ({
  groups: [
    { name: "Model", rows: ROWS },
    { name: "Nothing here", rows: [] },
  ],
  preferences: { persona: { user_name: "Craig", positioning: "fractional CTO" } },
  sections: ["persona"],
  warnings: [],
  restart_command: RESTART_COMMAND,
  ...over,
});

const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

// The script's load() and save() are async, and a save chains a second load
// behind itself, so every assertion has to come after several turns of the
// promise queue — one is not enough and gives a passing test by luck.
async function flush() {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

function mountShell() {
  document.body.innerHTML = `
    <div id="settingsBanners"></div>
    <div id="settingsGroups"><p class="empty">Loading…</p></div>
    <div class="savebar">
      <span class="status" id="settingsStatus"></span>
      <button type="button" id="settingsSave" disabled>Save</button>
    </div>`;
}

/** Mount the shell, run the script against `data`, and wait for the render. */
async function start(data = payload()) {
  global.fetch = jest.fn(async () => jsonResponse(data));
  mountShell();
  new Function(SRC)();
  await flush();
  return global.fetch;
}

/** Click Save, answering the POST with `result`, and wait for the render. */
async function save(result, status = 200) {
  global.fetch.mockImplementation(async (url, options) =>
    (options && options.method === "POST")
      ? jsonResponse(result, status)
      : jsonResponse(payload()));
  document.getElementById("settingsSave").click();
  await flush();
}

const field = (key) => document.querySelector(`[data-key="${key}"]`);
const input = (key) => document.getElementById(`set-${key}`);
const banners = () => [...document.getElementById("settingsBanners").children];
const bannerText = () => banners().map((b) => b.textContent);

/** The body of the last POST, parsed. */
function postedBody() {
  const call = global.fetch.mock.calls.find(
    ([, options]) => options && options.method === "POST");
  return call ? JSON.parse(call[1].body) : null;
}

// --------------------------------------------------------------------------- //
// Rendering
// --------------------------------------------------------------------------- //

test("a page without the mounts does not throw", () => {
  document.body.innerHTML = `<div>no mounts here</div>`;
  expect(() => new Function(SRC)()).not.toThrow();
});

test("every non-secret row gets an input, and empty groups are skipped", async () => {
  await start();
  for (const r of ROWS) {
    if (r.secret) continue;
    expect(input(r.key)).not.toBeNull();
  }
  const headings = [...document.querySelectorAll("#settingsGroups h2")]
    .map((h) => h.textContent);
  // "Nothing here" has no rows, so it must not render an empty card. The
  // Preferences card is added by the script, not by the API.
  expect(headings).toEqual(["Model", "Preferences"]);
});

test("a secret gets no input at all, only whether it is set", async () => {
  await start();
  expect(input("NTFY_TOKEN")).toBeNull();
  const tags = [...field("NTFY_TOKEN").querySelectorAll(".tag")]
    .map((t) => t.textContent);
  expect(tags).toContain("set");
  expect(field("NTFY_TOKEN").textContent).toContain("Secrets stay in config/.env");
});

test("an unset secret says not set", async () => {
  await start(payload({
    groups: [{ name: "Model", rows: [secretRow({ is_set: false })] }],
  }));
  const tags = [...field("NTFY_TOKEN").querySelectorAll(".tag")]
    .map((t) => t.textContent);
  expect(tags).toContain("not set");
});

test("a row the environment answers is locked and says what to run", async () => {
  await start();
  // The save would be accepted and the value would not move, because the
  // environment outranks the settings document. Locking it is the honest render.
  expect(input("OLLAMA_HOST").disabled).toBe(true);
  expect(field("OLLAMA_HOST").textContent).toContain("agent.migrate_settings --apply");
});

test("a locked row is disabled and shows the reason the API gave", async () => {
  await start();
  expect(input("WREN_CHAT_PORT").disabled).toBe(true);
  expect(field("WREN_CHAT_PORT").textContent)
    .toContain("the launchd plist binds this port");
});

test("a row with choices renders a select, preselected", async () => {
  await start();
  const el = input("WREN_LLM_BACKEND");
  expect(el.tagName).toBe("SELECT");
  expect([...el.options].map((o) => o.value)).toEqual(["ollama", "gemini"]);
  expect(el.value).toBe("ollama");
});

test("an int row renders a number input carrying its range", async () => {
  await start();
  const el = input("WREN_CHAT_SUMMARY_CHARS");
  expect(el.type).toBe("number");
  expect(el.min).toBe("200");
  expect(el.max).toBe("40000");
});

test("restart and next-run rows are tagged differently", async () => {
  await start();
  const tagsOf = (key) => [...field(key).querySelectorAll(".tag")]
    .map((t) => t.textContent);
  expect(tagsOf("WREN_CHAT_SUMMARY_CHARS")).toContain("needs restart");
  expect(tagsOf("WREN_MORNING_BRIEF_HOURS")).toContain("next run");
  expect(tagsOf("OLLAMA_KEEP_ALIVE")).not.toContain("needs restart");
});

test("a preference section renders as its own JSON textarea", async () => {
  await start();
  const area = document.getElementById("sec-persona");
  expect(area.tagName).toBe("TEXTAREA");
  expect(JSON.parse(area.value)).toEqual(
    { user_name: "Craig", positioning: "fractional CTO" });
});

test("text from the API is inserted as text, never as markup", async () => {
  // chat/static/ is public and the help strings come from the server, but a
  // reason or an error can carry a path a person typed. innerHTML here would
  // make one of them executable.
  await start(payload({
    groups: [{ name: "Model", rows: [row({ help: "<img src=x onerror=boom>" })] }],
  }));
  expect(field("OLLAMA_MODEL").querySelector("img")).toBeNull();
  expect(field("OLLAMA_MODEL").textContent).toContain("<img src=x onerror=boom>");
});

test("a startup warning from the API is shown on load", async () => {
  await start(payload({ warnings: ["OLLAMA_MODEL is set in config/.env; run migrate_settings"] }));
  expect(bannerText()[0]).toContain("OLLAMA_MODEL");
  expect(banners()[0].className).toContain("warn");
});

test("a failed load says so instead of rendering an empty form", async () => {
  global.fetch = jest.fn(async () => jsonResponse({ error: "not authenticated" }, 401));
  mountShell();
  new Function(SRC)();
  await flush();
  expect(document.getElementById("settingsGroups").textContent)
    .toContain("Session expired");
  expect(document.getElementById("settingsSave").disabled).toBe(true);
});

// --------------------------------------------------------------------------- //
// Saving
// --------------------------------------------------------------------------- //

test("only the fields that changed are sent", async () => {
  await start();
  input("OLLAMA_MODEL").value = "gemma5";
  await save({ changed: ["OLLAMA_MODEL"], restart_required: ["OLLAMA_MODEL"],
               next_run_only: [], restart_command: RESTART_COMMAND, warnings: [] });
  // Submitting the whole form would turn every untouched default into a saved
  // override, and would rewrite the document with values nobody looked at.
  expect(postedBody()).toEqual({ values: { OLLAMA_MODEL: "gemma5" }, preferences: {} });
});

test("an untouched form sends nothing at all", async () => {
  await start();
  await save({ changed: [], restart_required: [], next_run_only: [],
               restart_command: RESTART_COMMAND, warnings: [] });
  expect(postedBody()).toEqual({ values: {}, preferences: {} });
  expect(bannerText().join(" ")).toContain("Nothing changed");
});

test("a locked field is never sent, even if something sets its value", async () => {
  await start();
  input("OLLAMA_HOST").value = "http://elsewhere:11434";
  input("WREN_CHAT_PORT").value = "9999";
  input("OLLAMA_MODEL").value = "gemma5";
  await save({ changed: ["OLLAMA_MODEL"], restart_required: [], next_run_only: [],
               restart_command: RESTART_COMMAND, warnings: [] });
  expect(postedBody().values).toEqual({ OLLAMA_MODEL: "gemma5" });
});

test("an edited section is sent as parsed JSON, not as a string", async () => {
  await start();
  document.getElementById("sec-persona").value =
    JSON.stringify({ user_name: "Craig", positioning: "CTO" });
  await save({ changed: ["persona"], restart_required: [], next_run_only: [],
               restart_command: RESTART_COMMAND, warnings: [] });
  expect(postedBody().preferences)
    .toEqual({ persona: { user_name: "Craig", positioning: "CTO" } });
});

test("bad section JSON is caught before the network", async () => {
  await start();
  document.getElementById("sec-persona").value = "{ oops,";
  document.getElementById("settingsSave").click();
  await flush();
  // A stray comma must cost no round trip, and must highlight the same field
  // the server would have highlighted.
  expect(postedBody()).toBeNull();
  const section = document.querySelector(`[data-section="persona"]`);
  expect(section.classList.contains("bad")).toBe(true);
  expect(section.textContent).toContain("not valid JSON");
  expect(document.getElementById("settingsStatus").textContent).toBe("Not saved.");
});

test("a section that parses to a list is refused before the network", async () => {
  await start();
  document.getElementById("sec-persona").value = "[1, 2]";
  document.getElementById("settingsSave").click();
  await flush();
  expect(postedBody()).toBeNull();
  expect(document.querySelector(`[data-section="persona"]`).textContent)
    .toContain("must be a JSON object");
});

test("a refused save marks every bad field and says nothing was saved", async () => {
  await start();
  input("OLLAMA_MODEL").value = "gemma5";
  input("WREN_CHAT_SUMMARY_CHARS").value = "1";
  await save({
    error: "some fields were not accepted",
    field_errors: {
      OLLAMA_MODEL: "OLLAMA_MODEL is not a known setting",
      WREN_CHAT_SUMMARY_CHARS: "WREN_CHAT_SUMMARY_CHARS must be at least 200",
    },
  }, 400);
  expect(field("OLLAMA_MODEL").classList.contains("bad")).toBe(true);
  expect(field("WREN_CHAT_SUMMARY_CHARS").textContent).toContain("at least 200");
  expect(bannerText().join(" ")).toContain("some fields were not accepted");
  expect(document.getElementById("settingsStatus").textContent).toBe("Not saved.");
  // The typed value stays on screen, so the fix is an edit and not a retype.
  expect(input("OLLAMA_MODEL").value).toBe("gemma5");
});

test("a second save clears the errors from the first", async () => {
  await start();
  input("OLLAMA_MODEL").value = "gemma5";
  await save({ error: "some fields were not accepted",
               field_errors: { OLLAMA_MODEL: "no" } }, 400);
  expect(document.querySelectorAll(".field-error")).toHaveLength(1);
  await save({ changed: ["OLLAMA_MODEL"], restart_required: [], next_run_only: [],
               restart_command: RESTART_COMMAND, warnings: [] });
  expect(document.querySelectorAll(".field-error")).toHaveLength(0);
});

test("a good save re-reads the form from the server", async () => {
  await start();
  input("OLLAMA_MODEL").value = "gemma5";
  await save({ changed: ["OLLAMA_MODEL"], restart_required: [], next_run_only: [],
               restart_command: RESTART_COMMAND, warnings: [] });
  // A save changes `source` and can clear a field back to its default, and the
  // server is the only thing that knows which layer answered afterwards.
  const gets = global.fetch.mock.calls.filter(([, o]) => !o || o.method !== "POST");
  expect(gets.length).toBe(2);
  expect(document.getElementById("settingsSave").disabled).toBe(false);
});

test("an unreachable server is a message, not a silent no-op", async () => {
  await start();
  input("OLLAMA_MODEL").value = "gemma5";
  global.fetch.mockImplementation(async (url, options) => {
    if (options && options.method === "POST") throw new Error("network down");
    return jsonResponse(payload());
  });
  document.getElementById("settingsSave").click();
  await flush();
  expect(bannerText().join(" ")).toContain("Could not reach the server");
  expect(document.getElementById("settingsSave").disabled).toBe(false);
});

// --------------------------------------------------------------------------- //
// The three banners
// --------------------------------------------------------------------------- //

test("a live change and a restart change produce two different banners", async () => {
  // The manual check the plan asks for, pinned: OLLAMA_KEEP_ALIVE is read per
  // call and WREN_CHAT_SUMMARY_CHARS is bound at import, so one is done and the
  // other is not, and the page must not say the same thing about both.
  await start();
  input("OLLAMA_KEEP_ALIVE").value = "30m";
  input("WREN_CHAT_SUMMARY_CHARS").value = "1200";
  await save({
    changed: ["OLLAMA_KEEP_ALIVE", "WREN_CHAT_SUMMARY_CHARS"],
    restart_required: ["WREN_CHAT_SUMMARY_CHARS"],
    next_run_only: [],
    restart_command: RESTART_COMMAND,
    warnings: [],
  });
  expect(banners()).toHaveLength(2);

  const [live, restart] = banners();
  expect(live.className).toContain("good");
  expect(live.textContent).toContain("already in effect");
  expect(live.textContent).toContain("OLLAMA_KEEP_ALIVE");
  expect(live.textContent).not.toContain("WREN_CHAT_SUMMARY_CHARS");

  expect(restart.className).toContain("warn");
  expect(restart.textContent).toContain("WREN_CHAT_SUMMARY_CHARS");
  expect(restart.textContent).not.toContain("OLLAMA_KEEP_ALIVE");
  expect(restart.querySelector("code").textContent).toBe(RESTART_COMMAND);
});

test("a next-run change says to do nothing, and asks for no restart", async () => {
  await start();
  input("WREN_MORNING_BRIEF_HOURS").value = "24";
  await save({ changed: ["WREN_MORNING_BRIEF_HOURS"], restart_required: [],
               next_run_only: ["WREN_MORNING_BRIEF_HOURS"],
               restart_command: RESTART_COMMAND, warnings: [] });
  expect(banners()).toHaveLength(1);
  expect(banners()[0].className).toContain("good");
  expect(banners()[0].textContent).toContain("next run");
  expect(banners()[0].querySelector("code")).toBeNull();
});

test("only the restart banner carries the command to run", async () => {
  await start();
  input("OLLAMA_KEEP_ALIVE").value = "30m";
  await save({ changed: ["OLLAMA_KEEP_ALIVE"], restart_required: [],
               next_run_only: [], restart_command: RESTART_COMMAND, warnings: [] });
  expect(document.querySelector("#settingsBanners code")).toBeNull();
  expect(document.querySelector("#settingsBanners button")).toBeNull();
});

test("the copy button says what happened when there is no clipboard", async () => {
  // navigator.clipboard is absent over plain http, which is how this page is
  // reached on the phone. The command is on screen either way.
  await start();
  input("WREN_CHAT_SUMMARY_CHARS").value = "1200";
  await save({ changed: ["WREN_CHAT_SUMMARY_CHARS"],
               restart_required: ["WREN_CHAT_SUMMARY_CHARS"], next_run_only: [],
               restart_command: RESTART_COMMAND, warnings: [] });
  const button = document.querySelector("#settingsBanners button");
  expect(navigator.clipboard).toBeUndefined();
  button.click();
  expect(button.textContent).toBe("Select it above");
});
