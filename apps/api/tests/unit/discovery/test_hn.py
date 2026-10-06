"""HN "Who is hiring?" adapter on captured Algolia responses (October 2026 thread)."""

from __future__ import annotations

import httpx
import respx

from scout.discovery.base import DiscoveryQuery
from scout.discovery.hn import HNHiringSource, parse_comment

from .conftest import defn, fixture_json

ALGOLIA = "https://hn.algolia.com/api/v1"


def test_parse_comment_first_line() -> None:
    info = parse_comment(
        'Close | Backend Engineers, Sr. Software Engineer (Marketing) | REMOTE (US) \n'
        '<a href="https:&#x2F;&#x2F;close.com&#x2F;careers" rel="nofollow">https:&#x2F;&#x2F;close.com&#x2F;careers</a>'
        "<p>We build a CRM.</p>"
    )
    assert info is not None
    assert info["company"] == "Close" and info["url"] == "https://close.com/careers"
    assert info["location"] == "REMOTE (US)" and "Backend Engineers, Sr. Software Engineer (Marketing)" in info["roles"]
    assert "We build a CRM." in info["text"]
    assert parse_comment("They are a big waste of time.") is None
    job_board_only = parse_comment('Acme | SWE | Remote | <a href="https://jobs.lever.co/acme">apply</a>')
    assert job_board_only is not None and job_board_only["url"] is None


def test_suitability_tech_only() -> None:
    src = HNHiringSource()
    assert src.suitability(defn(industries=["SaaS startups"], countries=["US"])) == 0.6
    assert src.suitability(defn(industries=["dentistes"], cities=["Lyon"])) == 0
    assert src.suitability(defn(industries=["marketing agency"])) == 0
    plan = src.plan(defn(industries=["AI startups"], countries=["US"]))
    assert [q.params["months_back"] for q in plan] == [0, 1]
    assert len(src.plan(defn(industries=["AI startups"]), expansion=1)) == 4


@respx.mock
async def test_discover_latest_thread_with_location_filter() -> None:
    stories = respx.get(f"{ALGOLIA}/search_by_date").mock(
        return_value=httpx.Response(200, json=fixture_json("hn_stories.json"))
    )
    comments = respx.get(f"{ALGOLIA}/search").mock(return_value=httpx.Response(200, json=fixture_json("hn_comments.json")))
    src = HNHiringSource()
    q = DiscoveryQuery(key="hn:who_is_hiring:0", params={"countries": ["US"], "cities": [], "months_back": 0})
    page = await src.discover(q, None)
    assert comments.calls.last.request.url.params["tags"] == "comment,story_49922569"  # latest "Who is hiring?"
    names = [c.name for c in page.candidates]
    assert names == ["SentiLink", "Mechanize", "Close", "Sphinx Defense", "Tenki Cloud by Luxor Tech", "InfoHawk"]
    c = page.candidates[0]
    assert c.source == "hn_hiring" and c.domain == "sentilink.com" and c.website == "https://sentilink.com/"
    assert c.source_url == "https://news.ycombinator.com/item?id=49972097"
    assert c.raw_data["signal"] == "hiring" and c.raw_data["story_title"] == "Ask HN: Who is hiring? (October 2026)"
    assert c.raw_data["comment_text"] and c.raw_data["posted_at"]
    assert page.next_cursor is None  # nbPages = 1 in the fixture

    anywhere = await src.discover(DiscoveryQuery(key="hn", params={"months_back": 0}), None)
    assert len(anywhere.candidates) > len(page.candidates)
    assert "G-Research" in {c.name for c in anywhere.candidates}
    assert stories.call_count == 1  # thread list cached per adapter instance
