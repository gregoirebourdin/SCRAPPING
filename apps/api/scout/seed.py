"""Demo seed data (spec §208) so the UI is realistically testable before scraping runs.

Everything seeded is clearly marked: lists are prefixed "Demo ·" and every domain uses the reserved
`.example` TLD, so demo records can never be mistaken for real companies or real people.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import (
    CampaignMode,
    CampaignStatus,
    CellStatus,
    ColumnDataType,
    ColumnKind,
    EmailDiscoveryMethod,
    EmailKind,
    EmailStatus,
    EntityType,
    ExposureType,
    ResolverType,
    RoleFamily,
    Seniority,
    SmtpResult,
    SourceType,
    WebsiteStatus,
)
from scout.db.models import (
    Campaign,
    CampaignStats,
    Company,
    CompanyFieldObservation,
    CustomColumn,
    CustomFieldValue,
    Email,
    LeadExposure,
    List,
    ListMembership,
    Person,
    PersonFieldObservation,
    QualificationScore,
    SavedView,
)
from scout.schemas.campaign import CampaignDefinition, CompanyFilters, EmployeeRange, ExclusionSpec, KeywordCondition, PeopleFilters
from scout.util.text import normalize_company_name, normalize_person_name

CITIES = [("Paris", "75", "Île-de-France", 0.32), ("Lyon", "69", "Auvergne-Rhône-Alpes", 0.12), ("Marseille", "13", "Provence-Alpes-Côte d'Azur", 0.07),
          ("Bordeaux", "33", "Nouvelle-Aquitaine", 0.08), ("Lille", "59", "Hauts-de-France", 0.07), ("Nantes", "44", "Pays de la Loire", 0.07),
          ("Toulouse", "31", "Occitanie", 0.07), ("Nice", "06", "Provence-Alpes-Côte d'Azur", 0.05), ("Rennes", "35", "Bretagne", 0.05),
          ("Strasbourg", "67", "Grand Est", 0.04), ("Montpellier", "34", "Occitanie", 0.04), ("Annecy", "74", "Auvergne-Rhône-Alpes", 0.02)]
PREFIX = ["Studio", "Agence", "Maison", "Atelier", "Collectif", "Bureau", "", "", ""]
WORDS = ["Lumen", "Nova", "Boréal", "Cobalt", "Ardoise", "Escale", "Méridien", "Sillage", "Opale", "Orbite", "Canopée", "Nacre",
         "Vertige", "Azimut", "Pollen", "Brume", "Ecume", "Fauve", "Ivoire", "Kairos", "Lagune", "Mistral", "Onyx", "Prisme",
         "Quartz", "Rivage", "Saphir", "Tamaris", "Velours", "Zénith", "Alizé", "Basalte", "Céleste", "Delta", "Éclat", "Faro",
         "Galet", "Horizon", "Indigo", "Jade", "Lueur", "Mosaïque", "Nectar", "Oasis", "Pivot", "Racine", "Satin", "Totem"]
SUFFIX = ["Social", "Digital", "Media", "Growth", "Studio", "Agency", "Lab", "Collective", "Creative", "", "", ""]
FIRST = ["Camille", "Julie", "Thomas", "Léa", "Nicolas", "Sarah", "Maxime", "Chloé", "Antoine", "Manon", "Hugo", "Inès", "Lucas",
         "Clara", "Mathieu", "Emma", "Romain", "Pauline", "Julien", "Marion", "Alexandre", "Elise", "Pierre", "Margaux", "Baptiste",
         "Lucie", "Quentin", "Anaïs", "Florian", "Laura", "Guillaume", "Charlotte", "Adrien", "Océane", "Simon", "Justine"]
LAST = ["Martin", "Bernard", "Dubois", "Lefèvre", "Moreau", "Laurent", "Garnier", "Faure", "Rousseau", "Blanc", "Guérin", "Muller",
        "Henry", "Roussel", "Nicolas", "Perrin", "Morin", "Mathieu", "Clément", "Gauthier", "Dumont", "Lopez", "Fontaine", "Chevalier",
        "Robin", "Masson", "Sanchez", "Noël", "Lemoine", "Leroux", "Marchand", "Duval", "Denis", "Benoît", "Renaud", "Picard"]
TITLES = [("Fondatrice & CEO", "Chief Executive Officer", RoleFamily.founder, Seniority.owner, 95),
          ("Fondateur", "Founder", RoleFamily.founder, Seniority.owner, 95),
          ("Gérant", "Managing Director", RoleFamily.executive, Seniority.owner, 92),
          ("Co-fondatrice", "Co-Founder", RoleFamily.founder, Seniority.owner, 90),
          ("Président", "President", RoleFamily.executive, Seniority.c_level, 88),
          ("Directrice associée", "Managing Partner", RoleFamily.executive, Seniority.c_level, 85),
          ("Head of Growth", "Head of Growth", RoleFamily.marketing, Seniority.head, 62),
          ("Responsable marketing", "Marketing Manager", RoleFamily.marketing, Seniority.manager, 45)]
OFFERS = ["Social media & contenu Instagram", "Campagnes Meta Ads et TikTok Ads", "Branding et identité visuelle", "SEO et contenu",
          "Influence marketing", "Création de sites Webflow", "Automatisation Instagram (ManyChat)", "Stratégie e-commerce Shopify"]
PATTERNS = ["{first}", "{first}.{last}", "{f}{last}"]


def _slug(s: str) -> str:
    return normalize_company_name(s).replace(" ", "-")


def _email(first: str, last: str, domain: str, pattern: str) -> str:
    f = normalize_person_name(first).replace(" ", "-")
    lst = normalize_person_name(last).replace(" ", "")
    local = pattern.replace("{first}", f).replace("{last}", lst).replace("{f}", f[0])
    return f"{local}@{domain}"


async def seed_workspace(s: AsyncSession, workspace_id: uuid.UUID, user_id: str | None, *, companies: int = 240, rng_seed: int = 7) -> dict[str, Any]:
    exists = await s.scalar(sa.select(List.id).where(List.workspace_id == workspace_id, List.name == "Demo · French agencies"))
    if exists:
        return {"seeded": False, "reason": "Demo data already present"}
    rnd = random.Random(rng_seed)
    now = datetime.now(UTC)
    defn = CampaignDefinition(
        name="Demo · FR marketing agencies", target_qualified_count=250,
        company_filters=CompanyFilters(industries=["marketing agency"], countries=["FR"], employee_range=EmployeeRange(min=2, max=30)),
        website_conditions=[KeywordCondition(type="keyword_any", terms=["instagram", "manychat"])],
        people_filters=PeopleFilters(titles=["Founder", "CEO", "Owner"]),
        exclusion=ExclusionSpec(mode="EXCLUDE_PREVIOUS_PEOPLE", previous_people=True),
    )
    main = List(workspace_id=workspace_id, name="Demo · French agencies", entity_type=EntityType.person,
                description="Seeded demo data (.example domains)", created_by=user_id)
    insta = List(workspace_id=workspace_id, name="Demo · Instagram agencies", entity_type=EntityType.person, created_by=user_id)
    hot = List(workspace_id=workspace_id, name="Demo · Hot leads", entity_type=EntityType.person, color="#8b8ff7", created_by=user_id)
    s.add_all([main, insta, hot])
    await s.flush()
    for lst in (main, insta, hot):
        s.add(SavedView(workspace_id=workspace_id, list_id=lst.id, entity_type=EntityType.person, name="All", is_default=True))
    s.add(SavedView(workspace_id=workspace_id, list_id=main.id, entity_type=EntityType.person, name="Verified only",
                    filters={"op": "and", "conditions": [{"field": "email_status", "operator": "eq", "value": "SAFE"}]}, position=1))
    s.add(SavedView(workspace_id=workspace_id, list_id=main.id, entity_type=EntityType.person, name="Founders",
                    filters={"op": "and", "conditions": [{"field": "role_family", "operator": "eq", "value": "founder"}]}, position=2))
    s.add(SavedView(workspace_id=workspace_id, list_id=main.id, entity_type=EntityType.person, name="Needs email",
                    filters={"op": "and", "conditions": [{"field": "email", "operator": "is_empty"}]}, position=3))
    camp = Campaign(workspace_id=workspace_id, name="Demo · FR marketing agencies", prompt="Find 250 marketing agencies in France with 2–30 employees. Founders or CEOs with professional emails. Website must mention Instagram or ManyChat. No leads I've already scraped.",
                    status=CampaignStatus.completed, stop_reason="Target reached: 250 qualified leads", definition=defn.model_dump(mode="json"),
                    definition_hash="demo", interpretation=[], target_qualified_count=250, target_list_id=main.id, mode=CampaignMode.people,
                    started_at=now - timedelta(hours=5), stopped_at=now - timedelta(hours=1), created_by=user_id)
    s.add(camp)
    await s.flush()
    main.source_campaign_id = camp.id
    cols = {
        "manychat": CustomColumn(workspace_id=workspace_id, list_id=None, name="ManyChat", slug="manychat", data_type=ColumnDataType.boolean,
                                 kind=ColumnKind.factual, entity_type=EntityType.company, resolver_type=ResolverType.CACHED_WEBSITE,
                                 instructions="Whether their site mentions ManyChat",
                                 configuration={"name": "ManyChat", "data_type": "boolean", "kind": "factual", "entity_type": "company", "resolver": "CACHED_WEBSITE", "strategy": "keyword", "keywords": ["manychat"], "cost_class": "FREE", "explanation": "Website keyword detection on existing crawl — no AI needed"},
                                 refresh_policy={"refresh_days": 30}, position=1, created_by=user_id),
        "instagram": CustomColumn(workspace_id=workspace_id, list_id=None, name="Offers Instagram", slug="offers_instagram", data_type=ColumnDataType.boolean,
                                  kind=ColumnKind.factual, entity_type=EntityType.company, resolver_type=ResolverType.AI_ON_CACHED_CONTENT,
                                  instructions="Whether the agency actually sells Instagram management",
                                  configuration={"name": "Offers Instagram", "data_type": "boolean", "kind": "factual", "entity_type": "company", "resolver": "AI_ON_CACHED_CONTENT", "strategy": "semantic_classifier", "concept": "offers Instagram management as a client service", "cost_class": "AI", "explanation": "Semantic classification of relevant website passages"},
                                  refresh_policy={"refresh_days": 30}, position=2, created_by=user_id),
        "offer": CustomColumn(workspace_id=workspace_id, list_id=None, name="Main offer", slug="main_offer", data_type=ColumnDataType.text,
                              kind=ColumnKind.factual, entity_type=EntityType.company, resolver_type=ResolverType.AI_ON_CACHED_CONTENT,
                              instructions="Their main offer", configuration={"name": "Main offer", "data_type": "text", "kind": "factual", "entity_type": "company", "resolver": "AI_ON_CACHED_CONTENT", "strategy": "ai_extraction", "cost_class": "AI", "explanation": "Extraction from services pages"},
                              refresh_policy={"refresh_days": 30}, position=3, created_by=user_id),
    }
    s.add_all(cols.values())
    await s.flush()
    stats = {"qualified": 0, "safe": 0, "emails": 0, "people": 0}
    person_ids: list[uuid.UUID] = []
    used: set[str] = set()
    for i in range(companies):
        while True:
            name = " ".join(x for x in (rnd.choice(PREFIX), rnd.choice(WORDS), rnd.choice(SUFFIX)) if x).strip()
            if name not in used:
                used.add(name)
                break
        domain = f"{_slug(name)}.example"
        city, dep, region, _ = rnd.choices(CITIES, weights=[c[3] for c in CITIES])[0]
        emp_min = rnd.choice([2, 3, 5, 6, 8, 10, 12, 15, 20])
        emp_max = emp_min + rnd.choice([3, 5, 9, 10, 15])
        status_roll = rnd.random()
        company = Company(
            workspace_id=workspace_id, name=name, normalized_name=normalize_company_name(name), domain=domain, normalized_domain=domain,
            website_url=f"https://{domain}/", description=f"{rnd.choice(OFFERS)} pour marques et PME à {city}.", country="FR",
            region=region, city=city, postal_code=f"{dep}0{rnd.randint(0, 9)}{rnd.randint(0, 9)}", industry="Marketing agency",
            category_raw="73.11Z Activités des agences de publicité", employee_min=emp_min, employee_max=emp_max, employee_confidence=0.9,
            phone=f"+33 {rnd.randint(1, 9)} {rnd.randint(10, 99)} {rnd.randint(10, 99)} {rnd.randint(10, 99)} {rnd.randint(10, 99)}",
            registry_source="fr_sirene", registry_id=str(800000000 + i * 37), website_status=WebsiteStatus.ok if status_roll > 0.04 else WebsiteStatus.unreachable,
            company_confidence=round(rnd.uniform(0.82, 0.98), 2), first_seen_at=now - timedelta(days=rnd.randint(0, 40)),
            last_crawled_at=now - timedelta(days=rnd.randint(0, 35)), times_discovered=1, first_campaign_id=camp.id, last_campaign_id=camp.id,
        )
        s.add(company)
        await s.flush()
        s.add(CompanyFieldObservation(workspace_id=workspace_id, company_id=company.id, field_name="name", value_json=name, source_type=SourceType.registry,
                                      source_key="fr_registry", source_url=f"https://annuaire-entreprises.data.gouv.fr/entreprise/{company.registry_id}",
                                      evidence=f"{name.upper()} — 73.11Z", confidence=0.95, observed_at=company.first_seen_at))
        s.add(CompanyFieldObservation(workspace_id=workspace_id, company_id=company.id, field_name="employee_count", value_json={"min": emp_min, "max": emp_max},
                                      source_type=SourceType.registry, source_key="fr_registry", evidence="Tranche d'effectif salarié (INSEE)", confidence=0.9,
                                      observed_at=company.first_seen_at))
        s.add(CompanyFieldObservation(workspace_id=workspace_id, company_id=company.id, field_name="description", value_json=company.description,
                                      source_type=SourceType.website, source_key="website", source_url=f"https://{domain}/", evidence=company.description,
                                      confidence=0.9, observed_at=company.last_crawled_at))
        # custom cells (company-level, includes non-terminal states for UI testing)
        mc_roll = rnd.random()
        ig_roll = rnd.random()
        mc_state = (CellStatus.success, "true" if mc_roll < 0.35 else "false") if mc_roll < 0.93 else ((CellStatus.unknown, "unknown") if mc_roll < 0.97 else (CellStatus.failed, None))
        ig_state = (CellStatus.success, "true" if ig_roll < 0.62 else "false") if ig_roll < 0.85 else ((CellStatus.unknown, "unknown") if ig_roll < 0.93 else ((CellStatus.running, None) if ig_roll < 0.97 else (CellStatus.queued, None)))
        for col_key, (st, dv) in (("manychat", mc_state), ("instagram", ig_state)):
            s.add(CustomFieldValue(
                workspace_id=workspace_id, column_id=cols[col_key].id, entity_type=EntityType.company, entity_id=company.id,
                value_json=None if dv in (None, "unknown") else dv == "true", display_value=dv, status=st,
                confidence=1.0 if col_key == "manychat" and st == CellStatus.success else (round(rnd.uniform(0.72, 0.95), 2) if st == CellStatus.success else None),
                source_id="website", source_url=f"https://{domain}/services" if st == CellStatus.success else None,
                evidence=("…automatisation de vos DMs Instagram avec ManyChat…" if dv == "true" and col_key == "manychat" else
                          "Nous gérons vos comptes Instagram : stratégie, contenus, community management." if dv == "true" else
                          "No mention on 8 crawled pages" if dv == "false" else None),
                resolver="keyword" if col_key == "manychat" else "ai_on_cached_content",
                error="Website unreachable" if st == CellStatus.failed else None, observed_at=now - timedelta(hours=rnd.randint(1, 200)) if st in (CellStatus.success, CellStatus.unknown) else None,
            ))
        s.add(CustomFieldValue(workspace_id=workspace_id, column_id=cols["offer"].id, entity_type=EntityType.company, entity_id=company.id,
                               value_json=company.description.split(" pour")[0], display_value=company.description.split(" pour")[0], status=CellStatus.success,
                               confidence=round(rnd.uniform(0.75, 0.93), 2), source_id="website", source_url=f"https://{domain}/services",
                               evidence=company.description, resolver="ai_on_cached_content", observed_at=now - timedelta(hours=rnd.randint(1, 100))))
        n_people = 1 if rnd.random() < 0.85 else 2
        for _ in range(n_people):
            first, last = rnd.choice(FIRST), rnd.choice(LAST)
            full = f"{first} {last}"
            orig, norm_title, fam, sen, power = rnd.choice(TITLES[:6] if rnd.random() < 0.85 else TITLES)
            conf = round(rnd.uniform(0.8, 0.97), 2)
            p = Person(workspace_id=workspace_id, company_id=company.id, first_name=first, last_name=last, full_name=full,
                       normalized_name=normalize_person_name(full), job_title=orig, normalized_title=norm_title, role_family=fam, seniority=sen,
                       decision_power=power, identity_confidence=conf, public_profile_url=f"https://www.linkedin.com/in/{_slug(full)}-{rnd.randint(100, 999)}" if rnd.random() < 0.55 else None,
                       first_seen_at=company.first_seen_at, times_discovered=1, first_campaign_id=camp.id, last_campaign_id=camp.id,
                       needs_review=conf < 0.85)
            s.add(p)
            await s.flush()
            src = rnd.choice(["team_card", "legal_notice", "registry"])
            s.add(PersonFieldObservation(workspace_id=workspace_id, person_id=p.id, field_name="full_name", value_json=full,
                                         source_type=SourceType.registry if src == "registry" else SourceType.website,
                                         source_key="fr_registry" if src == "registry" else "website",
                                         source_url=f"https://{domain}/equipe" if src == "team_card" else (f"https://{domain}/mentions-legales" if src == "legal_notice" else f"https://annuaire-entreprises.data.gouv.fr/entreprise/{company.registry_id}"),
                                         source_title="Notre équipe" if src == "team_card" else None,
                                         evidence=f"{full}\n{orig}" if src == "team_card" else (f"Directeur de la publication : {full}" if src == "legal_notice" else f"{last.upper()} {first} — {orig}"),
                                         confidence=conf, observed_at=company.first_seen_at))
            s.add(PersonFieldObservation(workspace_id=workspace_id, person_id=p.id, field_name="job_title", value_json=orig, source_type=SourceType.website,
                                         source_key="website", source_url=f"https://{domain}/equipe", evidence=f"{full} — {orig}", confidence=conf,
                                         observed_at=company.first_seen_at))
            roll = rnd.random()
            estatus = EmailStatus.SAFE if roll < 0.64 else EmailStatus.RISKY if roll < 0.78 else EmailStatus.CATCH_ALL if roll < 0.88 else EmailStatus.UNKNOWN if roll < 0.95 else EmailStatus.INVALID
            if rnd.random() < 0.93:
                pattern = rnd.choice(PATTERNS)
                addr = _email(first, last, domain, pattern)
                if await s.scalar(sa.select(Email.id).where(Email.workspace_id == workspace_id, Email.address == addr)) is None:
                    method = EmailDiscoveryMethod.published if rnd.random() < 0.25 else EmailDiscoveryMethod.known_pattern if rnd.random() < 0.5 else EmailDiscoveryMethod.permutation
                    oc = {EmailStatus.SAFE: 0.95, EmailStatus.RISKY: 0.74, EmailStatus.CATCH_ALL: 0.48, EmailStatus.UNKNOWN: 0.38, EmailStatus.INVALID: 0.0}[estatus]
                    e = Email(workspace_id=workspace_id, person_id=p.id, company_id=company.id, address=addr, local_part=addr.split("@")[0], domain=domain,
                              kind=EmailKind.person, discovery_method=method, pattern=pattern, source_url=f"https://{domain}/contact" if method == EmailDiscoveryMethod.published else None,
                              status=estatus, mx_valid=estatus != EmailStatus.INVALID,
                              smtp_result=SmtpResult.accepted if estatus == EmailStatus.SAFE else SmtpResult.rejected if estatus == EmailStatus.INVALID else SmtpResult.unknown,
                              catch_all=estatus == EmailStatus.CATCH_ALL, pattern_confidence=round(rnd.uniform(0.6, 0.97), 2), overall_confidence=oc,
                              is_primary=True, last_checked_at=now - timedelta(days=rnd.randint(0, 70)))
                    s.add(e)
                    await s.flush()
                    p.primary_email_id = e.id
                    stats["emails"] += 1
                    stats["safe"] += estatus == EmailStatus.SAFE
            score = rnd.randint(64, 97)
            s.add(QualificationScore(workspace_id=workspace_id, campaign_id=camp.id, company_id=company.id, person_id=p.id, icp_score=score,
                                     company_fit=round(rnd.uniform(0.75, 1), 2), person_fit=round(rnd.uniform(0.7, 1), 2), contactability=round(rnd.uniform(0.4, 1), 2),
                                     evidence_score=round(rnd.uniform(0.6, 0.95), 2), company_confidence=company.company_confidence, person_confidence=conf,
                                     email_confidence=0.95 if estatus == EmailStatus.SAFE else 0.6, overall_confidence=round(rnd.uniform(0.7, 0.95), 2),
                                     qualified=True, gate_results=[], weights={"company_fit": 35, "person_fit": 20, "contactability": 15, "evidence": 10},
                                     explanation=["Correct industry (marketing agency)", "Company size in range", "Mentions Instagram confirmed on website",
                                                  f"{norm_title} identified via {'official registry' if src == 'registry' else 'team page' if src == 'team_card' else 'legal notice'}",
                                                  f"{estatus.value.replace('_', '-')} email", "No intent signals collected (excluded from score)"]))
            person_ids.append(p.id)
            stats["people"] += 1
    await s.flush()
    # memberships + exposures
    rows = []
    for pid in person_ids:
        rows.append({"workspace_id": workspace_id, "list_id": main.id, "person_id": pid, "added_via": "campaign", "campaign_id": camp.id})
    insta_ids = person_ids[::3]
    hot_ids = person_ids[::11]
    rows += [{"workspace_id": workspace_id, "list_id": insta.id, "person_id": pid, "added_via": "ai"} for pid in insta_ids]
    rows += [{"workspace_id": workspace_id, "list_id": hot.id, "person_id": pid, "added_via": "manual"} for pid in hot_ids]
    await s.execute(sa.insert(ListMembership), rows)
    company_of = dict((await s.execute(sa.select(Person.id, Person.company_id).where(Person.id.in_(person_ids)))).all())
    expo = []
    for pid in person_ids:
        cid = company_of[pid]
        for et, eid in ((EntityType.person, pid), (EntityType.company, cid)):
            expo.append({"workspace_id": workspace_id, "entity_type": et, "entity_id": eid, "company_id": cid,
                         "exposure_type": ExposureType.DISCOVERED, "campaign_id": camp.id, "occurred_at": now - timedelta(hours=rnd.randint(1, 5))})
            expo.append({"workspace_id": workspace_id, "entity_type": et, "entity_id": eid, "company_id": cid,
                         "exposure_type": ExposureType.ADDED_TO_LIST, "campaign_id": camp.id, "list_id": main.id, "occurred_at": now - timedelta(hours=rnd.randint(1, 5))})
    await s.execute(sa.insert(LeadExposure), expo)
    s.add(CampaignStats(campaign_id=camp.id, raw_discovered=companies * 4, unique_new_companies=companies * 3, duplicates=companies // 2,
                        excluded_previous=companies // 5, companies_evaluated=int(companies * 2.6), companies_matched=int(companies * 1.3),
                        people_found=int(companies * 1.2), emails_found=stats["emails"], emails_safe=stats["safe"], emails_accepted=stats["safe"],
                        qualified=len(person_ids), rejected=int(companies * 1.8), cost_usd=Decimal("2.84")))
    return {"seeded": True, "companies": companies, "people": stats["people"], "lists": 3, "campaign_id": str(camp.id), "list_id": str(main.id)}
