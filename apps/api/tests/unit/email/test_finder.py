"""find_email waterfall with deterministic verifiers (fixture manifest, no network)."""

from __future__ import annotations

from scout.db.enums import EmailDiscoveryMethod as M
from scout.db.enums import EmailStatus as S
from scout.db.enums import SmtpResult as R
from scout.email import finder
from scout.email.finder import find_email, matches_person, published_candidates
from scout.email.verifier import set_verifier

from .fakes import fixture_verifier

LARGE = {"company_size_max": 5000, "country": "FR"}


async def test_published_address_first_and_safe_without_smtp():
    v = fixture_verifier({"agence-x.fr": {"mx": True, "smtp": False}})
    f = await find_email(
        first="Marie",
        last="Dupont",
        domain="agence-x.fr",
        published=[
            ("contact@agence-x.fr", "https://agence-x.fr/contact"),
            ("Marie.Dupont@agence-x.fr", "https://agence-x.fr/equipe"),
        ],
        verifier=v,
        **LARGE,
    )
    assert f.address == "marie.dupont@agence-x.fr" and f.status == S.SAFE and f.overall_confidence == 0.92
    assert f.method == M.published and f.pattern == "{first}.{last}"
    assert f.source_url == "https://agence-x.fr/equipe" and f.reason is None
    assert v.calls == ["marie.dupont@agence-x.fr"] and f.candidates_tried == v.calls


async def test_published_for_other_people_or_roles_is_ignored():
    v = fixture_verifier()
    f = await find_email(
        first="Marie",
        last="Dupont",
        domain="agence-x.fr",
        published=[
            ("contact@agence-x.fr", None),
            ("jean.martin@agence-x.fr", None),
            ("md@agence-x.fr", None),
        ],
        verifier=v,
        **LARGE,
    )
    assert f.method == M.permutation and f.address == "marie.dupont@agence-x.fr" and f.status == S.SAFE
    assert v.calls == ["marie.dupont@agence-x.fr"]


async def test_stops_at_first_safe():
    v = fixture_verifier(
        {"studio-y.fr": {"mx": True, "catch_all": False, "mailboxes": ["mdupont@studio-y.fr"]}}
    )
    f = await find_email(first="Marie", last="Dupont", domain="studio-y.fr", verifier=v, **LARGE)
    assert f.status == S.SAFE and f.overall_confidence == 0.95 and f.address == "mdupont@studio-y.fr"
    assert v.calls[-1] == "mdupont@studio-y.fr" and len(v.calls) == 2
    assert [a.status for a in f.attempts] == [S.INVALID, S.SAFE]


async def test_catch_all_probes_exactly_once():
    v = fixture_verifier()
    f = await find_email(first="Marie", last="Dupont", domain="catchall.fr", verifier=v, **LARGE)
    assert len(v.calls) == 1
    assert f.status == S.CATCH_ALL and f.address == "marie.dupont@catchall.fr"
    assert 0.35 <= f.overall_confidence <= 0.55 and f.reason == finder.REASON_CATCH_ALL


async def test_catch_all_with_strong_known_pattern_is_risky():
    v = fixture_verifier()
    f = await find_email(
        first="Marie",
        last="Dupont",
        domain="catchall.fr",
        known_patterns=[("{f}{last}", 0.925, 3)],
        verifier=v,
        **LARGE,
    )
    assert v.calls == ["mdupont@catchall.fr"]
    assert f.status == S.RISKY and f.method == M.known_pattern and 0.6 <= f.overall_confidence <= 0.8
    assert f.reason is None  # RISKY is accepted by default


async def test_smtp_unavailable_returns_best_candidate_without_looping():
    v = fixture_verifier({"nosmtp.fr": {"mx": True, "smtp": False}})
    f = await find_email(first="Marie", last="Dupont", domain="nosmtp.fr", verifier=v, **LARGE)
    assert v.calls == ["marie.dupont@nosmtp.fr"]
    assert f.address == "marie.dupont@nosmtp.fr" and f.status == S.UNKNOWN
    assert 0.3 <= f.overall_confidence <= 0.5
    assert f.reason == finder.REASON_SMTP_UNAVAILABLE
    assert f.verification.smtp_result == R.not_attempted


async def test_smtp_unavailable_with_strong_pattern_is_risky():
    v = fixture_verifier({"nosmtp.fr": {"mx": True, "smtp": False}})
    f = await find_email(
        first="Marie",
        last="Dupont",
        domain="nosmtp.fr",
        known_patterns=[("{first}", 0.9625, 4)],
        verifier=v,
        **LARGE,
    )
    assert v.calls == ["marie@nosmtp.fr"] and f.status == S.RISKY and f.reason is None


async def test_no_mx():
    v = fixture_verifier()
    f = await find_email(first="Marie", last="Dupont", domain="nomx.fr", verifier=v, **LARGE)
    assert f.address is None and f.status == S.INVALID and f.reason == finder.REASON_NO_MX
    assert len(v.calls) == 1


async def test_all_candidates_rejected_respects_max_probes():
    v = fixture_verifier({"empty.fr": {"mx": True, "catch_all": False, "mailboxes": []}})
    f = await find_email(first="Marie", last="Dupont", domain="empty.fr", verifier=v, max_probes=4, **LARGE)
    assert len(v.calls) == 4 and len(set(v.calls)) == 4
    assert f.address is None and f.status == S.INVALID and f.reason == finder.REASON_REJECTED
    assert f.candidates_tried == v.calls


async def test_no_domain_and_no_names():
    v = fixture_verifier()
    f = await find_email(first="Marie", last="Dupont", domain=None, verifier=v)
    assert f.address is None and f.reason == finder.REASON_NO_DOMAIN and v.calls == []
    f = await find_email(first=None, last=None, domain="agence-x.fr", verifier=v)
    assert f.reason == finder.REASON_NO_CANDIDATE and v.calls == []


async def test_published_free_provider_beats_unverifiable_guess():
    v = fixture_verifier({"gmail.com": {"mx": True, "smtp": False}, "nosmtp.fr": {"mx": True, "smtp": False}})
    f = await find_email(
        first="Marie",
        last="Dupont",
        domain="nosmtp.fr",
        published=[("marie.dupont75@gmail.com", "https://nosmtp.fr/")],
        verifier=v,
        **LARGE,
    )
    assert v.calls == ["marie.dupont75@gmail.com", "marie.dupont@nosmtp.fr"]
    assert f.address == "marie.dupont75@gmail.com" and f.status == S.RISKY and f.overall_confidence == 0.5


async def test_accept_controls_reason():
    v = fixture_verifier()
    f = await find_email(
        first="Marie",
        last="Dupont",
        domain="catchall.fr",
        known_patterns=[("{f}{last}", 0.925, 3)],
        verifier=v,
        accept={S.SAFE},
        **LARGE,
    )
    assert f.status == S.RISKY and f.reason == finder.REASON_RISKY


async def test_default_verifier_comes_from_get_verifier():
    v = fixture_verifier()
    set_verifier(v)
    try:
        f = await find_email(first="Jean", last="Martin", domain="agence-x.fr", **LARGE)
    finally:
        set_verifier(None)
    assert f.status == S.SAFE and v.calls == ["jean.martin@agence-x.fr"]


def test_matches_person_heuristics():
    assert matches_person("Marie", "Dupont", "marie.dupont") == (True, "{first}.{last}")
    assert matches_person("Marie", "Dupont", "dupont.marie.paris") == (True, None)
    assert matches_person("Marie", "Dupont", "md") == (False, "{f}{l}")  # initials are too weak
    assert matches_person("Marie", "Dupont", "mathieu.dupont") == (False, None)
    assert matches_person("Marie", "Dupont", "contact") == (False, None)


def test_published_candidates_order_and_filters():
    out = published_candidates(
        "Marie",
        "Dupont",
        "agence-x.fr",
        [
            ("marie.dupont@gmail.com", None),
            ("marie.dupont@agence-x.fr", "u1"),
            ("marie.dupont@other-co.fr", None),
            ("marie@paris.agence-x.fr", "u2"),
            ("MARIE.DUPONT@agence-x.fr", "dup"),
        ],
    )
    assert [c.address for c in out] == [
        "marie.dupont@agence-x.fr",
        "marie@paris.agence-x.fr",
        "marie.dupont@gmail.com",
    ]
    assert out[0].pattern_confidence == finder.PUBLISHED_ON_DOMAIN_CONFIDENCE
    assert out[-1].pattern_confidence == finder.PUBLISHED_FREE_CONFIDENCE
