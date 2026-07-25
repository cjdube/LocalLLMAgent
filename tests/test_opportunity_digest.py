"""Tests for tasks/opportunity_digest.py — the leadership-title filter, the
defensive score parsing, and the run contracts: one dead source degrades
rather than killing the digest, nothing new sends nothing, a send failure
leaves items unreported (and the watermark unadvanced) so the next run
retries, and a stalled opening is reported exactly once. All collaborators
are monkeypatched; nothing touches the network, the model, or Gmail."""

import json
import logging
from datetime import datetime, timedelta

import pytest

from agent.store import load_json
from agent.tools import opportunities as opp
from chat import insights
from tasks import opportunity_digest as od


# --------------------------------------------------------------------------- #
# _is_leadership
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title", [
    "VP of Engineering", "Vice President, Product", "Head of Product",
    "Director of Product Management", "Chief Technology Officer... CTO",
    "CTO", "Founding CPO", "Director, Engineering Technology",
])
def test_leadership_titles_match(title):
    assert od._is_leadership(title)


@pytest.mark.parametrize("title", [
    "Senior Software Engineer", "Product Manager", "Account Executive",
    "Head of Sales", "Director of Marketing", "Engineering Manager", "",
])
def test_non_leadership_titles_do_not_match(title):
    assert not od._is_leadership(title)


# --------------------------------------------------------------------------- #
# _parse_scores
# --------------------------------------------------------------------------- #

def _batch():
    return [{"id": "edgar:1"}, {"id": "hn:2"}]


def test_parse_scores_defensively():
    text = (
        "Here are the scores:\n"            # prose: dropped
        "1|9|Fresh raise, offer a velocity audit\n"
        "2|eleven|bad score\n"              # unparseable score: dropped
        "99|5|out of range\n"               # no such lead in the batch: dropped
        "1|15|clamped\n"                    # duplicate: last wins, clamped to 10
    )
    scores = od._parse_scores(text, _batch())
    assert scores == {"edgar:1": (10, "clamped")}


def test_parse_scores_maps_numbers_back_to_lead_ids():
    # The model never sees a lead id; Python owns the position -> id mapping.
    assert od._parse_scores("2|8|Angle", _batch()) == {"hn:2": (8, "Angle")}


def test_parse_scores_rejects_zero_and_negative_positions():
    assert od._parse_scores("0|8|x\n-1|8|y", _batch()) == {}


def test_parse_scores_empty_and_none():
    assert od._parse_scores("", _batch()) == {}
    assert od._parse_scores(None, _batch()) == {}


def test_compact_for_scoring_numbers_leads_and_hides_ids():
    compact = od._compact_for_scoring([
        {"id": "lever:acme:f1e2d3c4-5b6a-7890-abcd-ef1234567890",
         "signal": "hiring", "company": "Acme", "title": "VP Product"},
    ])
    assert compact[0]["n"] == 1
    assert "id" not in compact[0]


# --------------------------------------------------------------------------- #
# score_items
# --------------------------------------------------------------------------- #

def test_score_items_batches_overflow_instead_of_dropping_it(monkeypatch):
    """Overflow past the cap used to reach the digest unscored and then get
    marked 'digested' — losing its score/angle for good. Every item gets
    scored now, one bounded prompt per batch."""
    monkeypatch.setattr(od, "MAX_SCORE_ITEMS", 2)
    items = [{"id": f"edgar:{n}", "signal": "funded", "company": f"Co {n}",
              "title": "Form D filed 2026-07-10"} for n in range(5)]

    calls = []

    def fake_complete(**k):
        # Answer the leads in THIS prompt by their number — a real model call is
        # stateless, and numbering restarts at 1 for every batch.
        leads = json.loads(k["user_prompt"].split("leads: ", 1)[1])
        calls.append([lead["company"] for lead in leads])
        return "\n".join(f"{lead['n']}|7|Angle for {lead['company']}" for lead in leads)

    monkeypatch.setattr(od, "complete_text", fake_complete)

    scores = od.score_items(items)
    assert len(scores) == 5                       # nothing dropped
    assert [len(c) for c in calls] == [2, 2, 1]   # ceil(5/2) bounded prompts
    # Per-batch numbering must not collide across batches: lead 1 of batch 2 is
    # a different lead from lead 1 of batch 1.
    assert {i["id"] for i in items} == set(scores)
    assert scores["edgar:4"] == (7, "Angle for Co 4")


def test_score_items_with_nothing_to_score_makes_no_model_call(monkeypatch):
    monkeypatch.setattr(od, "complete_text",
                        lambda **k: pytest.fail("should not call the model"))
    assert od.score_items([]) == {}


# --------------------------------------------------------------------------- #
# build_and_send_digest / main contracts
# --------------------------------------------------------------------------- #

def _edgar_item(n=1):
    return {"id": f"edgar:{n}", "source": "edgar", "signal": "funded",
            "company": f"Startup {n}", "title": "Form D filed 2026-07-10",
            "url": "https://www.sec.gov/x", "location": "Portsmouth, NH",
            "posted_at": "2026-07-10"}


@pytest.fixture
def stubbed_run(tmp_path, monkeypatch):
    """Stub every collaborator for a happy-path run; tests then break the parts
    they're exercising. Returns the dict recording what got called."""
    monkeypatch.setattr(opp, "_STORE_PATH", tmp_path / "opportunities.json")
    monkeypatch.setattr(od, "STATE_PATH", tmp_path / "opportunities_state.json")

    seen = {"emails": [], "pushes": [], "failures": [], "prompts": []}
    monkeypatch.setattr(od, "poll_edgar",
                        lambda start, end, states: {"items": [_edgar_item()]})
    monkeypatch.setattr(od, "poll_ats", lambda watchlist: {"items": [], "errors": []})
    monkeypatch.setattr(od, "poll_hn", lambda: {"items": []})
    monkeypatch.setattr(od, "complete_text",
                        lambda **k: seen["prompts"].append(k) or "1|9|Offer a velocity audit.")
    monkeypatch.setattr(od, "send_email",
                        lambda subject, body, html=False:
                        seen["emails"].append((subject, body)) or {"message_id": "m1"})
    monkeypatch.setattr(od, "notify",
                        lambda **k: seen["pushes"].append(k) or {"ok": True})
    monkeypatch.setattr(od, "notify_failure",
                        lambda name, detail, logger=None: seen["failures"].append(str(detail)))
    return seen


def test_happy_path_scores_sends_and_pushes(stubbed_run):
    assert od.main() == 0
    assert len(stubbed_run["emails"]) == 1
    subject, body = stubbed_run["emails"][0]
    assert "Opportunity Digest" in subject
    assert "Startup 1" in body and "9/10" in body and "velocity audit" in body
    # Score 9 >= the default threshold of 8: an ntfy ping goes out too.
    assert len(stubbed_run["pushes"]) == 1
    assert stubbed_run["failures"] == []
    # Reported items leave 'new'; the EDGAR watermark advanced.
    assert opp.pending_new_items() == []
    assert load_json(od.STATE_PATH, {}).get("edgar_window_start")


def test_second_run_with_same_data_sends_nothing(stubbed_run):
    assert od.main() == 0
    assert od.main() == 0  # same poller output: all duplicates
    assert len(stubbed_run["emails"]) == 1
    assert len(stubbed_run["prompts"]) == 1  # nothing new was re-scored


def test_empty_day_sends_nothing_but_still_succeeds(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    result = od.build_and_send_digest()
    assert result == {"sent": False, "new_items": 0}
    assert stubbed_run["emails"] == []
    assert load_json(od.STATE_PATH, {}).get("edgar_window_start")


def test_dead_source_degrades_and_holds_its_watermark(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"error": "EDGAR 503"})
    monkeypatch.setattr(od, "poll_hn",
                        lambda: {"items": [{"id": "hn:7", "source": "hn",
                                            "signal": "hiring", "company": "TinyCo",
                                            "title": "Head of Product",
                                            "url": "https://news.ycombinator.com/item?id=7",
                                            "posted_at": None}]})
    monkeypatch.setattr(od, "complete_text", lambda **k: "hn:7|6|Say hi.")
    assert od.main() == 0  # HN item still went out
    assert "TinyCo" in stubbed_run["emails"][0][1]
    # EDGAR failed, so its window must NOT advance — those days get re-polled.
    assert load_json(od.STATE_PATH, {}) == {}


def test_truncated_edgar_holds_watermark_at_oldest_fetched(stubbed_run, monkeypatch):
    """A truncated poll never saw the oldest filings in its window, so the
    watermark holds where coverage ended rather than skipping past them."""
    monkeypatch.setattr(od, "poll_edgar",
                        lambda start, end, states: {"items": [_edgar_item()],
                                                    "oldest_fetched": "2026-07-10"})
    assert od.main() == 0
    assert len(stubbed_run["emails"]) == 1  # what it DID fetch still goes out
    assert load_json(od.STATE_PATH, {})["edgar_window_start"] == "2026-07-10"


def test_truncated_edgar_holds_watermark_on_the_nothing_new_path(stubbed_run, monkeypatch):
    # The early return has its own watermark write — it needs the clamp too.
    monkeypatch.setattr(od, "poll_edgar",
                        lambda *a: {"items": [], "oldest_fetched": "2026-07-11"})
    assert od.build_and_send_digest() == {"sent": False, "new_items": 0}
    assert load_json(od.STATE_PATH, {})["edgar_window_start"] == "2026-07-11"


def test_truncated_edgar_with_no_older_hits_holds_without_regressing(stubbed_run, monkeypatch):
    """Known residual: if one day's filings alone exceed the safety cap, the
    clamp can't advance. It must HOLD (re-poll the same window next run), never
    skip ahead — pinned so a future refactor can't start silently dropping."""
    od._write_edgar_watermark("2026-07-09")
    monkeypatch.setattr(od, "poll_edgar",
                        lambda start, end, states: {"items": [_edgar_item()],
                                                    "oldest_fetched": start})
    assert od.main() == 0
    assert load_json(od.STATE_PATH, {})["edgar_window_start"] == "2026-07-09"


def test_send_failure_exits_nonzero_and_leaves_items_new(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "send_email",
                        lambda subject, body, html=False: {"error": "gmail 503"})
    assert od.main() == 1
    assert any("gmail 503" in f for f in stubbed_run["failures"])
    # Nothing was marked digested and the watermark held: next run re-reports.
    assert len(opp.pending_new_items()) == 1
    assert load_json(od.STATE_PATH, {}) == {}


# --------------------------------------------------------------------------- #
# Dashboard run-history boundaries
# --------------------------------------------------------------------------- #
# The digest once logged no "Starting ... run" / "... run complete" lines, so
# chat/insights.py parsed zero runs out of a log full of successful ones and the
# dashboard showed "no runs" forever. These assert through the real parser, so
# rewording either side of the contract fails loudly.

@pytest.fixture
def digest_log(tmp_path):
    """A logger writing setup_logger's exact format to a throwaway file, plus a
    parse() that runs it back through the dashboard's parser."""
    path = tmp_path / "opportunity_digest.log"
    logger = logging.getLogger(f"test_digest_{path}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    yield logger, lambda: insights.parse_runs(path)
    logger.handlers.clear()


def test_successful_run_parses_as_one_success(stubbed_run, digest_log):
    logger, parse = digest_log
    od.build_and_send_digest(logger=logger)

    runs = parse()
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    # The tool activity landed inside the run rather than being orphaned.
    assert {c["name"] for c in runs[0]["tool_calls"]} >= {"poll_edgar", "send_email"}


def test_nothing_new_run_closes_instead_of_hanging(stubbed_run, digest_log, monkeypatch):
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    logger, parse = digest_log
    od.build_and_send_digest(logger=logger)

    runs = parse()
    assert len(runs) == 1
    # Not "running": the early return needs its own completion line.
    assert runs[0]["status"] == "success"


def test_send_failure_parses_as_a_failed_run(stubbed_run, digest_log, monkeypatch):
    monkeypatch.setattr(od, "send_email",
                        lambda subject, body, html=False: {"error": "gmail 503"})
    logger, parse = digest_log
    od.build_and_send_digest(logger=logger)

    runs = parse()
    assert len(runs) == 1
    assert runs[0]["status"] == "failure"
    assert "gmail 503" in runs[0]["error"]


def test_unexpected_exception_parses_as_a_failed_run(stubbed_run, digest_log, monkeypatch):
    monkeypatch.setattr(od, "poll_edgar", lambda *a: 1 / 0)
    logger, parse = digest_log
    od.build_and_send_digest(logger=logger)

    runs = parse()
    assert len(runs) == 1
    assert runs[0]["status"] == "failure"


def test_unparseable_scoring_output_still_sends(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "complete_text", lambda **k: "I cannot score these, sorry!")
    assert od.main() == 0
    assert len(stubbed_run["emails"]) == 1     # digest goes out unscored
    assert stubbed_run["pushes"] == []          # nothing crossed the threshold


def test_stalled_opening_reported_exactly_once(stubbed_run, monkeypatch):
    posted = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    posting = {"id": "greenhouse:acme:1", "source": "ats", "signal": "hiring",
               "company": "Acme", "title": "VP of Engineering",
               "url": "https://boards.greenhouse.io/acme/1", "posted_at": posted}
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    monkeypatch.setattr(od, "poll_ats", lambda wl: {"items": [posting], "errors": []})
    monkeypatch.setattr(od, "complete_text", lambda **k: "1|7|Offer to bridge.")

    assert od.main() == 0
    body = stubbed_run["emails"][0][1]
    # Already past the 45-day default when first seen: reported as stalled.
    assert "Stalled Searches" in body and "Acme" in body

    # The board still lists it on later runs — but it never re-alerts.
    assert od.main() == 0
    assert od.main() == 0
    assert len(stubbed_run["emails"]) == 1


def test_stalled_flip_rescores_under_the_new_signal(stubbed_run, monkeypatch):
    """A posting digested as 'hiring' crosses the stalled threshold later: it
    re-enters the digest and gets a FRESH score/angle — the hiring-era ones
    are shed on flip, since the stall is the stronger signal."""
    def posting(days_old):
        return {"id": "greenhouse:acme:1", "source": "ats", "signal": "hiring",
                "company": "Acme", "title": "VP of Engineering",
                "url": "https://boards.greenhouse.io/acme/1",
                "posted_at": (datetime.now() - timedelta(days=days_old))
                             .isoformat(timespec="seconds")}

    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    monkeypatch.setattr(od, "poll_ats",
                        lambda wl: {"items": [posting(10)], "errors": []})
    monkeypatch.setattr(od, "complete_text",
                        lambda **k: "1|4|Just posted, wait.")
    assert od.main() == 0
    assert "4/10" in stubbed_run["emails"][0][1]

    # 50 days later the board still lists the same posting.
    monkeypatch.setattr(od, "poll_ats",
                        lambda wl: {"items": [posting(60)], "errors": []})
    monkeypatch.setattr(od, "complete_text",
                        lambda **k: "1|9|Seat's been open 60 days — offer to bridge.")
    assert od.main() == 0
    body = stubbed_run["emails"][1][1]
    assert "Stalled Searches" in body and "9/10" in body and "bridge" in body


def test_digest_html_escapes_and_guards_urls(stubbed_run, monkeypatch):
    hostile = {"id": "hn:x", "source": "hn", "signal": "hiring",
               "company": "<script>alert(1)</script>",
               "title": "Head of Product <b>now</b>",
               "url": "javascript:alert(1)", "posted_at": None}
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    monkeypatch.setattr(od, "poll_hn", lambda: {"items": [hostile]})
    monkeypatch.setattr(od, "complete_text", lambda **k: "hn:x|3|Skip.")
    assert od.main() == 0
    body = stubbed_run["emails"][0][1]
    assert "<script>" not in body
    assert "javascript:" not in body


def test_digest_footer_links_to_triage_page(stubbed_run, monkeypatch):
    monkeypatch.setenv("WREN_PUBLIC_URL", "https://mini.ts.net/")
    assert od.main() == 0
    assert 'https://mini.ts.net/opportunities' in stubbed_run["emails"][0][1]


def test_digest_footer_absent_without_public_url(stubbed_run, monkeypatch):
    monkeypatch.delenv("WREN_PUBLIC_URL", raising=False)
    assert od.main() == 0
    assert "/opportunities" not in stubbed_run["emails"][0][1]


# --------------------------------------------------------------------------- #
# poll_hn — headline-only role matching
# --------------------------------------------------------------------------- #

def _hn_resp(payload):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return payload
    return _R()


def test_poll_hn_matches_headline_not_body(monkeypatch):
    story = {"hits": [{"objectID": "999", "title": "Ask HN: Who is hiring? (July 2026)"}]}
    comments = {"hits": [
        # Leadership role in the headline: kept.
        {"objectID": "1", "parent_id": 999,
         "comment_text": "Acme | Head of Product | Remote<p>Come build with us."},
        # IC role whose body name-drops the CTO: dropped.
        {"objectID": "2", "parent_id": 999,
         "comment_text": "Beta | Senior QA Engineer | NYC<p>You will report to our CTO."},
        # IC headline but the body offers fractional work: kept.
        {"objectID": "3", "parent_id": 999,
         "comment_text": "Gamma | Staff Engineer | Remote<p>Open to fractional arrangements."},
        # Reply chatter, not a top-level post: dropped regardless of content.
        {"objectID": "4", "parent_id": 111, "comment_text": "our cto agrees"},
        # "cto" must match as a word — not inside Yocto/factory/director.
        {"objectID": "5", "parent_id": 999,
         "comment_text": "Delta | Embedded Engineer (Yocto, C++) | Hybrid<p>Factory tooling."},
        # An engineering-director opening IS leadership: kept.
        {"objectID": "6", "parent_id": 999,
         "comment_text": "Echo | Engineering Director | Stockholm<p>Scale three teams."},
    ]}
    responses = iter([_hn_resp(story), _hn_resp(comments)])
    monkeypatch.setattr(od.requests, "get", lambda *a, **k: next(responses))
    items = od.poll_hn()["items"]
    assert [i["id"] for i in items] == ["hn:1", "hn:3", "hn:6"]
    assert items[0]["company"] == "Acme"


# --------------------------------------------------------------------------- #
# _ats_board_jobs — iCIMS sitemap parsing
# --------------------------------------------------------------------------- #

_ICIMS_SITEMAP = b"""<?xml version='1.0' encoding='utf-8'?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://corporatecareers-acme.icims.com/jobs/intro</loc></url>
  <url><loc>https://corporatecareers-acme.icims.com/jobs/20089/director%2c-engineering-technology/job</loc>
    <lastmod>2026-07-10T11:56:56-04:00</lastmod></url>
  <url><loc>https://corporatecareers-acme.icims.com/jobs/14941/senior-android-engineer/job</loc></url>
</urlset>"""


class _SitemapResp:
    content = _ICIMS_SITEMAP

    def raise_for_status(self):
        pass


def test_icims_board_parses_sitemap_and_filters_titles(monkeypatch):
    monkeypatch.setattr(od.requests, "get", lambda *a, **k: _SitemapResp())
    entry = {"ats": "icims", "slug": "corporatecareers-acme", "company": "Acme"}
    jobs = od._ats_board_jobs(entry)
    assert len(jobs) == 1  # intro page and non-leadership title dropped
    job = jobs[0]
    assert job["id"] == "icims:corporatecareers-acme:20089"
    assert job["title"] == "director, engineering technology"  # slug decoded
    assert job["url"].endswith("/jobs/20089/director%2c-engineering-technology/job")
    # lastmod moves on any edit, so it is deliberately NOT used as posted_at:
    # the stalled clock runs from first_seen instead.
    assert job["posted_at"] is None
    assert job["signal"] == "hiring" and job["company"] == "Acme"


def test_icims_malformed_sitemap_degrades_per_board(monkeypatch):
    class _Garbage:
        content = b"not xml at all"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(od.requests, "get", lambda *a, **k: _Garbage())
    result = od.poll_ats([{"ats": "icims", "slug": "x", "company": "X"}])
    assert result["items"] == []
    assert len(result["errors"]) == 1


# --------------------------------------------------------------------------- #
# poll_edgar — fund-name filtering and serial-filer collapsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "AQR Flex 1 Series LLC - Series 2026-07A",   # series LLC paperwork
    "NHIT: STRATEGIC ALPHA TRUST", "Mutual Bancorp", "Oak Hill Funds LLC",
    "Granite Capital Partners", "Seaport Acquisition Corp SPV",
    "GPE-G Co-Investment Limited Partnership",
])
def test_fund_names_are_filtered(name):
    assert od._FUND_NAME_RE.search(name)


@pytest.mark.parametrize("name", [
    "LinqAlpha, Inc.", "Corvus Robotics", "Seacoast Analytics Inc",
])
def test_operating_company_names_pass(name):
    assert not od._FUND_NAME_RE.search(name)


def _edgar_hit(adsh, name, cik, date="2026-07-10"):
    return {"_source": {"adsh": adsh, "display_names": [f"{name}  (CIK {cik})"],
                        "ciks": [cik], "file_date": date,
                        "biz_locations": ["Boston, MA"]}}


class _EdgarResp:
    def __init__(self, hits, total=None):
        self._hits = hits
        # None omits the key entirely — EDGAR always sends it, but poll_edgar
        # must not assume so.
        self._total = total

    def raise_for_status(self):
        pass

    def json(self):
        hits = {"hits": self._hits}
        if self._total is not None:
            hits["total"] = {"value": self._total, "relation": "eq"}
        return {"hits": hits}


def _page_of(n, date, start_cik):
    """n distinct operating-company hits, all filed on `date`."""
    return [_edgar_hit(f"0001-26-{start_cik + i:06d}", f"Startup {start_cik + i}",
                       f"{start_cik + i:010d}", date)
            for i in range(n)]


# Real EDGAR pages at 100; shrink it so fixtures stay readable.
@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(od, "_EDGAR_PAGE_SIZE", 10)


def test_poll_edgar_strides_by_a_full_page(small_pages, monkeypatch):
    """`from` must advance by the page size. Striding by less re-fetches hits
    already seen (the old stride of 10 against 100-hit pages pulled each hit
    five times) and caps total reach far below the page budget."""
    froms = []

    def fake_get(url, params=None, **k):
        froms.append(params["from"])
        page = params["from"] // 10
        # 2 full pages then a short one: 24 hits total.
        if page < 2:
            return _EdgarResp(_page_of(10, "2026-07-16", 100 + page * 10), total=24)
        return _EdgarResp(_page_of(4, "2026-07-15", 200), total=24)

    monkeypatch.setattr(od.requests, "get", fake_get)

    result = od.poll_edgar("2026-07-09", "2026-07-16", ["MA"])
    assert froms == [0, 10, 20]        # not [0, 1, 2] and not [0, 10, 20, 30...]
    assert len(result["items"]) == 24  # every hit reached, none double-counted
    assert "oldest_fetched" not in result  # total exhausted: nothing missed


def test_poll_edgar_flags_truncation_with_the_oldest_date_it_reached(
        small_pages, monkeypatch):
    """Hitting the safety cap must report where coverage actually ends —
    EDGAR is newest-first, so the unseen filings are the OLDEST ones."""
    monkeypatch.setattr(od, "_EDGAR_MAX_PAGES", 2)

    def fake_get(url, params=None, **k):
        date = "2026-07-16" if params["from"] == 0 else "2026-07-15"
        return _EdgarResp(_page_of(10, date, 100 + params["from"]), total=500)

    monkeypatch.setattr(od.requests, "get", fake_get)

    result = od.poll_edgar("2026-07-09", "2026-07-16", ["MA"])
    # Reached back to the 15th only — NOT start_date, and not run_day.
    assert result["oldest_fetched"] == "2026-07-15"


def test_poll_edgar_truncation_clamp_ignores_untruncated_states(
        small_pages, monkeypatch):
    """The clamp must come only from states that truncated. A fully-paged
    state has already reached start_date, so folding its oldest date in would
    collapse the clamp to start_date and the window would never advance."""
    monkeypatch.setattr(od, "_EDGAR_MAX_PAGES", 2)

    def fake_get(url, params=None, **k):
        if params["locationCodes"] == "MA":       # truncates at the 2-page cap
            date = "2026-07-16" if params["from"] == 0 else "2026-07-15"
            return _EdgarResp(_page_of(10, date, 100 + params["from"]), total=500)
        # ME: one short page, fully exhausted, dated back at start_date.
        return _EdgarResp(_page_of(2, "2026-07-09", 900), total=2)

    monkeypatch.setattr(od.requests, "get", fake_get)

    result = od.poll_edgar("2026-07-09", "2026-07-16", ["MA", "ME"])
    # ME's older 07-09 is ignored: it truncated nothing.
    assert result["oldest_fetched"] == "2026-07-15"


def test_poll_edgar_collapses_same_day_filings_per_filer(monkeypatch):
    hits = [
        _edgar_hit("0001-26-000001", "Acme Robotics Inc", "0000000111"),
        _edgar_hit("0001-26-000002", "Acme Robotics Inc", "0000000111"),
        _edgar_hit("0001-26-000003", "Acme Robotics Inc", "0000000111"),
        _edgar_hit("0002-26-000001", "Beta Labs", "0000000222"),
        _edgar_hit("0003-26-000001", "AQR Flex 1 Series LLC - Series X", "0000000333"),
    ]
    monkeypatch.setattr(od.requests, "get", lambda *a, **k: _EdgarResp(hits))
    result = od.poll_edgar("2026-07-08", "2026-07-11", ["MA"])
    by_company = {i["company"]: i for i in result["items"]}
    # The series-LLC filer is filtered outright; the triple filer collapses.
    assert set(by_company) == {"Acme Robotics Inc", "Beta Labs"}
    acme = by_company["Acme Robotics Inc"]
    assert acme["id"] == "edgar:111:2026-07-10"      # stable filer+day id
    assert acme["title"].endswith("(3 filings)")
    assert "filings" not in acme                      # bookkeeping key stripped
    assert "filings" not in by_company["Beta Labs"]["title"]
