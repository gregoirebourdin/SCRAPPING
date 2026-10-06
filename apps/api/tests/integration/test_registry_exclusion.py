"""Global registry dedupe, company/person distinction, exclusion modes evaluated in SQL, suppression.

Spec §19–§25 (registry, exposures), §87 (suppression), §172–§174 & §203 (exclusion, history survives list removal).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import EntityType, ExclusionMode, ExposureType, SourceType, SuppressionReason
from scout.db.models import Company, Email, LeadExposure, Person
from scout.errors import ValidationFailed
from scout.schemas.campaign import ExclusionSpec
from scout.services import lists as lists_svc
from scout.services import registry
from scout.services.exclusion import (
    ExclusionRule,
    compile_rules,
    excluded_entities,
    suppressed_companies,
    suppressed_people,
)
from scout.services.suppression import suppress

pytestmark = pytest.mark.integration

WEB = registry.Evidence(
    source_type=SourceType.website,
    confidence=0.9,
    source_key="website",
    source_url="https://agence-x.fr/equipe",
    evidence="Marie Dupont — Fondatrice",
)


async def _company(
    ws: uuid.UUID, name: str = "Agence X", website: str | None = "https://agence-x.fr", **kw
) -> Company:
    async with session_scope() as s:
        c, _ = await registry.upsert_company(
            s, ws, registry.CompanyInput(name=name, website=website, **kw), WEB
        )
        return c


async def _person(ws: uuid.UUID, company_id: uuid.UUID | None, name: str, **kw) -> tuple[Person, bool]:
    async with session_scope() as s:
        return await registry.upsert_person(s, ws, company_id=company_id, full_name=name, evidence=WEB, **kw)


async def _count(model, ws) -> int:
    async with session_scope() as s:
        return int(
            await s.scalar(sa.select(sa.func.count()).select_from(model).where(model.workspace_id == ws)) or 0
        )


# =============================================================================================
# Company identity
# =============================================================================================


async def test_company_dedupe_by_registrable_domain(workspace):
    ws, _ = workspace
    a = await _company(ws, website="https://agence-x.fr/contact")
    b = await _company(ws, name="AGENCE X SAS", website="http://www.Agence-X.fr")
    c = await _company(ws, name="Agence X Lyon", website="https://blog.agence-x.fr/")
    assert a.id == b.id == c.id
    assert await _count(Company, ws) == 1


async def test_company_dedupe_by_registry_id(workspace):
    ws, _ = workspace
    a = await _company(
        ws, name="Agence X", website=None, registry_source="fr_sirene", registry_id="812345676", city="Lyon"
    )
    b = await _company(
        ws,
        name="AGX Communication",
        website="https://agx.fr",
        registry_source="fr_sirene",
        registry_id="812345676",
    )
    assert a.id == b.id
    async with session_scope() as s:
        comp = await s.get(Company, a.id)
        assert comp is not None and comp.normalized_domain == "agx.fr"  # domain learnt, no second row
    assert await _count(Company, ws) == 1


async def test_siren_read_on_the_website_matches_the_registry_record(workspace):
    """A SIREN extracted from a legal notice gets its registry source, so the official registry candidate
    later resolves to the same company (no duplicate)."""
    ws, _ = workspace
    site = await _company(ws, name="Agence X", website="https://agence-x.fr", country="FR")
    async with session_scope() as s:
        comp = await s.get(Company, site.id)
        assert comp is not None
        await registry.observe_company(s, ws, comp, {"registry_id": "812345676"}, WEB)
        assert (comp.registry_source, comp.registry_id) == ("fr_sirene", "812345676")
    reg = await _company(
        ws, name="AGENCE X", website=None, registry_source="fr_sirene", registry_id="812345676"
    )
    assert reg.id == site.id


async def test_same_registry_id_on_two_companies_is_flagged_not_merged(workspace):
    ws, _ = workspace
    first = await _company(
        ws,
        name="Agence X",
        website="https://agence-x.fr",
        registry_source="fr_sirene",
        registry_id="812345676",
    )
    other = await _company(ws, name="Agence Y", website="https://agence-y.fr", country="FR")
    async with session_scope() as s:
        comp = await s.get(Company, other.id)
        assert comp is not None
        await registry.observe_company(s, ws, comp, {"registry_id": "812345676"}, WEB)
        assert comp.registry_id is None and comp.needs_review  # potential duplicate surfaced for review
    assert first.id != other.id


async def test_fuzzy_name_only_without_stronger_keys_and_only_in_the_same_city(workspace):
    ws, _ = workspace
    a = await _company(ws, name="Boulangerie Martin", website=None, city="Lyon")
    same = await _company(ws, name="BOULANGERIE MARTIN SARL", website=None, city="lyon")
    other_city = await _company(ws, name="Boulangerie Martin", website=None, city="Paris")
    with_domain = await _company(
        ws, name="Boulangerie Martin", website="https://boulangerie-martin.fr", city="Lyon"
    )
    assert same.id == a.id
    assert other_city.id != a.id
    assert with_domain.id != a.id  # a stronger key never fuzzy-merges
    assert await _count(Company, ws) == 3


async def test_registry_is_workspace_scoped(workspace):
    ws, _ = workspace
    async with session_scope() as s:
        from scout.db.models import Workspace

        other = Workspace(name="Other", slug="other-" + uuid.uuid4().hex[:6])
        s.add(other)
        await s.flush()
        other_id = other.id
    a = await _company(ws)
    b = await _company(other_id)
    assert a.id != b.id


# =============================================================================================
# Person identity + company ≠ person
# =============================================================================================


async def test_person_dedupe_by_company_and_normalized_name(workspace):
    ws, _ = workspace
    comp = await _company(ws)
    p1, created1 = await _person(ws, comp.id, "Marie Dupont", job_title="Fondatrice")
    p2, created2 = await _person(ws, comp.id, "MARIE DUPONT")
    p3, _ = await _person(ws, comp.id, "Marie  Dupont ")
    assert created1 and not created2 and p1.id == p2.id == p3.id
    other = await _company(ws, name="Agence Y", website="https://agence-y.fr")
    namesake, created = await _person(ws, other.id, "Marie Dupont")
    assert created and namesake.id != p1.id  # never merged by name alone across companies


async def test_person_dedupe_by_profile_url_and_email(workspace):
    ws, _ = workspace
    comp = await _company(ws)
    p, _ = await _person(ws, comp.id, "Marie Dupont", profile_url="https://www.linkedin.com/in/marie-dupont")
    new_company = await _company(ws, name="Agence Y", website="https://agence-y.fr")
    moved, created = await _person(
        ws, new_company.id, "Marie Dupont-Martin", profile_url="https://www.linkedin.com/in/marie-dupont"
    )
    assert not created and moved.id == p.id  # same public profile → same person (changed company/name)
    async with session_scope() as s:
        s.add(
            Email(
                workspace_id=ws,
                person_id=p.id,
                company_id=comp.id,
                address="marie@agence-x.fr",
                local_part="marie",
                domain="agence-x.fr",
                discovery_method="published",
            )
        )
    by_email, created = await _person(ws, None, "M. Dupont", email="Marie@Agence-X.fr")
    assert not created and by_email.id == p.id


@pytest.mark.parametrize(
    "name",
    [
        "Agence X",
        "Agence Lumière",
        "Lumière SAS",
        "Studio Nova",
        "Dupont Consulting",
        "Groupe Martin",
        "ACME Ltd",
    ],
)
async def test_company_name_is_never_stored_as_a_person(workspace, name):
    ws, _ = workspace
    comp = await _company(ws, name="Agence X")
    with pytest.raises(ValidationFailed, match="looks like a company name"):
        await _person(ws, comp.id, name)
    assert await _count(Person, ws) == 0


async def test_sole_proprietor_named_after_the_owner_is_still_a_person(workspace):
    ws, _ = workspace
    comp = await _company(ws, name="Jean Dupont", website="https://jeandupont-photo.fr")
    person, created = await _person(ws, comp.id, "Jean Dupont")
    assert created and person.full_name == "Jean Dupont"


async def test_registry_owner_of_an_ei_named_after_them_is_a_person(workspace):
    """'Marie Bois' fails the website name heuristics ('bois' is a word); as the official owner of the EI
    'MARIE BOIS' (registry evidence) she is still stored — organisation names are still refused."""
    ws, _ = workspace
    comp = await _company(ws, name="MARIE BOIS", website="https://mariebois-design.fr")
    official = registry.Evidence(
        source_type=SourceType.registry,
        confidence=0.95,
        source_key="fr_registry",
        source_url="https://annuaire-entreprises.data.gouv.fr/entreprise/912345678",
        evidence="Marie Bois — Chef d'entreprise (entrepreneur individuel) (official registry)",
    )
    with pytest.raises(ValidationFailed, match="looks like a company name"):
        await _person(ws, comp.id, "Marie Bois")  # website evidence: a heading repeating the company name
    async with session_scope() as s:
        person, created = await registry.upsert_person(
            s, ws, company_id=comp.id, full_name="Marie Bois", evidence=official
        )
    assert created and person.full_name == "Marie Bois"
    async with session_scope() as s:
        with pytest.raises(ValidationFailed, match="looks like a company name"):
            await registry.upsert_person(
                s, ws, company_id=comp.id, full_name="Groupe Martin", evidence=official
            )


async def test_person_requires_evidence(workspace):
    ws, _ = workspace
    comp = await _company(ws)
    async with session_scope() as s:
        with pytest.raises(ValidationFailed):
            await registry.upsert_person(
                s,
                ws,
                company_id=comp.id,
                full_name="Marie Dupont",
                evidence=registry.Evidence(source_type=SourceType.ai_extraction, confidence=0.9),
            )


async def test_concurrent_upserts_of_the_same_person_yield_one_row(workspace):
    ws, _ = workspace
    comp = await _company(ws)
    results = await asyncio.gather(*(_person(ws, comp.id, "Marie Dupont") for _ in range(4)))
    assert len({p.id for p, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    assert await _count(Person, ws) == 1


# =============================================================================================
# Exclusion modes (SQL)
# =============================================================================================


async def _expose(ws, exposure: ExposureType, *, person_ids=(), company_ids=(), days_ago: int = 0, **kw):
    async with session_scope() as s:
        await registry.record_exposures(
            s, ws, exposure, person_ids=list(person_ids), company_ids=list(company_ids), **kw
        )
        if days_ago:
            await s.execute(
                sa.update(LeadExposure)
                .where(LeadExposure.workspace_id == ws, LeadExposure.exposure_type == exposure)
                .values(occurred_at=datetime.now(UTC) - timedelta(days=days_ago))
            )


async def _excluded(
    ws, entity: EntityType, ids, spec: ExclusionSpec | list[ExclusionRule], **kw
) -> set[uuid.UUID]:
    rules = (
        spec if isinstance(spec, list) else compile_rules(spec, target_list_id=kw.pop("target_list_id", None))
    )
    async with session_scope() as s:
        return set(await excluded_entities(s, ws, entity, ids, rules, **kw))


@pytest.fixture
async def leads(workspace):
    """Two companies; Marie (seen), Paul (never seen) at company A; Lea at company B."""
    ws, _ = workspace
    a = await _company(ws)
    b = await _company(ws, name="Agence Y", website="https://agence-y.fr")
    marie, _ = await _person(ws, a.id, "Marie Dupont")
    paul, _ = await _person(ws, a.id, "Paul Girard")
    lea, _ = await _person(ws, b.id, "Léa Morel")
    return ws, a, b, marie, paul, lea


async def test_exclude_previous_people_keeps_new_people_at_known_companies(leads):
    ws, a, _b, marie, paul, lea = leads
    await _expose(ws, ExposureType.DISCOVERED, person_ids=[marie.id])
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE)
    assert await _excluded(ws, EntityType.person, [marie.id, paul.id, lea.id], spec) == {marie.id}
    assert await _excluded(ws, EntityType.company, [a.id], spec) == set()  # company stays eligible (§201)
    strict = ExclusionSpec(
        mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE, allow_new_people_at_existing_companies=False
    )
    assert await _excluded(ws, EntityType.company, [a.id], strict) == {a.id}


async def test_exclude_previous_companies(leads):
    ws, a, b, marie, _paul, _lea = leads
    await _expose(ws, ExposureType.DISCOVERED, person_ids=[marie.id])  # also exposes company A
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_PREVIOUS_COMPANIES)
    assert await _excluded(ws, EntityType.company, [a.id, b.id], spec) == {a.id}


async def test_exclude_exported_ignores_other_exposures(leads):
    ws, _a, _b, marie, paul, _lea = leads
    await _expose(ws, ExposureType.DISCOVERED, person_ids=[marie.id, paul.id])
    await _expose(ws, ExposureType.EXPORTED, person_ids=[paul.id])
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_EXPORTED)
    assert await _excluded(ws, EntityType.person, [marie.id, paul.id], spec) == {paul.id}


async def test_exclude_contacted(leads):
    ws, _a, _b, marie, paul, _lea = leads
    await _expose(ws, ExposureType.CONTACTED, person_ids=[marie.id])
    await _expose(ws, ExposureType.EXPORTED, person_ids=[paul.id])
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_CONTACTED)
    assert await _excluded(ws, EntityType.person, [marie.id, paul.id], spec) == {marie.id}


async def test_cooldown_only_excludes_recent_exposures(leads):
    ws, _a, _b, marie, paul, _lea = leads
    await _expose(ws, ExposureType.DISCOVERED, person_ids=[marie.id], days_ago=120)
    await _expose(ws, ExposureType.EXPORTED, person_ids=[paul.id], days_ago=10)
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_WITHIN_COOLDOWN, cooldown_days=90)
    assert await _excluded(ws, EntityType.person, [marie.id, paul.id], spec) == {paul.id}


async def test_internal_rejections_and_enrichment_are_not_user_exposures(leads):
    ws, _a, _b, marie, _paul, _lea = leads
    await _expose(ws, ExposureType.ENRICHED, person_ids=[marie.id])
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE)
    assert await _excluded(ws, EntityType.person, [marie.id], spec) == set()


async def test_current_campaign_exposures_do_not_exclude_itself(leads):
    ws, _a, _b, marie, _paul, _lea = leads
    cid = uuid.uuid4()
    await _expose(ws, ExposureType.DISCOVERED, person_ids=[marie.id], campaign_id=cid)
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE)
    assert await _excluded(ws, EntityType.person, [marie.id], spec, current_campaign_id=cid) == set()
    assert await _excluded(ws, EntityType.person, [marie.id], spec, current_campaign_id=uuid.uuid4()) == {
        marie.id
    }


async def test_imported_contacts_excluded_by_import_id(leads):
    ws, _a, _b, marie, paul, _lea = leads
    imp, other_imp = uuid.uuid4(), uuid.uuid4()
    await _expose(ws, ExposureType.IMPORTED, person_ids=[marie.id], import_id=imp)
    await _expose(ws, ExposureType.IMPORTED, person_ids=[paul.id], import_id=other_imp)
    spec = ExclusionSpec(import_ids=[imp])
    assert await _excluded(ws, EntityType.person, [marie.id, paul.id], spec) == {marie.id}


async def test_list_exclusion_survives_removal_from_the_list(leads):
    """§203: deleting a contact from a list never erases its history; list exclusion still applies."""
    ws, a, _b, marie, paul, _lea = leads
    async with session_scope() as s:
        lst, _ = await lists_svc.create_list(
            s, ws, name="Clients", user_id=None, entity_type=EntityType.person
        )
        await lists_svc.add_to_list(s, ws, lst.id, EntityType.person, [marie.id, paul.id], user_id=None)
        await lists_svc.remove_from_list(s, ws, lst.id, EntityType.person, [marie.id])
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_SPECIFIC_LISTS, list_ids=[lst.id])
    assert await _excluded(ws, EntityType.person, [marie.id, paul.id], spec) == {marie.id, paul.id}
    no_history = [ExclusionRule(EntityType.person, list_ids=[lst.id], include_list_history=False)]
    assert await _excluded(ws, EntityType.person, [marie.id, paul.id], no_history) == {paul.id}
    # company-scoped list rule: a company is excluded through its people's membership
    company_scope = ExclusionSpec(
        mode=ExclusionMode.EXCLUDE_SPECIFIC_LISTS, list_ids=[lst.id], list_scope="companies"
    )
    assert await _excluded(ws, EntityType.company, [a.id], company_scope) == {a.id}
    # and the never-scraped rule still sees the removed contact (global history persists)
    await _expose(ws, ExposureType.DISCOVERED, person_ids=[marie.id])
    async with session_scope() as s:
        await lists_svc.delete_list(s, ws, lst.id)
    assert await _excluded(
        ws, EntityType.person, [marie.id], ExclusionSpec(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE)
    ) == {marie.id}


async def test_exposures_of_another_workspace_never_exclude(leads):
    ws, _a, _b, marie, _paul, _lea = leads
    await _expose(ws, ExposureType.DISCOVERED, person_ids=[marie.id])
    spec = ExclusionSpec(mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE)
    assert await _excluded(uuid.uuid4(), EntityType.person, [marie.id], spec) == set()


# =============================================================================================
# Suppression
# =============================================================================================


async def test_suppression_by_person_email_domain_and_company(leads):
    ws, a, b, marie, paul, _lea = leads
    async with session_scope() as s:
        s.add(
            Email(
                workspace_id=ws,
                person_id=paul.id,
                company_id=a.id,
                address="paul.girard@agence-x.fr",
                local_part="paul.girard",
                domain="agence-x.fr",
                discovery_method="published",
            )
        )
    async with session_scope() as s:
        await suppress(
            s,
            ws,
            reason=SuppressionReason.opt_out,
            user_id=None,
            person_ids=[marie.id],
            emails=[" Someone@Agence-Y.fr "],
            domains=["https://www.Spam.fr/page"],
        )
        await suppress(s, ws, reason=SuppressionReason.do_not_contact, user_id=None, company_ids=[b.id])
    async with session_scope() as s:
        ids, _ = await suppressed_people(s, ws, person_ids=[marie.id, paul.id])
        _, emails = await suppressed_people(s, ws, emails=["someone@agence-y.fr", "paul.girard@agence-x.fr"])
        cids, domains = await suppressed_companies(
            s, ws, company_ids=[a.id, b.id], domains=["spam.fr", "agence-y.fr"]
        )
    assert ids == {marie.id}
    assert emails == {"someone@agence-y.fr"}
    assert cids == {b.id}
    assert domains == {"spam.fr", "agence-y.fr"}  # URL input is canonicalized to its registrable domain


async def test_suppressed_people_are_never_added_to_lists(leads):
    ws, _a, _b, marie, paul, _lea = leads
    async with session_scope() as s:
        await suppress(s, ws, reason=SuppressionReason.user_request, user_id=None, person_ids=[marie.id])
        lst, _ = await lists_svc.create_list(
            s, ws, name="Outreach", user_id=None, entity_type=EntityType.person
        )
        change = await lists_svc.add_to_list(
            s, ws, lst.id, EntityType.person, [marie.id, paul.id], user_id=None
        )
    assert change.added == [paul.id] and change.skipped_suppressed == 1
