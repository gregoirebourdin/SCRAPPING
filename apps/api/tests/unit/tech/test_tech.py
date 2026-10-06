"""Built-in technology fingerprints from realistic home pages + the Go service client."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from scout.config import get_settings
from scout.crawl.parser import parse_html
from scout.tech.builtin import KNOWN_TECHNOLOGIES, SIGNATURES, canonical_tech_name, detect
from scout.tech.service import detect_via_service

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "html"


def _detect(name: str, url: str, headers: dict[str, str] | None = None) -> dict:
    html = (FIX / name).read_text(encoding="utf-8")
    page = parse_html(html, url, is_home=True)
    return {t.name: t for t in detect(headers or {}, page.head_html, page.content_text, page.links)}


def test_signature_catalog_size_and_aliases():
    assert len({s.name for s in SIGNATURES}) >= 70
    assert KNOWN_TECHNOLOGIES["shopify"] == "Shopify"
    assert KNOWN_TECHNOLOGIES["facebook pixel"] == "Meta Pixel"
    assert KNOWN_TECHNOLOGIES["sendinblue"] == "Brevo"
    assert KNOWN_TECHNOLOGIES["nextjs"] == "Next.js"
    assert KNOWN_TECHNOLOGIES["ga4"] == "Google Analytics"
    assert canonical_tech_name("Next JS") == "Next.js"
    assert canonical_tech_name("WooCommerce") == "WooCommerce"
    assert canonical_tech_name("Totally Unknown CMS") is None


def test_shopify_store():
    techs = _detect(
        "tech/shopify.html",
        "https://maison-celeste.fr/",
        {"x-shopid": "62345678", "server": "cloudflare", "cf-ray": "8a1b"},
    )
    assert techs["Shopify"].confidence >= 0.9 and techs["Shopify"].category == "Ecommerce"
    assert "cdn.shopify.com" in techs["Shopify"].evidence or "x-shopid" in techs["Shopify"].evidence
    assert techs["Google Analytics"].version == "GA4"
    for name in ("Klaviyo", "Stripe", "Cloudflare"):
        assert name in techs, name
    assert "WordPress" not in techs


def test_wordpress_elementor_site():
    techs = _detect(
        "agence_lumiere/home.html",
        "https://agence-lumiere.fr/",
        {"server": "nginx/1.24.0", "x-powered-by": "PHP/8.2.12"},
    )
    assert techs["WordPress"].version == "6.6.2" and techs["WordPress"].confidence >= 0.95
    assert techs["Elementor"].version == "3.24.4"
    for name in ("Google Tag Manager", "Meta Pixel", "HubSpot", "Google Fonts", "jQuery", "YouTube"):
        assert name in techs, name
    assert techs["Nginx"].version == "1.24.0" and techs["PHP"].version == "8.2.12"
    assert "Shopify" not in techs and "Next.js" not in techs


def test_hubspot_meta_pixel_manychat():
    techs = _detect("tech/hubspot_meta_manychat.html", "https://growthly.com/")
    for name in (
        "HubSpot",
        "HubSpot Forms",
        "Meta Pixel",
        "ManyChat",
        "LinkedIn Insight Tag",
        "Hotjar",
        "Axeptio",
        "Calendly",
    ):
        assert name in techs, name
    assert "fbq('init'" in techs["Meta Pixel"].evidence or "fbevents.js" in techs["Meta Pixel"].evidence
    assert techs["Calendly"].confidence == 0.8  # from a link, not a script


def test_nextjs_app():
    techs = _detect(
        "tech/nextjs.html",
        "https://fluxo.io/",
        {"x-powered-by": "Next.js", "x-vercel-id": "cdg1::abc", "server": "Vercel"},
    )
    assert techs["Next.js"].confidence >= 0.9
    assert techs["React"].evidence == "implied by Next.js"
    for name in ("Vercel", "Google Tag Manager", "Plausible", "Segment", "Intercom"):
        assert name in techs, name
    assert "WordPress" not in techs


def test_detect_handles_empty_input():
    assert detect({}, None, None, None) == []


@pytest.fixture
def service_env(monkeypatch):
    monkeypatch.setenv("TECH_SERVICE_URL", "http://verifier.internal:8080")
    monkeypatch.setenv("VERIFIER_SERVICE_TOKEN", "s3cret")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("TECH_SERVICE_URL")
    monkeypatch.delenv("VERIFIER_SERVICE_TOKEN")
    get_settings.cache_clear()


async def test_service_client(service_env):
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://verifier.internal:8080/v1/tech").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "Shopify", "categories": ["Ecommerce"], "version": ""},
                    {"name": "Facebook Pixel", "categories": ["Advertising"], "version": "2.9"},
                ],
            )
        )
        techs = await detect_via_service(
            "https://maison-celeste.fr/", {"server": "cloudflare"}, "<html></html>"
        )
        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer s3cret"
    assert techs is not None
    assert [(t.name, t.version) for t in techs] == [("Shopify", None), ("Meta Pixel", "2.9")]


async def test_service_errors_and_unconfigured(service_env, monkeypatch):
    with respx.mock() as mock:
        mock.post("http://verifier.internal:8080/v1/tech").mock(return_value=httpx.Response(500))
        assert await detect_via_service("https://x.fr/", {}, "") is None
        mock.post("http://verifier.internal:8080/v1/tech").mock(side_effect=httpx.ConnectError("down"))
        assert await detect_via_service("https://x.fr/", {}, "") is None
    monkeypatch.delenv("TECH_SERVICE_URL")
    monkeypatch.delenv("VERIFIER_SERVICE_URL", raising=False)
    get_settings.cache_clear()
    assert await detect_via_service("https://x.fr/", {}, "") is None
    monkeypatch.setenv("TECH_SERVICE_URL", "http://verifier.internal:8080")
