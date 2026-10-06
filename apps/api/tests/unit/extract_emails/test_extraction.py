"""Email extraction: labelled corpus precision/recall + unit tests of the de-obfuscation helpers."""

from __future__ import annotations

import pytest

from scout.crawl.parser import parse_html
from scout.extract import email_extract as ee
from tests.unit.extract_emails.bench import current_extract, run, score
from tests.unit.extract_emails.corpus import CORPUS, cf

# Documented limitation: spaced obfuscations without any corroborating context are not accepted.
KNOWN_MISSES = {"spaced-list-no-cue"}


def test_benchmark_precision_and_recall():
    results = run()
    cur, old = results["current"], results["legacy"]
    assert cur.fp == 0 and cur.precision == 1.0, cur.errors
    assert cur.recall >= 0.95, cur.errors
    assert cur.recall > old.recall and cur.precision > old.precision


@pytest.mark.parametrize("case", [c for c in CORPUS if c.kind == "decoy"], ids=lambda c: c.id)
def test_decoys_yield_nothing(case):
    assert current_extract(case) == set()


@pytest.mark.parametrize(
    "case", [c for c in CORPUS if c.kind == "positive" and c.id not in KNOWN_MISSES], ids=lambda c: c.id
)
def test_positives_are_found_exactly(case):
    assert current_extract(case) == set(case.expected)


def test_known_misses_stay_precise():
    s = score("current", current_extract, [c for c in CORPUS if c.id in KNOWN_MISSES])
    assert s.fp == 0


def test_cloudflare_decoding():
    assert ee.decode_cfemail(cf("jean@acme-test.fr", 0x42)) == "jean@acme-test.fr"
    assert ee.decode_cfemail(cf("jean@acme-test.fr", 0x42).upper()) == "jean@acme-test.fr"
    assert (
        ee.decode_cfemail("zz") is None and ee.decode_cfemail("42") is None and ee.decode_cfemail("") is None
    )
    assert ee.cfemail_from_href(f"/cdn-cgi/l/email-protection#{cf('rh@acme-test.fr')}") == "rh@acme-test.fr"
    assert ee.cfemail_from_href("/contact#team") is None


def test_mailto_parsing():
    assert ee.emails_from_mailto("mailto:a@acme-test.fr;b@acme-test.fr?subject=x") == [
        "a@acme-test.fr",
        "b@acme-test.fr",
    ]
    assert ee.emails_from_mailto("MAILTO:?to=c%40acme-test.fr&bcc=d@acme-test.fr&body=e@acme-test.fr") == [
        "c@acme-test.fr",
        "d@acme-test.fr",
    ]
    assert ee.emails_from_mailto("mailto:jean%2Bnews@acme-test.fr") == ["jean+news@acme-test.fr"]
    assert ee.emails_from_mailto("mailto:") == []


def test_clean_email_requires_a_public_suffix_and_filters_noise():
    assert ee.clean_email("Jean@Acme-Test.FR") == "jean@acme-test.fr"
    assert ee.clean_email("jean@acme-test.frtel") is None
    assert ee.clean_email("rf.emca@tnopud.naej") is None
    assert ee.clean_email("jean@intranet.local") is None
    assert ee.clean_email("logo@2x.png") is None
    assert ee.clean_email("con​tact@acme-test.fr") == "contact@acme-test.fr"


def test_weak_spaced_forms_need_corroboration():
    assert ee.emails_from_text("jean at nova-studio dot io") == []
    assert ee.emails_from_text("Écrivez-nous : jean at nova-studio dot io") == ["jean@nova-studio.io"]
    assert ee.emails_from_text("jean at nova-studio dot io", site_domain="nova-studio.io") == [
        "jean@nova-studio.io"
    ]
    assert ee.emails_from_text("JEAN AT NOVA-STUDIO DOT IO") == ["jean@nova-studio.io"]
    assert ee.emails_from_text("Contact: paul@nova-studio.io, jean at nova-studio dot io") == [
        "paul@nova-studio.io",
        "jean@nova-studio.io",
    ]
    # Pronouns / prose words are never local parts, even with a cue word.
    assert ee.emails_from_text("Email us at nova-studio dot io") == []
    assert ee.emails_from_text("Contact me at the office dot com") == []
    # The cue must be on the same line, shortly before.
    assert ee.emails_from_text("Contact\njean at nova-studio dot io") == []


def test_json_islands_are_decoded_before_scanning():
    raw = '{"a":"\\u003cb\\u003eteam@acme-test.fr\\u003c/b\\u003e","n":[1,{"m":"mailto:x@acme-test.fr"}]}'
    assert ee.emails_from_json_island(raw) == ["team@acme-test.fr", "x@acme-test.fr"]
    assert ee.emails_from_json_island("not json @") == []
    assert ee.emails_from_json_island('{"a": "' + "x" * (ee.MAX_JSON_BYTES + 1) + '@b.fr"}') == []


def test_jsonld_emails_at_any_depth():
    objs = [{"@type": "Organization", "founder": [{"@type": "Person", "email": ["a@acme-test.fr", "n/a"]}]}]
    assert ee.emails_from_jsonld(objs) == ["a@acme-test.fr"]


def test_reversed_text_requires_bidi_css():
    html = (
        '<p><span style="direction:rtl;unicode-bidi:bidi-override">rf.tset-emca@ofni</span></p>'
        '<p><span style="direction:rtl">rf.tset-emca@semaj</span></p>'  # rtl alone does not reverse
    )
    assert parse_html(f"<html><body>{html}</body></html>", "https://acme-test.fr/").emails == [
        "info@acme-test.fr"
    ]


def test_provenance_order_links_first_then_text():
    html = (
        "<html><body><p>Écrire : marie(at)acme-test.fr</p>"
        '<a href="mailto:contact@acme-test.fr">Contact</a></body></html>'
    )
    assert parse_html(html, "https://acme-test.fr/").emails == ["contact@acme-test.fr", "marie@acme-test.fr"]
