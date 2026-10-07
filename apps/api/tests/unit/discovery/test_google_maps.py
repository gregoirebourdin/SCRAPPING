"""Google Maps adapter: gosom job lifecycle (respx) and CSV parsing."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scout.discovery.base import DiscoveryQuery
from scout.discovery.google_maps import GoogleMapsSource, parse_csv
from scout.errors import FetchError

from .conftest import defn, fixture_text

BASE = "http://maps-scraper.railway.internal:8080"


@pytest.fixture
def configured(settings_env):
    return settings_env(GMAPS_SCRAPER_URL=BASE)


@pytest.fixture
def src() -> GoogleMapsSource:
    return GoogleMapsSource(poll_budget_s=0, max_time_s=240)


def query() -> DiscoveryQuery:
    return DiscoveryQuery(
        key="maps:dentist:FR:Lyon",
        params={
            "keywords": ["dentiste Lyon", "cabinet dentaire Lyon"],
            "lang": "fr",
            "city": "Lyon",
            "country": "FR",
            "depth": 1,
        },
    )


def test_parse_csv_rows() -> None:
    cands = parse_csv(fixture_text("gmaps_results.csv"), query=query())
    assert [c.name for c in cands] == [
        "Cabinet Dentaire Bellecour",
        "Dr Sophie Martin - Chirurgien-dentiste",
        "Centre Dentaire Part-Dieu",
    ]  # row without title skipped
    a, b, c = cands
    assert a.source == "google_maps" and a.source_entity_id == "ChIJLRybj1rq9EcRgXBv5dk8Kxo"
    assert a.website == "https://dentaire-bellecour.fr/" and a.phone == "+33 4 78 00 00 01"
    assert a.location == {
        "country": "FR",
        "city": "Lyon",
        "postal_code": "69002",
        "address": "12 Pl. Bellecour, 69002 Lyon, France",
        "lat": 45.7578,
        "lng": 4.832,
    }
    assert a.category == "Dentiste" and a.status == "active"
    assert a.raw_data["rating"] == 4.7 and a.raw_data["review_count"] == 214
    assert "user_reviews" not in a.raw_data  # reviewer data never kept
    assert a.source_url.startswith("https://www.google.com/maps/place/")
    assert b.website is None and b.raw_data["website_raw"] == "https://www.facebook.com/drsophiemartin"
    assert b.source_entity_id == "ChIJ00000000000000000002"
    assert c.status == "closed" and c.source_entity_id == "0x47f4ea:0x33"  # data_id fallback
    assert c.emails == ["contact@centredentaire-partdieu.com", "rdv@centredentaire-partdieu.com"]


def test_configuration_and_suitability(settings_env) -> None:
    src = GoogleMapsSource()
    assert not src.is_configured()
    settings_env(GMAPS_SCRAPER_URL=BASE)
    assert src.is_configured()
    assert src.suitability(defn(industries=["dentistes"], cities=["Lyon"])) == 1.2
    # agencies in a named city: Maps is the best source there too (website, phone, exact address)
    assert (
        src.suitability(defn(industries=["social media agency"], cities=["Miami"], countries=["US"])) == 1.2
    )
    assert src.suitability(defn(industries=["SaaS"], countries=["US"])) < 0.5
    assert src.suitability(defn(industries=["dentist"])) == 0  # nowhere to search


def test_plan_queries_by_city_and_language() -> None:
    plan = GoogleMapsSource().plan(defn(industries=["dentistes"], cities=["Lyon"]))
    assert len(plan) == 1
    q = plan[0]
    assert q.key == "maps:dentist:FR:Lyon"
    assert q.params["keywords"] == ["dentiste Lyon", "cabinet dentaire Lyon"]
    assert q.params["lang"] == "fr" and q.params["depth"] == 1
    wider = GoogleMapsSource().plan(defn(industries=["dentistes"], cities=["Lyon"]), expansion=1)
    assert len(wider) > 1 and {"maps:dentist:FR:Lyon"} <= {x.key for x in wider}
    assert all(x.params["depth"] == 2 for x in wider)
    de = GoogleMapsSource().plan(defn(industries=["dentist"], countries=["DE"]))
    assert de[0].params["keywords"][0] == "Zahnarzt Berlin" and de[0].params["lang"] == "de"


@respx.mock
async def test_job_lifecycle(configured, src: GoogleMapsSource) -> None:
    create = respx.post(f"{BASE}/api/v1/jobs").mock(return_value=httpx.Response(201, json={"id": "job-1"}))
    status = respx.get(f"{BASE}/api/v1/jobs/job-1").mock(
        return_value=httpx.Response(200, json={"ID": "job-1", "Name": "x", "Status": "working", "Data": {}})
    )
    # 1st call: job created, still running → empty page, cursor carries the job id.
    page = await src.discover(query(), None)
    assert page.candidates == [] and page.next_cursor["job_id"] == "job-1"
    body = json.loads(create.calls.last.request.content)
    assert body["keywords"] == ["dentiste Lyon", "cabinet dentaire Lyon"] and body["lang"] == "fr"
    assert body["depth"] == 1 and body["max_time"] == 240 and body["email"] is False
    assert body["Name"].startswith("scout ") and body["fast_mode"] is False and body["proxies"] == []

    # 2nd call: done → download CSV, delete job, query exhausted.
    status.mock(return_value=httpx.Response(200, json={"ID": "job-1", "Status": "ok"}))
    respx.get(f"{BASE}/api/v1/jobs/job-1/download").mock(
        return_value=httpx.Response(
            200, text=fixture_text("gmaps_results.csv"), headers={"content-type": "text/csv"}
        )
    )
    delete = respx.delete(f"{BASE}/api/v1/jobs/job-1").mock(return_value=httpx.Response(200))
    done = await src.discover(query(), page.next_cursor)
    assert len(done.candidates) == 3 and done.next_cursor is None
    assert delete.called and create.call_count == 1


@respx.mock
async def test_failed_job_is_cleaned_up_and_retryable(configured, src: GoogleMapsSource) -> None:
    respx.get(f"{BASE}/api/v1/jobs/job-2").mock(return_value=httpx.Response(200, json={"Status": "failed"}))
    delete = respx.delete(f"{BASE}/api/v1/jobs/job-2").mock(return_value=httpx.Response(200))
    with pytest.raises(FetchError):
        await src.discover(query(), {"job_id": "job-2", "created_at": 0})
    assert delete.called


@respx.mock
async def test_vanished_job_is_recreated(configured, src: GoogleMapsSource) -> None:
    respx.get(f"{BASE}/api/v1/jobs/gone").mock(return_value=httpx.Response(404, json={"code": 404}))
    respx.post(f"{BASE}/api/v1/jobs").mock(return_value=httpx.Response(201, json={"id": "job-3"}))
    page = await src.discover(query(), {"job_id": "gone"})
    assert page.candidates == [] and page.next_cursor["job_id"] == "job-3"


@respx.mock
async def test_stuck_job_raises(configured, src: GoogleMapsSource) -> None:
    respx.get(f"{BASE}/api/v1/jobs/old").mock(return_value=httpx.Response(200, json={"Status": "pending"}))
    respx.delete(f"{BASE}/api/v1/jobs/old").mock(return_value=httpx.Response(200))
    with pytest.raises(FetchError, match="stuck"):
        await src.discover(query(), {"job_id": "old", "created_at": 1.0})


@respx.mock
async def test_bounded_polling_until_ok(configured) -> None:
    src = GoogleMapsSource(poll_budget_s=5)
    respx.post(f"{BASE}/api/v1/jobs").mock(return_value=httpx.Response(201, json={"id": "j"}))
    respx.get(f"{BASE}/api/v1/jobs/j").mock(
        side_effect=[
            httpx.Response(200, json={"Status": "pending"}),
            httpx.Response(200, json={"Status": "ok"}),
        ]
    )
    respx.get(f"{BASE}/api/v1/jobs/j/download").mock(
        return_value=httpx.Response(404, text="csv file not found")
    )
    respx.delete(f"{BASE}/api/v1/jobs/j").mock(return_value=httpx.Response(200))
    sleeps: list[float] = []

    async def no_sleep(s: float) -> None:
        sleeps.append(s)

    src.sleep = no_sleep
    page = await src.discover(query(), None)
    assert sleeps == [2.0]
    assert page.candidates == [] and page.next_cursor is None  # finished with no CSV → exhausted
