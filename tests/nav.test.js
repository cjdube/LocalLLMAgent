/**
 * Tests for chat/static/nav.js — the shared top-nav rendered on every Wren
 * view. Like chat-dock.js it's a plain <script> wrapped in an IIFE that runs on
 * load and binds to a page-supplied mount (<nav id="wren-nav">), so each test
 * sets the current URL, rebuilds the mount, and re-runs the source with
 * `new Function` for a clean render that sidesteps the require cache.
 *
 * The point of the file is a single source of truth for the menu, so the tests
 * pin the canonical view list and the active-item behavior the pages rely on.
 */

const fs = require("fs");
const path = require("path");

const NAV_SRC = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "nav.js"), "utf8");

const VIEWS = ["chat", "dashboard", "logs", "memories", "opportunities", "starred", "games", "wiki", "lint", "map"];

// Render the nav as if the page were served at `pathname`.
function loadNavAt(pathname) {
  window.history.pushState({}, "", pathname);
  document.body.innerHTML = `<nav id="wren-nav"></nav>`;
  new Function(NAV_SRC)();
  return document.getElementById("wren-nav");
}

const labels = (mount) =>
  [...mount.children].map((el) => el.textContent);

test("renders every canonical view, in order", () => {
  const mount = loadNavAt("/dashboard");
  expect(labels(mount)).toEqual(VIEWS);
});

test("the current view is an active, non-link span; the rest are links", () => {
  const mount = loadNavAt("/dashboard");
  const current = [...mount.children].find((el) => el.textContent === "dashboard");
  expect(current.tagName).toBe("SPAN");
  expect(current.classList.contains("active")).toBe(true);
  expect(current.getAttribute("aria-current")).toBe("page");
  expect(current.hasAttribute("href")).toBe(false);

  const others = [...mount.children].filter((el) => el.textContent !== "dashboard");
  expect(others).toHaveLength(VIEWS.length - 1);
  for (const el of others) {
    expect(el.tagName).toBe("A");
    expect(el.getAttribute("href")).toBeTruthy();
    expect(el.classList.contains("active")).toBe(false);
  }
});

test("root path marks the chat view active", () => {
  const mount = loadNavAt("/");
  const chat = [...mount.children].find((el) => el.textContent === "chat");
  expect(chat.tagName).toBe("SPAN");
  expect(chat.classList.contains("active")).toBe(true);
});

test("a trailing slash still matches the active view", () => {
  const mount = loadNavAt("/starred/");
  const starred = [...mount.children].find((el) => el.textContent === "starred");
  expect(starred.tagName).toBe("SPAN");
  expect(starred.classList.contains("active")).toBe(true);
});

test("an unknown path renders all views as links (nothing active)", () => {
  const mount = loadNavAt("/whatever");
  expect([...mount.children].every((el) => el.tagName === "A")).toBe(true);
});

test("a page without the mount does not throw", () => {
  window.history.pushState({}, "", "/dashboard");
  document.body.innerHTML = `<div>no mount here</div>`;
  expect(() => new Function(NAV_SRC)()).not.toThrow();
});
