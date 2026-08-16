"""Tests for agent.tools.chrome_history.fetch_chrome_history's date handling.

The fix under test: day boundaries are resolved in the *local* timezone (not
UTC), and days are resolved in Python (via agent.dates) rather than trusting the
model. TIMEZONE is pinned so the local zone is deterministic, and _query_history
is stubbed so no real Chrome DB is touched — the assertions are about the
datetimes the tool hands it.
"""

import agent.tools.chrome_history as ch


def _capture_query(monkeypatch):
    """Stub _query_history to record the (start, end) datetimes and return no
    rows, so fetch_chrome_history runs without a real Chrome History file."""
    captured = {}

    def fake_query(start, end):
        captured["start"] = start
        captured["end"] = end
        return []

    monkeypatch.setattr(ch, "_query_history", fake_query)
    return captured


def test_days_ago_builds_local_two_day_window(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    captured = _capture_query(monkeypatch)

    result = ch.fetch_chrome_history(days_ago=1)

    # Boundaries are timezone-aware and in the pinned local zone, not UTC.
    assert str(captured["start"].tzinfo) == "America/New_York"
    assert str(captured["end"].tzinfo) == "America/New_York"
    # Full-day span: 00:00:00 -> 23:59:59, from yesterday to today.
    assert (captured["start"].hour, captured["start"].minute, captured["start"].second) == (0, 0, 0)
    assert (captured["end"].hour, captured["end"].minute, captured["end"].second) == (23, 59, 59)
    assert (captured["end"].date() - captured["start"].date()).days == 1
    assert result["total_meaningful_visits"] == 0
    assert "range" in result


def test_explicit_range_uses_local_offset_not_utc(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    captured = _capture_query(monkeypatch)

    ch.fetch_chrome_history("2026-06-01", "2026-06-07")

    # June in New York is EDT (UTC-04:00) — the old code stamped these as UTC.
    assert captured["start"].isoformat() == "2026-06-01T00:00:00-04:00"
    assert captured["end"].isoformat() == "2026-06-07T23:59:59.999999-04:00"


def test_bare_month_day_is_resolved_in_python(monkeypatch):
    # A bare "MM-DD" must be accepted and given a year (past-biased), not left
    # for the model to guess — same contract as fetch_strava.
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    captured = _capture_query(monkeypatch)

    result = ch.fetch_chrome_history("06-01", "06-07")

    assert captured["start"].month == 6 and captured["start"].day == 1
    # Year was filled in (4-digit) rather than passed through verbatim.
    assert result["range"].startswith("20")


def test_requires_days_ago_or_full_range(monkeypatch):
    _capture_query(monkeypatch)
    assert "error" in ch.fetch_chrome_history()
    assert "error" in ch.fetch_chrome_history(start="2026-06-01")  # end missing


# --------------------------------------------------------------------------- #
# _extract_domain — the port bug that defeated NOISE_DOMAINS
# --------------------------------------------------------------------------- #

def test_extract_domain_strips_port():
    # The bug: .netloc kept the port, so "127.0.0.1:8420" never matched the
    # "127.0.0.1" noise entry and the user's own dashboard looked like a real site.
    assert ch._extract_domain("http://127.0.0.1:8420/dashboard") == "127.0.0.1"
    assert ch._extract_domain("http://localhost:3000/x") == "localhost"


def test_extract_domain_strips_userinfo_and_lowercases():
    assert ch._extract_domain("https://user:pw@Example.COM/x") == "example.com"


def test_extract_domain_of_junk_url_is_empty():
    assert ch._extract_domain("about:blank") == ""
    assert ch._extract_domain("") == ""


def test_local_server_is_filtered_as_noise_now():
    rows = [{"url": "http://127.0.0.1:8420/dashboard", "title": "Wren Dashboard", "visits": 8},
            {"url": "https://ai.google.dev/docs", "title": "Gemini", "visits": 2}]
    assert [s["domain"] for s in ch._filter_and_group(rows)] == ["ai.google.dev"]


# --------------------------------------------------------------------------- #
# _filter_and_group — per-domain page paths (opt-in, chat's shape unchanged)
# --------------------------------------------------------------------------- #

def _rows():
    return [
        {"url": "https://ai.google.dev/gemini-api/docs/models", "title": "Models", "visits": 9},
        {"url": "https://ai.google.dev/gemini-api/docs/pricing", "title": "Pricing", "visits": 4},
        {"url": "https://ai.google.dev/", "title": "Home", "visits": 3},
        {"url": "https://tailscale.com/kb/acl", "title": "ACLs", "visits": 2},
    ]


def test_default_result_has_no_pages_key_so_chat_is_unchanged():
    # A day's result is already near OLLAMA_MAX_TOOL_RESULT_CHARS; the default
    # must not grow it.
    sites = ch._filter_and_group(_rows())
    assert all("pages" not in s for s in sites)
    assert [s["domain"] for s in sites] == ["ai.google.dev", "tailscale.com"]


def test_pages_per_domain_keeps_top_paths_by_visits():
    sites = ch._filter_and_group(_rows(), pages_per_domain=5)
    google = next(s for s in sites if s["domain"] == "ai.google.dev")
    assert [p["path"] for p in google["pages"]] == [
        "/gemini-api/docs/models", "/gemini-api/docs/pricing",
    ]


def test_pages_drops_query_strings():
    rows = [{"url": "https://example.com/a/b?utm_source=spam&id=99", "title": "T", "visits": 1}]
    sites = ch._filter_and_group(rows, pages_per_domain=5)
    assert sites[0]["pages"] == [{"path": "/a/b", "visits": 1}]


def test_pages_dedupes_paths_that_differ_only_by_query():
    # Real case: a LinkedIn profile path was reached by three tracking urls and
    # filled three of the five page slots.
    rows = [
        {"url": "https://example.com/in/x/?tr=1", "title": "P", "visits": 5},
        {"url": "https://example.com/in/x/?tr=2", "title": "P", "visits": 3},
        {"url": "https://example.com/feed/", "title": "F", "visits": 1},
    ]
    sites = ch._filter_and_group(rows, pages_per_domain=5)
    assert [p["path"] for p in sites[0]["pages"]] == ["/in/x/", "/feed/"]


def test_pages_respects_the_cap():
    rows = [{"url": f"https://example.com/p{i}", "title": "T", "visits": i} for i in range(10)]
    sites = ch._filter_and_group(rows, pages_per_domain=3)
    assert len(sites[0]["pages"]) == 3


def test_pages_omitted_when_only_the_homepage_was_visited():
    # A bare "/" carries no signal the domain doesn't already give.
    rows = [{"url": "https://example.com/", "title": "T", "visits": 1}]
    assert "pages" not in ch._filter_and_group(rows, pages_per_domain=5)[0]


def test_long_path_is_truncated():
    rows = [{"url": "https://example.com/" + "a" * 500, "title": "T", "visits": 1}]
    sites = ch._filter_and_group(rows, pages_per_domain=5)
    assert len(sites[0]["pages"][0]["path"]) == ch._MAX_PATH_CHARS


def test_pages_per_domain_is_not_exposed_to_the_model():
    # Python-only knob: the schema must not offer it, or the model may set it and
    # blow the tool-result cap in chat.
    assert "pages_per_domain" not in ch.TOOL_SCHEMA["function"]["parameters"]["properties"]


# --- the chat path is bounded, the batch callers are not ---------------------
# One day of real browsing is 8897 chars against the loop's 8000-char cap, and
# total_meaningful_visits used to be the LAST key — so the trim took the count
# that was the only sign the list was short.

def _stub_sites(monkeypatch, n, title_len=60):
    monkeypatch.setattr(ch, "_query_history", lambda start, end: [])
    monkeypatch.setattr(
        ch, "_filter_and_group",
        lambda rows, pages_per_domain=1: [
            {"domain": f"site-{i:03d}.example.com", "title": "T" * title_len,
             "visit_count": n - i, "pages": [f"/path-{i}"]}
            for i in range(n)
        ],
    )


def test_chat_calls_are_capped_and_say_they_are_partial(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    _stub_sites(monkeypatch, 200)

    result = ch.fetch_chrome_history(days_ago=1)

    assert result["total_meaningful_visits"] == 200      # the true total
    assert result["sites_shown"] < 200
    assert len(result["sites"]) == result["sites_shown"]
    assert "not describe this as everything" in result["partial"].lower()


def test_the_capped_result_fits_the_tool_result_cap(monkeypatch):
    import json

    from agent.loop import MAX_TOOL_RESULT_CHARS
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    # Long titles: 60 sites was under the count cap and still 8916 chars.
    _stub_sites(monkeypatch, 200, title_len=200)

    result = ch.fetch_chrome_history(days_ago=1)
    assert len(json.dumps(result)) < MAX_TOOL_RESULT_CHARS


def test_the_count_leads_so_a_trim_cannot_take_it(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    _stub_sites(monkeypatch, 200)

    keys = list(ch.fetch_chrome_history(days_ago=1))
    assert keys.index("total_meaningful_visits") < keys.index("sites")
    assert keys.index("partial") < keys.index("sites")


def test_batch_callers_opt_out_and_get_everything(monkeypatch):
    # daily_chrome_learnings and daily_synthesis summarize the whole day.
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    _stub_sites(monkeypatch, 200)

    result = ch.fetch_chrome_history(days_ago=1, max_sites=None)

    assert len(result["sites"]) == 200
    assert "partial" not in result


def test_a_short_day_is_not_marked_partial(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    _stub_sites(monkeypatch, 5)

    result = ch.fetch_chrome_history(days_ago=1)
    assert result["sites_shown"] == 5 and "partial" not in result
