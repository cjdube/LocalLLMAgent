"""Tests for agent/tools/research.py — the Form D XML parse, the degrade
contracts (one dead search never fails the brief; everything dead does, with
the failure recorded on the item; an exception mid-pipeline is caught), and
that the brief lands on the opportunity. Store isolated to tmp_path; search,
model, and HTTP are monkeypatched — no network."""

import pytest
import requests

from agent.tools import opportunities as opp
from agent.tools import research as rs


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(opp, "_STORE_PATH", tmp_path / "opportunities.json")


@pytest.fixture
def seeded_item():
    return opp.insert_new_items([{
        "id": "hn:1", "source": "hn", "signal": "hiring", "company": "TinyCo",
        "title": "TinyCo | Head of Product", "url": "https://example.com",
        "posted_at": None,
    }])[0]["id"]


def _search_stub(results=("TinyCo builds tiny things",), answer=None):
    def stub(query, **kwargs):
        out = {"results": [{"title": "hit", "url": "https://x", "content": c}
                           for c in results]}
        if answer:
            out["answer"] = answer
        return out
    return stub


@pytest.fixture
def stubbed_pipeline(monkeypatch):
    seen = {"prompts": []}
    monkeypatch.setattr(rs, "search_web", _search_stub())
    monkeypatch.setattr(rs, "complete_text",
                        lambda **k: seen["prompts"].append(k) or "What they do: tiny things.")
    return seen


# --------------------------------------------------------------------------- #
# research_company contracts
# --------------------------------------------------------------------------- #

def test_happy_path_stores_brief_on_the_item(seeded_item, stubbed_pipeline):
    result = rs.research_opportunity(seeded_item)
    assert result["summary"] == "What they do: tiny things."
    research = opp.get_item(seeded_item)["research"]
    assert research["status"] == "done"
    assert research["summary"] == "What they do: tiny things."
    # The model saw the search snippets and the signal context.
    prompt = stubbed_pipeline["prompts"][0]["user_prompt"]
    assert "TinyCo" in prompt and "tiny things" in prompt and "hiring" in prompt


def test_unknown_id_errors_without_searching(stubbed_pipeline):
    result = rs.research_opportunity("missing")
    assert "error" in result
    assert stubbed_pipeline["prompts"] == []


def test_one_dead_search_still_produces_a_brief(seeded_item, stubbed_pipeline, monkeypatch):
    calls = iter([{"error": "tavily 503"}, _search_stub()("q"), _search_stub()("q")])
    monkeypatch.setattr(rs, "search_web", lambda *a, **k: next(calls))
    result = rs.research_opportunity(seeded_item)
    assert "summary" in result
    assert opp.get_item(seeded_item)["research"]["status"] == "done"


def test_everything_dead_marks_the_item_failed(seeded_item, stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(rs, "search_web", lambda *a, **k: {"error": "tavily down"})
    result = rs.research_opportunity(seeded_item)
    assert "error" in result and "tavily down" in result["error"]
    assert opp.get_item(seeded_item)["research"]["status"] == "failed"
    assert stubbed_pipeline["prompts"] == []  # no model call on empty input


def test_model_exception_is_caught_and_marked_failed(seeded_item, stubbed_pipeline, monkeypatch):
    def boom(**k):
        raise RuntimeError("ollama not running")
    monkeypatch.setattr(rs, "complete_text", boom)
    result = rs.research_opportunity(seeded_item)
    assert "error" in result and "ollama" in result["error"]
    assert opp.get_item(seeded_item)["research"]["status"] == "failed"


# --------------------------------------------------------------------------- #
# form_d_facts — deterministic parse of the filing XML
# --------------------------------------------------------------------------- #

_FORM_D_XML = b"""<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/formd">
  <primaryIssuer><entityName>Acme Inc</entityName></primaryIssuer>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName><firstName>Jane</firstName><lastName>Doe</lastName></relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Executive Officer</relationship>
        <relationship>Director</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup><industryGroupType>Other Technology</industryGroupType></industryGroup>
    <issuerSize><revenueRange>$1 - $1,000,000</revenueRange></issuerSize>
    <typeOfFiling><dateOfFirstSale><value>2026-06-15</value></dateOfFirstSale></typeOfFiling>
    <offeringSalesAmounts>
      <totalOfferingAmount>6000000</totalOfferingAmount>
      <totalAmountSold>4000000</totalAmountSold>
    </offeringSalesAmounts>
  </offeringData>
</edgarSubmission>"""


def _edgar_item(url="https://www.sec.gov/Archives/edgar/data/1/000100/0001-26-000001-index.htm"):
    return {"source": "edgar", "url": url}


class _FakeResp:
    def __init__(self, content=_FORM_D_XML):
        self.content = content

    def raise_for_status(self):
        pass


def test_form_d_facts_parses_the_filing(monkeypatch):
    monkeypatch.setattr(rs.requests, "get", lambda url, **k: _FakeResp())
    facts = rs.form_d_facts(_edgar_item())
    assert facts["officers"] == ["Jane Doe (Executive Officer, Director)"]
    assert facts["total_offering_amount"] == "6000000"
    assert facts["total_amount_sold"] == "4000000"
    assert facts["revenue_range"] == "$1 - $1,000,000"
    assert facts["industry"] == "Other Technology"
    assert facts["date_of_first_sale"] == "2026-06-15"


def test_form_d_facts_skips_non_edgar_and_degrades(monkeypatch):
    assert rs.form_d_facts({"source": "hn", "url": "https://x"}) is None
    assert rs.form_d_facts(_edgar_item(url="https://weird/shape")) is None

    def boom(url, **k):
        raise requests.exceptions.ConnectionError("edgar down")
    monkeypatch.setattr(rs.requests, "get", boom)
    assert rs.form_d_facts(_edgar_item()) is None

    monkeypatch.setattr(rs.requests, "get", lambda url, **k: _FakeResp(b"not xml"))
    assert rs.form_d_facts(_edgar_item()) is None


# --------------------------------------------------------------------------- #
# research_company — the general-purpose entry point (any name, no store)
# --------------------------------------------------------------------------- #

def test_research_company_works_on_any_name_without_the_store(stubbed_pipeline):
    result = rs.research_company("Corvus Robotics")
    assert result["summary"] == "What they do: tiny things."
    assert result["company"] == "Corvus Robotics"
    # Nothing persisted — this entry point is store-free.
    assert opp.all_items() == []
    prompt = stubbed_pipeline["prompts"][0]["user_prompt"]
    assert "general research request" in prompt


def test_research_company_rejects_empty_name(stubbed_pipeline):
    assert "error" in rs.research_company("   ")
    assert stubbed_pipeline["prompts"] == []


def test_research_company_reports_dead_sources(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(rs, "search_web", lambda *a, **k: {"error": "tavily down"})
    result = rs.research_company("Corvus Robotics")
    assert "error" in result and "tavily down" in result["error"]
