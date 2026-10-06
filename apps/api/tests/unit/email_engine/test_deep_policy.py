"""Deep-path policy: decide vs confirm, lean candidate sets and the single expansion round."""

from __future__ import annotations

from scout.db.enums import EmailStatus, MailProvider, SmtpHealthState, SmtpResult
from scout.email.contracts import (
    DomainIntel,
    DomainProbeResult,
    PatternStat,
    RcptVerdict,
    SessionOutcome,
    Verdict,
)
from scout.email.engine import (
    EXPANSION_ROUND,
    MAX_CANDIDATES,
    Candidate,
    build_candidates,
    deep_candidates,
    deep_plan,
    expand_candidates,
    should_expand,
)

H = SmtpHealthState


def _intel(**kw: object) -> DomainIntel:
    base: dict[str, object] = {
        "domain": "acme.com",
        "provider": MailProvider.ovh,
        "mx_hosts": ["mx1.mail.ovh.net"],
        "has_mx": True,
        "accepts_mail": True,
    }
    base.update(kw)
    return DomainIntel(**base)  # type: ignore[arg-type]


def _v(
    status: EmailStatus, resolver: str = "domain_pattern", address: str = "john.smith@acme.com"
) -> Verdict:
    return Verdict(address=address, status=status, confidence=0.8, resolver=resolver)


def _probe(verdicts: dict[str, SmtpResult], *, catch_all: bool | None = False) -> DomainProbeResult:
    return DomainProbeResult(
        domain="acme.com",
        session=SessionOutcome.ok,
        verdicts={
            a: RcptVerdict(a, r, 550 if r == SmtpResult.rejected else 250) for a, r in verdicts.items()
        },
        catch_all=catch_all,
    )


def test_ambiguous_verdicts_go_to_the_deep_path_and_final_ones_do_not() -> None:
    intel = _intel()
    assert deep_plan(_v(EmailStatus.RISKY), intel, H.HEALTHY, smtp_capable=True) == "decide"
    assert deep_plan(_v(EmailStatus.UNKNOWN), intel, H.UNKNOWN, smtp_capable=True) == "decide"
    assert deep_plan(_v(EmailStatus.SAFE), intel, H.HEALTHY, smtp_capable=True) == "none"
    assert deep_plan(_v(EmailStatus.INVALID), intel, H.HEALTHY, smtp_capable=True) == "none"


def test_no_deep_path_without_a_usable_smtp_path_or_on_catch_all() -> None:
    assert deep_plan(_v(EmailStatus.RISKY), _intel(), H.BLOCKED, smtp_capable=True) == "none"
    assert deep_plan(_v(EmailStatus.RISKY), _intel(), H.HEALTHY, smtp_capable=False) == "none"
    assert deep_plan(_v(EmailStatus.RISKY), _intel(catch_all=True), H.HEALTHY, smtp_capable=True) == "none"
    assert (
        deep_plan(_v(EmailStatus.RISKY), _intel(accepts_mail=False), H.HEALTHY, smtp_capable=True) == "none"
    )


def test_likely_safe_on_a_thin_pattern_is_confirmed_in_the_background() -> None:
    thin = _intel(patterns=[PatternStat("{first}.{last}", 1.0, 0.84, samples=1)])
    strong = _intel(patterns=[PatternStat("{first}.{last}", 1.0, 0.95, samples=6, successes=2)])
    assert deep_plan(_v(EmailStatus.LIKELY_SAFE), thin, H.HEALTHY, smtp_capable=True) == "confirm"
    assert deep_plan(_v(EmailStatus.LIKELY_SAFE), strong, H.HEALTHY, smtp_capable=True) == "none"
    # a published address is not a pattern guess: nothing to confirm
    assert (
        deep_plan(_v(EmailStatus.LIKELY_SAFE, "published_website"), thin, H.HEALTHY, smtp_capable=True)
        == "none"
    )


def test_deep_candidates_are_lean() -> None:
    intel = _intel(patterns=[PatternStat("{first}.{last}", 1.0, 0.84, samples=1)])
    cands = build_candidates("John", "Smith", intel)
    assert cands[0].resolver == "domain_pattern"
    assert [c.address for c in deep_candidates(cands, "decide")] == [cands[0].address]
    confirm = deep_candidates(cands, "confirm", _v(EmailStatus.LIKELY_SAFE, address=cands[0].address))
    assert [c.address for c in confirm] == [cands[0].address]
    priors_only = build_candidates("John", "Smith", _intel())
    assert len(deep_candidates(priors_only, "decide")) == min(MAX_CANDIDATES, len(priors_only))


def test_expansion_round_after_every_guess_was_rejected_on_a_healthy_non_catch_all_server() -> None:
    intel = _intel()
    first = build_candidates("John", "Smith", intel)
    rejected = _probe({c.address: SmtpResult.rejected for c in first})
    assert should_expand(first, intel, rejected, H.HEALTHY)
    nxt = expand_candidates("John", "Smith", intel, [c.address for c in first])
    assert nxt and len(nxt) <= MAX_CANDIDATES
    assert not {c.address for c in nxt} & {c.address for c in first}
    assert all(c.round == EXPANSION_ROUND for c in nxt)
    # only once
    assert not should_expand(nxt, intel, _probe({c.address: SmtpResult.rejected for c in nxt}), H.HEALTHY)


def test_no_expansion_when_the_answer_is_not_a_clean_rejection() -> None:
    intel = _intel()
    cands = build_candidates("John", "Smith", intel)
    mixed = {c.address: SmtpResult.rejected for c in cands} | {cands[0].address: SmtpResult.temporary}
    assert not should_expand(cands, intel, _probe(mixed), H.HEALTHY)
    all_rejected = {c.address: SmtpResult.rejected for c in cands}
    assert not should_expand(cands, intel, _probe(all_rejected, catch_all=None), H.HEALTHY)
    assert not should_expand(cands, intel, _probe(all_rejected), H.BLOCKED)
    proven = _intel(patterns=[PatternStat("{first}.{last}", 1.0, 0.96, samples=8)])
    pat = build_candidates("John", "Smith", proven)
    assert not should_expand(pat, proven, _probe({c.address: SmtpResult.rejected for c in pat}), H.HEALTHY)
    published = [
        Candidate.from_dict(
            {"address": "john.smith@acme.com", "resolver": "published_website", "method": "published"}
        )
    ]
    assert not should_expand(
        published, intel, _probe({"john.smith@acme.com": SmtpResult.rejected}), H.HEALTHY
    )


def test_pilot_person_narrows_the_rest_to_the_confirmed_pattern() -> None:
    from scout.email.engine import narrow_after_pilot, pick_pilot

    intel = _intel()
    batch = [
        build_candidates(f, l_, intel) for f, l_ in (("John", "Smith"), ("Marie", "Durand"), ("Paul", "Roux"))
    ]
    idx = pick_pilot(batch, intel)
    assert idx is not None
    pilot = batch[idx]
    accepted = pilot[1]
    probe = _probe(
        {c.address: (SmtpResult.accepted if c is accepted else SmtpResult.rejected) for c in pilot}
    )
    others = [b for i, b in enumerate(batch) if i != idx]
    narrowed = narrow_after_pilot(pilot, probe, others)
    assert narrowed is not None
    assert all(len(n) == 1 and n[0].pattern == accepted.pattern for n in narrowed)
    # no narrowing on catch-all or when the pilot is ambiguous
    assert (
        narrow_after_pilot(pilot, _probe({accepted.address: SmtpResult.accepted}, catch_all=None), others)
        is None
    )
    both = _probe({c.address: SmtpResult.accepted for c in pilot[:2]})
    assert narrow_after_pilot(pilot, both, others) is None


def test_no_pilot_when_the_convention_is_known_or_for_a_single_person() -> None:
    from scout.email.engine import pick_pilot

    known = _intel(patterns=[PatternStat("{first}.{last}", 1.0, 0.9, samples=4)])
    assert (
        pick_pilot(
            [build_candidates("John", "Smith", known), build_candidates("Marie", "Durand", known)], known
        )
        is None
    )
    intel = _intel()
    assert pick_pilot([build_candidates("John", "Smith", intel)], intel) is None
