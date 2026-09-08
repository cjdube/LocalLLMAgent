// The /settings form. PUBLIC FILE: Flask serves chat/static/ at /static/<file>
// with no auth check, so treat everything here as readable by anyone who can
// reach the server. It therefore holds no keys, no values and no copy of the
// schema — it renders only what GET /api/settings hands it, and that endpoint
// is behind the session gate. The page shell lives in chat/views/settings.html,
// which Flask does not serve.
//
// Text goes in with textContent, never innerHTML, matching every other view: a
// help string or an error message is data, and one of them is a path a person
// typed.
(() => {
  const groupsEl = document.getElementById("settingsGroups");
  const railEl = document.getElementById("settingsRail");
  const bannersEl = document.getElementById("settingsBanners");
  const statusEl = document.getElementById("settingsStatus");
  const saveEl = document.getElementById("settingsSave");
  if (!groupsEl || !saveEl) return;                      // degrade, don't throw

  // What the server said when the page loaded. A save sends the fields that
  // differ from this, not every field on the page: submitting an untouched form
  // wholesale would rewrite the document with values the user never looked at,
  // and turn every default into a saved override.
  let loaded = { values: {}, sections: {} };
  const inputs = new Map();                              // key -> input element
  const fields = new Map();                              // key -> its .field row

  // One group on screen at a time. Both maps are keyed by group name, which is
  // what the API sends and what the rail button reads.
  const cards = new Map();                               // group name -> .card
  const railButtons = new Map();                         // group name -> button

  // One accent per tab, so the rail reads as thirteen places rather than
  // thirteen words. Mid-ramp values: text never sits on one, only a dot, a rule
  // under the panel heading and a 10% wash behind the open tab.
  //
  // Handed out by position, not keyed by group name. A name table here would be
  // a second copy of something the schema already owns, in a file served with
  // no auth check — and by position a group added to agent/schema.py gets a
  // colour with no edit to this file.
  const ACCENTS = [
    "#534ab7", "#378add", "#1d9e75", "#d4537e", "#ef9f27", "#e24b4a", "#8e24aa",
    "#0f6e56", "#639922", "#d85a30", "#0891b2", "#6b7280", "#888780",
  ];
  // Which tab is open. Held across a re-render because a save re-reads the
  // whole document, and being thrown back to the first tab after every save
  // would undo the point of having tabs.
  let activeGroup = "";

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function tag(parent, cls, text) {
    parent.appendChild(el("span", `tag ${cls}`, text));
  }

  // ---- rendering ----------------------------------------------------------

  function renderField(row) {
    const field = el("div", "field");
    field.dataset.key = row.key;
    fields.set(row.key, field);

    const head = el("div", "field-head");
    head.appendChild(el("span", "field-label", row.label));
    head.appendChild(el("span", "field-key", row.key));
    if (row.applies === "restart") tag(head, "restart", "needs restart");
    if (row.applies === "next_run") tag(head, "next-run", "next run");
    if (row.secret) tag(head, row.is_set ? "set" : "", row.is_set ? "set" : "not set");
    if (row.source === "env") tag(head, "env", "set in .env");
    if (!row.editable && !row.secret) tag(head, "locked", "read only");
    field.appendChild(head);

    if (row.help) field.appendChild(el("p", "field-help", row.help));

    // A secret gets no input at all. There is no value to put in one — the API
    // never sends it — and a blank box beside a live token invites a person to
    // retype it into a page that would refuse the save anyway.
    if (row.secret) {
      field.appendChild(el("p", "lock-note",
        "Secrets stay in config/.env. This page is only told whether one is set."));
      return field;
    }

    const input = row.choices && row.choices.length
      ? renderSelect(row)
      : renderInput(row);
    input.id = `set-${row.key}`;
    // The environment outranks the settings document, so a row still assigned
    // in config/.env cannot be changed from here — the save would succeed and
    // the value would not move. Locking the input is the honest rendering.
    input.disabled = !row.editable || row.source === "env";
    inputs.set(row.key, input);
    field.appendChild(input);

    if (input.disabled) {
      field.appendChild(el("p", "lock-note", row.source === "env"
        ? "config/.env sets this, and the environment wins. Run: "
          + ".venv/bin/python -m agent.migrate_settings --apply"
        : row.reason));
    }
    return field;
  }

  function renderSelect(row) {
    const select = el("select");
    for (const choice of row.choices) {
      const option = el("option", null, choice);
      option.value = choice;
      select.appendChild(option);
    }
    select.value = row.value || "";
    return select;
  }

  function renderInput(row) {
    const input = el("input");
    // type="number" only where the schema says the field is one, so a phone
    // shows the numeric keypad for a port and the full keyboard for a path.
    input.type = (row.type === "int" || row.type === "float") ? "number" : "text";
    if (row.minimum !== null && row.minimum !== undefined) input.min = String(row.minimum);
    if (row.maximum !== null && row.maximum !== undefined) input.max = String(row.maximum);
    if (row.default) input.placeholder = row.default;
    input.value = row.value || "";
    return input;
  }

  function renderSection(name, value) {
    const field = el("div", "field");
    field.dataset.section = name;
    fields.set(name, field);

    const head = el("div", "field-head");
    head.appendChild(el("span", "field-label", name));
    tag(head, "restart", "needs restart");
    field.appendChild(head);
    field.appendChild(el("p", "field-help",
      "Edited as JSON. A section is saved whole, never merged, and a bad shape "
      + "is refused with the reason before anything is written."));

    const area = el("textarea");
    area.id = `sec-${name}`;
    area.value = JSON.stringify(value, null, 2);
    inputs.set(name, area);
    field.appendChild(area);
    return field;
  }

  // ---- the tab rail -------------------------------------------------------

  // One card and one rail button per group. The card keeps its group name in a
  // data attribute so a field can name its own tab later, when a save comes
  // back with an error against a row that is not on screen.
  function addPanel(name) {
    const accent = ACCENTS[cards.size % ACCENTS.length];

    const card = el("div", "card");
    card.dataset.group = name;
    card.style.setProperty("--accent", accent);
    card.appendChild(el("h2", null, name));
    groupsEl.appendChild(card);
    cards.set(name, card);

    if (railEl) {
      const button = el("button");
      button.type = "button";
      button.style.setProperty("--accent", accent);
      // The dot carries the colour, so the label stays plain text at full
      // contrast. A coloured label would have to pass a contrast bar the accent
      // was not picked for.
      button.appendChild(el("span", "dot"));
      button.appendChild(el("span", "rail-label", name));
      button.addEventListener("click", () => activate(name));
      railEl.appendChild(button);
      railButtons.set(name, button);
    }
    return card;
  }

  function activate(name) {
    if (!cards.has(name)) return;
    activeGroup = name;
    for (const [group, card] of cards) {
      card.classList.toggle("active", group === name);
    }
    for (const [group, button] of railButtons) {
      if (group === name) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    }
    // Keep the open chip in view on a phone, where the rail scrolls sideways
    // and the tab a save jumped to may be off the right edge.
    const button = railButtons.get(name);
    if (button && button.scrollIntoView) {
      button.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function render(data, keepBanners) {
    groupsEl.replaceChildren();
    if (railEl) railEl.replaceChildren();
    inputs.clear();
    fields.clear();
    cards.clear();
    railButtons.clear();
    loaded = { values: {}, sections: {} };

    for (const group of data.groups) {
      const rows = group.rows || [];
      if (!rows.length) continue;
      const card = addPanel(group.name);
      for (const row of rows) {
        if (!row.secret) loaded.values[row.key] = row.value || "";
        card.appendChild(renderField(row));
      }
    }

    const card = addPanel("Preferences");
    for (const name of data.sections) {
      const value = (data.preferences || {})[name] || {};
      loaded.sections[name] = JSON.stringify(value, null, 2);
      card.appendChild(renderSection(name, value));
    }

    // Back to the tab that was open, if it still exists. It will not on the
    // first load, so that falls through to the first one.
    activate(cards.has(activeGroup) ? activeGroup : (cards.keys().next().value || ""));

    saveEl.disabled = false;
    // A save re-reads the form, and that re-read must not wipe the banner the
    // save just raised — the restart one is the whole reason the page tells you
    // anything. reportSaved has already rendered the warnings from the save's
    // own response, which are the fresher copy.
    if (!keepBanners) showWarnings(data.warnings);
  }

  // ---- banners ------------------------------------------------------------

  function banner(kind, lines, code, copyLabel) {
    const box = el("div", `banner ${kind}`);
    for (const line of lines) box.appendChild(el("p", null, line));
    if (code) {
      box.appendChild(el("code", null, code));
      const button = el("button", null, copyLabel || "Copy");
      button.type = "button";
      button.addEventListener("click", () => {
        // navigator.clipboard is absent over plain http and in older WebViews;
        // the command is on screen either way, so say what happened rather than
        // throwing behind a button that looks like it worked.
        const done = navigator.clipboard && navigator.clipboard.writeText;
        if (!done) { button.textContent = "Select it above"; return; }
        navigator.clipboard.writeText(code)
          .then(() => { button.textContent = "Copied"; })
          .catch(() => { button.textContent = "Select it above"; });
      });
      box.appendChild(button);
    }
    bannersEl.appendChild(box);
    return box;
  }

  function showWarnings(warnings) {
    bannersEl.replaceChildren();
    for (const text of warnings || []) banner("warn", [text]);
  }

  // ---- saving -------------------------------------------------------------

  function clearErrors() {
    for (const field of groupsEl.querySelectorAll(".field.bad")) {
      field.classList.remove("bad");
      const message = field.querySelector(".field-error");
      if (message) message.remove();
    }
    for (const button of railButtons.values()) {
      const flag = button.querySelector(".flag");
      if (flag) flag.remove();
    }
  }

  function showFieldErrors(errors) {
    // Looked up in the map built during render, not by a selector: a key comes
    // back from the server inside an error, and building a selector out of it
    // would need escaping to stay correct.
    const bad = new Set();
    for (const [key, message] of Object.entries(errors || {})) {
      const field = fields.get(key);
      if (!field) continue;
      field.classList.add("bad");
      field.appendChild(el("p", "field-error", message));
      const card = field.closest(".card");
      if (card && card.dataset.group) bad.add(card.dataset.group);
    }
    if (!bad.size) return;

    // The Save button writes every tab at once, so a refused field is usually
    // on a tab you are not looking at. Flag those tabs and open the first one:
    // otherwise the page says "Not saved" and shows nothing that explains it.
    let first = "";
    for (const [group, button] of railButtons) {
      if (!bad.has(group)) continue;
      if (!first) first = group;
      if (!button.querySelector(".flag")) button.appendChild(el("span", "flag", "!"));
    }
    if (first && first !== activeGroup) activate(first);
  }

  // Only what the user actually changed. Also where a malformed section is
  // caught before the network: JSON.parse fails here with the same field
  // highlighting the server would produce, so a stray comma costs no round trip.
  function collect() {
    const values = {};
    const sections = {};
    const localErrors = {};

    for (const [key, input] of inputs) {
      if (input.disabled) continue;
      if (input.tagName === "TEXTAREA") {
        if (input.value === loaded.sections[key]) continue;
        try {
          const parsed = JSON.parse(input.value);
          if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
            localErrors[key] = `${key} must be a JSON object`;
            continue;
          }
          sections[key] = parsed;
        } catch (e) {
          localErrors[key] = `${key} is not valid JSON: ${e.message}`;
        }
        continue;
      }
      if (input.value === loaded.values[key]) continue;
      values[key] = input.value;
    }
    return { values, sections, localErrors };
  }

  function reportSaved(result) {
    bannersEl.replaceChildren();
    for (const text of result.warnings || []) banner("warn", [text]);

    if (!result.changed || !result.changed.length) {
      banner("good", ["Nothing changed — the form already matched what is saved."]);
      return;
    }
    const live = result.changed.filter(
      (k) => !result.restart_required.includes(k) && !result.next_run_only.includes(k));
    if (live.length) {
      banner("good", [`Saved and already in effect: ${live.join(", ")}.`]);
    }
    if (result.next_run_only.length) {
      banner("good", [
        `Saved: ${result.next_run_only.join(", ")}. These are read by the `
        + "scheduled jobs, so the change lands at their next run. Nothing to do."]);
    }
    // The restart banner is deliberately the loud one, and deliberately not a
    // button: the server runs under launchd KeepAlive, so a route that killed
    // its own process would be a self-DoS if the save that preceded it was
    // wrong.
    if (result.restart_required.length) {
      banner("warn", [
        `Saved: ${result.restart_required.join(", ")}. These are read once when `
        + "the chat server starts, so the running server still has the old "
        + "value. Restart it:"],
        result.restart_command, "Copy command");
    }
  }

  async function save() {
    clearErrors();
    const { values, sections, localErrors } = collect();
    if (Object.keys(localErrors).length) {
      showFieldErrors(localErrors);
      statusEl.textContent = "Not saved.";
      return;
    }

    saveEl.disabled = true;
    statusEl.textContent = "Saving…";
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values, preferences: sections }),
      });
      const result = await res.json();
      if (!res.ok) {
        showFieldErrors(result.field_errors);
        bannersEl.replaceChildren();
        banner("bad", [result.error || "The save was refused."]);
        statusEl.textContent = "Not saved.";
        return;
      }
      reportSaved(result);
      statusEl.textContent = "";
      // Re-read rather than patch what is on screen: a save changes `source`
      // and can clear a field back to its default, and the server is the only
      // thing that knows which layer answered afterwards.
      await load(true);
    } catch (e) {
      bannersEl.replaceChildren();
      banner("bad", [`Could not reach the server: ${e.message}`]);
      statusEl.textContent = "Not saved.";
    } finally {
      saveEl.disabled = false;
    }
  }

  // ---- loading ------------------------------------------------------------

  async function load(keepBanners) {
    try {
      const res = await fetch("/api/settings");
      if (!res.ok) {
        groupsEl.replaceChildren(el("p", "empty",
          res.status === 401 ? "Session expired — reload and sign in again."
                             : `Could not load settings (${res.status}).`));
        return;
      }
      render(await res.json(), keepBanners);
    } catch (e) {
      groupsEl.replaceChildren(el("p", "empty", `Could not load settings: ${e.message}`));
    }
  }

  saveEl.addEventListener("click", save);
  load();
})();
