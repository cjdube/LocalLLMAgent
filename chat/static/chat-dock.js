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
// emits structure and class names and never touches presentation. The one
// exception is #input's height: it is a <textarea> that grows with what is
// typed, and only script can measure the content, so this file writes
// style.height on it. The page still owns the cap (max-height) and the
// overflow behaviour past it.
//
// Class names to style: .msg.user / .msg.wren / .msg.system, .msg.typing with
// .typing-dot children, .confirm with .detail / .actions / .yes / .no, and
// .offer (a row of .escalate buttons, used for the busy offer).
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

  // Grow the composer to fit the message so a long one stays readable while
  // it is written. Reset to "auto" first — scrollHeight never shrinks below
  // the height already set, so without it the box only ever gets taller.
  //
  // scrollHeight leaves out the border, and the page styles #input as
  // border-box, so handing it back as the height loses those pixels and
  // leaves the box permanently one scrollbar short of its own text.
  // offsetHeight - clientHeight is that border, measured at "auto".
  function autoGrow() {
    input.style.height = "auto";
    const border = input.offsetHeight - input.clientHeight;
    input.style.height = input.scrollHeight + border + "px";
  }

  // Shell-style recall of your own past messages: Up walks back through them,
  // Down walks forward. The list lives for the life of the page and survives
  // "New chat" on purpose — re-asking the last question in a fresh thread is
  // the common reason to reach for it.
  const sentHistory = [];   // your sent messages, oldest first
  let historyIndex = null;  // null = typing a fresh draft, not walking
  let draft = "";           // what was in the box before the first Up

  function recordSent(message) {
    // Skip a repeat, the way a shell skips a repeated command.
    if (sentHistory[sentHistory.length - 1] !== message) sentHistory.push(message);
  }

  // Put a recalled message in the box: grow to fit it (a recalled long message
  // would otherwise sit in a one-line box), caret at the end to append or edit.
  function recall(text) {
    input.value = text;
    autoGrow();
    input.setSelectionRange(text.length, text.length);
  }

  function resetInput() {
    input.value = "";
    input.style.height = "auto";
    historyIndex = null;  // the box is a fresh draft again
  }

  input.addEventListener("input", () => {
    autoGrow();
    // Editing a recalled message makes it yours again: the arrows go back to
    // moving the caret, and the next Up starts over from the newest message.
    historyIndex = null;
  });

  // A textarea takes Enter as a newline, so the send key has to be rebound:
  // Enter sends, Shift+Enter (and Ctrl/Cmd+Enter) starts a new line.
  //
  // Up is shared with the caret for the same reason: a long message wraps to
  // several lines, and Up has to keep moving between them. It only means "the
  // message before this one" from the very start of the box, where there is no
  // line above to move to — or when a walk is already under way, so that a
  // second Up keeps going back instead of stopping on the first recall. Down
  // needs no caret test: it is only ever a step forward through a walk that Up
  // began. Typing anything ends the walk and hands both keys straight back.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      form.requestSubmit ? form.requestSubmit() : sendBtn.click();
      return;
    }
    if (!sentHistory.length) return;

    const walking = historyIndex !== null;
    const atStart = input.selectionStart === 0 && input.selectionEnd === 0;

    if (e.key === "ArrowUp" && (walking || atStart)) {
      if (!walking) {
        draft = input.value;
        historyIndex = sentHistory.length - 1;
      } else if (historyIndex > 0) {
        historyIndex -= 1;
      } else {
        return;  // already on the oldest message — nothing further back
      }
      e.preventDefault();
      recall(sentHistory[historyIndex]);
    } else if (e.key === "ArrowDown" && walking) {
      e.preventDefault();
      if (historyIndex < sentHistory.length - 1) {
        historyIndex += 1;
        recall(sentHistory[historyIndex]);
      } else {
        // Past the newest message: back to whatever you were typing.
        historyIndex = null;
        recall(draft);
      }
    }
  });

  // Wren's reply text comes from a model that can read untrusted content, so
  // build its links as DOM nodes instead of parsing model-supplied HTML. The
  // first alternative owns a complete Markdown link (safe or not), which keeps
  // unsupported schemes literal rather than linkifying a URL inside its syntax.
  const LINK_TOKEN_RE = /\[([^\]\n]+)\]\(([^\n)]*)\)|(https?:\/\/[^\s<>"']+)/g;
  const TRAILING_URL_PUNCTUATION_RE = /[.,!?;:]+$/;

  function safeHttpUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function appendLink(fragment, label, href) {
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchor.textContent = label;
    fragment.appendChild(anchor);
  }

  function linkifiedText(text) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    let match;
    LINK_TOKEN_RE.lastIndex = 0;
    while ((match = LINK_TOKEN_RE.exec(text)) !== null) {
      if (match.index > last) {
        fragment.appendChild(document.createTextNode(text.slice(last, match.index)));
      }

      const markdown = match[1] !== undefined;
      const rawUrl = markdown ? match[2] : match[3];
      const url = markdown ? rawUrl : rawUrl.replace(TRAILING_URL_PUNCTUATION_RE, "");
      const trailing = markdown ? "" : rawUrl.slice(url.length);
      const href = safeHttpUrl(url);
      if (href) {
        appendLink(fragment, markdown ? match[1] : url, href);
        if (trailing) fragment.appendChild(document.createTextNode(trailing));
      } else {
        fragment.appendChild(document.createTextNode(match[0]));
      }
      last = LINK_TOKEN_RE.lastIndex;
    }
    if (last < text.length) fragment.appendChild(document.createTextNode(text.slice(last)));
    return fragment;
  }

  function addMessage(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    if (role === "wren") div.appendChild(linkifiedText(text));
    else div.textContent = text;
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

  // An offer to use the frontier model — the "Redo with" button on the most
  // recent local reply, or the pair on a busy notice — lives on the newest
  // message only. A new turn or an escalation clears any previous one, so
  // exactly one is ever present. The .offer wrapper goes too, or a busy notice
  // leaves an empty row behind it.
  function clearOffers() {
    messagesEl.querySelectorAll(".escalate, .offer").forEach((b) => b.remove());
  }

  // Set while an escalation is in flight so a failure (or a cancel) can re-enable
  // the button it was launched from: the local answer is unchanged, so a retry
  // is valid. A successful escalation leaves the button spent (disabled).
  let pendingEscalateBtn = null;

  function renderFinal(result) {
    // The model sometimes ends a turn with no text at all (measured 2026-08-15:
    // 5 of 11 runs on one eval case). Rendered as a wren bubble that's just
    // blank, it reads as a rendering bug and hides that the turn is over — so
    // say what happened instead. The escalate button below still attaches,
    // which is the useful next move.
    const div = result.text && result.text.trim()
      ? addMessage("wren", result.text)
      : addMessage("system", "Wren returned an empty reply.");
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

  // The local model's one request slot was taken, so this turn was never
  // started — see probe_local_model. Nothing was sent and nothing needs
  // cancelling: whichever button is tapped just re-sends the same message,
  // saying which way to go.
  //
  // Two buttons because the choice is real. The frontier model sends this
  // conversation off the Mac mini, and some questions are not for that; waiting
  // is what would have happened anyway, and it stays one tap away.
  function renderBusy(result, message) {
    addMessage("system", result.reason);
    const offer = document.createElement("div");
    offer.className = "offer";
    const ask = document.createElement("button");
    ask.className = "escalate";
    ask.textContent = "Ask " + (result.escalate_to || "the frontier model");
    const wait = document.createElement("button");
    wait.className = "escalate";
    wait.textContent = "Wait for Wren";
    offer.appendChild(ask);
    offer.appendChild(wait);

    // Spend the offer on the first tap. The session allows one turn at a time,
    // so a second tap on the other button would only earn a 409.
    const resend = (extra) => {
      if (busy) return;
      offer.remove();
      setBusy(true);
      postTurn("/chat", Object.assign({ message: message }, extra), addTyping());
    };
    ask.onclick = () => resend({ backend: "frontier" });
    wait.onclick = () => resend({ force_local: true });

    messagesEl.appendChild(offer);
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
      return handleResult(await resp.json(), typingEl, body);
    } catch (err) {
      handleResult(
        { error: `couldn't reach Wren (${err.message}). Your message wasn't sent — try again.` },
        typingEl,
        body,
      );
    }
  }

  // `body` is what was posted; a busy answer re-sends its .message, so the
  // text survives without a second copy of it living outside the turn.
  function handleResult(result, typingEl, body) {
    if (typingEl) typingEl.remove();
    // The server summarized this session's oldest turns away before answering.
    // Said here rather than in any one branch: it happened before the turn ran,
    // so it is just as true of an error or a cancel as of a reply. Marked in the
    // thread so a later "she forgot that" has a visible cause to scroll back to.
    if (result.compacted) {
      addMessage("system", "Earlier messages were summarized to save room.");
    }
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
    } else if (result.type === "busy") {
      renderBusy(result, (body || {}).message || "");
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
    recordSent(message);
    clearOffers();  // a new turn supersedes the last reply's redo offer
    addMessage("user", message);
    resetInput();
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
    resetInput();
    messagesEl.replaceChildren();
    addMessage("wren", GREETING);
  });

  addMessage("wren", GREETING);
})();
