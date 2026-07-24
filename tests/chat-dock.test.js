/**
 * Tests for chat/static/chat-dock.js — the chat dock shared by /chat and the
 * dashboard's side panel.
 *
 * The dock is a plain <script>, not a module: it wraps itself in an IIFE that
 * runs on load and binds to elements the page supplies. So rather than adding
 * exports to production code just to test it, each test rebuilds the markup and
 * re-runs the source with `new Function`, which gives a clean dock per test and
 * sidesteps the require cache.
 *
 * The network is never touched — global.fetch is stubbed in every test. Most of
 * these guard the failure paths, because a failed turn is what actually broke:
 * a rejected fetch left the typing dots animating over a permanently disabled
 * composer, looking exactly like Wren thinking while nothing ran.
 */

const fs = require("fs");
const path = require("path");

const DOCK_SRC = fs.readFileSync(
  path.join(__dirname, "..", "chat", "static", "chat-dock.js"), "utf8");

// The dock's contract with a page is this markup: the ids it binds to. Both
// index.html and dashboard.html supply exactly these.
const MARKUP = `
  <div id="messages"></div>
  <form id="composer">
    <input id="input">
    <button id="send" type="submit">Send</button>
  </form>
  <button id="newChat">New chat</button>
`;

function loadDock() {
  document.body.innerHTML = MARKUP;
  new Function(DOCK_SRC)();
}

const $ = (sel) => document.querySelector(sel);
const messages = () => document.getElementById("messages");
const input = () => document.getElementById("input");
const sendBtn = () => document.getElementById("send");
const lastMessage = () => messages().lastElementChild.textContent;
const dots = () => document.querySelectorAll(".msg.typing .typing-dot");

function submit(text) {
  if (text !== undefined) input().value = text;
  document.getElementById("composer")
    .dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
}

// Let the dock's awaited fetch/.json() settle before asserting.
const settle = () => new Promise((r) => setTimeout(r, 0));

// A turn that stays in flight, so the busy state can be inspected.
function pendingTurn() {
  global.fetch = jest.fn(() => new Promise(() => {}));
}

function resolvesWith(payload) {
  global.fetch = jest.fn(() => Promise.resolve({ json: async () => payload }));
}

beforeEach(() => {
  global.fetch = jest.fn(() => Promise.resolve({ json: async () => ({}) }));
  loadDock();
});

describe("boot", () => {
  test("greets once on load", () => {
    const wren = messages().querySelectorAll(".msg.wren");
    expect(wren).toHaveLength(1);
    expect(wren[0].textContent).toContain("Hi Craig");
  });
});

describe("sending a turn", () => {
  test("posts the message to /chat and echoes it into the thread", async () => {
    resolvesWith({ type: "final", text: "hi back" });
    submit("hello");
    expect(global.fetch).toHaveBeenCalledWith("/chat", expect.objectContaining({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "hello" }),
    }));
    expect(messages().querySelector(".msg.user").textContent).toBe("hello");
    expect(input().value).toBe("");  // cleared so it can't be sent twice
  });

  test("ignores an empty or whitespace-only message", () => {
    submit("   ");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test("locks the composer and shows typing dots while the turn runs", () => {
    pendingTurn();
    submit("hello");
    expect(dots()).toHaveLength(3);
    expect(input().disabled).toBe(true);
    // The button stays enabled during a turn — it is the Stop control.
    expect(sendBtn().disabled).toBe(false);
    expect(sendBtn().textContent).toBe("Stop");
    expect(sendBtn().classList.contains("stop")).toBe(true);
  });

  test("renders the reply and unlocks the composer when the turn ends", async () => {
    resolvesWith({ type: "final", text: "VibeFoundry is your brand." });
    submit("hello");
    await settle();
    expect(dots()).toHaveLength(0);
    expect(lastMessage()).toBe("VibeFoundry is your brand.");
    expect(input().disabled).toBe(false);
    expect(sendBtn().textContent).toBe("Send");
    expect(sendBtn().classList.contains("stop")).toBe(false);
  });
});

describe("a turn that fails", () => {
  // The regression this file exists for. A dropped connection (laptop asleep,
  // tailnet blip) rejects the fetch; an HTML error page rejects .json(). Either
  // one escaping unhandled leaves the dots spinning over a dead composer, which
  // reads as "Wren is thinking" forever and silently eats the message.
  test("a rejected fetch clears the dots and unlocks the composer", async () => {
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    submit("hello");
    await settle();
    expect(dots()).toHaveLength(0);
    expect(input().disabled).toBe(false);
    expect(sendBtn().textContent).toBe("Send");
    expect(lastMessage()).toContain("couldn't reach Wren");
    expect(lastMessage()).toContain("Failed to fetch");
    expect(lastMessage()).toContain("wasn't sent");
  });

  test("a non-JSON error page clears the dots and unlocks the composer", async () => {
    global.fetch = jest.fn(() => Promise.resolve({
      json: () => Promise.reject(new SyntaxError("Unexpected token '<'")),
    }));
    submit("hello");
    await settle();
    expect(dots()).toHaveLength(0);
    expect(input().disabled).toBe(false);
    expect(lastMessage()).toContain("couldn't reach Wren");
  });

  test("a server error payload is surfaced, not swallowed", async () => {
    resolvesWith({ error: "a turn is already running for this session" });
    submit("hello");
    await settle();
    expect(dots()).toHaveLength(0);
    expect(input().disabled).toBe(false);
    expect(lastMessage()).toContain("a turn is already running");
  });

  test("the dock still works after a failure — recovery is real, not cosmetic", async () => {
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    submit("first");
    await settle();

    resolvesWith({ type: "final", text: "second worked" });
    submit("second");
    await settle();
    expect(global.fetch).toHaveBeenLastCalledWith("/chat", expect.objectContaining({
      body: JSON.stringify({ message: "second" }),
    }));
    expect(lastMessage()).toBe("second worked");
    expect(input().disabled).toBe(false);
  });
});

describe("stopping a turn", () => {
  test("submitting while busy cancels instead of sending a second message", () => {
    pendingTurn();
    submit("hello");
    expect(global.fetch).toHaveBeenCalledTimes(1);

    submit();  // the button is showing "Stop"
    expect(global.fetch).toHaveBeenCalledTimes(2);
    expect(global.fetch).toHaveBeenLastCalledWith("/chat/cancel", { method: "POST" });
  });

  test("a failed cancel doesn't throw — the running turn still owns the UI", async () => {
    // The cancel is fire-and-forget: the turn's own fetch resolves as
    // "cancelled" and does the cleanup, so a rejected cancel must stay quiet
    // rather than surface as an unhandled rejection.
    pendingTurn();
    submit("hello");
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    expect(() => submit()).not.toThrow();
    await settle();
  });

  test("a cancelled turn reports Stopped and unlocks the composer", async () => {
    resolvesWith({ type: "cancelled" });
    submit("hello");
    await settle();
    expect(dots()).toHaveLength(0);
    expect(lastMessage()).toBe("Stopped.");
    expect(input().disabled).toBe(false);
  });
});

describe("confirming a write", () => {
  const CONFIRM = {
    type: "confirm",
    summary: "Send email to craig@example.com",
    detail: "Hi, following up on claim #4471",
  };

  test("renders the summary and detail, clears the dots, and waits", async () => {
    resolvesWith(CONFIRM);
    submit("email the adjuster");
    await settle();
    expect($(".confirm").textContent).toContain("Send email to craig@example.com");
    expect($(".confirm .detail").textContent).toBe("Hi, following up on claim #4471");
    expect($(".confirm .actions").querySelectorAll("button")).toHaveLength(2);
    expect(dots()).toHaveLength(0);
    // Unlocked while it waits on the decision — the turn is paused, not running.
    expect(input().disabled).toBe(false);
  });

  test("Confirm approves the write and dismisses the card", async () => {
    resolvesWith(CONFIRM);
    submit("email the adjuster");
    await settle();

    resolvesWith({ type: "final", text: "Sent." });
    $(".confirm .yes").click();
    expect(global.fetch).toHaveBeenLastCalledWith("/chat/confirm", expect.objectContaining({
      body: JSON.stringify({ approved: true }),
    }));
    await settle();
    expect($(".confirm")).toBeNull();
    expect(lastMessage()).toBe("Sent.");
  });

  test("Cancel declines the write", async () => {
    resolvesWith(CONFIRM);
    submit("email the adjuster");
    await settle();

    resolvesWith({ type: "final", text: "Okay, skipped." });
    $(".confirm .no").click();
    expect(global.fetch).toHaveBeenLastCalledWith("/chat/confirm", expect.objectContaining({
      body: JSON.stringify({ approved: false }),
    }));
    await settle();
    expect($(".confirm")).toBeNull();
  });

  test("a confirm whose continuation fails still unlocks the composer", async () => {
    resolvesWith(CONFIRM);
    submit("email the adjuster");
    await settle();

    global.fetch = jest.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    $(".confirm .yes").click();
    await settle();
    expect(dots()).toHaveLength(0);
    expect(input().disabled).toBe(false);
    expect(lastMessage()).toContain("couldn't reach Wren");
  });

  test("summary and detail are text, never markup", async () => {
    // Both are model-derived, and the model reads untrusted input (emails, web
    // pages), so a payload like this is reachable. It must land as literal text.
    resolvesWith({
      type: "confirm",
      summary: '<img src=x onerror="globalThis.PWNED = true">',
      detail: "<script>globalThis.PWNED = true</script>",
    });
    submit("do the thing");
    await settle();
    expect($(".confirm img")).toBeNull();
    expect($(".confirm script")).toBeNull();
    expect(globalThis.PWNED).toBeUndefined();
    expect($(".confirm").textContent).toContain("<img src=x");
  });
});

describe("new chat", () => {
  test("resets the server session, clears the thread, and re-greets", async () => {
    resolvesWith({ type: "final", text: "hi back" });
    submit("hello");
    await settle();
    expect(messages().children.length).toBeGreaterThan(1);

    document.getElementById("newChat").click();
    await settle();
    expect(global.fetch).toHaveBeenLastCalledWith("/chat/new", { method: "POST" });
    const remaining = messages().children;
    expect(remaining).toHaveLength(1);
    expect(remaining[0].textContent).toContain("Hi Craig");
  });

  test("unlocks the composer when started mid-turn", async () => {
    // /chat/new cancels the running turn server-side, so its postTurn never
    // returns to clear the busy state. Without the client-side reset the dock
    // sits showing "Stop" forever on a fresh session that can't be typed into.
    pendingTurn();
    submit("hello");
    expect(sendBtn().textContent).toBe("Stop");

    document.getElementById("newChat").click();
    await settle();
    expect(sendBtn().textContent).not.toBe("Stop");
    expect(input().disabled).toBe(false);
  });

  test("a failed /chat/new still resets the dock", async () => {
    global.fetch = jest.fn(() => Promise.reject(new Error("offline")));
    document.getElementById("newChat").click();
    await settle();
    // The rejected fetch must not skip the reset — the thread still clears and
    // re-greets rather than leaving a half-reset dock.
    expect(messages().children).toHaveLength(1);
    expect(lastMessage()).toContain("Hi Craig");
    expect(sendBtn().textContent).not.toBe("Stop");
  });
});

describe("frontier escalation", () => {
  const escBtn = () => document.querySelector(".escalate");
  const LOCAL = { type: "final", text: "local answer", escalate_to: "gemini-2.5-flash (gemini)" };

  test("a local reply that can be escalated offers a redo button", async () => {
    resolvesWith(LOCAL);
    submit("hello");
    await settle();
    expect(escBtn()).not.toBeNull();
    expect(escBtn().textContent).toContain("Redo with gemini-2.5-flash");
  });

  test("a local reply with no escalate_to offers no button", async () => {
    resolvesWith({ type: "final", text: "local answer" });
    submit("hello");
    await settle();
    expect(escBtn()).toBeNull();
  });

  test("clicking redo posts to /chat/escalate and locks the composer", async () => {
    resolvesWith(LOCAL);
    submit("hello");
    await settle();

    pendingTurn();
    escBtn().click();
    expect(global.fetch).toHaveBeenLastCalledWith("/chat/escalate", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({}),
    }));
    expect(dots()).toHaveLength(3);
    expect(input().disabled).toBe(true);
  });

  test("an escalated reply is badged and offers no further redo", async () => {
    resolvesWith(LOCAL);
    submit("hello");
    await settle();

    resolvesWith({ type: "final", text: "frontier answer", escalated: true,
                   model_label: "gemini-2.5-flash (gemini)" });
    escBtn().click();
    await settle();
    const badge = document.querySelector(".msg.wren.escalated .badge");
    expect(badge).not.toBeNull();
    expect(badge.textContent).toContain("⚡");
    expect(badge.textContent).toContain("gemini-2.5-flash");
    // The frontier reply carries no fresh redo button; the one that launched it
    // is spent (disabled), so the same turn can't be escalated twice.
    expect(document.querySelectorAll(".escalate")).toHaveLength(1);
    expect(escBtn().disabled).toBe(true);
  });

  test("a new message clears the prior redo button", async () => {
    resolvesWith(LOCAL);
    submit("hello");
    await settle();
    expect(escBtn()).not.toBeNull();

    resolvesWith({ ...LOCAL, text: "another" });
    submit("again");
    await settle();
    // Exactly one — the old button was cleared when the new turn began.
    expect(document.querySelectorAll(".escalate")).toHaveLength(1);
  });

  test("a failed escalation re-enables the button — the local answer is unchanged", async () => {
    resolvesWith(LOCAL);
    submit("hello");
    await settle();

    global.fetch = jest.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    escBtn().click();
    await settle();
    expect(escBtn().disabled).toBe(false);  // retry is valid
    expect(input().disabled).toBe(false);
    expect(lastMessage()).toContain("couldn't reach Wren");
  });
});
