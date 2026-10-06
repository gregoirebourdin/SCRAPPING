"""Domain Intelligence Profiles end to end (Postgres; DNS faked, GitHub/RDAP mocked with respx)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import dns.name
import dns.resolver
import httpx
import pytest
import respx
import sqlalchemy as sa

from scout.config import get_settings
from scout.db.engine import session_scope
from scout.db.enums import EmailEvidenceSource as Src
from scout.db.enums import MailProvider
from scout.db.models import (
    Company,
    DomainDnsCache,
    DomainEmailPattern,
    DomainEmailSample,
    DomainProfile,
    Person,
    WebsitePage,
)
from scout.discovery.common import Throttle
from scout.email import dns as edns
from scout.email import store
from scout.email.contracts import ObservedEmail
from scout.email.intel import (
    flush_stats,
    get_domain_intel,
    load_observed,
    profile_invalidated_at,
    rdap,
    record_observed_emails,
    record_pattern_outcome,
    relearn_domain_patterns,
    state,
    update_smtp_facts,
)
from scout.email.intel import github as gh

pytestmark = pytest.mark.integration

DOMAIN = "acme.fr"


# ---- fakes -------------------------------------------------------------------------------------


class FakeResolver:
    """dnspython-like resolver: MX answers per domain, counts MX queries."""

    def __init__(self, mx: dict[str, list[str]]):
        self.mx = mx
        self.calls: list[tuple[str, str]] = []

    def mx_calls(self, domain: str) -> int:
        return sum(1 for q, t in self.calls if q == domain and t == "MX")

    async def resolve(self, qname, rdtype):
        self.calls.append((qname, rdtype))
        await asyncio.sleep(0.01)  # give concurrent callers a chance to race
        if rdtype == "MX":
            hosts = self.mx.get(qname)
            if hosts is None:
                raise dns.resolver.NXDOMAIN()
            return [
                SimpleNamespace(preference=10 * (i + 1), exchange=dns.name.from_text(h + "."))
                for i, h in enumerate(hosts)
            ]
        if rdtype == "A":
            return ["192.0.2.1"]
        raise dns.resolver.NoAnswer()


@pytest.fixture
def resolver(monkeypatch):
    r = FakeResolver(
        {
            DOMAIN: ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
            "gmail.com": ["gmail-smtp-in.l.google.com"],
        }
    )
    monkeypatch.setattr(edns, "resolver_factory", lambda: r)
    return r


@pytest.fixture(autouse=True)
def _intel_state(monkeypatch):
    state.reset()
    gh.reset_budget()
    monkeypatch.setattr(rdap, "_throttle", Throttle(0))
    yield
    state.set_network_evidence(None)
    state.reset()
    gh.reset_budget()


def api() -> str:
    return get_settings().github_api_url.rstrip("/")


def commit(sha, name, email, date="2026-08-01T10:00:00Z"):
    return {
        "sha": sha,
        "html_url": f"https://github.com/acme/api/commit/{sha}",
        "commit": {"author": {"name": name, "email": email, "date": date}, "committer": None},
    }


RDAP_ACME = {
    "objectClassName": "domain",
    "ldhName": DOMAIN,
    "entities": [
        {
            "roles": ["registrant"],
            "vcardArray": [
                "vcard",
                [
                    ["fn", {}, "text", "ACME SAS"],
                    ["kind", {}, "text", "org"],
                    ["org", {}, "text", "ACME SAS"],
                ],
            ],
        },
        {"roles": ["technical"], "vcardArray": ["vcard", [["email", {}, "text", "hostmaster@acme.fr"]]]},
    ],
}


def mock_remote(mock: respx.MockRouter, *, commits=None):
    """GitHub org `acme` (one repo) + RDAP for acme.fr. Returns the routes."""
    repos = mock.get(f"{api()}/users/acme/repos").mock(
        return_value=httpx.Response(
            200, json=[{"full_name": "acme/api", "pushed_at": "2026-09-01T00:00:00Z", "size": 9}]
        )
    )
    commits_route = mock.get(f"{api()}/repos/acme/api/commits").mock(
        return_value=httpx.Response(
            200,
            json=commits
            if commits is not None
            else [
                commit("c1", "Jean Martin", "jean.martin@acme.fr"),
                commit("c2", "Paul Durand", "paul.durand@acme.fr"),
                commit("c3", "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"),
            ],
        )
    )
    rdap_route = mock.get(f"https://rdap.org/domain/{DOMAIN}").mock(
        return_value=httpx.Response(200, json=RDAP_ACME)
    )
    return SimpleNamespace(repos=repos, commits=commits_route, rdap=rdap_route)


async def make_company(ws, domain=DOMAIN, *, employee_max=40, country="FR"):
    async with session_scope() as s:
        c = Company(
            workspace_id=ws,
            name="Acme",
            normalized_name="acme",
            domain=domain,
            normalized_domain=domain,
            employee_max=employee_max,
            country=country,
        )
        s.add(c)
        await s.flush()
        return c.id


async def make_person(ws, company_id, first, last):
    async with session_scope() as s:
        p = Person(
            workspace_id=ws,
            company_id=company_id,
            first_name=first,
            last_name=last,
            full_name=f"{first} {last}",
            normalized_name=f"{first} {last}".lower(),
        )
        s.add(p)
        await s.flush()
        return p.id


async def add_page(ws, company_id, url, emails, links=None):
    async with session_scope() as s:
        s.add(
            WebsitePage(
                workspace_id=ws,
                company_id=company_id,
                url=url,
                canonical_url=url,
                content_hash=url,
                emails=emails,
                links=links or {},
            )
        )


async def seed_acme(ws):
    cid = await make_company(ws)
    await make_person(ws, cid, "Marie", "Dupont")
    await make_person(ws, cid, "Léa", "Petit")
    await add_page(
        ws,
        cid,
        "https://acme.fr/equipe",
        ["marie.dupont@acme.fr", "lea.petit@acme.fr", "contact@acme.fr", "someone@gmail.com"],
        {"social": {}, "external": [{"url": "https://github.com/acme", "text": "GitHub"}], "internal": []},
    )
    return cid


async def profile_row(domain=DOMAIN) -> DomainProfile | None:
    async with session_scope() as s:
        return await s.get(DomainProfile, domain)


async def age(**columns: timedelta):
    """Move profile timestamps (and the DNS cache) into the past."""
    now = datetime.now(UTC)
    async with session_scope() as s:
        await s.execute(
            sa.update(DomainProfile)
            .where(DomainProfile.domain == DOMAIN)
            .values(**{k: now - v for k, v in columns.items()})
        )
        if "mx_checked_at" in columns:
            await s.execute(
                sa.update(DomainDnsCache)
                .where(DomainDnsCache.domain == DOMAIN)
                .values(checked_at=now - columns["mx_checked_at"])
            )


# ---- one build for many contacts ---------------------------------------------------------------


async def test_profile_built_once_for_concurrent_contacts(workspace, resolver):
    ws, _ = workspace
    cid = await seed_acme(ws)
    state.set_network_evidence(True)
    with respx.mock(assert_all_called=True) as mock:
        routes = mock_remote(mock)
        results = await asyncio.gather(
            *(get_domain_intel("ACME.fr", workspace_id=ws, company_id=cid) for _ in range(20))
        )
    assert resolver.mx_calls(DOMAIN) == 1
    assert routes.repos.call_count == 1 and routes.commits.call_count == 1 and routes.rdap.call_count == 1
    assert sum(1 for r in results if not r.cache_hits["profile"]) == 1  # one build, 19 cache hits

    intel = results[0]
    assert all(r.domain == DOMAIN and r.patterns == intel.patterns for r in results)
    assert intel.provider == MailProvider.google_workspace and intel.has_mx and intel.accepts_mail
    assert intel.mx_hosts == ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]
    assert intel.dominant is not None and intel.dominant.pattern == "{first}.{last}"
    assert intel.dominant.samples == 4 and intel.dominant.confidence > 0.9

    observed = {o.address: o for o in intel.observed}
    assert set(observed) == {
        "marie.dupont@acme.fr",
        "lea.petit@acme.fr",
        "contact@acme.fr",
        "jean.martin@acme.fr",
        "paul.durand@acme.fr",
        "hostmaster@acme.fr",
    }
    assert observed["marie.dupont@acme.fr"].source == Src.website
    assert observed["marie.dupont@acme.fr"].source_url == "https://acme.fr/equipe"
    assert observed["jean.martin@acme.fr"].source == Src.github  # pattern evidence only
    assert observed["contact@acme.fr"].is_role and observed["hostmaster@acme.fr"].source == Src.rdap
    assert intel.named_samples == 4

    signals = {e["signal"]: e for e in intel.evidence}
    assert signals["provider"]["value"] == "google_workspace"
    assert signals["rdap_registrant_org"]["value"] == "ACME SAS"
    assert signals["github_org"]["source_url"] == "https://github.com/acme"
    assert all("observed_at" in e and "source" in e for e in intel.evidence)

    assert await flush_stats() == 1
    row = await profile_row()
    assert row.stats["builds"] == 1 and row.stats["cache_hits"] == 19
    assert row.github_org == "acme" and row.github_checked_at and row.rdap_checked_at and row.mx_checked_at
    assert row.dominant_pattern == "{first}.{last}" and row.named_samples == 4
    assert row.stats["rdap"]["registrant_org"] == "ACME SAS" and row.stats["claims"] == {}


async def test_component_freshness_and_mx_change(workspace, resolver):
    ws, _ = workspace
    cid = await seed_acme(ws)
    state.set_network_evidence(True)
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_remote(mock)
        await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)

        # New process / TTL expired: every component is served from the profile row.
        state.reset()
        again = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
        assert again.cache_hits == {
            "mx": True,
            "website": True,
            "github": True,
            "search": False,  # not needed: the company's own pages already show named addresses
            "rdap": True,
            "patterns": True,
            "profile": False,
        }
        assert resolver.mx_calls(DOMAIN) == 1 and routes.repos.call_count == 1 and routes.rdap.call_count == 1

        # MX older than 7 days → re-resolved (and the MX moved to Microsoft 365); GitHub still fresh.
        await update_smtp_facts(DOMAIN, catch_all=False, catch_all_confidence=0.9, smtp_reachable=True)
        resolver.mx[DOMAIN] = ["acme-fr.mail.protection.outlook.com"]
        await age(mx_checked_at=timedelta(days=8))
        state.reset()
        moved = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
        assert resolver.mx_calls(DOMAIN) == 2 and not moved.cache_hits["mx"] and moved.cache_hits["github"]
        assert moved.provider == MailProvider.microsoft_365
        assert moved.catch_all is None and moved.smtp_reachable is None  # SMTP facts belonged to the old MX
        assert "MX_CHANGED" in {e["signal"] for e in moved.evidence}
        assert await profile_invalidated_at(DOMAIN) is not None

        # GitHub older than 30 days, RDAP older than 90 days → refreshed.
        await age(github_checked_at=timedelta(days=31), rdap_checked_at=timedelta(days=91))
        state.reset()
        refreshed = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
        assert routes.repos.call_count == 2 and routes.rdap.call_count == 2
        assert not refreshed.cache_hits["github"] and not refreshed.cache_hits["rdap"]

        # New website content (a page fetched later) → website re-imported, patterns relearned.
        await add_page(ws, cid, "https://acme.fr/nous", ["hugo.bernard@acme.fr"])
        await make_person(ws, cid, "Hugo", "Bernard")
        state.reset()
        web = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
        assert not web.cache_hits["website"] and not web.cache_hits["patterns"]
        assert web.dominant.samples == 5

        # refresh=True rebuilds everything.
        forced = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid, refresh=True)
        assert not any(forced.cache_hits.values())
        assert resolver.mx_calls(DOMAIN) == 3 and routes.repos.call_count == 3


async def test_no_network_evidence_by_default_in_tests(workspace, resolver):
    ws, _ = workspace
    cid = await seed_acme(ws)
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_remote(mock)
        intel = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
    assert routes.repos.call_count == 0 and routes.rdap.call_count == 0
    assert intel.dominant.pattern == "{first}.{last}" and intel.dominant.samples == 2
    assert {o.source for o in intel.observed} == {Src.website}


async def test_github_rate_limit_is_not_marked_checked(workspace, resolver):
    ws, _ = workspace
    cid = await seed_acme(ws)
    state.set_network_evidence(True)
    with respx.mock(assert_all_called=False) as mock:
        limited = mock.get(f"{api()}/users/acme/repos").mock(
            return_value=httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 600)},
            )
        )
        mock.get(f"https://rdap.org/domain/{DOMAIN}").mock(return_value=httpx.Response(404))
        await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
        row = await profile_row()
        assert row.github_checked_at is None and row.rdap_checked_at is not None  # RDAP 404 = checked
        assert row.stats["claims"] == {}
        state.invalidate(DOMAIN)
        await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
        assert limited.call_count == 1  # blocked until reset: no second request


async def test_free_provider_gets_a_minimal_profile(db, resolver):
    intel = await get_domain_intel("gmail.com")
    assert intel.provider == MailProvider.google_workspace and intel.accepts_mail
    assert intel.patterns == [] and intel.observed == []
    assert intel.evidence[0]["signal"] == "free_provider"
    assert await profile_row("gmail.com") is None
    assert (
        await record_observed_emails(
            "gmail.com", [ObservedEmail("a.b@gmail.com", "a.b", Src.website, "A", "B")]
        )
        == 0
    )
    invalid = await get_domain_intel("not a domain")
    assert invalid.provider == MailProvider.unknown and invalid.evidence[0]["signal"] == "invalid_domain"


# ---- samples ------------------------------------------------------------------------------------


async def sample_rows(domain=DOMAIN):
    async with session_scope() as s:
        return (await s.scalars(sa.select(DomainEmailSample).where(DomainEmailSample.domain == domain))).all()


async def test_samples_upsert_is_idempotent(workspace):
    ws, _ = workspace
    early = datetime(2026, 1, 1, tzinfo=UTC)
    late = datetime(2026, 6, 1, tzinfo=UTC)
    items = [
        ObservedEmail("Marie.Dupont@ACME.fr", "marie.dupont", Src.website, observed_at=late, confidence=0.8),
        ObservedEmail("jean@acme.fr", "jean", Src.website, "Jean", "Martin", observed_at=late),
        ObservedEmail("contact@acme.fr", "contact", Src.website),
        ObservedEmail("paul@gmail.com", "paul", Src.website, "Paul", "Durand"),
        ObservedEmail("x@other.fr", "x", Src.website),
    ]
    assert await record_observed_emails(DOMAIN, items, workspace_id=ws) == 3
    again = [
        ObservedEmail(
            "marie.dupont@acme.fr",
            "marie.dupont",
            Src.website,
            "Marie",
            "Dupont",
            observed_at=early,
            confidence=0.95,
        ),
        ObservedEmail("jean@acme.fr", "jean", Src.website, observed_at=early),  # no names: keeps Jean Martin
        ObservedEmail("contact@acme.fr", "contact", Src.website),
    ]
    assert await record_observed_emails(DOMAIN, again, workspace_id=ws) == 3
    rows = {r.address: r for r in await sample_rows()}
    assert set(rows) == {"marie.dupont@acme.fr", "jean@acme.fr", "contact@acme.fr"}
    marie = rows["marie.dupont@acme.fr"]
    assert marie.times_seen == 2 and marie.confidence == 0.95
    assert (marie.first_name, marie.last_name, marie.pattern) == ("Marie", "Dupont", "{first}.{last}")
    assert marie.observed_at == early and marie.last_seen_at == late
    jean = rows["jean@acme.fr"]
    assert (jean.first_name, jean.pattern, jean.times_seen) == ("Jean", "{first}", 2)
    assert rows["contact@acme.fr"].is_role and rows["contact@acme.fr"].pattern is None

    # Same address from another source is another row; learning still counts the address once.
    await record_observed_emails(
        DOMAIN, [ObservedEmail("marie.dupont@acme.fr", "marie.dupont", Src.github, "Marie", "Dupont")]
    )
    assert len(await sample_rows()) == 4
    patterns = dict((p, n) for p, _, n in await store.load_domain_patterns(DOMAIN))
    assert patterns == {"{first}.{last}": 1, "{first}": 1}


async def test_private_samples_are_only_visible_to_their_workspace(workspace):
    ws, _ = workspace
    other = await _second_workspace()
    await record_observed_emails(
        DOMAIN, [ObservedEmail("ceo@acme.fr", "ceo", Src.import_, "Anna", "Ceo")], workspace_id=ws
    )
    await record_observed_emails(
        DOMAIN, [ObservedEmail("info@acme.fr", "info", Src.website)], workspace_id=other
    )
    assert {o.address for o in await load_observed(DOMAIN, workspace_id=ws)} == {
        "ceo@acme.fr",
        "info@acme.fr",
    }
    assert {o.address for o in await load_observed(DOMAIN, workspace_id=other)} == {"info@acme.fr"}
    assert {o.address for o in await load_observed(DOMAIN)} == {"info@acme.fr"}


async def _second_workspace():
    import uuid

    from scout.db.models import Workspace

    ws_id = uuid.uuid4()
    async with session_scope() as s:
        s.add(Workspace(id=ws_id, name="Other", slug="other-" + ws_id.hex[:6]))
    return ws_id


# ---- learning persistence -----------------------------------------------------------------------


async def pattern_row(pattern, domain=DOMAIN):
    async with session_scope() as s:
        return await s.scalar(
            sa.select(DomainEmailPattern).where(
                DomainEmailPattern.domain == domain, DomainEmailPattern.pattern == pattern
            )
        )


def named(first, last, local, source=Src.website):
    return ObservedEmail(f"{local}@{DOMAIN}", local, source, first, last)


async def test_relearn_after_new_samples_switches_the_dominant_pattern(db):
    await record_observed_emails(
        DOMAIN, [named("Marie", "Dupont", "mdupont"), named("Jean", "Martin", "jmartin")]
    )
    assert (await profile_row()).dominant_pattern == "{f}{last}"
    flast = await pattern_row("{f}{last}")
    assert flast.supporting_samples == 2 and flast.share == 1.0 and flast.last_confirmed_at is not None

    newer = [
        named("Paul", "Durand", "paul.durand"),
        named("Léa", "Petit", "lea.petit"),
        named("Hugo", "Bernard", "hugo.bernard"),
        named("Chloé", "Thomas", "chloe.thomas"),
        named("Louis", "Robert", "louis.robert"),
    ]
    await record_observed_emails(DOMAIN, newer)
    row = await profile_row()
    assert row.dominant_pattern == "{first}.{last}" and row.dominant_pattern_confidence > 0.6
    assert row.stats["flags"][-1]["flag"] == "PATTERN_CHANGED"
    assert row.stats["flags"][-1]["from"] == "{f}{last}" and row.stats["invalidated_at"]
    assert (await pattern_row("{first}.{last}")).share == pytest.approx(5 / 7, abs=0.001)
    loaded = await store.load_domain_patterns(DOMAIN)
    assert loaded[0][0] == "{first}.{last}" and loaded[0][2] == 5
    assert [p for p, _, _ in loaded] == ["{first}.{last}", "{f}{last}"]

    # The stored posterior is reused until it is 30 days old, then recomputed (recency decay).
    async with session_scope() as s:
        await s.execute(
            sa.update(DomainEmailPattern).values(updated_at=datetime.now(UTC) - timedelta(days=31))
        )
    await store.load_domain_patterns(DOMAIN)
    assert (await pattern_row("{first}.{last}")).updated_at > datetime.now(UTC) - timedelta(minutes=5)


async def test_record_pattern_outcome_counters(db):
    await record_observed_emails(DOMAIN, [named("Marie", "Dupont", "marie.dupont")])
    base = (await pattern_row("{first}.{last}")).confidence
    await record_pattern_outcome(DOMAIN, "{first}.{last}", True)
    up = await pattern_row("{first}.{last}")
    assert up.successful_checks == 1 and up.confidence > base and up.last_confirmed_at is not None
    await record_pattern_outcome(DOMAIN, "{first}.{last}", success=False)
    await record_pattern_outcome(DOMAIN, "{first}.{last}", success=False)
    down = await pattern_row("{first}.{last}")
    assert down.failed_checks == 2 and down.confidence < up.confidence and down.last_failed_at is not None
    stats = await relearn_domain_patterns(DOMAIN)
    assert stats[0].pattern == "{first}.{last}" and stats[0].successes == 1 and stats[0].failures == 2


async def test_v1_pattern_rows_are_carried_over(db):
    async with session_scope() as s:
        s.add(DomainEmailPattern(domain=DOMAIN, pattern="{f}{last}", supporting_samples=3, confidence=0.925))
        s.add(DomainEmailPattern(domain=DOMAIN, pattern="{first}", successful_checks=1, confidence=0.7))
    loaded = await store.load_domain_patterns(DOMAIN)
    assert [(p, n) for p, _, n in loaded] == [("{f}{last}", 3), ("{first}", 1)]
    row = await profile_row()
    assert row.stats["legacy_samples"] == {"{f}{last}": 3} and row.stats["patterns_v2"] is True
    # Real samples later recorded for the same convention do not double count the legacy ones.
    await record_observed_emails(
        DOMAIN, [named("Marie", "Dupont", "mdupont"), named("Jean", "Martin", "jmartin")]
    )
    assert (await pattern_row("{f}{last}")).supporting_samples == 3


# ---- SMTP facts -----------------------------------------------------------------------------------


async def test_update_smtp_facts_and_catch_all_flip(workspace, resolver):
    ws, _ = workspace
    cid = await seed_acme(ws)
    first = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
    assert first.catch_all is None and first.smtp_reachable is None

    await update_smtp_facts(
        DOMAIN,
        catch_all=False,
        catch_all_confidence=0.85,
        catch_all_method="random_rcpt",
        smtp_reachable=True,
        smtp_last_result="accepted",
    )
    after = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)  # TTL cache was invalidated
    assert not after.cache_hits["profile"]
    assert after.catch_all is False and after.catch_all_confidence == 0.85 and after.smtp_reachable is True
    row = await profile_row()
    assert row.catch_all_method == "random_rcpt" and row.smtp_last_result == "accepted"
    assert row.catch_all_checked_at is not None and row.smtp_checked_at is not None
    assert "flags" not in row.stats

    await update_smtp_facts(DOMAIN, greylisting_seen=True)  # other fields unchanged
    row = await profile_row()
    assert row.greylisting_seen and row.catch_all is False and row.catch_all_confidence == 0.85

    await update_smtp_facts(DOMAIN, catch_all=True, catch_all_method="random_rcpt")
    row = await profile_row()
    assert row.catch_all is True and row.stats["flags"][-1]["flag"] == "CATCH_ALL_FLIPPED"
    assert row.stats["flags"][-1]["from"] is False and row.stats["flags"][-1]["to"] is True
    flipped = await get_domain_intel(DOMAIN, workspace_id=ws, company_id=cid)
    assert flipped.catch_all is True and flipped.greylisting_seen
    assert "CATCH_ALL_FLIPPED" in {e["signal"] for e in flipped.evidence}

    await update_smtp_facts("brand-new.fr", smtp_reachable=False)  # creates the row
    assert (await profile_row("brand-new.fr")).smtp_reachable is False
    await update_smtp_facts("not a domain", catch_all=True)  # ignored


async def test_update_smtp_facts_from_a_deep_path_probe(db):
    from scout.db.enums import SmtpHealthState
    from scout.email.contracts import DomainProbeResult, SessionOutcome

    probe = DomainProbeResult(
        domain=DOMAIN, session=SessionOutcome.ok, catch_all=True, catch_all_confidence=0.8, verifier="builtin"
    )
    await update_smtp_facts(
        DOMAIN, probe, smtp_state=SmtpHealthState.HEALTHY
    )  # call shape of scout.email.deep
    row = await profile_row()
    assert row.catch_all is True and row.catch_all_confidence == 0.8 and row.smtp_reachable is True
    assert row.catch_all_method == "smtp_random_probes:builtin" and row.smtp_last_result == "ok"
    # Explicit keywords win over the probe; a flip is flagged.
    flipped = DomainProbeResult(domain=DOMAIN, session=SessionOutcome.ok, catch_all=True)
    await update_smtp_facts(DOMAIN, flipped, catch_all=False, catch_all_method="manual")
    row = await profile_row()
    assert row.catch_all is False and row.catch_all_method == "manual"
    assert row.stats["flags"][-1]["flag"] == "CATCH_ALL_FLIPPED"
