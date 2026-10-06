"""INSEE headcount bands ↔ numeric ranges."""

from __future__ import annotations

import pytest

from scout.discovery.employee_bands import (
    band_label,
    overlaps,
    parse_range,
    range_for_tranche,
    tranches_for_range,
)


def test_tranches_for_range_overlapping_bands() -> None:
    assert tranches_for_range(2, 30) == ["01", "02", "03", "11", "12"]
    assert tranches_for_range(10, 19) == ["11"]
    assert tranches_for_range(1000, 1000) == ["42"]
    assert tranches_for_range(None, None) == []


def test_non_employers_only_when_min_is_zero() -> None:
    assert tranches_for_range(0, 5)[:2] == ["NN", "00"]
    assert tranches_for_range(None, 2) == ["NN", "00", "01"]
    assert "NN" not in tranches_for_range(1, 5) and "00" not in tranches_for_range(1, 5)


def test_open_ended_range() -> None:
    assert tranches_for_range(500, None) == ["41", "42", "51", "52", "53"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [("12", (20, 49)), ("01", (1, 2)), ("53", (10000, None)), ("NN", (0, 0)), ("XX", (None, None)), (None, (None, None))],
)
def test_range_for_tranche(code: str | None, expected: tuple[int | None, int | None]) -> None:
    assert range_for_tranche(code) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2-30", (2, 30)),
        ("2 – 30 employees", (2, 30)),
        ("2 à 30 salariés", (2, 30)),
        ("between 5 and 20", (5, 20)),
        ("10+", (10, None)),
        ("more than 10", (11, None)),
        ("<50", (None, 49)),
        ("≤ 50", (None, 50)),
        ("up to 50", (None, 50)),
        ("1k+", (1000, None)),
        ("1,000-5,000", (1000, 5000)),
        ("12", (12, 12)),
        (12, (12, 12)),
        ("lots", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_range(text: str | int | None, expected: tuple[int | None, int | None]) -> None:
    assert parse_range(text) == expected


def test_labels_and_overlap() -> None:
    assert band_label("12") == "20–49"
    assert band_label("53") == "10,000+"
    assert overlaps((20, 49), 2, 30) and not overlaps((50, 99), 2, 30)
    assert overlaps((None, None), 2, 30)
