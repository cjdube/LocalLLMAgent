"""The table of every setting Wren has, and what the settings page may do with it.

One frozen `Setting` row per configuration key. The row — not the call site —
is what the /settings page renders, what agent/config.py resolves a default
from, and what agent/migrate_settings.py uses to decide whether a key in
config/.env moves into the settings document or stays put.

A key with no row here is invisible to all three. That is deliberate and it is
load-bearing: STRAVA_*, ANTHROPIC_API_KEY and the SCRIBEJAY_* keys still sit in
some developers' config/.env with zero readers in this repo, and neither
deleting them nor showing a set/not-set field for something nothing reads is
acceptable. No row means "not ours, leave it alone".

Two drift guards in tests/test_schema.py keep the table honest in both
directions: every literal key passed to config.getenv must have a row, and no
key that has a row may still be read with a raw os.getenv — a page that shows a
field, accepts an edit and saves it while the code reads somewhere else is
worse than no page.

`applies` is the Wren-only field. ScribeJay, whose design the rest of this is
copied from, is all short-lived launchd processes, so every change there is
live. Wren has a chat server that runs for weeks, so each row has to say when a
saved value actually lands:

    live      read per call in every process — the save is the whole story
    next_run  read only by launchd tasks, which re-import on each run
    restart   bound at import inside the chat server's import closure

Strongest reader wins. An import-time read inside the chat server means the
user must act, so that beats both others; a per-call reader needs nothing, so
it beats a task's next run. tests/test_schema.py verifies the `restart` set
both ways against the server's real import closure. It deliberately does not
try to tell `live` from `next_run` — that is not statically decidable, and
guessing `live` when the truth is `next_run` is a harmless under-promise.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# Values a row's `applies` may take, in order of how much the user must do.
APPLIES = ("live", "next_run", "restart")

# The command the page shows beside a restart-class save. No button: the server
# runs under launchd KeepAlive, so a route that kills its own process is a
# self-DoS if the save that preceded it was the wrong one.
#
# The label is local.wren.wren, not local.wren.chat — the plist is named for the
# agent, not for the module it runs. A wrong label does not error usefully; it
# prints "Could not find service ... in domain for user gui", which reads like a
# permissions problem. tests/test_schema.py pins this against launchd/.
CHAT_SERVER_LABEL = "local.wren.wren"
RESTART_COMMAND = f"launchctl kickstart -k gui/$UID/{CHAT_SERVER_LABEL}"


@dataclass(frozen=True)
class Setting:
    """One configuration key, and everything the page and the resolver need.

    `default` is the schema layer of agent/config.py's four-layer resolve, and
    an empty string means "this row has no default" — the caller's own default
    then applies. That is right for the handful of keys whose real default is a
    path computed from this machine (WREN_SKILLS_DIR, WREN_LOGS_DIR): pinning
    an absolute path in a committed table would be a lie on any other machine.
    """

    key: str
    group: str
    label: str
    help: str
    type: str = "str"           # str | int | float | bool | path | choice
    default: str = ""
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    applies: str = "live"
    editable: bool = True
    reason: str = ""            # why not editable; required when editable is False
    secret: bool = False        # value never leaves the process; only is_set does


# --------------------------------------------------------------------------- #
# Groups, in the order the page renders them.
# --------------------------------------------------------------------------- #

GROUPS = (
    "Model",
    "Chat server",
    "You",
    "Google account",
    "Mail",
    "Notifications",
    "Web and search",
    "Opportunity scout",
    "Wiki and learnings",
    "Projects and builds",
    "Games",
    "Logs and usage",
)


SETTINGS: tuple[Setting, ...] = (
    # ----------------------------------------------------------------- Model
    Setting(
        key="OLLAMA_MODEL", group="Model", label="Local model",
        help="The Ollama model tag every local call uses. Changing it does not "
             "pull the model — do that first, or the next call fails.",
        default="gemma4", applies="live",
    ),
    Setting(
        key="OLLAMA_HOST", group="Model", label="Ollama address",
        help="Base URL of the Ollama server. Local-first: this normally stays "
             "on loopback.",
        default="http://localhost:11434", applies="live",
    ),
    Setting(
        key="OLLAMA_TIMEOUT", group="Model", label="Model read timeout (s)",
        help="Gap allowed between streamed chunks on a background call, not a "
             "cap on total generation. It has to cover the wait for the first "
             "token, and prefill of a large batch prompt runs about 50 seconds.",
        type="int", default="300", minimum=10, maximum=3600, applies="live",
    ),
    Setting(
        key="OLLAMA_NUM_CTX", group="Model", label="Context window (tokens)",
        help="Tokens requested per call. Ollama otherwise falls back to about "
             "4096 and silently truncates the FRONT of the prompt, where the "
             "system prompt lives. Raise this together with the chat history "
             "budget below.",
        type="int", default="8192", minimum=2048, maximum=131072, applies="live",
    ),
    Setting(
        key="OLLAMA_NUM_PREDICT", group="Model", label="Max tokens per reply",
        help="Cap on tokens generated per call, so a repetition loop cannot "
             "burn thousands of junk tokens into the session history. Keep it "
             "comfortably above the longest legitimate reply.",
        type="int", default="3072", minimum=256, maximum=32768, applies="live",
    ),
    Setting(
        key="OLLAMA_KEEP_ALIVE", group="Model", label="Keep model resident",
        help="How long Ollama holds the model in memory after a call. A "
             "duration such as 30m, or -1 to pin it. Keeping the large model "
             "warm between the day's tasks avoids re-paying the cold load.",
        default="30m", applies="live",
    ),
    Setting(
        key="OLLAMA_WARM_TIMEOUT", group="Model", label="Warm-up timeout (s)",
        help="Budget for the preload a scheduled task runs before a big "
             "generation, so the cold model load does not eat into the "
             "generation's own read timeout.",
        type="int", default="600", minimum=30, maximum=3600, applies="live",
    ),
    Setting(
        key="OLLAMA_MAX_TOOL_RESULT_CHARS", group="Model",
        label="Tool result cap (chars)",
        help="Cap on one tool result before it is appended to the conversation, "
             "at roughly four characters per token. A trimmed result reads as "
             "complete to the model, so raise a tool's own budget with it.",
        type="int", default="8000", minimum=1000, maximum=64000, applies="restart",
    ),
    Setting(
        key="WREN_LLM_BACKEND", group="Model", label="Backend",
        help="Which model serves the calls. Local Ollama is the default and the "
             "whole point of the design. Choosing gemini sends conversation "
             "history and tool results off this machine to Google.",
        type="choice", choices=("ollama", "gemini"), default="ollama", applies="live",
    ),
    Setting(
        key="WREN_ESCALATION_BACKEND", group="Model", label="Frontier backend",
        help="Backend behind the redo-with-the-frontier-model button, and behind "
             "the offer made when the local model's one slot is busy. Its own "
             "setting, not the backend above, because the default names no "
             "frontier target. Empty hides the button.",
        type="choice", choices=("", "gemini"), default="", applies="live",
    ),
    Setting(
        key="WREN_GEMINI_MODEL", group="Model", label="Cloud model",
        help="Model id used when a call routes to Gemini.",
        default="gemini-2.5-flash", applies="live",
    ),
    Setting(
        key="WREN_GEMINI_MAX_OUTPUT_TOKENS", group="Model",
        label="Cloud max output tokens",
        help="Cap on what the cloud model generates per call. Thinking tokens "
             "count against it, so a small value can return an empty draft.",
        type="int", default="8192", minimum=256, maximum=65536, applies="live",
    ),
    Setting(
        key="WREN_GEMINI_THINKING_BUDGET", group="Model",
        label="Cloud thinking budget",
        help="A hint, not a cap — measured overruns on the 3.x models. Not "
             "portable either: 0 is rejected outright by gemini-2.5-pro and "
             "gemini-3.6-flash. 128, or -1 for dynamic, is accepted everywhere. "
             "Verify with one real call after changing the cloud model.",
        type="int", default="0", minimum=-1, maximum=32768, applies="live",
    ),
    Setting(
        key="GEMINI_API_KEY", group="Model", label="Gemini API key",
        help="Key for the cloud backend. Either this or the Google API key "
             "below works; this one wins if both are set.",
        secret=True, applies="live",
    ),
    Setting(
        key="GOOGLE_API_KEY", group="Model", label="Google API key",
        help="Alternative spelling of the Gemini key — the SDK checks both. "
             "Set one, not both.",
        secret=True, applies="live",
    ),

    # ----------------------------------------------------------- Chat server
    Setting(
        key="WREN_CHAT_TOKEN", group="Chat server", label="Login token",
        help="The password for the chat UI. Generate a new one with: python -c "
             "\"import secrets; print(secrets.token_hex(32))\". Changing it "
             "signs every device out.",
        secret=True, applies="restart",
    ),
    Setting(
        key="FLASK_SECRET_KEY", group="Chat server", label="Session secret",
        help="Signs the login cookie. A different long random value from the "
             "token above. Changing it signs every device out.",
        secret=True, applies="restart",
    ),
    Setting(
        key="WREN_CHAT_PORT", group="Chat server", label="Port",
        help="The port the server listens on.",
        type="int", default="8420", applies="restart", editable=False,
        reason="The launchd plist and the tailscale serve mapping both assume "
               "8420. A bad edit here makes the server unreachable from the "
               "phone, with no way to fix it from the phone.",
    ),
    Setting(
        key="WREN_CHAT_HOST", group="Chat server", label="Bind address",
        help="The interface the server binds. Loopback is all tailscale serve "
             "needs.",
        default="127.0.0.1", applies="restart", editable=False,
        reason="Same reason as the port: widening the bind from this page is "
               "how the phone loses its only route back in.",
    ),
    Setting(
        key="WREN_PUBLIC_URL", group="Chat server", label="Public URL",
        help="The HTTPS base URL Tailscale serves this on. Used to build the "
             "tap-to-approve buttons on approval pushes and the game links. "
             "Empty means pushes still arrive, but without buttons.",
        default="", applies="live",
    ),
    Setting(
        key="WREN_CHAT_MODEL_TIMEOUT", group="Chat server",
        label="Chat read timeout (s)",
        help="Kept tighter than the model read timeout because someone is "
             "waiting on the answer. Ollama serves one request at a time and "
             "queues the rest silently.",
        type="int", default="120", minimum=10, maximum=1800, applies="restart",
    ),
    Setting(
        key="WREN_CHAT_MAX_HISTORY_CHARS", group="Chat server",
        label="History budget (chars)",
        help="Budget for the conversation history re-sent each turn. This is "
             "not the whole prompt: the system message and every tool schema "
             "sit on top of it and have no knob, so sizing the context window "
             "against this number alone under-sizes it.",
        type="int", default="16000", minimum=2000, maximum=200000, applies="restart",
    ),
    Setting(
        key="WREN_CHAT_SUMMARY_CHARS", group="Chat server",
        label="Rolling summary (chars)",
        help="Before dropping old turns the server has the model compact them "
             "into notes that ride in the system message. Spent out of the "
             "history budget above, so a large value crowds out live "
             "conversation. 0 goes back to plain dropping.",
        type="int", default="1500", minimum=0, maximum=20000, applies="restart",
    ),
    Setting(
        key="WREN_CHAT_BUSY_PROBE", group="Chat server", label="Busy-slot probe",
        help="Before each turn, ask Ollama whether its one request slot is free "
             "and offer the frontier model instead of queueing silently. Costs "
             "about 0.05s per turn. Only runs when a frontier backend is set.",
        type="bool", default="1", applies="restart",
    ),
    Setting(
        key="WREN_CHAT_BUSY_PROBE_TIMEOUT", group="Chat server",
        label="Probe timeout (s)",
        help="How long to wait for that probe before calling the slot taken.",
        type="int", default="3", minimum=1, maximum=60, applies="live",
    ),

    # ------------------------------------------------------------------- You
    Setting(
        key="DEFAULT_LOCATION", group="You", label="Default location",
        help="Where the weather comes from when nobody names a place. City, "
             "state and country, e.g. Portland,OR,US.",
        default="", applies="restart",
    ),
    Setting(
        key="TIMEZONE", group="You", label="Timezone",
        help="Used when creating calendar events. A full IANA name such as "
             "America/New_York — Google Calendar rejects abbreviations like "
             "EDT. Empty uses this machine's own zone.",
        default="", applies="live",
    ),
    Setting(
        key="BRIEF_TO_EMAIL", group="You", label="Email address",
        help="Where the morning brief and the weekly digest are sent. Also the "
             "contact address in the User-Agent the SEC requires.",
        default="", applies="restart",
    ),

    # -------------------------------------------------------- Google account
    Setting(
        key="GOOGLE_CREDENTIALS_PATH", group="Google account",
        label="OAuth client file",
        help="Path to the OAuth client downloaded from Google Cloud.",
        type="path", default="config/google_credentials.json", applies="live",
        editable=False,
        reason="Must match a Google Cloud configuration that is not editable "
               "here; a mismatch fails silently at the next token refresh.",
    ),
    Setting(
        key="GOOGLE_TOKEN_PATH", group="Google account", label="Token file",
        help="Where the granted OAuth token is cached.",
        type="path", default="config/google_token.json", applies="live",
        editable=False,
        reason="Paired with the client file above; moving one without the "
               "other silently re-triggers consent, which a launchd job "
               "cannot answer.",
    ),
    Setting(
        key="GOOGLE_CALENDAR_ID", group="Google account", label="Calendar id",
        help="Which calendar the tools read and write. primary is the account's "
             "own calendar.",
        default="primary", applies="live",
    ),
    Setting(
        key="GOOGLE_TASKLIST_ID", group="Google account", label="Task list id",
        help="Scope task reads to one list. Empty reads across every list on "
             "the account, which is usually what you want — tasks spread "
             "across several named lists.",
        default="", applies="live",
    ),
    Setting(
        key="GOOGLE_HTTP_TIMEOUT_S", group="Google account",
        label="Google API timeout (s)",
        help="Outbound timeout for every Google API call.",
        type="int", default="30", minimum=5, maximum=300, applies="restart",
    ),
    Setting(
        key="GOOGLE_OAUTH_PORT", group="Google account", label="Consent port",
        help="Port the one-time consent flow listens on. 0 picks a random one. "
             "Pin it when consenting from another machine over SSH, because "
             "the callback only arrives through a tunnel and a tunnel needs a "
             "fixed port.",
        type="int", default="0", minimum=0, maximum=65535, applies="live",
    ),

    # ------------------------------------------------------------------ Mail
    Setting(
        key="MAIL_PUBSUB_PROJECT", group="Mail", label="Cloud project id",
        help="The Google Cloud project holding the Pub/Sub topic — the same "
             "project as the OAuth client. Empty and both mail tasks refuse "
             "to start.",
        default="", applies="next_run", editable=False,
        reason="Must match a Google Cloud configuration that is not editable "
               "here. A wrong value does not error; the watcher simply never "
               "hears anything again.",
    ),
    Setting(
        key="MAIL_PUBSUB_TOPIC", group="Mail", label="Pub/Sub topic",
        help="The topic Gmail publishes changes to. Must already exist in the "
             "console.",
        default="wren-mail", applies="next_run", editable=False,
        reason="Same Google Cloud configuration as the project id above.",
    ),
    Setting(
        key="MAIL_PUBSUB_SUBSCRIPTION", group="Mail", label="Pub/Sub subscription",
        help="The pull subscription on that topic. Must already exist.",
        default="wren-mail-sub", applies="next_run", editable=False,
        reason="Same Google Cloud configuration as the project id above.",
    ),
    Setting(
        key="MAIL_WATCH_LABEL", group="Mail", label="Watch label",
        help="The Gmail label that decides which mail Wren is told about. Wren "
             "looks its internal id up from this name — never configure the id "
             "by hand.",
        default="Wren/Watch", applies="restart",
    ),
    Setting(
        key="MAIL_ACT_LABEL", group="Mail", label="Act label",
        help="The label that means handle this, not just tell me. Mail on a "
             "thread carrying it becomes a background job. A Gmail label "
             "applies to the whole thread, so every later reply is handed over "
             "too — peel it off when the thing is done.",
        default="Wren/Do", applies="restart",
    ),
    Setting(
        key="MAIL_BODY_CHAR_BUDGET", group="Mail", label="Body budget (chars)",
        help="How much of one email body is kept. A raw email runs to tens of "
             "thousands of characters, so it is trimmed at the source rather "
             "than by the tool-result cap.",
        type="int", default="1500", minimum=200, maximum=20000, applies="restart",
    ),
    Setting(
        key="MAIL_THREAD_CHAR_BUDGET", group="Mail", label="Thread budget (chars)",
        help="Cap on a whole thread handed to the model. Sits under the tool "
             "result cap on purpose — raise them as a pair or the loop re-trims "
             "what this already trimmed.",
        type="int", default="12000", minimum=500, maximum=64000, applies="restart",
    ),
    Setting(
        key="MAIL_SEARCH_CHAR_BUDGET", group="Mail", label="Search budget (chars)",
        help="Cap on a page of mail search results. Same pairing rule as the "
             "thread budget above.",
        type="int", default="6000", minimum=500, maximum=64000, applies="restart",
    ),

    # --------------------------------------------------------- Notifications
    Setting(
        key="NTFY_URL", group="Notifications", label="Push topic URL",
        help="Full topic URL on the self-hosted ntfy server. Empty turns phone "
             "push off entirely — scheduled-task failures then go unreported.",
        default="", applies="live",
    ),
    Setting(
        key="NTFY_TOKEN", group="Notifications", label="Push token",
        help="Publish token for that topic.",
        secret=True, applies="live",
    ),

    # -------------------------------------------------------- Web and search
    Setting(
        key="OPENWEATHERMAP_API_KEY", group="Web and search",
        label="OpenWeatherMap key",
        help="Powers the weather tool.",
        secret=True, applies="live",
    ),
    Setting(
        key="TAVILY_API_KEY", group="Web and search", label="Tavily key",
        help="Powers web search.",
        secret=True, applies="live",
    ),
    Setting(
        key="FIRECRAWL_API_KEY", group="Web and search", label="Firecrawl key",
        help="Powers fetching a page as readable markdown, and the app "
             "evaluator built on it.",
        secret=True, applies="live",
    ),
    Setting(
        key="GITHUB_TOKEN", group="Web and search", label="GitHub token",
        help="A classic token with no scopes covers public starred repos; add "
             "read access to repo to include private stars.",
        secret=True, applies="live",
    ),
    Setting(
        key="CLICKUP_API_TOKEN", group="Web and search", label="ClickUp token",
        help="Personal API token from ClickUp. Sent as a raw Authorization "
             "header with no Bearer prefix — that prefix is for an OAuth app "
             "token and returns 401 here. Empty means the backlog tools say "
             "not set and nothing else breaks.",
        secret=True, applies="live",
    ),
    Setting(
        key="WEB_FETCH_MAX_CHARS", group="Web and search",
        label="Page fetch cap (chars)",
        help="Cap on the markdown a page fetch returns. Raising it needs a "
             "matching raise of the tool result cap, or the loop re-cuts a page "
             "the tool already cut.",
        type="int", default="14000", minimum=1000, maximum=64000, applies="restart",
    ),
    Setting(
        key="WREN_RESEARCH_MODEL_TIMEOUT", group="Web and search",
        label="Research timeout (s)",
        help="Read timeout for the opportunities page's research summary. It "
             "gets chat's bound, not the background one: a person triggered it "
             "and is still holding the phone, and every second it runs is a "
             "second a chat turn is queued behind it.",
        type="int", default="120", minimum=10, maximum=1800, applies="restart",
    ),

    # ----------------------------------------------------- Opportunity scout
    Setting(
        key="OPP_STATES", group="Opportunity scout", label="States",
        help="States whose new SEC Form D filings count as a just-funded "
             "signal. Comma-separated. Empty falls back to the job-search "
             "states in your preferences below.",
        default="", applies="next_run",
    ),
    Setting(
        key="OPP_STALLED_DAYS", group="Opportunity scout", label="Stalled after (days)",
        help="How long a watched leadership opening stays open before it is "
             "flagged as stalled.",
        type="int", default="45", minimum=1, maximum=365, applies="next_run",
    ),
    Setting(
        key="OPP_SCORE_THRESHOLD", group="Opportunity scout",
        label="Push score threshold",
        help="Minimum model score, 1 to 10, that earns a phone push on top of "
             "the weekly email.",
        type="int", default="8", minimum=1, maximum=10, applies="next_run",
    ),

    # ---------------------------------------------------- Wiki and learnings
    Setting(
        key="WIKI_VAULT_PATH", group="Wiki and learnings", label="Vault root",
        help="Root of the Obsidian vault Wren reads to answer what did I decide "
             "about X. Keep it out of Documents, Desktop and Downloads: those "
             "are permission-protected, and a launchd job has no session to "
             "show the consent prompt in, so it blocks on a dialog nobody sees.",
        type="path", default="~/Vaults/llm-wiki-learnings", applies="live",
    ),
    Setting(
        key="LEARNINGS_DIR", group="Wiki and learnings", label="Daily reviews dir",
        help="Where the daily review Markdown files are written, one per day. "
             "This is the wiki builder's ingest queue.",
        type="path", default="~/Vaults/llm-wiki-learnings/raw", applies="live",
    ),
    Setting(
        key="SYNTHESIS_DIR", group="Wiki and learnings", label="Nudges dir",
        help="Where the dated nudge archive is written. Must NOT be the daily "
             "reviews dir: that one is an ingest queue, and a nudge asking is "
             "this worth adding becomes a fabricated wiki claim once ingested. "
             "The directory must already exist.",
        type="path", default="~/Vaults/llm-wiki-learnings/nudges", applies="live",
    ),
    Setting(
        key="WREN_WIKI_LINT_ROOT", group="Wiki and learnings", label="Wiki lint checkout",
        help="The sibling checkout the wiki lint page shells out to. That repo "
             "owns the audit; Wren runs it as a subprocess with the checkout's "
             "own interpreter and renders the result. Missing means the view "
             "says so and nothing else breaks.",
        type="path", default="~/Projects/ObsidianWikiAgent", applies="live",
    ),
    Setting(
        key="WREN_SKILLS_DIR", group="Wiki and learnings", label="Skills dir",
        help="Where reusable procedures are stored, one Markdown file per "
             "skill. Empty uses this repo's own skills directory.",
        type="path", default="", applies="live",
    ),
    Setting(
        key="WREN_EXTERNAL_TASK_ROOTS", group="Wiki and learnings",
        label="Sibling repos on the dashboard",
        help="Sibling repos whose launchd jobs the dashboard reports on "
             "alongside Wren's own. Report-only: no run button, because they "
             "are not Wren's to spawn.",
        default="", applies="live", editable=False,
        reason="A name=path#prefix mini-language that needs an editor of its "
               "own; a malformed entry is skipped silently, so a typo here "
               "looks exactly like a repo that has no jobs.",
    ),

    # -------------------------------------------------- Projects and builds
    Setting(
        key="PROJECTS_DIR", group="Projects and builds", label="Projects dir",
        help="Where your local checkouts live. Each direct subdirectory is "
             "scanned for git freshness plus its README, its first configured "
             "instruction file and its docs headings — and nothing else.",
        type="path", default="~/Projects", applies="live",
    ),
    Setting(
        key="WREN_BUILD_REPO_ROOT", group="Projects and builds",
        label="Build checkout",
        help="The one checkout a tagged build is allowed to touch. One repo on "
             "purpose: a plan for a sibling repo tagged today would be built in "
             "the wrong place, and no precondition can detect that.",
        type="path", default="~/Projects/LocalLLMAgent", applies="next_run",
    ),
    Setting(
        key="WREN_BUILD_WORKTREE_ROOT", group="Projects and builds",
        label="Build worktrees dir",
        help="Where throwaway build worktrees are created — outside the repo, "
             "so a build never shows up in the main checkout's git status. "
             "They are not deleted afterwards: the branch is the deliverable.",
        type="path", default="~/Projects/.wren-builds", applies="next_run",
    ),
    Setting(
        key="WREN_CLAUDE_BIN", group="Projects and builds", label="Claude Code binary",
        help="An absolute path, because launchd does not inherit a login "
             "shell's PATH.",
        type="path", default="~/.local/bin/claude", applies="next_run",
    ),
    Setting(
        key="WREN_BUILD_TIMEOUT_S", group="Projects and builds",
        label="Build timeout (s)",
        help="Ceiling that stops a wedged build holding the worker forever. A "
             "plan-sized change takes a few minutes.",
        type="int", default="1800", minimum=60, maximum=21600, applies="next_run",
    ),
    Setting(
        key="WREN_BUILD_MODEL", group="Projects and builds", label="Build model",
        help="Model for the build, such as opus or sonnet. Empty uses Claude "
             "Code's own default.",
        default="", applies="next_run",
    ),
    Setting(
        key="WREN_CLAUDE_PROJECTS_ROOT", group="Projects and builds",
        label="Claude transcripts dir",
        help="Where Claude Code keeps its transcripts. There is no reason to "
             "move this; it exists so the tests can point somewhere temporary.",
        type="path", default="~/.claude/projects", applies="live",
    ),
    Setting(
        key="WREN_CLAUDE_PLANS_ROOT", group="Projects and builds",
        label="Claude plans dir",
        help="Where Claude Code keeps its plan files. Same note as above.",
        type="path", default="~/.claude/plans", applies="live",
    ),

    # ----------------------------------------------------------------- Games
    Setting(
        key="WEIGH_ANCHOR_DIR", group="Games", label="Weigh Anchor checkout",
        help="Where the game's own repo lives. Wren serves its built bundle "
             "under her own origin so the login gate covers it.",
        type="path", default="~/Projects/WeighAnchor", applies="live",
    ),
    Setting(
        key="WEIGH_ANCHOR_PORT", group="Games", label="Weigh Anchor port",
        help="Must match the port in that game's own launchd plist.",
        type="int", default="3002", minimum=1, maximum=65535, applies="live",
    ),

    # ------------------------------------------------------- Logs and usage
    Setting(
        key="WREN_LOGS_DIR", group="Logs and usage", label="Logs dir",
        help="Where the structured per-task logs are written. Empty means the "
             "repo's own logs directory, which is what launchd and the "
             "dashboard expect.",
        type="path", default="", applies="restart", editable=False,
        reason="The log viewer's file glob and every launchd plist's output "
               "path both point at the repo's logs directory. Moving it from "
               "here empties the viewer and the dashboard's run history at "
               "once.",
    ),
    Setting(
        key="WREN_USAGE_RETENTION_DAYS", group="Logs and usage",
        label="Usage retention (days)",
        help="The model-usage ledger is pruned on write, not rotated: past the "
             "size cap below it is rewritten keeping only rows newer than this.",
        type="int", default="90", minimum=1, maximum=3650, applies="live",
    ),
    Setting(
        key="WREN_USAGE_MAX_BYTES", group="Logs and usage", label="Usage size cap",
        help="The size that triggers that prune. At Wren's rate the retention "
             "window fires first, so this is the backstop.",
        type="int", default="5000000", minimum=100000, maximum=1000000000,
        applies="live",
    ),
)


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #

_BY_KEY = {s.key: s for s in SETTINGS}


def by_key(key: str) -> Setting | None:
    """The row for `key`, or None when the key is not ours to manage."""
    return _BY_KEY.get(key)


def keys() -> tuple[str, ...]:
    """Every key that has a row, in table order."""
    return tuple(s.key for s in SETTINGS)


def grouped() -> list[tuple[str, list[Setting]]]:
    """[(group, rows)] in GROUPS order — the order the page renders."""
    return [(g, [s for s in SETTINGS if s.group == g]) for g in GROUPS]


def secret_keys() -> frozenset[str]:
    """Keys whose value must never leave this process."""
    return frozenset(s.key for s in SETTINGS if s.secret)


def default_for(key: str) -> str:
    """The schema layer's value for `key`: empty when the row has no default,
    or when there is no row at all."""
    row = _BY_KEY.get(key)
    return row.default if row else ""


# --------------------------------------------------------------------------- #
# Structured defaults (the preference sections)
# --------------------------------------------------------------------------- #

# Loaded from the committed template rather than inlined as a Python literal:
# config/preferences.example.json is what a fresh clone already boots from, and
# two copies of the same shipped values drift the moment one is edited.
_EXAMPLE_PREFS = _ROOT / "config" / "preferences.example.json"

# Sections a user may save. The example file's keys minus the free-text
# "_comment" ones, which are dropped on the way in — their text belongs in a
# row's help, where the page shows it, not in a file only a JSON reader opens.
PREFERENCE_SECTIONS = (
    "persona", "projects", "calendar", "morning_brief",
    "sports", "learnings", "job_search",
)


def _strip_comments(value):
    """Drop every _comment key, at any depth, from a loaded template."""
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items()
                if not k.startswith("_comment")}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def _load_structured_defaults() -> dict:
    try:
        raw = json.loads(_EXAMPLE_PREFS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.error(f"could not load {_EXAMPLE_PREFS}: {e}")
        return {}
    if not isinstance(raw, dict):
        logger.error(f"{_EXAMPLE_PREFS} is not a JSON object")
        return {}
    clean = _strip_comments(raw)
    return {name: clean[name] for name in PREFERENCE_SECTIONS if name in clean}


STRUCTURED_DEFAULTS = _load_structured_defaults()


# --------------------------------------------------------------------------- #
# Section validation
# --------------------------------------------------------------------------- #
#
# These live here, next to PREFERENCE_SECTIONS, and not in agent/prefs.py where
# they were written. agent/prefs.py imports agent/config.py, so config could not
# import it back — which left config.set_preference checking only that a section
# NAME is known and the value is an object, while every flat key beside it got
# the full schema check. "Validate everything, then write once" was true of one
# half. Both live callers remembered to call the validator first; a third would
# not have, and the accessors in prefs.py degrade a bad section silently (an
# emptied job_search list matches nothing rather than erroring).
#
# agent/prefs.py re-exports validate_section, so its callers and the tests that
# assert through it are unchanged. This module still imports nothing of ours.

# The job-search lists the opportunity scout builds its matchers from. Every one
# has to be a non-empty list of non-empty strings: an emptied list does not
# narrow the search, it silently matches nothing.
_JOB_SEARCH_LISTS = ("seniority_terms", "function_terms", "title_acronyms",
                     "hn_phrases", "states")

# Calendar roles with a consumer, plus the three kept as legacy. strava_download
# needs `fitness`; calendar_colorizer needs exactly one `fallback`.
_REQUIRED_CALENDAR_ROLES = ("work", "meetings", "appointments", "fitness")


def validate_section(name: str, value) -> list[str]:
    """Problems with one preference section, as sentences a person can act on.

    Empty means the section is usable. The accessors in agent/prefs.py already
    degrade safely on a bad section — this is the layer that says so out loud,
    before a save lands, rather than letting the Scores block quietly go missing.

    Unknown section names return one problem rather than raising: the callers
    are a save route handling a form and a migration reporting a plan, and a
    name they do not know is a message to show, not a crash.
    """
    if name not in _VALIDATORS:
        return [f"{name} is not a known preference section"]
    if not isinstance(value, dict):
        return [f"{name} must be an object"]
    return _VALIDATORS[name](value)


def _validate_persona(value: dict) -> list[str]:
    return [f"persona.{field} is missing or empty"
            for field in ("user_name", "positioning", "engagement_model")
            if not value.get(field)]


def _validate_calendar(value: dict) -> list[str]:
    entries = value.get("categories")
    if not isinstance(entries, list) or not entries:
        return ["calendar.categories must be a non-empty list"]

    problems = []
    for i, category in enumerate(entries):
        if not isinstance(category, dict):
            problems.append(f"calendar.categories[{i}] is not an object")
            continue
        for field in ("name", "color_id", "color_name"):
            if not category.get(field):
                problems.append(f"calendar.categories[{i}].{field} is missing or empty")

    roles = [c.get("role") for c in entries if isinstance(c, dict) and c.get("role")]
    problems += [f"no calendar category has role {role!r}"
                 for role in _REQUIRED_CALENDAR_ROLES if role not in roles]
    if roles.count("fallback") != 1:
        problems.append("exactly one calendar category must have role 'fallback', "
                        f"found {roles.count('fallback')}")
    return problems


def _validate_job_search(value: dict) -> list[str]:
    problems = []
    for key in _JOB_SEARCH_LISTS:
        entries = value.get(key)
        if not isinstance(entries, list) or not entries:
            problems.append(f"job_search.{key} must be a non-empty list")
            continue
        if not all(isinstance(v, str) and v for v in entries):
            problems.append(f"job_search.{key} must hold non-empty strings")
    return problems


def _validate_projects(value: dict) -> list[str]:
    entries = value.get("instruction_files")
    if not isinstance(entries, list) or not entries:
        return ["projects.instruction_files must be a non-empty list"]
    # A bare filename, never a path: the scanner reads these from a project root
    # it does not otherwise trust, so a separator would widen that boundary.
    return [f"projects.instruction_files[{i}] must be a bare filename, not a path"
            for i, entry in enumerate(entries)
            if (not isinstance(entry, str) or not entry or entry in (".", "..")
                or "/" in entry or "\\" in entry)]


def _validate_morning_brief(value: dict) -> list[str]:
    hours = value.get("calendar_hours_ahead")
    if hours is None:
        return []
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        return ["morning_brief.calendar_hours_ahead must be a positive whole "
                "number of hours"]
    return []


def _validate_sports(value: dict) -> list[str]:
    entries = value.get("teams")
    if entries is None or entries == []:
        return []  # no teams means the Scores block is off, which is allowed
    if not isinstance(entries, list):
        return ["sports.teams must be a list"]
    return [f"sports.teams[{i}] needs a league and an id"
            for i, team in enumerate(entries)
            if not isinstance(team, dict) or not team.get("league") or not team.get("id")]


def _validate_learnings(value: dict) -> list[str]:
    return []  # no consumer asserts a shape here yet


_VALIDATORS = {
    "persona": _validate_persona,
    "calendar": _validate_calendar,
    "job_search": _validate_job_search,
    "projects": _validate_projects,
    "morning_brief": _validate_morning_brief,
    "sports": _validate_sports,
    "learnings": _validate_learnings,
}
