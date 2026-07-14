// The chat dock, shared by /chat (full page) and /dashboard (side panel).
//
// This lived twice — once inline in each page — and the copies drifted: the
// dashboard's never grew the Stop button, and a missing try/catch around the
// turn fetch had to be fixed in both. One copy now, included as:
//
//   <script src="/static/chat-dock.js"></script>
//
// The contract is the markup, not a config object: a page supplies #messages,
// #composer, #input, #send, and #newChat, and owns all the CSS. Styling is the
// only thing the two pages legitimately disagree about (the panel runs a size
// smaller than the full page), so it stays in their stylesheets — this file
// emits structure and class names and never touches presentation.
//
// Class names to style: .msg.user / .msg.wren / .msg.system, .msg.typing with
// .typing-dot children, and .confirm with .detail / .actions / .yes / .no.
// Note .typing-dot, not .dot — the dashboard already uses .dot for run-history
// status dots, and a collision there is what forced the copies apart before.
//
// Wrapped in an IIFE: the dashboard's inline script shares this global scope
// and has its own el()/api() helpers to not clobber.
(() => {
  const messagesEl = document.getElementById("messages");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const newChatBtn = document.getElementById("newChat");
  const GREETING = "Hi Craig, what can I do for you today?";

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
      setBusy(false);
      return;
    }
    if (result.type === "final") {
      addMessage("wren", result.text);
      setBusy(false);
    } else if (result.type === "cancelled") {
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
    addMessage("user", message);
    input.value = "";
    setBusy(true);
    postTurn("/chat", { message }, addTyping());
  });

  newChatBtn.addEventListener("click", async () => {
    await fetch("/chat/new", { method: "POST" });
    messagesEl.replaceChildren();
    addMessage("wren", GREETING);
  });

  addMessage("wren", GREETING);
})();
