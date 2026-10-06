"""Column planner: deterministic EN/FR rules first, AI planner only for ambiguous asks."""

from __future__ import annotations

import pytest

from scout.db.enums import ColumnDataType, ColumnKind, CostClass, EntityType, PageType, ResolverType
from scout.enrich.planner import describe_plan, plan_column

CASES = [
    # (name, instruction, strategy)
    ("Mentions ManyChat", "Add a column showing whether their site mentions ManyChat", "keyword"),
    ("ManyChat", None, "keyword"),
    ("Instagram management", "Add whether the agency actually offers Instagram management", "semantic_classifier"),
    ("Shopify", "Add whether they use Shopify", "tech_detection"),
    ("Instagram", "Find their Instagram account", "social_profile"),
    ("Target customer", "Add their main target customer", "ai_extraction"),
    ("Summary", "Add a one-sentence summary", "generated_text"),
    ("Outreach angle", "Add a personalized outreach angle", "generated_text"),
    ("Latest job", "Find their latest job posting", "web_research"),
    ("Podcasts", "Find the founder's podcast appearances", "web_research"),
    ("Testimonials", "Add whether their homepage has testimonials", "website_field"),
    ("CTA", "Find their main CTA", "website_field"),
    ("Pricing", "Find their pricing", "ai_extraction"),
    # French
    ("ManyChat FR", "Ajoute une colonne indiquant si leur site mentionne ManyChat", "keyword"),
    ("Gestion Instagram", "Est-ce que l'agence propose vraiment la gestion Instagram ?", "semantic_classifier"),
    ("Instagram FR", "Trouve leur compte Instagram", "social_profile"),
    ("Shopify FR", "Ajoute s'ils utilisent Shopify", "tech_detection"),
    ("Résumé", "Ajoute un résumé en une phrase", "generated_text"),
    ("Accroche", "Une accroche personnalisée", "generated_text"),
    ("Offre d'emploi", "Trouve leur dernière offre d'emploi", "web_research"),
    ("Levée", "Leur dernière levée de fonds", "web_research"),
    ("Cible", "Leur client cible principal", "ai_extraction"),
    ("Témoignages", "Est-ce que leur page d'accueil a des témoignages ?", "website_field"),
    ("Tarifs", "Trouve leurs tarifs", "ai_extraction"),
    # canonical / other
    ("City", None, "deterministic_field"),
    ("Company size", None, "deterministic_field"),
    ("SIREN", "Find their SIREN number", "regex"),
    ("CEO email", "Find the CEO's email", "composite"),
    ("Phone", "Find their phone number", "website_field"),
    ("Booking link", "Find their booking link", "website_field"),
]


@pytest.mark.parametrize(("name", "instruction", "strategy"), CASES)
async def test_deterministic_strategies(name, instruction, strategy, local_ai):
    plan = await plan_column(name, instruction)
    assert plan.strategy == strategy, (name, instruction, plan)
    assert plan.explanation


async def test_keyword_plan_details_and_no_ai_call(fake_ai):
    plan = await plan_column("ManyChat", "Add a column showing whether their site mentions ManyChat")
    assert fake_ai.calls == []
    assert plan.strategy == "keyword"
    assert plan.resolver == ResolverType.CACHED_WEBSITE
    assert plan.cost_class == CostClass.FREE
    assert plan.data_type == ColumnDataType.boolean
    assert "ManyChat" in plan.keywords and "Many Chat" in plan.keywords
    assert plan.explanation == "Website keyword detection on existing crawl — no AI needed"


async def test_strong_rules_never_call_ai(fake_ai):
    for name, instruction, _ in CASES:
        await plan_column(name, instruction)
    assert fake_ai.calls == []


async def test_semantic_plan_fields(local_ai):
    plan = await plan_column("Instagram mgmt", "Add whether the agency actually offers Instagram management")
    assert plan.resolver == ResolverType.AI_ON_CACHED_CONTENT and plan.cost_class == CostClass.AI
    assert plan.data_type == ColumnDataType.boolean
    assert plan.concept and "Instagram management" in plan.concept
    assert "Instagram" in plan.keywords
    assert {PageType.services, PageType.home, PageType.about, PageType.case_studies} <= set(plan.input_sources)


async def test_generated_and_research_plans(local_ai):
    summary = await plan_column("Summary", "Add a one-sentence summary")
    assert summary.kind == ColumnKind.generated and summary.cost_class == CostClass.AI
    assert summary.resolver == ResolverType.AI_ON_CACHED_CONTENT and summary.field == "summary"
    research = await plan_column("Jobs", "Find their latest job posting")
    assert research.resolver == ResolverType.AI_WEB_RESEARCH and research.cost_class == CostClass.WEB_SEARCH
    assert research.refresh_days == 14


async def test_social_tech_field_details(local_ai):
    social = await plan_column("IG", "Find their Instagram account")
    assert social.field == "instagram" and social.data_type == ColumnDataType.url and social.cost_class == CostClass.FREE
    tech = await plan_column("Shopify", "Add whether they use Shopify")
    assert tech.technologies == ["Shopify"] and tech.cost_class == CostClass.CHEAP
    testimonials = await plan_column("T", "Add whether their homepage has testimonials")
    assert testimonials.field == "testimonials" and testimonials.input_sources == [PageType.home]
    assert (await plan_column("CTA", "Find their main CTA")).field == "cta"
    status = await plan_column("Email status", None)
    assert status.strategy == "deterministic_field" and status.entity_type == EntityType.person


async def test_keyword_rule_beats_service_wording(local_ai):
    plan = await plan_column("x", 'Does their website mention "Google Partner" or "Meta Business Partner"?')
    assert plan.strategy == "keyword" and plan.keywords == ["Google Partner", "Meta Business Partner"]
    tech = await plan_column("x", "Whether their site mentions Shopify")
    assert tech.strategy == "keyword"  # grounded search / tech detection never used for a mention question


async def test_ambiguous_ask_uses_ai_planner(fake_ai):
    fake_ai.on("ColumnPlanDraft", lambda prompt: {
        "strategy": "ai_extraction", "data_type": "number", "concept": "Number of office locations",
        "keywords": ["offices", "agences"], "input_sources": ["about", "contact"], "explanation": "Stated on site",
    })
    plan = await plan_column("Offices", "Office locations count")
    assert len(fake_ai.calls) == 1
    assert plan.strategy == "ai_extraction" and plan.data_type == ColumnDataType.number
    assert plan.input_sources == [PageType.about, PageType.contact]


async def test_ambiguous_ask_without_ai_falls_back(local_ai):
    plan = await plan_column("Offices", "Office locations count")
    assert plan.strategy == "ai_extraction"
    boolean = await plan_column("Eco", "Eco-friendly packaging", data_type=ColumnDataType.boolean)
    assert boolean.strategy == "semantic_classifier"


async def test_ai_planner_failure_falls_back(fake_ai):
    plan = await plan_column("Offices", "Office locations count")  # no handler registered → planner error
    assert plan.strategy == "ai_extraction"


async def test_describe_plan_labels(local_ai):
    card = describe_plan(await plan_column("ManyChat", None))
    assert card["resolver_label"] == "Website keyword match"
    assert card["sources_label"].startswith("Cached website")
    assert card["cost_label"].startswith("Free")
    research = describe_plan(await plan_column("Jobs", "Find their latest job posting"))
    assert research["sources_label"] == "Web search with cited sources"
    assert research["cost_label"].startswith("Web search")
