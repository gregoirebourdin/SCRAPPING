"""Labelled email-extraction corpus: HTML snippets → addresses a careful human reads on the page.

Ground truth rule: an address is expected only when it is unambiguously published (visible text,
mailto links, Cloudflare-protected links, structured data). Decoys must yield nothing: prose that merely
contains "at" / "dot", asset file names, package versions, handles, placeholders, reversed text without
the CSS that makes it readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SITE = "https://acme-test.fr/contact"


def cf(address: str, key: int = 0x5A) -> str:
    """Cloudflare email-protection encoding (first byte = XOR key)."""
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in address)


@dataclass(frozen=True)
class Case:
    id: str
    html: str
    expected: frozenset[str] = field(default_factory=frozenset)
    url: str = SITE
    kind: str = "positive"  # positive | decoy


def P(id_: str, body: str, *expected: str, url: str = SITE) -> Case:
    return Case(id_, f"<html><body>{body}</body></html>", frozenset(expected), url, "positive")


def D(id_: str, body: str, url: str = SITE) -> Case:
    return Case(id_, f"<html><body>{body}</body></html>", frozenset(), url, "decoy")


CORPUS: list[Case] = [
    # ---- plain text & mailto ------------------------------------------------------------------
    P("plain", "<p>Contact : contact@acme-test.fr</p>", "contact@acme-test.fr"),
    P("plain-trailing-dot", "<p>Écrivez à jean@acme-test.fr.</p>", "jean@acme-test.fr"),
    P("plain-parens", "<p>Marie Martin (marie.martin@acme-test.fr)</p>", "marie.martin@acme-test.fr"),
    P(
        "mailto-subject",
        '<a href="mailto:Jean.Dupont@Acme-Test.fr?subject=Devis%20urgent">Écrire</a>',
        "jean.dupont@acme-test.fr",
    ),
    P(
        "mailto-percent",
        '<a href="mailto:marie%2Emartin%40acme-test.fr">Marie</a>',
        "marie.martin@acme-test.fr",
    ),
    P(
        "mailto-multi",
        '<a href="mailto:a.bernard@acme-test.fr,c.durand@acme-test.fr">Équipe</a>',
        "a.bernard@acme-test.fr",
        "c.durand@acme-test.fr",
    ),
    P(
        "mailto-query-to",
        '<a href="mailto:?to=sales@acme-test.fr&amp;subject=Hi">Sales</a>',
        "sales@acme-test.fr",
    ),
    P(
        "mailto-cc",
        '<a href="mailto:ceo@acme-test.fr?cc=assistant@acme-test.fr">CEO</a>',
        "ceo@acme-test.fr",
        "assistant@acme-test.fr",
    ),
    P("mailto-plus", '<a href="mailto:jean%2Bpresse@acme-test.fr">Presse</a>', "jean+presse@acme-test.fr"),
    P(
        "mailto-entities",
        '<a href="mailto:&#105;&#110;&#102;&#111;&#64;acme-test.fr">écrire</a>',
        "info@acme-test.fr",
    ),
    # ---- Cloudflare email protection ----------------------------------------------------------
    P(
        "cf-span",
        f'<p>Mail : <span class="__cf_email__" data-cfemail="{cf("paul.leroy@acme-test.fr")}">[email&#160;protected]</span></p>',
        "paul.leroy@acme-test.fr",
    ),
    P(
        "cf-link",
        f'<a href="/cdn-cgi/l/email-protection#{cf("rh@acme-test.fr", 0x13)}">[email&#160;protected]</a>',
        "rh@acme-test.fr",
    ),
    P(
        "cf-link-absolute",
        f'<a href="https://acme-test.fr/cdn-cgi/l/email-protection?ref=1#{cf("presse@acme-test.fr", 0x7F)}">x</a>',
        "presse@acme-test.fr",
    ),
    D(
        "cf-garbage",
        '<span data-cfemail="zz12">[email&#160;protected]</span><a href="/cdn-cgi/l/email-protection#ab">x</a>',
    ),
    # ---- bracketed obfuscations ---------------------------------------------------------------
    P("bracket-at-dot", "<p>jean [at] acme-test [dot] fr</p>", "jean@acme-test.fr"),
    P("paren-at", "<p>marie(at)acme-test.fr</p>", "marie@acme-test.fr"),
    P("curly-arobase-point", "<p>paul{arobase}acme-test{point}fr</p>", "paul@acme-test.fr"),
    P("bracket-symbol", "<p>lea [@] acme-test.fr</p>", "lea@acme-test.fr"),
    P("bracket-co-uk", "<p>ventes[at]domain-test[dot]co[dot]uk</p>", "ventes@domain-test.co.uk"),
    P("paren-de", "<p>Kontakt: kontakt (at) firma-test.de</p>", "kontakt@firma-test.de"),
    # ---- spaced word obfuscations (need context) ----------------------------------------------
    P("upper-at-dot", "<p>SUPPORT AT ACME-TEST DOT FR</p>", "support@acme-test.fr"),
    P("spaced-cue", "<p>Email: bob at nova-studio dot io</p>", "bob@nova-studio.io"),
    P("spaced-site-domain", "<p>Questions ? jean at acme-test dot fr</p>", "jean@acme-test.fr"),
    P("spaced-literal-dot-cue", "<p>Contact: jean at acme-test.fr</p>", "jean@acme-test.fr"),
    P("spaced-at-symbol-cue", "<p>E-mail : hello @ acme-test.fr</p>", "hello@acme-test.fr"),
    P(
        "spaced-list-no-cue",
        "<p>Sales: anna at nova-studio dot io — Support: bob at nova-studio dot io</p>",
        "anna@nova-studio.io",
        "bob@nova-studio.io",
    ),
    # ---- encodings & invisible characters -----------------------------------------------------
    P("entities", "<p>jean&#64;acme-test&#46;fr</p>", "jean@acme-test.fr"),
    P("word-joiner", "<p>Email : jean⁠@acme-test.fr</p>", "jean@acme-test.fr"),
    P("soft-hyphen", "<p>marie.mar­tin@acme-test.fr</p>", "marie.martin@acme-test.fr"),
    P("zero-width-joiner", "<p>sup‍port@acme-test.fr</p>", "support@acme-test.fr"),
    P("fullwidth-at", "<p>paul＠acme-test.fr</p>", "paul@acme-test.fr"),
    P("comment-split", "<p>jean<!-- x -->@<!-- y -->acme-test.fr</p>", "jean@acme-test.fr"),
    P(
        "hidden-decoy-span",
        '<p>jean<span style="display:none">REMOVE</span>@acme-test.fr</p>',
        "jean@acme-test.fr",
    ),
    P(
        "glued-inline",
        "<p><a href='#'>contact@acme-test.fr</a><span>Tél : 01 23 45 67 89</span></p>",
        "contact@acme-test.fr",
    ),
    P(
        "glued-digits",
        "<p><span>direction@acme-test.fr</span><span>0123456789</span></p>",
        "direction@acme-test.fr",
    ),
    # ---- CSS reversed text ----------------------------------------------------------------------
    P(
        "bidi-span",
        '<p><span style="unicode-bidi:bidi-override; direction: rtl;">rf.tset-emca@ofni</span></p>',
        "info@acme-test.fr",
    ),
    P("bdo-rtl", '<p><bdo dir="rtl">rf.tset-emca@semaj</bdo></p>', "james@acme-test.fr"),
    D("reversed-without-css", "<p>rf.tset-emca@ofni et rf.emca@tnopud.naej</p>"),
    # ---- structured data -------------------------------------------------------------------------
    Case(
        "jsonld-nested",
        '<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization",'
        '"name":"Acme","contactPoint":{"@type":"ContactPoint","email":"support@acme-test.fr"}}</script></head>'
        "<body><p>Acme</p></body></html>",
        frozenset({"support@acme-test.fr"}),
    ),
    Case(
        "jsonld-mailto",
        '<html><head><script type="application/ld+json">{"@type":"Organization","email":"mailto:hello@acme-test.fr"}'
        "</script></head><body><p>Acme</p></body></html>",
        frozenset({"hello@acme-test.fr"}),
    ),
    Case(
        "next-data",
        '<html><body><div id="__next"></div><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"contact":{"label":"Écrivez-nous","value":"team@acme-test.fr"}}}}</script></body></html>',
        frozenset({"team@acme-test.fr"}),
    ),
    # ---- decoys ----------------------------------------------------------------------------------
    D("prose-venue", "<p>Meet us at the venue dot com for the launch.</p>"),
    D("prose-find-us", "<p>Find us at acme-test dot com</p>"),
    D("prose-contact-us", "<p>Contact us at acme-test.fr or call us.</p>"),
    D("prose-dot-com-boom", "<p>We were born at the dot com boom.</p>"),
    D("prose-scale", "<p>Our platform works at scale dot io and beyond.</p>"),
    D("prose-released", "<p>Version 2.0 released at acme-test.fr last week.</p>"),
    D("prose-tickets", "<p>Tickets @ eventbrite.com</p>"),
    D("assets", "<p>Logo: logo@2x.png, icon@3x.webp, Download photo@2x.jpg</p>"),
    D("css-asset", "<style>.bg{background:url(img@2x.png)}</style><p>Bienvenue</p>"),
    D("package-versions", "<p>npm install lodash@4.17.21 react@18.2.0</p>"),
    D("handle", "<p>Follow @acmetest on Twitter and @media queries</p>"),
    D("placeholders", "<p>Format : prenom.nom@domaine.fr, user@example.com, votre.email@votredomaine.fr</p>"),
    D("placeholder-obfuscated", "<p>Email: info at example dot com</p>"),
    D("sentry-dsn", "<p>https://abc123def4567890@o450.ingest.sentry.io/123</p>"),
    D("glued-unknown-tld", "<p>Écrire à jean@acme-test.frtel</p>"),
    D("price-at", "<p>5 at 10.00 each, meet at noon dot</p>"),
    D(
        "jsonld-empty-email",
        '<script type="application/ld+json">{"@type":"Organization","email":"n/a"}</script><p>x</p>',
    ),
]
