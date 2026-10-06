"""Confidence engine, candidate generation (≤ 3, evidence first) and deep-path conclusions."""

from __future__ import annotations

from scout.db.enums import EmailEvidenceSource, EmailStatus, MailProvider, SmtpHealthState, SmtpResult
from scout.email.affinity import name_affinity
from scout.email.confidence import Evidence, assess
from scout.email.contracts import (
    DomainIntel,
    DomainProbeResult,
    ObservedEmail,
    PatternStat,
    RcptVerdict,
    SessionOutcome,
)
from scout.email.engine import (
    MAX_CANDIDATES,
    build_candidates,
    conclude_after_probe,
    deep_would_help,
    evaluate,
    pick,
)
from scout.email.stats import StatRow, precision


def _intel(**kw: object) -> DomainIntel:
    base: dict[str, object] = {
        "domain": "acme.com",
        "provider": MailProvider.google_workspace,
        "mx_hosts": ["aspmx.l.google.com"],
        "has_mx": True,
        "accepts_mail": True,
    }
    base.update(kw)
    return DomainIntel(**base)  # type: ignore[arg-type]


def _ev(**kw: object) -> Evidence:
    base: dict[str, object] = {
        "address": "john.smith@acme.com",
        "resolver": "domain_pattern",
        "base_probability": 0.95,
        "base_detail": "Domain convention",
        "affinity": name_affinity("john.smith", "John", "Smith"),
        "has_mx": True,
        "accepts_mail": True,
    }
    base.update(kw)
    return Evidence(**base)  # type: ignore[arg-type]


# ---- confidence engine -----------------------------------------------------------------------


def test_confirmed_pattern_with_mx_is_likely_safe_without_smtp() -> None:
    v = assess(_ev())
    assert v.status == EmailStatus.LIKELY_SAFE
    assert v.confidence >= 0.8
    assert {s.name for s in v.signals} >= {"base", "name_affinity", "mx_valid"}


def test_published_on_own_site_is_safe() -> None:
    v = assess(_ev(resolver="published_website", base_probability=0.93, published_on_domain=True))
    assert v.status == EmailStatus.SAFE


def test_smtp_accept_on_non_catch_all_is_safe_but_not_on_catch_all() -> None:
    proven = assess(
        _ev(
            base_probability=0.41,
            resolver="permutation",
            smtp=SmtpResult.accepted,
            catch_all=False,
            smtp_state=SmtpHealthState.HEALTHY,
        )
    )
    assert proven.status == EmailStatus.SAFE
    catch_all = assess(
        _ev(
            base_probability=0.41,
            resolver="permutation",
            smtp=SmtpResult.accepted,
            catch_all=True,
            smtp_state=SmtpHealthState.HEALTHY,
        )
    )
    assert catch_all.status == EmailStatus.CATCH_ALL
    assert catch_all.status != EmailStatus.SAFE


def test_unhealthy_smtp_never_concludes_negatively() -> None:
    v = assess(
        _ev(
            base_probability=0.41,
            resolver="permutation",
            smtp=SmtpResult.blocked,
            smtp_state=SmtpHealthState.BLOCKED,
        )
    )
    assert v.status != EmailStatus.INVALID
    assert any(s.name == "smtp_unavailable" for s in v.signals)


def test_temporary_answer_is_temporary_unknown_while_retrying() -> None:
    v = assess(
        _ev(base_probability=0.41, resolver="permutation", smtp=SmtpResult.temporary, retry_pending=True)
    )
    assert v.status == EmailStatus.TEMPORARY_UNKNOWN
    final = assess(
        _ev(base_probability=0.41, resolver="permutation", smtp=SmtpResult.temporary, retry_pending=False)
    )
    assert final.status not in (EmailStatus.INVALID, EmailStatus.TEMPORARY_UNKNOWN)


def test_rejected_user_unknown_is_invalid() -> None:
    v = assess(_ev(smtp=SmtpResult.rejected, catch_all=False, smtp_state=SmtpHealthState.HEALTHY))
    assert v.status == EmailStatus.INVALID


def test_affinity_guard_blocks_attribution() -> None:
    v = assess(_ev(address="marie@acme.com", affinity=name_affinity("marie", "John", "Smith")))
    assert v.confidence == 0.0
    assert any(s.name == "affinity_guard" for s in v.signals)


def test_no_mx_is_invalid() -> None:
    assert assess(_ev(accepts_mail=False, has_mx=False)).status == EmailStatus.INVALID


def test_empirical_precision_moves_the_prior() -> None:
    snap = {
        ("resolver", "github"): StatRow(
            attempts=200, correct=20, wrong=180, inconclusive=0, latency_ms_total=0
        )
    }
    assert precision("resolver", "github", 0.82, snap) < 0.3
    assert precision("resolver", "github", 0.82, None) == 0.82


# ---- candidates -----------------------------------------------------------------------------


def test_observed_email_for_person_comes_first_and_others_are_ignored() -> None:
    intel = _intel(
        observed=[
            ObservedEmail(
                "marie@acme.com", "marie", EmailEvidenceSource.website, first_name="Marie", last_name="Curie"
            ),
            ObservedEmail(
                "john.smith@acme.com",
                "john.smith",
                EmailEvidenceSource.website,
                source_url="https://acme.com/team",
            ),
        ],
        patterns=[PatternStat("{first}.{last}", share=0.9, confidence=0.93, samples=8)],
    )
    cands = build_candidates("John", "Smith", intel)
    assert cands[0].address == "john.smith@acme.com"
    assert cands[0].published_on_domain is True
    assert all(c.address != "marie@acme.com" for c in cands)
    assert len(cands) <= MAX_CANDIDATES


def test_github_addresses_are_pattern_evidence_only() -> None:
    intel = _intel(
        observed=[
            ObservedEmail(
                "john.smith@acme.com",
                "john.smith",
                EmailEvidenceSource.github,
                first_name="John",
                last_name="Smith",
            )
        ]
    )
    cands = build_candidates("John", "Smith", intel)
    assert all(c.resolver != "github" for c in cands)


def test_confirmed_convention_gives_one_or_two_candidates() -> None:
    intel = _intel(
        patterns=[
            PatternStat("{first}.{last}", share=0.96, confidence=0.95, samples=8),
            PatternStat("{f}{last}", share=0.04, confidence=0.05, samples=1),
        ]
    )
    cands = build_candidates("John", "Smith", intel)
    assert 1 <= len(cands) <= 2
    assert cands[0].address == "john.smith@acme.com"
    verdicts = evaluate(cands, intel)
    best = pick(verdicts)
    assert best is not None and best.status == EmailStatus.LIKELY_SAFE
    assert not deep_would_help(best, intel, SmtpHealthState.HEALTHY)


def test_unknown_domain_falls_back_to_at_most_three_priors() -> None:
    cands = build_candidates("John", "Smith", _intel(), company_size_max=200, country="FR")
    assert 1 <= len(cands) <= MAX_CANDIDATES
    best = pick(evaluate(cands, _intel()))
    assert best is not None and best.status in (EmailStatus.RISKY, EmailStatus.UNKNOWN)


def test_catch_all_domain_is_final_without_smtp() -> None:
    intel = _intel(catch_all=True)
    best = pick(evaluate(build_candidates("John", "Smith", intel), intel))
    assert best is not None and best.status == EmailStatus.CATCH_ALL
    assert not deep_would_help(best, intel, SmtpHealthState.HEALTHY)


# ---- deep-path conclusion -------------------------------------------------------------------


def _probe(
    verdicts: dict[str, SmtpResult],
    *,
    catch_all: bool | None = False,
    session: SessionOutcome = SessionOutcome.ok,
) -> DomainProbeResult:
    return DomainProbeResult(
        domain="acme.com",
        session=session,
        verdicts={
            a: RcptVerdict(
                a, r, 250 if r == SmtpResult.accepted else 550 if r == SmtpResult.rejected else 450
            )
            for a, r in verdicts.items()
        },
        catch_all=catch_all,
        probes=len(verdicts) + 2,
    )


def test_deep_conclusion_picks_the_accepted_candidate() -> None:
    intel = _intel()
    cands = [c.as_dict() for c in build_candidates("John", "Smith", intel, country="FR")]
    addrs = [c["address"] for c in cands]
    assert addrs[0] == "john.smith@acme.com"
    probe = _probe({addrs[0]: SmtpResult.accepted, addrs[1]: SmtpResult.rejected})
    verdict, retry = conclude_after_probe(
        cands, intel, probe, smtp_state=SmtpHealthState.HEALTHY, attempt=1, max_attempts=3
    )
    assert verdict is not None and verdict.address == addrs[0]
    assert verdict.status == EmailStatus.SAFE
    assert retry is False


def test_smtp_proven_first_name_only_is_likely_safe_not_safe() -> None:
    intel = _intel()
    cands = [c.as_dict() for c in build_candidates("John", "Smith", intel, country="FR")]
    first_only = next(c["address"] for c in cands if c["address"] == "john@acme.com")
    probe = _probe(
        {
            c["address"]: (SmtpResult.accepted if c["address"] == first_only else SmtpResult.rejected)
            for c in cands
        }
    )
    verdict, _ = conclude_after_probe(
        cands, intel, probe, smtp_state=SmtpHealthState.HEALTHY, attempt=1, max_attempts=3
    )
    assert verdict is not None and verdict.address == first_only
    assert verdict.status == EmailStatus.LIKELY_SAFE


def test_greylisting_schedules_a_retry_then_never_invalid() -> None:
    intel = _intel()
    cands = [c.as_dict() for c in build_candidates("John", "Smith", intel)]
    probe = _probe({c["address"]: SmtpResult.temporary for c in cands}, catch_all=None)
    verdict, retry = conclude_after_probe(
        cands, intel, probe, smtp_state=SmtpHealthState.HEALTHY, attempt=1, max_attempts=3
    )
    assert retry is True
    assert verdict is not None and verdict.status == EmailStatus.TEMPORARY_UNKNOWN
    final, retry2 = conclude_after_probe(
        cands, intel, probe, smtp_state=SmtpHealthState.HEALTHY, attempt=3, max_attempts=3
    )
    assert retry2 is False
    assert final is not None and final.status != EmailStatus.INVALID


def test_blocked_infrastructure_concludes_without_negative_verdict() -> None:
    intel = _intel()
    cands = [c.as_dict() for c in build_candidates("John", "Smith", intel)]
    verdict, retry = conclude_after_probe(
        cands, intel, None, smtp_state=SmtpHealthState.BLOCKED, attempt=1, max_attempts=3
    )
    assert retry is False
    assert verdict is not None and verdict.status != EmailStatus.INVALID
