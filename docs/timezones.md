# UTC sources, local day windows

Every source Wren reads stamps its timestamps in UTC. Every question Wren
answers is about a *local* calendar day — "what did I browse yesterday", "what
did I Like on Monday". Converting between the two is the single bug this
codebase has shipped most often.

## The rule

**A source's timestamps are UTC until proven otherwise; our day windows are
local. Convert before comparing or truncating.**

Never slice an ISO stamp and match it against a local calendar day:

```python
# WRONG — published_at is UTC, `day` is local
if published_at[:10] == day:

# RIGHT — convert first
from zoneinfo import ZoneInfo
from agent.dates import local_timezone

dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
if dt.astimezone(ZoneInfo(local_timezone())).date().isoformat() == day:
```

`agent/tools/youtube.py:_liked_local_date` is the reference implementation. It
returns `""` for an unparseable stamp, which no window matches — degrade, don't
crash.

## Why it survives review

This bug is not subtle in its effect, only in its timing. **The UTC date and the
local date agree for most of the day.** In `America/New_York` they diverge only
after 8pm local (EDT) or 7pm (EST) — the moment UTC rolls over to tomorrow.

So the sliced-string version:

- passes code review, because the comparison looks obviously right
- passes a spot-check, because anyone testing it at midday sees correct output
- passes the test suite, if the test happens to use a midday timestamp
- then silently misfiles or drops **evening data only**, every day, forever

Nothing raises. Nothing logs. The digest is just quietly short. That is what
makes it worth a rule rather than a code comment.

## The three incidents

| Where | Commit | What happened |
|---|---|---|
| Chrome history | `01c0718` | Day boundaries resolved in UTC, not local. Fixed by resolving the window through `agent.dates` and promoting `_local_timezone` into the shared `local_timezone()`. |
| YouTube Likes | `5607532` | The API stamps `publishedAt` in UTC. A video Liked at 9:20pm EDT carries the *next* UTC date, so windowing on the raw stamp dropped every evening Like — and the next day's run attributed it to the wrong day. |
| Weather forecast | — | Fixed defensively while building the multi-day forecast, before it could ship the bug. |

The YouTube case is the clearest illustration: Liking something at 9:20pm on
Jul 13 local stamps `2026-07-14T01:20:00Z`. The Jul 13 run dropped it. The Jul 14
run then claimed you Liked it on the 14th.

## `local_timezone()`

`agent/dates.py:36` is the one home for the local zone. Several tools and tasks
need the same answer, so none of them roll their own.

- Reads the real zoneinfo path via `/etc/localtime` rather than `tzinfo.__str__`.
  Google Calendar rejects abbreviations like `EDT`, so the IANA name
  (`America/New_York`) is the only usable form.
- Overridable with the `TIMEZONE` environment variable.
- Falls back to `"UTC"` if the path can't be resolved.

Callers include `agent/tools/chrome_history.py`, `agent/tools/youtube.py`,
`agent/tools/reminders.py`, and `tasks/morning_brief.py`.

## Testing it

**Any test that touches a day boundary pins `TIMEZONE` rather than inheriting
the host's zone.** A test that passes on a laptop in New York and fails in CI in
UTC is worse than no test — and one that inherits UTC on both silently stops
testing the thing it was written for.

```python
def test_fetch_windows_on_local_date_not_utc(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
```

(`monkeypatch` is pytest's fixture for setting a value and undoing it when the
test ends.)

Pick a fixture timestamp **in the divergent window** — an evening local time
whose UTC date is the next day. A midday timestamp exercises nothing:

```python
_item("EVENING", "2026-07-14T01:20:00Z")  # 9:20pm Jul 13 local
```

Suites already doing this: `tests/test_youtube.py`, `tests/test_chrome_history.py`,
`tests/test_push_log.py`, `tests/test_calendar.py`, `tests/test_morning_brief.py`,
`tests/test_daily_synthesis.py`, `tests/test_nudges.py`.

## Checklist for a new source

1. What zone does this API stamp in? Assume UTC until the docs say otherwise.
2. Convert to local before any comparison, truncation, or grouping by day.
3. Use `local_timezone()` — do not read `/etc/localtime` again.
4. Return `""` (or `None`) for an unparseable stamp, and make sure no window matches it.
5. Write the test with an evening timestamp and a pinned `TIMEZONE`.

## Related

- [model-constraints.md](model-constraints.md) — why date math never goes to the model
- [module-map.md](module-map.md) — where `agent/dates.py` sits
