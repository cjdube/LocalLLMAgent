// The chat dock, used by /chat (full page). It was also the dashboard's side
// panel until that panel was removed — the dashboard is a read-only view now.
//
// It once lived twice — inline in each page — and the copies drifted: the
// dashboard's never grew the Stop button, and a missing try/catch around the
// turn fetch had to be fixed in both. One copy now, included as:
//
//   <script src="/static/chat-dock.js"></script>
//
// The contract is the markup, not a config object: a page supplies #messages,
// #composer, #input, #send, and #newChat, and owns all the CSS — this file
// emits structure and class names and never touches presentation.
//
// Class names to style: .msg.user / .msg.wren / .msg.system, .msg.typing with
// .typing-dot children, and .confirm with .detail / .actions / .yes / .no.
// Note .typing-dot, not .dot — a host page may already use .dot for something
// else (the dashboard's run-history status dots), and that collision is what
// forced the copies apart before.
//
// Wrapped in an IIFE: a host page's inline script shares this global scope, so
// this file declares nothing on it.
(() => {
  const messagesEl = document.getElementById("messages");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const newChatBtn = document.getElementById("newChat");
  const GREETING = "Hi — what can I do for you today?";

  function scrollToEnd() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addMessage(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToEnd();
    return div;
  }

  function addTyping() {
    const div = document.createElement("div");
    div.className = "msg wren typing";
    for (let i = 0; i < 3; i++) {
      const dot = document.createElement("span");
      dot.className = "typing-dot";
      div.appendChild(dot);
    }
    messagesEl.appendChild(div);
    scrollToEnd();
    return div;
  }

  function addConfirm(summary, detail, onDecide) {
    const div = document.createElement("div");
    div.className = "confirm";

    // summary is model-derived (email subjects, event titles Wren reads from
    // your data), so assign it as text — never innerHTML — to avoid an
    // HTML/script injection sink.
    const summaryEl = document.createElement("div");
    summaryEl.textContent = summary;
    div.appendChild(summaryEl);

    // detail (e.g. the email body) lets you see what's actually being sent
    // before approving. Same rule: textContent only, never innerHTML.
    if (detail) {
      const detailEl = document.createElement("div");
      detailEl.className = "detail";
      detailEl.textContent = detail;
      div.appendChild(detailEl);
    }

    const actions = document.createElement("div");
    actions.className = "actions";
    const yes = document.createElement("button");
    yes.className = "yes";
    yes.textContent = "Confirm";
    const no = document.createElement("button");
    no.className = "no";
    no.textContent = "Cancel";
    actions.appendChild(yes);
    actions.appendChild(no);
    div.appendChild(actions);

    yes.onclick = () => { onDecide(true); div.remove(); };
    no.onclick = () => { onDecide(false); div.remove(); };
    messagesEl.appendChild(div);
    scrollToEnd();
  }

  // The escalate button ("Redo with the frontier model") lives on the most
  // recent local reply only — a new turn or an escalation clears any previous
  // one, so exactly one is ever present.
  function clearEscalateButtons() {
    messagesEl.querySelectorAll(".escalate").forEach((b) => b.remove());
  }

  // Set while an escalation is in flight so a failure (or a cancel) can re-enable
  // the button it was launched from: the local answer is unchanged, so a retry
  // is valid. A successful escalation leaves the button spent (disabled).
  let pendingEscalateBtn = null;

  function renderFinal(result) {
    const div = addMessage("wren", result.text);
    if (result.escalated) {
      // An off-device reply — badge it so a long thread never blurs which
      // answers came from the frontier model.
      div.classList.add("escalated");
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "⚡ " + (result.model_label || "frontier model");
      div.prepend(badge);
    } else if (result.escalate_to) {
      // A local reply the server says can be redone on the frontier model.
      const btn = document.createElement("button");
      btn.className = "escalate";
      btn.textContent = "Redo with " + result.escalate_to;
      btn.onclick = () => {
        if (busy) return;
        btn.disabled = true;
        pendingEscalateBtn = btn;
        setBusy(true);
        postTurn("/chat/escalate", {}, addTyping());
      };
      messagesEl.appendChild(btn);
      scrollToEnd();
    }
  }

  // While a turn is running the Send button becomes a Stop button (it stays
  // enabled so it can cancel); the input is disabled until the turn ends.
  let busy = false;
  function setBusy(b) {
    busy = b;
    input.disabled = b;
    sendBtn.textContent = b ? "Stop" : "Send";
    sendBtn.classList.toggle("stop", b);
  }

  // A dropped connection (laptop asleep, tailnet blip) rejects the fetch, and
  // an error page rejects .json(). Either one escaping as an unhandled
  // rejection left the typing dots animating over a permanently disabled
  // composer — indistinguishable from Wren still thinking. Route both into
  // handleResult's error path so the dots clear and the composer unlocks.
  async function postTurn(path, body, typingEl) {
    try {
      const resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return handleResult(await resp.json(), typingEl);
    } catch (err) {
      handleResult(
        { error: `couldn't reach Wren (${err.message}). Your message wasn't sent — try again.` },
        typingEl,
      );
    }
  }

  function handleResult(result, typingEl) {
    if (typingEl) typingEl.remove();
    if (result.error) {
      addMessage("system", "Error: " + result.error);
      // A failed escalation leaves the local answer intact, so let it be retried.
      if (pendingEscalateBtn) { pendingEscalateBtn.disabled = false; pendingEscalateBtn = null; }
      setBusy(false);
      return;
    }
    if (result.type === "final") {
      pendingEscalateBtn = null;  // a successful escalation leaves its button spent
      renderFinal(result);
      setBusy(false);
    } else if (result.type === "cancelled") {
      if (pendingEscalateBtn) { pendingEscalateBtn.disabled = false; pendingEscalateBtn = null; }
      addMessage("system", "Stopped.");
      setBusy(false);
    } else if (result.type === "confirm") {
      addConfirm(result.summary, result.detail, (approved) => {
        setBusy(true);
        postTurn("/chat/confirm", { approved }, addTyping());
      });
      setBusy(false);
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) {  // the button is showing "Stop" — cancel the running turn
      // The running turn's own postTurn owns the UI: the server ends it and
      // that fetch resolves as {type: "cancelled"}, which clears the dots and
      // unlocks the composer. Nothing to do here but ask, and stay out of the
      // way if the ask itself fails.
      fetch("/chat/cancel", { method: "POST" }).catch(() => {});
      return;
    }
    const message = input.value.trim();
    if (!message) return;
    clearEscalateButtons();  // a new turn supersedes the last reply's redo offer
    addMessage("user", message);
    input.value = "";
    setBusy(true);
    postTurn("/chat", { message }, addTyping());
  });

  newChatBtn.addEventListener("click", () => {
    // /chat/new cancels a running turn server-side, so that turn's postTurn
    // never returns to clear the busy state — reset it here or the composer
    // stays locked showing "Stop" on a session that can't be typed into.
    // Deliberately not awaited: the reset must not hinge on the round-trip
    // landing, or a slow/failed request reintroduces the same stuck dock.
    fetch("/chat/new", { method: "POST" }).catch(() => {});
    setBusy(false);
    messagesEl.replaceChildren();
    addMessage("wren", GREETING);
  });

  addMessage("wren", GREETING);
})();
