"""derive_status: every rule branch, and catch-all never SAFE for a guessed address."""

from __future__ import annotations

import pytest

from scout.db.enums import EmailDiscoveryMethod as M
from scout.db.enums import EmailStatus as S
from scout.db.enums import SmtpResult as R
from scout.email.status import derive_status

from .fakes import cand, vr

PUBLISHED = cand(method=M.published, pattern="{first}.{last}", pc=0.95)
GUESS = cand(method=M.permutation, pc=0.4)
STRONG = cand(method=M.known_pattern, pc=0.97, samples=4)
STRONG_INFERRED = cand(method=M.inferred_pattern, pc=0.85, samples=2)
ONE_SAMPLE = cand(method=M.known_pattern, pc=0.9, samples=1)


# ---- 1. INVALID ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "v",
    [vr(syntax=False, mx=None), vr(disposable=True, mx=None), vr(mx=False)],
    ids=["syntax", "disposable", "no_mx_no_a"],
)
def test_invalid_definitive(v):
    assert derive_status(PUBLISHED, v) == (S.INVALID, 0.0)
    assert derive_status(GUESS, v) == (S.INVALID, 0.0)


def test_invalid_when_smtp_rejected_even_if_published():
    assert derive_status(PUBLISHED, vr(smtp=R.rejected, catch_all=False)) == (S.INVALID, 0.02)
    assert derive_status(GUESS, vr(smtp=R.rejected, catch_all=True))[0] == S.INVALID


# ---- 2. free provider / role address used as a person email --------------------------------


def test_free_provider_is_risky_at_most_half():
    free_pub = cand("marie.dupont@gmail.com", method=M.published, pc=0.6)
    assert derive_status(free_pub, vr(free=True)) == (S.RISKY, 0.5)
    status, conf = derive_status(cand("marie.dupont@gmail.com", pc=0.4), vr(free=True))
    assert status == S.RISKY and conf <= 0.45
    assert derive_status(GUESS, vr(free=True, smtp=R.accepted, catch_all=False)) == (S.RISKY, 0.5)


def test_role_address_as_person_email_is_risky():
    role = cand("contact@agence-x.fr", method=M.published, pattern=None, pc=0.95)
    assert derive_status(role, vr(role=True)) == (S.RISKY, 0.45)
    assert derive_status(cand("contact@agence-x.fr", method=M.import_, pattern=None), vr(role=True)) == (
        S.RISKY,
        0.3,
    )


def test_role_flag_ignored_for_name_derived_candidates_and_company_emails():
    # Local part generated from the person's name: a role-list collision is not a role mailbox.
    assert derive_status(GUESS, vr(role=True, smtp=R.accepted, catch_all=False)) == (S.SAFE, 0.95)
    company = cand("contact@agence-x.fr", method=M.published, pattern=None)
    assert derive_status(company, vr(role=True), for_person=False) == (S.SAFE, 0.92)


# ---- 3–4. SAFE ------------------------------------------------------------------------------


def test_safe_smtp_accepted_not_catch_all():
    assert derive_status(GUESS, vr(smtp=R.accepted, catch_all=False)) == (S.SAFE, 0.95)


def test_safe_published_with_mx_without_smtp():
    assert derive_status(PUBLISHED, vr(smtp=R.not_attempted)) == (S.SAFE, 0.92)
    assert derive_status(PUBLISHED, vr(smtp=R.timeout, catch_all=False)) == (S.SAFE, 0.92)


def test_published_needs_valid_mx_for_safe():
    assert derive_status(PUBLISHED, vr(mx=None)) == (S.RISKY, 0.7)


# ---- 5. catch-all ---------------------------------------------------------------------------


@pytest.mark.parametrize("smtp", list(R))
@pytest.mark.parametrize("method", [M.permutation, M.known_pattern, M.inferred_pattern, M.import_, M.user])
def test_catch_all_never_safe_for_guessed(smtp, method):
    status, _ = derive_status(cand(method=method, pc=0.99, samples=10), vr(smtp=smtp, catch_all=True))
    assert status != S.SAFE


def test_catch_all_guess_is_catch_all_scaled_by_pattern_confidence():
    lo = derive_status(cand(pc=0.0), vr(smtp=R.accepted, catch_all=True))
    hi = derive_status(cand(pc=1.0), vr(smtp=R.accepted, catch_all=True))
    assert lo == (S.CATCH_ALL, 0.35) and hi == (S.CATCH_ALL, 0.55)
    assert derive_status(ONE_SAMPLE, vr(smtp=R.accepted, catch_all=True))[0] == S.CATCH_ALL


def test_catch_all_strong_pattern_or_published_is_risky():
    status, conf = derive_status(STRONG, vr(smtp=R.accepted, catch_all=True))
    assert status == S.RISKY and 0.6 <= conf <= 0.8 and conf == 0.8
    status, conf = derive_status(STRONG_INFERRED, vr(smtp=R.unknown, catch_all=True))
    assert status == S.RISKY and conf == 0.6
    assert derive_status(PUBLISHED, vr(smtp=R.accepted, catch_all=True)) == (S.RISKY, 0.8)


# ---- 6. accepted, catch-all unknown -------------------------------------------------------------


def test_accepted_but_catch_all_unknown_is_risky():
    status, conf = derive_status(GUESS, vr(smtp=R.accepted, catch_all=None))
    assert status == S.RISKY and 0.7 <= conf <= 0.8


# ---- 7. SMTP unavailable / inconclusive ------------------------------------------------------


@pytest.mark.parametrize("smtp", [R.not_attempted, R.timeout, R.blocked, R.unknown])
def test_unavailable_smtp(smtp):
    status, conf = derive_status(GUESS, vr(smtp=smtp))
    assert status == S.UNKNOWN and conf == pytest.approx(0.3 + 0.2 * 0.4)
    status, conf = derive_status(STRONG, vr(smtp=smtp))
    assert status == S.RISKY and 0.6 <= conf <= 0.75
    assert derive_status(ONE_SAMPLE, vr(smtp=smtp))[0] == S.UNKNOWN


def test_unknown_confidence_bounds_and_dns_inconclusive():
    assert derive_status(cand(pc=0.0), vr())[1] == 0.3
    assert derive_status(cand(pc=1.0), vr())[1] == 0.5
    assert derive_status(GUESS, vr(mx=None))[0] == S.UNKNOWN
