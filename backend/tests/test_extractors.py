"""Unit tests for the pure (network-free) building blocks."""

from __future__ import annotations

from app.discovery.bing import decode_bing_url
from app.discovery.listicle import extract_listicle_links, is_listicle
from app.discovery.queries import build_queries
from app.extract.contacts import decode_cf_email, extract_emails_from_html
from app.extract.tech import detect_tech, primary_platform
from app.fetch.crawler import classify_url
from app.qualify.language import detect_language
from app.util.text import parse_html
from app.util.urls import canonicalize, is_blocked_domain, registrable_domain, social_network


def test_registrable_domain() -> None:
    assert registrable_domain("https://www.foo.co.uk/x?y=1") == "foo.co.uk"
    assert registrable_domain("http://blog.example.com") == "example.com"
    assert registrable_domain("https://jane.mykajabi.com/offers/x") == "jane.mykajabi.com"
    assert registrable_domain("https://www.example.com") == "example.com"


def test_canonicalize_strips_tracking() -> None:
    assert canonicalize("HTTPS://Example.com/a/?utm_source=x&b=2#frag") == "https://example.com/a?b=2"


def test_blocklist_and_social() -> None:
    assert is_blocked_domain("facebook.com")
    assert is_blocked_domain("business.linkedin.com")
    assert not is_blocked_domain("sodinimarketing.com")
    assert social_network("https://www.instagram.com/foo") == "instagram"
    assert social_network("https://foo.com") is None


def test_bing_url_decode() -> None:
    u = "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9zb2RpbmltYXJrZXRpbmcuY29tLw&ntb=1"
    assert decode_bing_url(u) == "https://sodinimarketing.com/"


def test_cloudflare_email_decode() -> None:
    # "hello@example.com" encoded with key 0x5a
    key = 0x5A
    enc = bytes([key] + [ord(c) ^ key for c in "hello@example.com"]).hex()
    assert decode_cf_email(enc) == "hello@example.com"


def test_email_extraction_and_filters() -> None:
    html = '<a href="mailto:Hello@Agency.com?subject=hi">mail</a> <img src="x@2x.png"> <span>team [at] agency [dot] com</span>'
    text = "Contact: hello@agency.com or team [at] agency [dot] com; not u@example.com"
    found = dict(extract_emails_from_html(html, text, "https://agency.com"))
    assert "hello@agency.com" in found
    assert "team@agency.com" in found
    assert "u@example.com" not in found
    assert all(not e.endswith(".png") for e in found)


def test_parse_html_blocks_and_meta() -> None:
    html = """<html lang="en"><head><title>Acme | Meta Ads for Coaches</title><meta name="description" content="We scale coaches">
    <script type="application/ld+json">{"@type":"Organization","name":"Acme Agency"}</script></head>
    <body><h1>We help <b>coaches</b> grow</h1><p>Para one.</p><script>var x=1</script><a href="/about">About us</a></body></html>"""
    p = parse_html(html, "https://acme.com")
    assert p.title.startswith("Acme")
    assert p.meta_description == "We scale coaches"
    assert p.jsonld[0]["name"] == "Acme Agency"
    assert "We help coaches grow" in p.text and "var x" not in p.text
    assert ("/about", "About us") in p.links
    assert p.headings == ["We help coaches grow"]


def test_language_detection() -> None:
    en = "We help coaches and course creators scale their business with Facebook ads and high converting funnels. Book a call today."
    code, conf = detect_language(en * 3, "en")
    assert code == "en" and conf > 0.8
    fr = "Nous aidons les coachs et les formateurs à développer leur activité grâce à la publicité Facebook et aux tunnels de vente."
    code, _ = detect_language(fr * 3, "")
    assert code == "fr"


def test_classify_url() -> None:
    assert classify_url("https://a.com/case-studies/jane")[0] == "case_studies"
    assert classify_url("https://a.com/about-us")[0] == "about"
    assert classify_url("https://a.com/contact")[0] == "contact"
    assert classify_url("https://a.com/blog/post")[0] == "blog"
    assert classify_url("https://a.com/x", "Our Clients")[0] == "clients"


def test_tech_fingerprints() -> None:
    html = '<script src="https://cdn.kajabi.com/x.js"></script><script src="https://connect.facebook.net/en_US/fbevents.js"></script>'
    tech = detect_tech(html)
    assert "kajabi" in tech and "meta pixel" in tech
    assert primary_platform(tech) == "kajabi"


def test_listicle_detection_and_links() -> None:
    assert is_listicle("Top 15 Facebook Ads Agencies for Coaches in 2026")
    assert not is_listicle("Sodini Marketing - Meta Ads Agency for Coaches")
    html = """<html><body><h2>1. Acme Growth</h2><a href="https://acmegrowth.com/">Visit website</a>
    <h2>2. Beta Media</h2><a href="https://betamedia.io/?utm_source=list">Beta Media</a>
    <a href="https://www.facebook.com/x">fb</a><a href="https://listsite.com/other">internal</a></body></html>"""
    p = parse_html(html, "https://listsite.com/top-agencies")
    links = extract_listicle_links(p, "listsite.com")
    doms = [l.domain for l in links]
    assert "acmegrowth.com" in doms and "betamedia.io" in doms
    assert "facebook.com" not in doms and "listsite.com" not in doms


def test_query_matrix() -> None:
    qs = build_queries(["custom query"])
    assert qs[0].text == "custom query"
    texts = [q.text for q in qs]
    assert len(texts) == len(set(t.lower() for t in texts))
    assert any("for coaches" in t for t in texts)
    assert len(texts) > 800
