* 2026-07-01 - Initial commit: local LLM agent system for scheduled tasks
* 2026-07-01 - Add daily calendar colorizer task
* 2026-07-02 - Give the agent an identity (Wren) and an ad hoc chat interface
* 2026-07-02 - Harden and tidy the agent after Opus review
* 2026-07-02 - Add Tavily-backed web search tool for chat
* 2026-07-02 - Add AI news to morning brief; give chat a real send_morning_brief tool
* 2026-07-02 - Harden web-search brief rendering and refresh docs
* 2026-07-02 - Let Wren fetch multi-day weather forecasts
* 2026-07-02 - Add starred-repo update tracking to Wren
* 2026-07-06 - Replace Composio with direct Strava API integration
* 2026-07-06 - Fix weekly_learnings Ollama read timeout
* 2026-07-06 - Resolve dates in chat/Strava without trusting the model for the year
* 2026-07-06 - Extract date resolution into a shared agent.dates helper
* 2026-07-06 - Add Wren dashboard (schedules, run history, capabilities, chat)
* 2026-07-06 - Redesign dashboard as one page + add Wren favicon
* 2026-07-07 - Keep morning-brief AI news fresh (last 24h, dated, newest-first)
* 2026-07-07 - Add pure-function test suite (pytest)
* 2026-07-07 - Fix calendar date bias; guard multi-call confirm turns
* 2026-07-07 - Bind chat server to loopback; document trust boundaries
* 2026-07-07 - Refactor tool boilerplate; share Google-service factory
* 2026-07-07 - Parallelize GitHub enrich; cache runs; throttle login; pin deps
* 2026-07-08 - Harden chat surface; fix Chrome-history local dates; add server tests
* 2026-07-08 - Add Google Tasks capability: view, create, reschedule, complete
* 2026-07-08 - Show date in morning brief calendar entries
* 2026-07-08 - Add two-tier long-term memory: active (pinned) vs archival
* 2026-07-08 - Add read-only /memories page to view active and archival memories
* 2026-07-08 - Write weekly learnings to Obsidian Markdown file instead of Google Doc
* 2026-07-09 - Remove AI News from morning brief
* 2026-07-09 - Add .gitignore for local CodeGraph index data
* 2026-07-09 - Update README: remove AI news from morning brief description
* 2026-07-09 - Remove dead _parse_pub_date/_sort_by_recency tests
* 2026-07-09 - Let Wren query the personal learnings wiki
* 2026-07-09 - Set explicit Ollama num_ctx and log prompt token usage
* 2026-07-09 - Convert daily_log to deterministic Python, dropping the model
* 2026-07-09 - Add procedural memory (skills) to Wren's chat
* 2026-07-09 - Merge skills-procedural-memory
* 2026-07-09 - Make memory store writes atomic and thread-safe
* 2026-07-09 - Make morning_brief starred-state write atomic
* 2026-07-09 - Add YouTube liked videos as a weekly-learnings source
* 2026-07-09 - Merge add-youtube-liked-videos
* 2026-07-09 - Add /map — explorable radial system map of the agent
* 2026-07-10 - Add proactive push: ntfy failure alerts and scheduled reminders
* 2026-07-10 - Add background tasks with notify-on-done (A + push-to-approve)
* 2026-07-10 - Add /chat/cancel Stop button + cap oversized tool results
* 2026-07-10 - Add codebase review improvement plan (2026-07-10 audit)
* 2026-07-10 - Add agent/store.py: cross-process locking + corrupt-file resilience for JSON stores
* 2026-07-10 - Exclude prompt-state writers from unattended background runs
* 2026-07-10 - Pin send_email's recipient for the model and show it on both approval surfaces
* 2026-07-11 - Budget chat history per turn; extract the shared /chat turn-runner
* 2026-07-11 - Make the background worker cheap when idle and resilient when running
* 2026-07-11 - Add a timeout to Google API calls — the last HTTP surface without one
* 2026-07-11 - Close the unattended-task test gaps; fix color-id drift and the silent email fallback
* 2026-07-11 - Server hygiene: one turn per session, idle-session eviction, throttle hardening
* 2026-07-11 - Remaining hygiene: store pruning, dashboard truthfulness, dead code, alerts, docs
* 2026-07-11 - Add the opportunity scout: daily fractional-work signal digest
* 2026-07-11 - Stop task tests from writing fixture data into production logs
* 2026-07-11 - Ignore rotated .log.bak files in logs/
* 2026-07-11 - Fix weekly_learnings cold-model timeout with warm-up + keep-alive
* 2026-07-11 - Block ntfy pushes in tests so the suite can't alert the user's phone
* 2026-07-11 - Add /opportunities triage page to the dashboard
* 2026-07-11 - Add company research briefs to the opportunity scout
* 2026-07-11 - Stop server tests from spawning real research threads
* 2026-07-11 - Codify the tests-never-touch-production-state rule in CLAUDE.md
* 2026-07-11 - Generalize company research: research any company by name
* 2026-07-11 - Filter fund paperwork and collapse serial Form D filers
* 2026-07-11 - Document the opportunity scout lifecycle in docs/opportunity-scout.md
* 2026-07-11 - Re-score openings when they flip to stalled_search
* 2026-07-11 - Add iCIMS as a fourth watchable ATS (via portal sitemap)
* 2026-07-11 - Match HN roles against the headline, with word boundaries
* 2026-07-11 - Move personal preferences out of Python into config/preferences.json
* 2026-07-11 - Add Firecrawl page fetch (fetch_webpage) and app teardown (evaluate_app)
* 2026-07-12 - Cap generation length and rebuild the system message every turn
* 2026-07-12 - Tighten tool schema descriptions to cut fixed prompt overhead
* 2026-07-12 - Lazy-load chat tools by group to cut per-turn schema overhead
* 2026-07-12 - Note core/group categorization in the New tool checklist
* 2026-07-12 - Add a pluggable LLM backend seam (Ollama default, opt-in Gemini)
* 2026-07-13 - Split weekly learnings into two focused daily tasks; harden Gemini backend
* 2026-07-13 - Skip the daily Chrome log when there's nothing relevant to record
* 2026-07-13 - Add daily AI-chat-learnings task (Claude Code sessions + Gemini drop-folder)
* 2026-07-13 - Add recategorize memory tool to relabel a fact in place
* 2026-07-14 - Stop the test suite writing fixture rows into production logs
* 2026-07-14 - Log run boundaries so the dashboard sees opportunity digest runs
* 2026-07-14 - Keep volunteer-admin and Microsoft 365 browsing out of the daily learnings
* 2026-07-14 - Give the daily review page paths, and fix the port bug hiding local servers
* 2026-07-14 - Drop the volunteer-admin subject from the learnings entirely, and unclamp the prompt caps
* 2026-07-14 - Window Liked videos on the local date, not the raw UTC stamp
* 2026-07-14 - Log a run-complete boundary on the quiet-day early returns
* 2026-07-14 - Write down the UTC-vs-local day-boundary rule
* 2026-07-14 - Add changelog.md — one line per commit, generated from git log
* 2026-07-14 - Unstick the chat composer when a turn's fetch fails
* 2026-07-14 - Refactor the two chat docks into one shared script
* 2026-07-14 - Add a jest/jsdom suite for the shared chat dock
* 2026-07-14 - Drop the wiki tools that read raw/ — it's a write-only handoff
* 2026-07-14 - Regenerate changelog from git log
* 2026-07-14 - Update learnings destination from /Volumes/T7 to ~/Documents/llm-wiki-learnings
* 2026-07-14 - Expand tilde in wiki/learnings paths from .env vars
* 2026-07-15 - Drop calendar events from the daily learnings routine
* 2026-07-15 - Fix docs referencing the deleted weekly_learnings task
* 2026-07-15 - Expand the tilde in WREN_GEMINI_CHATS_DIR
* 2026-07-15 - Retry Gemini's transient errors in bg_worker
* 2026-07-15 - Confine Gemini tool-call pairing to the emitting turn
* 2026-07-15 - Record why fetch_webpage needs no host allowlist
* 2026-07-15 - Correct the documented WREN_GEMINI_MAX_OUTPUT_TOKENS default
* 2026-07-15 - Guard every config/ store suite-wide, not just per-test
* 2026-07-15 - Stop describing the vault as an unmounted external drive
* 2026-07-15 - Cancel the running turn when a new chat starts
* 2026-07-15 - Warn at startup when the context budget can overflow num_ctx
* 2026-07-15 - Stop jest scanning worktree package.json copies as a duplicate haste module
* 2026-07-15 - Give the daily Chrome log room and a ranking rule
* 2026-07-15 - Retime the morning tasks to finish before the brief
* 2026-07-16 - Rename daily_log to strava_download — the name is the capability, not the cadence
* 2026-07-16 - Correct the scheduled-task table's stale times and order it by clock
* 2026-07-16 - Add a log inspector — Wren watched everything except itself
* 2026-07-16 - Reschedule Opportunity Digest from daily to weekly Sundays at 9:00 PM
* 2026-07-16 - Stop colima giving up after one failed start, and let a dead push channel speak
* 2026-07-16 - Refuse test log handlers on the real logs/, don't just redirect them
* 2026-07-16 - Page EDGAR by 100s — the poller was striding 10 through 100-hit pages
* 2026-07-16 - Show the push channel's health on the dashboard, not just at 8am
* 2026-07-21 - Add a /starred view summarizing starred repos with cached README blurbs
* 2026-07-21 - Wrap the nav menu onto its own row on mobile instead of clipping it
* 2026-07-21 - Share one top-nav across every view instead of per-page copies
* 2026-07-21 - Label the health pill "ntfy up", not "push up"
* 2026-07-21 - Let Wren answer what tasks she runs and when
* 2026-07-21 - Make recall() emit an explicit scope so memory lists agree
* 2026-07-22 - Chmod google_credentials.json to owner-only on read
* 2026-07-22 - Confirm-gate memory writes and give every write tool a describer
* 2026-07-22 - Add tests for the previously untested tool modules
* 2026-07-22 - Split chat/server.py: extract login throttle and the JSON-API blueprints
* 2026-07-22 - Extract the Gemini backend from agent/loop.py
* 2026-07-22 - Split opportunities.py: move the scout/digest CRUD to _opportunities_feed
* 2026-07-22 - Unify the brief/digest logger-binding wrappers; refresh CLAUDE.md map
* 2026-07-22 - Add docs/memory.md and docs/background.md; link from README
* 2026-07-22 - Fix stale weekly→daily learnings references across docs
* 2026-07-22 - Add release-awareness column to the /starred view
* 2026-07-22 - Wire the four unmapped routines into the /map view; guard against future drift
* 2026-07-24 - Add manual frontier escalation to chat; fix Gemini thought_signature round-trip
* 2026-07-24 - Add installed-version tracking to the /starred view; drop "Last updated"
* 2026-07-24 - Add proactive synthesis task and standards-lens evaluation
* 2026-07-24 - Correct the CORE tools list in tool-loading docs
* 2026-07-24 - Widen daily learnings scope to include product management