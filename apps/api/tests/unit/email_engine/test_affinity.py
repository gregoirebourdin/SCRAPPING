"""Name-affinity guard: an address is attributed to a person only when it matches their identity."""

from __future__ import annotations

import pytest

from scout.email.affinity import ATTRIBUTE_MIN, STRONG, name_affinity


@pytest.mark.parametrize(
    ("local", "expected_min"),
    [
        ("john.smith", 0.95),
        ("johnsmith", 0.95),
        ("smith.john", 0.95),
        ("jsmith", 0.85),
        ("j.smith", 0.85),
    ],
)
def test_full_name_patterns_are_strong(local: str, expected_min: float) -> None:
    a = name_affinity(local, "John", "Smith")
    assert a.score >= expected_min
    assert a.score >= STRONG


def test_someone_elses_first_name_is_never_attributed() -> None:
    a = name_affinity("marie", "John", "Smith")
    assert a.score < ATTRIBUTE_MIN
    a2 = name_affinity("marie.dupont", "John", "Smith")
    assert a2.score < ATTRIBUTE_MIN


def test_role_mailbox_is_not_personal() -> None:
    assert name_affinity("contact", "John", "Smith").score < ATTRIBUTE_MIN
    assert name_affinity("info", "John", "Smith").score < ATTRIBUTE_MIN


def test_first_name_only_depends_on_domain_convention() -> None:
    weak = name_affinity("john", "John", "Smith")
    strong = name_affinity("john", "John", "Smith", dominant_pattern="{first}", dominant_share=0.9)
    assert ATTRIBUTE_MIN <= weak.score < STRONG
    assert strong.score >= STRONG


def test_first_name_shared_with_colleague_is_ambiguous() -> None:
    a = name_affinity(
        "john", "John", "Smith", dominant_pattern="{first}", dominant_share=0.9, colleagues=[("John", "Doe")]
    )
    assert a.score < ATTRIBUTE_MIN


def test_address_of_a_colleague_is_rejected() -> None:
    a = name_affinity("sarah.martin", "John", "Smith", colleagues=[("Sarah", "Martin")])
    assert a.score == 0.0
    assert "Sarah Martin" in a.reason


def test_initials_only_is_weak() -> None:
    assert name_affinity("js", "John", "Smith").score < ATTRIBUTE_MIN
    assert (
        name_affinity("js", "John", "Smith", dominant_pattern="{f}{l}", dominant_share=0.8).score
        >= ATTRIBUTE_MIN
    )


def test_accents_and_particles() -> None:
    assert name_affinity("helene.delafontaine", "Hélène", "de la Fontaine").score >= STRONG
    assert name_affinity("jean-pierre.dupont", "Jean-Pierre", "Dupont").score >= STRONG


def test_free_text_containing_both_names() -> None:
    a = name_affinity("smith.john.paris", "John", "Smith")
    assert a.score >= STRONG


def test_unrelated_local_part() -> None:
    assert name_affinity("sales2024", "John", "Smith").score < ATTRIBUTE_MIN
