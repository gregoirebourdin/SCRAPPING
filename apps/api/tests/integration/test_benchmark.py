"""Benchmark harness end to end through the real FastAPI app: dataset import → registry / live / suite runs →
measured metrics, per-item diffs, comparison; admin-only access and workspace isolation."""

from __future__ import annotations

import sys
import types
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from scout.auth.context import issue_service_token
from scout.benchmark import runner
from scout.db.benchmark_models import BenchmarkResult, BenchmarkRun
from scout.db.engine import session_scope
from scout.db.enums import (
    ColumnDataType,
    EmailDiscoveryMethod,
    EmailResolutionPath,
    EmailStatus,
    EntityType,
    ResolverType,
    SourceType,
    UsageCategory,
)
from scout.db.models import (
    Company,
    CustomColumn,
    CustomFieldValue,
    DomainProfile,
    Email,
    EmailResolution,
    Person,
    User,
    Workspace,
    WorkspaceMember,
)
from scout.services import registry

pytestmark = pytest.mark.integration

CSV = (
    "company_domain,company_name,person_first,person_last,person_title,email,email_status,enrich:uses_hubspot\n"
    "acme-bench.fr,Acme,Marie,Dupont,Fondatrice,marie.dupont@acme-bench.fr,SAFE,true\n"
    "acme-bench.fr,Acme,Jean,Martin,Directeur commercial,jean.martin@acme-bench.fr,,\n"
    "acme-bench.fr,Acme,Paul,Leroy,,,INVALID,\n"
    "globex-bench.com,Globex,John,Smith,CEO,john@globex-bench.com,,false\n"
    "missing-bench.io,Missing Co,Ann,Lee,CEO,ann@missing-bench.io,,\n"
)


@pytest.fixture
async def client(db):
    from scout.main import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://scout.test") as c:
        yield c


def _h(user: str, ws: uuid.UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issue_service_token(user, email=f'{user}@example.com')}",
        "X-Workspace-Id": str(ws),
    }


def _ev(url: str) -> registry.Evidence:
    return registry.Evidence(
        source_type=SourceType.website,
        confidence=0.9,
        source_key="website",
        source_url=url,
        evidence="team page",
    )


async def _email(s, ws, person: Person, address: str, status: EmailStatus) -> None:
    local, dom = address.split("@")
    s.add(
        Email(
            workspace_id=ws,
            person_id=person.id,
            company_id=person.company_id,
            address=address,
            local_part=local,
            domain=dom,
            discovery_method=EmailDiscoveryMethod.known_pattern,
            pattern="{first}.{last}",
            status=status,
            is_primary=True,
        )
    )


async def _seed_registry(ws: uuid.UUID) -> None:
    async with session_scope() as s:
        acme, _ = await registry.upsert_company(
            s,
            ws,
            registry.CompanyInput(name="Acme", website="https://acme-bench.fr"),
            _ev("https://acme-bench.fr"),
        )
        globex, _ = await registry.upsert_company(
            s,
            ws,
            registry.CompanyInput(name="Globex", website="https://globex-bench.com"),
            _ev("https://globex-bench.com"),
        )
        people = {}
        for comp, name, title in (
            (acme, "Marie Dupont", "CEO"),
            (acme, "Jean Martin", "Head of Sales"),
            (acme, "Paul Leroy", None),
            (acme, "Zoé Bernard", "Office manager"),
            (globex, "John Smith", "Founder"),
        ):
            p, _ = await registry.upsert_person(
                s,
                ws,
                company_id=comp.id,
                full_name=name,
                job_title=title,
                evidence=_ev(f"https://{comp.normalized_domain}/team"),
            )
            people[name] = p
        await s.flush()
        await _email(s, ws, people["Marie Dupont"], "marie.dupont@acme-bench.fr", EmailStatus.SAFE)
        await _email(s, ws, people["Jean Martin"], "j.martin@acme-bench.fr", EmailStatus.LIKELY_SAFE)
        await _email(s, ws, people["Paul Leroy"], "paul.leroy@acme-bench.fr", EmailStatus.SAFE)
        await _email(s, ws, people["John Smith"], "john@globex-bench.com", EmailStatus.RISKY)
        s.add(DomainProfile(domain="acme-bench.fr", dominant_pattern="{first}.{last}", catch_all=False))
        for person, path, ms in (
            (people["Marie Dupont"], EmailResolutionPath.fast, 120),
            (people["Jean Martin"], EmailResolutionPath.fast, 80),
            (people["Jean Martin"], EmailResolutionPath.deep, 2000),
        ):
            s.add(
                EmailResolution(
                    workspace_id=ws,
                    person_id=person.id,
                    domain="acme-bench.fr",
                    path=path,
                    status=EmailStatus.SAFE,
                    resolver="pattern_memory",
                    duration_ms=ms,
                    cache_hits={"domain_profile": path == EmailResolutionPath.fast and ms == 120},
                )
            )
        col = CustomColumn(
            workspace_id=ws,
            name="Uses HubSpot",
            slug="uses_hubspot",
            data_type=ColumnDataType.boolean,
            entity_type=EntityType.company,
            resolver_type=ResolverType.DETERMINISTIC,
        )
        s.add(col)
        await s.flush()
        s.add(
            CustomFieldValue(
                workspace_id=ws,
                column_id=col.id,
                entity_type=EntityType.company,
                entity_id=acme.id,
                value_json=True,
                display_value="true",
                status="success",
                resolver="keyword",
            )
        )


@pytest.fixture
def learning(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool | None]]:
    """A stand-in for scout.learning.stats (another workstream) capturing fed outcomes."""
    calls: list[tuple[str, str, bool | None]] = []
    mod = types.ModuleType("scout.learning.stats")

    async def record_outcome(dimension: str, key: str, correct: bool | None) -> None:
        calls.append((dimension, key, correct))

    mod.record_outcome = record_outcome  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scout.learning.stats", mod)
    return calls


def _m(metrics: dict[str, Any], key: str) -> tuple[Any, Any]:
    return metrics[key]["value"], metrics[key]["n"]


async def test_dataset_import_registry_run_and_metrics(client, workspace, learning) -> None:
    ws, user = workspace
    h = _h(user, ws)
    await _seed_registry(ws)

    r = await client.post("/v1/benchmark/datasets/preview", headers=h, json={"csv": CSV})
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == {
        "items": 3,
        "people": 5,
        "emails": 4,
        "invalid_emails": 1,
        "enrichment_values": 2,
    }
    r = await client.post("/v1/benchmark/datasets", headers=h, json={"name": "Ground truth v1", "csv": CSV})
    assert r.status_code == 201, r.text
    dataset = r.json()
    assert dataset["item_count"] == 3 and dataset["kind"] == "leads"

    r = await client.post(
        "/v1/benchmark/runs", headers=h, json={"mode": "registry", "dataset_id": dataset["id"]}
    )
    assert r.status_code == 202, r.text
    run_id = uuid.UUID(r.json()["id"])
    assert r.json()["status"] == "queued" and r.json()["items_total"] == 3
    assert await runner.execute(run_id, slice_s=None) == "completed"

    run = (await client.get(f"/v1/benchmark/runs/{run_id}", headers=h)).json()
    assert run["status"] == "completed" and run["items_done"] == 3 and run["cost_usd"] == 0
    m = run["metrics"]
    # every company identity was given as input → excluded from company P/R, coverage measured
    assert _m(m, "company_precision") == (None, 0)
    assert m["company_identity_given"]["value"] == 3
    assert _m(m, "company_coverage") == (pytest.approx(2 / 3, abs=1e-3), 3)
    assert _m(m, "person_precision") == (0.8, 5)  # Zoé is spurious
    assert _m(m, "person_recall") == (0.8, 5)  # Ann is missing
    assert _m(m, "role_precision") == (1.0, 3)
    assert _m(m, "email_precision") == (0.5, 4)  # Marie ✓, John ✓, Jean ✗, Paul (no mailbox) ✗
    assert _m(m, "email_recall") == (0.25, 4)  # only Marie is correct AND deliverable
    assert _m(m, "email_discovery_recall") == (0.5, 4)
    assert _m(m, "safe_email_precision") == (0.5, 2)
    assert _m(m, "invalid_false_positive_rate") == (1.0, 1)
    assert _m(m, "domain_pattern_accuracy") == (pytest.approx(1 / 3, abs=1e-3), 3)
    assert _m(m, "enrichment_accuracy") == (0.5, 2)
    assert _m(m, "enrichment_precision") == (1.0, 1)
    assert _m(m, "smtp_fallback_rate") == (0.5, 2)
    assert m["avg_email_resolution_ms"]["value"] == 1100
    assert m["false_positives"]["value"] == 3 and m["false_negatives"]["value"] == 4
    assert m["qualified_leads"]["value"] == 1
    for metric in m.values():
        if metric["unit"] == "rate" and metric["n"]:
            lo, hi = metric["ci90"]
            assert lo <= metric["value"] <= hi

    # learning feedback: definitive outcomes only, keyed by provenance
    assert ("people.source", "website", True) in learning
    assert ("people.source", "website", False) in learning  # Zoé (exhaustive ground truth)
    assert ("enrich.resolver", "keyword", True) in learning
    assert ("pattern", "{first}.{last}", False) in learning  # Jean's wrong address
    assert all(c is not None for _, _, c in learning)

    res = (await client.get(f"/v1/benchmark/runs/{run_id}/results?limit=2", headers=h)).json()
    assert res["total"] == 3 and len(res["items"]) == 2 and res["items"][0]["label"] == "acme-bench.fr"
    acme = res["items"][0]
    assert {r["verdict"] for r in acme["verdicts"]["emails"]["rows"]} == {"correct", "wrong", "invalid_fp"}
    assert acme["verdicts"]["people"]["spurious"] == ["Zoé Bernard"]
    errors = (await client.get(f"/v1/benchmark/runs/{run_id}/results?errors_only=true", headers=h)).json()
    assert errors["total"] == 3  # every item has at least one FP/FN here

    runs = (await client.get("/v1/benchmark/runs", headers=h)).json()
    assert runs[0]["id"] == str(run_id) and runs[0]["dataset_name"] == "Ground truth v1"
    datasets = (await client.get("/v1/benchmark/datasets", headers=h)).json()
    assert datasets[0]["runs"] == 1
    detail = (await client.get(f"/v1/benchmark/datasets/{dataset['id']}?limit=1", headers=h)).json()
    assert len(detail["items"]) == 1 and detail["items"][0]["expected"]["people"][0]["first"] == "Marie"

    # a re-execution never stores an item twice
    async with session_scope() as s:
        await s.execute(sa.update(BenchmarkRun).where(BenchmarkRun.id == run_id).values(status="running"))
    await runner.execute(run_id, slice_s=None)
    async with session_scope() as s:
        n = await s.scalar(
            sa.select(sa.func.count()).select_from(BenchmarkResult).where(BenchmarkResult.run_id == run_id)
        )
    assert n == 3

    # deleting the dataset keeps past runs and their snapshotted results
    assert (await client.delete(f"/v1/benchmark/datasets/{dataset['id']}", headers=h)).status_code == 200
    kept = (await client.get(f"/v1/benchmark/runs/{run_id}", headers=h)).json()
    assert kept["dataset_id"] is None and kept["metrics"]["person_precision"]["value"] == 0.8


async def test_import_validation_errors_are_reported_per_row(client, workspace) -> None:
    ws, user = workspace
    r = await client.post(
        "/v1/benchmark/datasets",
        headers=_h(user, ws),
        json={"name": "bad", "csv": "company_domain,email\nacme-bench.fr,nope\n,x@y.fr\n"},
    )
    assert r.status_code == 422
    errs = r.json()["error"]["details"]["errors"]
    assert {e["where"] for e in errs} == {"row 2", "row 3"}
    r = await client.post(
        "/v1/benchmark/datasets", headers=_h(user, ws), json={"name": "x", "csv": "a", "items": [{}]}
    )
    assert r.status_code == 422
    tpl = await client.get("/v1/benchmark/template.csv", headers=_h(user, ws))
    assert tpl.status_code == 200 and tpl.text.startswith("company_domain,")


async def test_suite_run_strategies_side_by_side_and_compare(client, workspace) -> None:
    ws, user = workspace
    h = _h(user, ws)
    suites = (await client.get("/v1/benchmark/suites", headers=h)).json()
    assert "demo" in {s["key"] for s in suites}
    r = await client.post(
        "/v1/benchmark/runs",
        headers=h,
        json={"mode": "suite", "suite": "demo", "config": {"strategies": ["exact", "nope"]}},
    )
    assert r.status_code == 422
    r = await client.post("/v1/benchmark/runs", headers=h, json={"mode": "suite", "suite": "demo"})
    run_id = uuid.UUID(r.json()["id"])
    assert await runner.execute(run_id) == "completed"
    run = (await client.get(f"/v1/benchmark/runs/{run_id}", headers=h)).json()
    assert set(run["strategies"]) == {"exact", "normalized"}
    assert run["strategies"]["exact"]["person_recall"]["n"] == 7
    assert run["strategies"]["normalized"]["person_recall"]["definition"].startswith("Pairs labelled")
    exact = (
        await client.get(f"/v1/benchmark/runs/{run_id}/results?strategy=exact&limit=200", headers=h)
    ).json()
    assert exact["total"] == 10 and all(i["strategy"] == "exact" for i in exact["items"])
    cmp = (await client.get(f"/v1/benchmark/compare?ids={run_id}", headers=h)).json()
    assert [c["strategy"] for c in cmp["columns"]] == ["exact", "normalized"]
    recall = next(x for x in cmp["metrics"] if x["key"] == "person_recall")
    assert recall["values"][1]["value"] == 1.0 and recall["values"][0]["value"] < 0.5
    assert "not conclusive" in cmp["note"]


async def test_live_run_attributes_cost_per_item_and_respects_the_cost_cap(
    client, workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live mode without network: crawl off, e-mail resolution stubbed (records usage like the real engine)."""
    ws, user = workspace
    h = _h(user, ws)
    import scout.email.engine as engine
    from scout.services.usage import record_usage

    async def fake_resolve(workspace_id, person_id, **kw):
        async with session_scope() as s:
            p = await s.get(Person, person_id)
            comp = await s.get(Company, p.company_id)
            addr = f"{p.first_name.lower()}@{comp.normalized_domain}"
            await _email(s, workspace_id, p, addr, EmailStatus.SAFE)
            s.add(
                EmailResolution(
                    workspace_id=workspace_id,
                    person_id=p.id,
                    domain=comp.normalized_domain,
                    path=EmailResolutionPath.fast,
                    status=EmailStatus.SAFE,
                    resolver="pattern_memory",
                    address=addr,
                    duration_ms=40,
                    cache_hits={"domain_profile": True},
                )
            )
        await record_usage(UsageCategory.verification_request, cost_usd=0.002)
        return types.SimpleNamespace(deep_requested=False)

    monkeypatch.setattr(engine, "resolve_for_person", fake_resolve)
    csv = (
        "company_domain,person_first,person_last,email\n"
        "alpha-bench.fr,Marie,Dupont,marie@alpha-bench.fr\n"
        "beta-bench.fr,Jean,Martin,jean.martin@beta-bench.fr\n"
        "gamma-bench.fr,Paul,Leroy,paul@gamma-bench.fr\n"
    )
    d = (
        await client.post(
            "/v1/benchmark/datasets", headers=h, json={"name": "emails", "kind": "email", "csv": csv}
        )
    ).json()
    bad = await client.post(
        "/v1/benchmark/runs",
        headers=h,
        json={"mode": "live", "dataset_id": d["id"], "config": {"max_cost_usd": 100}},
    )
    assert bad.status_code == 422
    r = await client.post(
        "/v1/benchmark/runs",
        headers=h,
        json={
            "mode": "live",
            "dataset_id": d["id"],
            "config": {"crawl": False, "enrichment": False, "concurrency": 1, "max_cost_usd": 0.003},
        },
    )
    assert r.status_code == 202, r.text
    run_id = uuid.UUID(r.json()["id"])
    assert r.json()["strategy"] == "email:fast · no-crawl"
    assert await runner.execute(run_id, slice_s=None) == "completed"
    run = (await client.get(f"/v1/benchmark/runs/{run_id}", headers=h)).json()
    assert run["items_done"] == 2  # third item never launched: cap reached
    assert any("Cost cap reached: 1 item" in n for n in run["notes"])
    m = run["metrics"]
    assert run["cost_usd"] == pytest.approx(0.004)
    assert "person_precision" not in m  # people identities were given as input: not measured
    assert _m(m, "email_precision") == (0.5, 2)  # marie@ ✓, jean@ ✗ (truth: jean.martin@)
    assert _m(m, "cache_hit_rate") == (1.0, 2)
    assert m["cost_per_email"]["value"] == pytest.approx(0.002)
    assert m["cost_per_qualified_lead"]["value"] == pytest.approx(0.004)
    async with session_scope() as s:
        costs = (
            await s.scalars(sa.select(BenchmarkResult.cost_usd).where(BenchmarkResult.run_id == run_id))
        ).all()
    assert sorted(costs) == [Decimal("0.002000"), Decimal("0.002000")]


async def test_benchmark_is_admin_only_and_workspace_scoped(client, workspace) -> None:
    ws, owner = workspace
    member = "user_member_" + uuid.uuid4().hex[:6]
    other = "user_other_" + uuid.uuid4().hex[:6]
    async with session_scope() as s:
        s.add(User(id=member, name="Member", email=f"{member}@example.com"))
        s.add(User(id=other, name="Other", email=f"{other}@example.com"))
        other_ws = Workspace(name="Other", slug="other-" + uuid.uuid4().hex[:6])
        s.add(other_ws)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws, user_id=member, role="member"))
        s.add(WorkspaceMember(workspace_id=other_ws.id, user_id=other, role="owner"))
    assert (await client.get("/v1/benchmark/datasets")).status_code == 401
    for method, path in (
        ("GET", "/v1/benchmark/datasets"),
        ("GET", "/v1/benchmark/runs"),
        ("GET", "/v1/benchmark/suites"),
        ("POST", "/v1/benchmark/datasets"),
    ):
        r = await client.request(method, path, headers=_h(member, ws), json={"name": "x", "csv": CSV})
        assert r.status_code == 403, (method, path, r.status_code)
    d = (
        await client.post("/v1/benchmark/datasets", headers=_h(owner, ws), json={"name": "mine", "csv": CSV})
    ).json()
    oh = _h(other, other_ws.id)
    assert (await client.get(f"/v1/benchmark/datasets/{d['id']}", headers=oh)).status_code == 404
    assert (await client.delete(f"/v1/benchmark/datasets/{d['id']}", headers=oh)).status_code == 404
    r = await client.post("/v1/benchmark/runs", headers=oh, json={"mode": "registry", "dataset_id": d["id"]})
    assert r.status_code == 404
    assert (await client.get("/v1/benchmark/datasets", headers=oh)).json() == []


async def test_job_handler_slices_long_runs_and_reschedules(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scout.benchmark import jobs
    from scout.jobs.registry import JobContext, get_handler
    from scout.services import benchmark as svc

    ws, user = workspace
    await _seed_registry(ws)
    parsed = svc.parse_import(
        kind="leads", csv=CSV, items=None, people_exhaustive=True, company_input="domain"
    )
    async with session_scope() as s:
        d = await svc.create_dataset(
            s,
            ws,
            name="sliced",
            kind="leads",
            description=None,
            parsed=parsed,
            people_exhaustive=True,
            company_input="domain",
            columns=None,
            source="csv",
            user_id=user,
        )
        await s.flush()
        run = await svc.start_run(
            s, ws, mode="registry", dataset_id=d.id, suite=None, strategy=None, config={}, user_id=user
        )
        await s.flush()
        run_id, job_id = run.id, run.job_id
    assert job_id is not None and get_handler(jobs.JOB_TYPE) is not None
    monkeypatch.setattr(runner, "SLICE_S", 0.0)  # every slice ends after the first launch window
    real_execute = runner.execute

    async def one_slice(rid, slice_s=None):
        return await real_execute(rid, slice_s=1e-9)

    monkeypatch.setattr(runner, "execute", one_slice)
    ctx = JobContext(
        job_id=job_id,
        workspace_id=ws,
        campaign_id=None,
        type=jobs.JOB_TYPE,
        payload={"run_id": str(run_id)},
        attempt=1,
        worker_id="test",
    )
    results = []
    for _ in range(10):
        ctx.reschedule_after = None
        results.append(await jobs.benchmark_run(ctx))
        if ctx.reschedule_after is None:
            break
    assert results[0]["status"] == "continue" and results[-1]["status"] == "completed"
    async with session_scope() as s:
        r = await s.get(BenchmarkRun, run_id)
        assert r is not None and r.items_done == 3 and r.metrics["person_recall"]["value"] == 0.8
