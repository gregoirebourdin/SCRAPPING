"""Shared harness for end-to-end campaign pipeline tests.

* A local :class:`FixtureServer` serves a handful of small agency websites (the "Agence Lumière"
  fixture plus inline sites), wired through ``crawler_host_overrides`` — no internet.
* A fixture discovery manifest (+ email domains for the fixture verifier) is written to a temp dir.
* :func:`drive` runs the real Postgres job queue in-process (``Worker.run_until_idle``) and
  fast-forwards delayed jobs (campaign ticks, back-off) until every campaign is terminal.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import CAMPAIGN_TERMINAL, CampaignStatus, JobStatus
from scout.db.models import Campaign, Job
from tests.unit.crawl.fixture_server import AGENCE_LUMIERE_PAGES, FixtureServer

LUMIERE = "agence-lumiere.fr"
NOVA = "studio-nova.fr"
KREA = "kreacom.fr"
PIXEL = "pixel-factory.fr"
OPTOUT = "agence-optout.fr"
SPAM = "spam-agency.fr"

LUMIERE_STAFF = {"Claire Fontaine", "Julien Moreau", "Sarah Benali", "Thomas Petit", "Inès Garnier"}
LUMIERE_TESTIMONIALS = {"Sophie Bernard", "Marc Lefebvre"}


def _page(title: str, body: str, *, description: str = "") -> str:
    nav = (
        '<nav><a href="/">Accueil</a> <a href="/equipe">Notre équipe</a> <a href="/contact">Contact</a></nav>'
    )
    return (
        f'<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta name="description" content="{description}"></head><body><header>{nav}</header>'
        f'<main>{body}</main><footer><a href="/mentions-legales">Mentions légales</a></footer></body></html>'
    )


def _agency_site(
    name: str, city: str, team: list[tuple[str, str]], *, contact_email: str, phone: str
) -> dict[str, str]:
    cards = "".join(f'<div class="team-card"><h3>{n}</h3><p class="role">{t}</p></div>' for n, t in team)
    desc = f"{name} est une agence marketing digital basée à {city} : stratégie social media, publicité et contenus."
    return {
        "/": _page(
            f"{name} – Agence marketing à {city}",
            f"<h1>{name}, agence marketing digital à {city}</h1><p>{desc}</p>"
            "<h2>Nos services</h2><p>Stratégie marketing, réseaux sociaux, publicité en ligne et création de contenus.</p>"
            f'<p>Tél. : {phone}</p><p><a href="mailto:{contact_email}">{contact_email}</a></p>',
            description=desc,
        ),
        "/equipe": _page(
            f"Notre équipe – {name}", f'<h1>Notre équipe</h1><div class="team-grid">{cards}</div>'
        ),
        "/contact": _page(
            f"Contact – {name}",
            f"<h1>Contactez-nous</h1><p>{name}</p><p>Téléphone : {phone}</p>"
            f'<p>Email : <a href="mailto:{contact_email}">{contact_email}</a></p>',
        ),
        "/mentions-legales": _page(
            f"Mentions légales – {name}",
            f"<h1>Mentions légales</h1><p>{name} SAS — Siège social : 3 place Bellecour, 69002 {city}</p>",
        ),
    }


INLINE_SITES: dict[str, dict[str, str]] = {
    NOVA: _agency_site(
        "Studio Nova",
        "Lyon",
        [
            ("Antoine Lefort", "Fondateur & CEO"),
            ("Julie Bernard", "Responsable marketing"),
            ("Hugo Lambert", "Graphiste"),
        ],
        contact_email="hello@studio-nova.fr",
        phone="04 72 00 00 01",
    ),
    KREA: _agency_site(
        "Kréa Com",
        "Lyon",
        [("Lucie Roy", "Gérante & fondatrice"), ("Nina Faure", "Chargée de communication")],
        contact_email="contact@kreacom.fr",
        phone="04 72 00 00 02",
    ),
    PIXEL: _agency_site(
        "Pixel Factory",
        "Lyon",
        [("Bruno Caron", "Community manager"), ("Léa Morel", "Développeuse web")],
        contact_email="contact@pixel-factory.fr",
        phone="04 72 00 00 03",
    ),
    OPTOUT: _agency_site(
        "Agence Optout",
        "Lyon",
        [("Paul Girard", "Fondateur")],
        contact_email="contact@agence-optout.fr",
        phone="04 72 00 00 04",
    ),
    SPAM: _agency_site(
        "Spam Agency",
        "Lyon",
        [("Victor Spam", "Fondateur")],
        contact_email="contact@spam-agency.fr",
        phone="04 72 00 00 05",
    ),
}

ALL_HOSTS = [LUMIERE, *INLINE_SITES]

EMAIL_DOMAINS: dict[str, dict[str, Any]] = {
    # Agence Lumière: published founder addresses are real mailboxes, domain not catch-all → SAFE
    LUMIERE: {
        "mx": True,
        "catch_all": False,
        "mailboxes": [
            "claire.fontaine@agence-lumiere.fr",
            "julien.moreau@agence-lumiere.fr",
            "contact@agence-lumiere.fr",
        ],
    },
    # Studio Nova: nothing published for people; the {first}.{last} permutation is accepted by SMTP → SAFE
    NOVA: {
        "mx": True,
        "catch_all": False,
        "mailboxes": [
            "antoine.lefort@studio-nova.fr",
            "julie.bernard@studio-nova.fr",
            "hello@studio-nova.fr",
        ],
    },
    # Kréa Com: catch-all domain → a guessed address can never be SAFE (honest CATCH_ALL status)
    KREA: {"mx": True, "catch_all": True, "mailboxes": []},
    PIXEL: {"mx": True, "catch_all": False, "mailboxes": []},
    OPTOUT: {"mx": True, "catch_all": False, "mailboxes": ["paul.girard@agence-optout.fr"]},
    SPAM: {"mx": True, "catch_all": False, "mailboxes": ["victor.spam@spam-agency.fr"]},
}


def manifest_companies() -> list[dict[str, Any]]:
    return [
        {
            "name": "Agence Lumière",
            "website": f"https://{LUMIERE}",
            "city": "Paris",
            "country": "FR",
            "category": "Agence marketing",
            "employees": "2-10",
        },
        # same registrable domain through another URL/name → one registry company, in-campaign duplicate
        {
            "name": "Agence Lumière (bureau de Lyon)",
            "website": f"https://www.{LUMIERE}/",
            "city": "Lyon",
            "country": "FR",
            "category": "Agence marketing",
        },
        {
            "name": "Studio Nova",
            "website": f"https://{NOVA}",
            "city": "Lyon",
            "country": "FR",
            "category": "Agence marketing digital",
        },
        {
            "name": "Kréa Com",
            "website": f"https://{KREA}",
            "city": "Lyon",
            "country": "FR",
            "category": "Agence marketing",
        },
        {
            "name": "Pixel Factory",
            "website": f"https://{PIXEL}",
            "city": "Lyon",
            "country": "FR",
            "category": "Agence marketing",
        },
        {
            "name": "Agence Optout",
            "website": f"https://{OPTOUT}",
            "city": "Lyon",
            "country": "FR",
            "category": "Agence marketing",
        },
        {
            "name": "Spam Agency",
            "website": f"https://{SPAM}",
            "city": "Lyon",
            "country": "FR",
            "category": "Agence marketing",
        },
    ]


def write_manifest(directory: Path, companies: list[dict[str, Any]] | None = None) -> Path:
    path = directory / "pipeline_manifest.json"
    path.write_text(
        json.dumps(
            {
                "companies": manifest_companies() if companies is None else companies,
                "email": {"domains": EMAIL_DOMAINS},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def build_server(hosts: Iterable[str] = ALL_HOSTS) -> FixtureServer:
    server = FixtureServer()
    for host in hosts:
        if host == LUMIERE:
            server.add_site(LUMIERE, AGENCE_LUMIERE_PAGES)
        else:
            for path, html in INLINE_SITES[host].items():
                server.add(host, path, html)
    return server


# ------------------------------------------------------------------------------------------------
# queue driver
# ------------------------------------------------------------------------------------------------


@dataclass
class DriveReport:
    rounds: int = 0
    statuses: dict[uuid.UUID, CampaignStatus] = field(default_factory=dict)


async def campaign_statuses(ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, CampaignStatus]:
    async with session_scope() as s:
        rows = (
            await s.execute(sa.select(Campaign.id, Campaign.status).where(Campaign.id.in_(list(ids))))
        ).all()
    return {i: st for i, st in rows}


async def fast_forward(campaign_ids: Iterable[uuid.UUID] | None = None) -> int:
    """Make delayed pending/retrying jobs runnable now (ticks, back-pressure, retry back-off)."""
    async with session_scope() as s:
        q = sa.update(Job).where(
            Job.status.in_([JobStatus.pending, JobStatus.retrying]), Job.run_after > sa.func.now()
        )
        if campaign_ids is not None:
            q = q.where(Job.campaign_id.in_(list(campaign_ids)))
        res = await s.execute(q.values(run_after=sa.func.now()).returning(Job.id))
        return len(res.scalars().all())


async def drive(
    campaign_ids: Iterable[uuid.UUID],
    *,
    timeout_s: float = 120.0,
    slots: int = 8,
    until_terminal: bool = True,
) -> DriveReport:
    """Run the worker until every campaign is terminal (or, with ``until_terminal=False``, until no runnable job
    remains — delayed jobs are fast-forwarded once per round either way)."""
    from scout.jobs.worker import Worker
    from scout.main import load_handlers

    load_handlers()
    ids = list(campaign_ids)
    worker = Worker(slots=slots)
    report = DriveReport()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        report.rounds += 1
        await worker.run_until_idle(timeout_s=max(1.0, deadline - time.monotonic()), idle_rounds=2)
        report.statuses = await campaign_statuses(ids)
        done = all(st in CAMPAIGN_TERMINAL for st in report.statuses.values())
        if until_terminal and done:
            # Terminal campaigns cancel their jobs; nothing may remain runnable.
            async with session_scope() as s:
                left = await s.scalar(
                    sa.select(sa.func.count())
                    .select_from(Job)
                    .where(
                        Job.campaign_id.in_(ids),
                        Job.status.in_(
                            [JobStatus.pending, JobStatus.claimed, JobStatus.running, JobStatus.retrying]
                        ),
                    )
                )
            assert left == 0, f"{left} jobs still active after campaigns stopped"
            return report
        moved = await fast_forward(ids)
        if not until_terminal and moved == 0:
            return report
    raise TimeoutError(f"campaigns not terminal after {timeout_s}s: {report.statuses}")
