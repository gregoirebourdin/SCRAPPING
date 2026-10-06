"""Simulated mail world: same batching/classification code as production, deterministic, no I/O."""

from __future__ import annotations

import time
from pathlib import Path

from scout.db.enums import MailProvider
from scout.db.enums import SmtpHealthState as H
from scout.db.enums import SmtpResult as R
from scout.email.contracts import SessionOutcome as S
from scout.email.smtp.health import MemoryHealthStore, SmtpHealthMonitor
from scout.email.smtp.world import MailWorld, WorldDeepVerifier

MANIFEST = Path(__file__).resolve().parents[2] / "fixtures" / "email" / "manifest.json"


def mem_monitor() -> SmtpHealthMonitor:
    return SmtpHealthMonitor(MemoryHealthStore(), cache_ttl_s=0, enabled=lambda: True)


async def probe(v: WorldDeepVerifier, domain: str, addrs: list[str], **kw):
    kw.setdefault("check_catch_all", True)
    return await v.probe_domain(domain, [], addrs, **kw)


async def test_normal_domain_google_style():
    w = MailWorld()
    w.add("acme.fr", mailboxes={"anne.martin"}, provider=MailProvider.google_workspace)
    v = WorldDeepVerifier(w)
    rep = await probe(v, "acme.fr", ["anne.martin@acme.fr", "a.martin@acme.fr"])
    assert rep.session == S.ok and rep.catch_all is False and rep.mx_host == "aspmx.l.google.com"
    assert rep.verdicts["anne.martin@acme.fr"].result == R.accepted
    assert (
        rep.verdicts["a.martin@acme.fr"].result == R.rejected
        and "5.1.1" in rep.verdicts["a.martin@acme.fr"].message
    )
    assert w.mailbox_exists("anne.martin@acme.fr") and not w.mailbox_exists("a.martin@acme.fr")
    assert (w.sessions, w.connections, w.rcpt_commands) == (1, 1, 4)


async def test_microsoft_dbeb_and_tenant_without_dbeb():
    w = MailWorld()
    w.add("dbeb.fr", mailboxes={"anne.martin"}, provider=MailProvider.microsoft_365)
    w.add("nodbeb.fr", mailboxes={"anne.martin"}, provider=MailProvider.microsoft_365, catch_all=True)
    v = WorldDeepVerifier(w)
    rep = await probe(v, "dbeb.fr", ["anne.martin@dbeb.fr", "x.y@dbeb.fr"])
    assert rep.verdicts["x.y@dbeb.fr"].result == R.rejected and rep.catch_all is False
    rep2 = await probe(v, "nodbeb.fr", ["x.y@nodbeb.fr"])
    assert rep2.catch_all is True and rep2.verdicts["x.y@nodbeb.fr"].result == R.accepted


async def test_greylist_first_then_success_on_retry_with_same_random_probes():
    w = MailWorld()
    w.add("grey.fr", mailboxes={"anne.martin"}, behaviour="greylist_first", greylist_retries=1)
    v = WorldDeepVerifier(w)
    first = await probe(v, "grey.fr", ["anne.martin@grey.fr", "a.martin@grey.fr"])
    assert {x.result for x in first.verdicts.values()} == {R.temporary}
    assert first.catch_all is None and first.greylisted and first.session == S.ok
    second = await probe(
        v,
        "grey.fr",
        ["anne.martin@grey.fr", "a.martin@grey.fr"],
        catch_all_addresses=list(first.random_verdicts),
    )
    assert second.verdicts["anne.martin@grey.fr"].result == R.accepted
    assert second.verdicts["a.martin@grey.fr"].result == R.rejected
    assert second.catch_all is False  # the reused random probes got past greylisting too


async def test_port25_blocked_marks_health_blocked_and_never_rejects():
    w = MailWorld(port25_blocked=True)
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr", "f.fr"):
        w.add(d, mailboxes={"anne.martin"})
    mon = mem_monitor()
    v = WorldDeepVerifier(w, monitor=mon)
    results = []
    for d in ("a.fr", "b.fr", "c.fr", "d.fr", "e.fr"):
        results.append(await probe(v, d, [f"anne.martin@{d}", f"x.y@{d}"]))
    assert all(r.session == S.infra_failure for r in results)
    assert all(x.result == R.timeout for r in results for x in r.verdicts.values())
    assert not any(x.result == R.rejected for r in results for x in r.verdicts.values())
    assert await mon.current_state() == H.BLOCKED
    gate = await mon.gate(claim_canary=True)
    assert not gate.may_probe
    assert w.connections == 5 * 2  # each domain: both MX hosts timed out, nothing more
    assert w.simulated_latency_ms == 10 * w.timeout_ms


async def test_one_dead_domain_does_not_block_health():
    w = MailWorld()
    w.add("dead.fr", behaviour="timeout")
    w.add("ok.fr", mailboxes={"anne.martin"})
    mon = mem_monitor()
    v = WorldDeepVerifier(w, monitor=mon)
    await probe(v, "ok.fr", ["anne.martin@ok.fr"])
    for _ in range(8):
        await probe(v, "dead.fr", ["anne.martin@dead.fr"])
    assert await mon.current_state() == H.DEGRADED


async def test_gateway_policy_and_temporary_behaviours():
    w = MailWorld()
    w.add("gw.fr", behaviour="accept_all_gateway", provider=MailProvider.secure_gateway)
    w.add("rbl.fr", behaviour="policy_block")
    w.add("busy.fr", behaviour="temporary_always")
    mon = mem_monitor()
    v = WorldDeepVerifier(w, monitor=mon)
    gw = await probe(v, "gw.fr", ["anne.martin@gw.fr"], check_catch_all=False)
    assert (
        gw.catch_all is None
        and gw.verdicts["anne.martin@gw.fr"].result == R.unknown
        and gw.uninformative_accepts
    )
    rbl = await probe(v, "rbl.fr", ["anne.martin@rbl.fr"])
    assert rbl.session == S.policy_block and rbl.verdicts["anne.martin@rbl.fr"].result == R.blocked
    busy = await probe(v, "busy.fr", ["anne.martin@busy.fr", "b@busy.fr", "c@busy.fr", "d@busy.fr"])
    assert busy.session == S.temporary and {x.result for x in busy.verdicts.values()} == {R.temporary}
    assert w.sessions_by_domain["busy.fr"] == 1


async def test_batching_counts_and_simulated_latency_without_sleeping():
    w = MailWorld()
    w.add("big.fr", mailboxes={f"user{i}.name" for i in range(0, 60, 2)}, latency_ms=100)
    v = WorldDeepVerifier(w)
    targets = [f"user{i}.name@big.fr" for i in range(60)]
    t0 = time.monotonic()
    rep = await probe(v, "big.fr", targets)
    assert time.monotonic() - t0 < 1.0
    assert w.sessions == 1 and w.connections == 20 and w.rcpt_commands == 62
    assert rep.duration_ms == w.simulated_latency_ms == 20 * 300 + 62 * 100
    assert sum(x.result == R.accepted for x in rep.verdicts.values()) == 30


async def test_deterministic_random_probes():
    def build():
        w = MailWorld(seed=42)
        w.add("acme.fr")
        return WorldDeepVerifier(w)

    a = await probe(build(), "acme.fr", [])
    b = await probe(build(), "acme.fr", [])
    assert list(a.random_verdicts) == list(b.random_verdicts) and len(a.random_verdicts) == 2


async def test_unknown_domain_null_mx_and_disabled_world():
    w = MailWorld()
    w.add("nullmx.fr", accepts_mail=False)
    v = WorldDeepVerifier(w)
    unknown = await probe(v, "nowhere.fr", ["a.b@nowhere.fr"])
    nullmx = await probe(v, "nullmx.fr", ["a.b@nullmx.fr"])
    assert unknown.session == nullmx.session == S.not_attempted and "RFC 7505" in (nullmx.error or "")
    assert w.sessions == 0
    w.smtp_disabled = True
    w.add("acme.fr", mailboxes={"a.b"})
    assert not v.enabled
    off = await probe(v, "acme.fr", ["a.b@acme.fr"])
    assert off.session == S.not_attempted and off.verdicts["a.b@acme.fr"].result == R.not_attempted


async def test_world_from_fixture_manifest():
    w = MailWorld.from_manifest_path(MANIFEST)
    assert w.mailbox_exists("marie.dupont@agence-x.fr") and w.spec("catchall.fr").catch_all  # type: ignore[union-attr]
    assert w.spec("nomx.fr") is None and w.spec("nosmtp.fr").behaviour == "timeout"  # type: ignore[union-attr]
    v = WorldDeepVerifier(w)
    rep = await probe(v, "agence-x.fr", ["marie.dupont@agence-x.fr", "m.dupont@agence-x.fr"])
    assert rep.verdicts["marie.dupont@agence-x.fr"].result == R.accepted
    assert rep.verdicts["m.dupont@agence-x.fr"].result == R.rejected and rep.catch_all is False
