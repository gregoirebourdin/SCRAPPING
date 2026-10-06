"""Offline world for the browser end-to-end suite (``apps/web/e2e``) — no internet, deterministic data.

Run::

    cd apps/api && uv run python -m tests.e2e.world <out_dir> [--port 8765] [--latency 0.15,0.5]

* Serves 40 French marketing agency websites (home, services, team, contact, legal notice) from one local
  :class:`FixtureServer` routed by Host header, with a small random latency per request (real websites are not
  instant), so the real SSRF-safe crawler runs through ``CRAWLER_HOST_OVERRIDES``.
* Writes into ``out_dir``: ``manifest.json`` (fixture discovery companies + the email world used by
  ``VERIFIER_BACKEND=fixture``), ``hosts.json`` (host → ``127.0.0.1:<port>``, the ``CRAWLER_HOST_OVERRIDES``
  value), ``world.json`` (ground truth the specs assert against) and the CSV files the specs upload.
* ``GET /__requests`` (any host) returns the request log (total, per host) — proof that a new column re-uses
  cached pages instead of crawling again; ``GET /__health`` answers ``ok``.
* Prints ``WORLD READY http://127.0.0.1:<port> …`` once listening, then serves until killed.

The data is designed for the four critical scenarios (brief §200–203):

* every other agency really sells Instagram management (services page); the others only have a
  "Suivez-nous sur Instagram" footer link (a social-follow mention, not a service); every fourth mentions ManyChat;
* founders' mailboxes: ``first.last@`` confirmed by SMTP (→ SAFE); a ``first.last@`` convention proven by three
  published colleague addresses but no probe (→ LIKELY_SAFE); catch-all domains (never SAFE → not delivered); and
  a few agencies without any founder;
* "Acme" (Lyon): John Carter (CEO) + Sarah Lambert (Head of Marketing) for "a different decision maker";
* cities interleave Lyon / Paris / Marseille / Nantes / Lille so a city-limited search finds matches early.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import random
import signal
import sys
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from tests.integration.pipeline_helpers import _page
from tests.unit.crawl.fixture_server import FixtureServer

EmailMode = Literal["safe", "pattern", "catch_all"]

FOUNDER_FIRST = [
    "Camille", "Julien", "Thomas", "Inès", "Hugo", "Léa", "Antoine", "Chloé", "Maxime", "Manon",
    "Louis", "Emma", "Nathan", "Clara", "Lucas", "Jade", "Paul", "Zoé", "Arthur", "Alice",
    "Victor", "Lina", "Jules", "Romane", "Gabriel", "Margaux", "Raphaël", "Juliette", "Adrien", "Anaïs",
    "Bastien", "Céline", "Damien", "Élodie", "Florian", "Gaëlle", "Henri", "Isabelle", "Jérôme",
]  # fmt: skip
MARKETING_FIRST = [
    "Mathilde", "Quentin", "Pauline", "Yann", "Sophie", "Benoît", "Laura", "Kevin", "Noémie", "Olivier",
    "Charlotte", "Mehdi", "Estelle", "Thibault", "Audrey", "Vincent", "Morgane", "Cédric", "Lucie", "Fabien",
    "Amandine", "Grégory", "Hélène", "Loïc", "Marion", "Nicolas", "Océane", "Pierre", "Rachel", "Samuel",
    "Tiphaine", "Ugo", "Valérie", "William", "Xavier", "Yasmine", "Zacharie", "Agathe", "Bruno",
]  # fmt: skip
LAST = [
    "Martin", "Bernard", "Dubois", "Moreau", "Laurent", "Simon", "Michel", "Lefebvre", "Leroy", "Roux",
    "David", "Bertrand", "Morel", "Fournier", "Girard", "Bonnet", "Dupont", "Lambert", "Fontaine", "Rousseau",
    "Vincent", "Muller", "Faure", "André", "Mercier", "Blanc", "Guérin", "Boyer", "Garnier", "Chevalier",
    "François", "Legrand", "Gauthier", "Garcia", "Perrin", "Robin", "Clément", "Morin", "Nicolas",
]  # fmt: skip
WORDS = [
    "Nova", "Lumen", "Pixel", "Atelier", "Krea", "Orbit", "Sillage", "Prisme", "Echo", "Boréal",
    "Cosmos", "Hélice", "Mosaïque", "Onde", "Rivage", "Saison", "Tandem", "Vertige", "Zénith", "Azur",
    "Brume", "Cèdre", "Delta", "Escale", "Flux", "Galet", "Horizon", "Iris", "Kairos", "Lagune",
    "Mistral", "Nacre", "Opale", "Pollen", "Quartz", "Récif", "Solstice", "Tilleul", "Ultra",
]  # fmt: skip
FEMALE = {
    "Camille", "Inès", "Léa", "Chloé", "Manon", "Emma", "Clara", "Jade", "Zoé", "Alice", "Lina", "Romane",
    "Margaux", "Juliette", "Anaïs", "Céline", "Élodie", "Gaëlle", "Isabelle",
}  # fmt: skip
CITY_CYCLE = ["Lyon", "Paris", "Marseille", "Lyon", "Paris", "Marseille", "Nantes", "Lille"]
POSTCODE = {"Lyon": "69002", "Paris": "75002", "Marseille": "13001", "Nantes": "44000", "Lille": "59000"}
N_AGENCIES = 39  # + Acme

# Founders of these agencies (by index) are in "my uploaded file" (scenario 202) — all have deliverable emails
# and sit at the front of the discovery order, so a "new contacts" search always reaches them.
IMPORTED_FOUNDERS = [0, 1, 3, 4, 7]
UNRELATED_CONTACTS = [  # rows of the uploaded file that are not in the world (no website to crawl)
    ("Marie Curie", "marie.curie@example.org", "Radium Conseil", "", "Gérante"),
    ("Pierre Dac", "pierre.dac@example.org", "Studio Dac", "", "Fondateur"),
]


@dataclass
class Person:
    full_name: str
    title: str
    email: str | None  # the address that is a real mailbox (None: no mailbox, e.g. catch-all guess)
    expected_status: str | None  # what the email engine should conclude for this person
    published: bool = False  # the address is printed on the team page


@dataclass
class Agency:
    index: int
    name: str
    domain: str
    city: str
    team: list[Person]
    founder: Person | None
    marketing: Person
    sells_instagram: bool
    mentions_manychat: bool
    email_mode: EmailMode
    deliverable_founder: bool  # a founder/CEO search can deliver this agency's founder
    mailboxes: list[str] = field(default_factory=list)


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return "-".join(part for part in "".join(c if c.isalnum() else " " for c in s).split())


def local(name: str) -> str:
    return ".".join(slug(p) for p in name.split())


def build_agencies() -> list[Agency]:
    agencies: list[Agency] = []
    for i in range(N_AGENCIES):
        word = WORDS[i]
        city = CITY_CYCLE[i % len(CITY_CYCLE)]
        name = [f"Agence {word}", f"Studio {word}", f"{word} Communication", f"{word} Digital"][i % 4]
        domain = f"{slug(word)}-{slug(city)}.fr"
        mode: EmailMode = "catch_all" if i % 7 == 6 else ("pattern" if i % 3 == 2 else "safe")
        has_founder = i % 9 != 8
        f_name = f"{FOUNDER_FIRST[i]} {LAST[i]}"
        founder: Person | None = None
        if has_founder:
            female = FOUNDER_FIRST[i] in FEMALE
            title = (
                ["Fondatrice & CEO", "CEO", "Gérante & fondatrice"]
                if female
                else ["Fondateur & CEO", "CEO", "Gérant & fondateur"]
            )[i % 3]
            # safe: SMTP confirms the mailbox → SAFE. pattern: colleagues' first.last@ addresses are published,
            # the founder's is not → a well-evidenced convention guess, LIKELY_SAFE without any probe.
            status = {"safe": "SAFE", "pattern": "LIKELY_SAFE", "catch_all": "CATCH_ALL"}[mode]
            f_email = None if mode == "catch_all" else f"{local(f_name)}@{domain}"
            founder = Person(f_name, title, f_email, status)
        colleagues = [
            (
                f"{MARKETING_FIRST[i]} {LAST[(i * 13 + 7) % len(LAST)]}",
                "Head of Marketing" if i % 2 else "Responsable marketing",
            ),
            (f"{FOUNDER_FIRST[(i + 11) % N_AGENCIES]} {LAST[(i + 5) % len(LAST)]}", "Graphiste"),
            (f"{MARKETING_FIRST[(i + 7) % N_AGENCIES]} {LAST[(i + 21) % len(LAST)]}", "Chef de projet"),
        ]
        staff = [
            Person(
                n,
                t,
                None if mode == "catch_all" else f"{local(n)}@{domain}",
                None,
                published=mode == "pattern",
            )
            for n, t in colleagues
        ]
        marketing = staff[0]
        team = [founder or Person(f_name, "Directrice de projet", None, None), *staff]
        mailboxes = [f"contact@{domain}", *(p.email for p in team if p.email)]
        agencies.append(
            Agency(
                index=i,
                name=name,
                domain=domain,
                city=city,
                team=team,
                founder=founder,
                marketing=marketing,
                sells_instagram=i % 2 == 0,
                mentions_manychat=i % 4 == 0,
                email_mode=mode,
                deliverable_founder=founder is not None and mode != "catch_all",
                mailboxes=[] if mode == "catch_all" else mailboxes,
            )
        )
    john = Person("John Carter", "CEO", "john.carter@acme-agence.fr", "SAFE")
    sarah = Person("Sarah Lambert", "Head of Marketing", "sarah.lambert@acme-agence.fr", "SAFE")
    agencies.append(
        Agency(
            index=N_AGENCIES,
            name="Acme",
            domain="acme-agence.fr",
            city="Lyon",
            team=[john, sarah, Person("Hugo Petit", "Développeur web", None, None)],
            founder=john,
            marketing=sarah,
            sells_instagram=True,
            mentions_manychat=False,
            email_mode="safe",
            deliverable_founder=True,
            mailboxes=[
                "contact@acme-agence.fr",
                "john.carter@acme-agence.fr",
                "sarah.lambert@acme-agence.fr",
            ],
        )
    )
    names = [p.full_name for a in agencies for p in a.team]
    dupes = [n for n, c in Counter(names).items() if c > 1]
    assert not dupes, f"person names must be unique across the world: {dupes}"
    return agencies


# ---- websites ------------------------------------------------------------------------------------------------

FOLLOW = '<p class="social">Suivez-nous sur Instagram : <a href="https://www.instagram.com/{handle}">@{handle}</a></p>'


def _with_footer(html: str, handle: str) -> str:
    return html.replace("</main>", "</main>" + FOLLOW.format(handle=handle), 1)


def site_pages(a: Agency) -> dict[str, str]:
    handle = slug(a.name).replace("-", "")
    phone = f"0{4 + a.index % 5} 72 {a.index:02d} 10 20"
    desc = f"{a.name} est une agence marketing digital basée à {a.city} : stratégie, contenus et acquisition."
    services = [
        "<li><h3>Stratégie de marque</h3><p>Positionnement, plateforme de marque et plan marketing annuel.</p></li>",
        "<li><h3>Création de sites web</h3><p>Sites vitrines et e-commerce rapides, pensés pour la conversion.</p></li>",
        "<li><h3>Référencement naturel (SEO)</h3><p>Audit technique, contenus et netlinking.</p></li>",
    ]
    if a.sells_instagram:
        services.insert(
            0,
            "<li><h3>Gestion de comptes Instagram</h3><p>Nous gérons vos comptes Instagram de A à Z : stratégie "
            "éditoriale, création de contenus, community management et campagnes Instagram Ads.</p></li>",
        )
    else:
        services.append(
            "<li><h3>Campagnes Google Ads</h3><p>Création et pilotage de vos campagnes de recherche payante.</p></li>"
        )
    if a.mentions_manychat:
        services.append(
            "<li><h3>Automatisation des DM</h3><p>Nous installons des chatbots ManyChat pour répondre automatiquement "
            "aux messages privés et qualifier vos prospects.</p></li>"
        )
    cards = "".join(
        f'<div class="team-card"><h3>{p.full_name}</h3><p class="role">{p.title}</p>'
        + (
            f'<p class="email"><a href="mailto:{p.email}">{p.email}</a></p>'
            if p.published and p.email
            else ""
        )
        + "</div>"
        for p in a.team
    )
    contact = f"contact@{a.domain}"
    pages = {
        "/": _page(
            f"{a.name} – Agence marketing à {a.city}",
            f"<h1>{a.name}, agence marketing digital à {a.city}</h1><p>{desc}</p>"
            f'<p><a href="/services">Nos services</a> · <a href="/equipe">Notre équipe</a></p>'
            f'<p>Tél. : {phone}</p><p><a href="mailto:{contact}">{contact}</a></p>',
            description=desc,
        ),
        "/services": _page(
            f"Nos services – {a.name}",
            f'<h1>Nos services</h1><p>Ce que nous faisons pour nos clients à {a.city} :</p><ul class="services">'
            + "".join(services)
            + "</ul>",
        ),
        "/equipe": _page(
            f"Notre équipe – {a.name}", f'<h1>Notre équipe</h1><div class="team-grid">{cards}</div>'
        ),
        "/contact": _page(
            f"Contact – {a.name}",
            f"<h1>Contactez-nous</h1><p>{a.name}</p><p>Téléphone : {phone}</p>"
            f'<p>Email : <a href="mailto:{contact}">{contact}</a></p>',
        ),
        "/mentions-legales": _page(
            f"Mentions légales – {a.name}",
            f"<h1>Mentions légales</h1><p>{a.name} SAS — Siège social : 12 rue de la République, "
            f"{POSTCODE[a.city]} {a.city}</p>",
        ),
    }
    return {path: _with_footer(html, handle) for path, html in pages.items()}


# ---- files written for the API and the specs -------------------------------------------------------------------


def manifest(agencies: list[Agency]) -> dict[str, object]:
    return {
        "companies": [
            {
                "name": a.name,
                "website": f"https://{a.domain}",
                "city": a.city,
                "country": "FR",
                "category": "Agence marketing",
                "employees": "2-10",
            }
            for a in agencies
        ],
        "email": {
            "domains": {
                a.domain: {"mx": True, "catch_all": a.email_mode == "catch_all", "mailboxes": a.mailboxes}
                for a in agencies
            }
        },
    }


def contacts_csv(rows: list[tuple[str, str, str, str, str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Name", "Email", "Company", "Website", "Title"])
    w.writerows(rows)
    return buf.getvalue()


def write_files(out: Path, agencies: list[Agency], target: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    hosts = {h: target for a in agencies for h in (a.domain, f"www.{a.domain}")}
    (out / "hosts.json").write_text(json.dumps(hosts), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest(agencies), ensure_ascii=False), encoding="utf-8")
    imported = []
    for i in IMPORTED_FOUNDERS:
        a = agencies[i]
        assert a.founder is not None and a.founder.email and a.deliverable_founder
        imported.append((a.founder.full_name, a.founder.email, a.name, a.domain, a.founder.title))
    (out / "contacts.csv").write_text(contacts_csv([*imported, *UNRELATED_CONTACTS]), encoding="utf-8")
    acme = agencies[-1]
    assert acme.founder is not None and acme.founder.email
    (out / "acme.csv").write_text(
        contacts_csv(
            [(acme.founder.full_name, acme.founder.email, acme.name, acme.domain, acme.founder.title)]
        ),
        encoding="utf-8",
    )
    truth = {
        "agencies": [asdict(a) for a in agencies],
        "imported_founders": [r[0] for r in imported],
        "acme": {"domain": acme.domain, "ceo": acme.founder.full_name, "marketing": acme.marketing.full_name},
    }
    (out / "world.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")


# ---- server ----------------------------------------------------------------------------------------------------


class WorldServer(FixtureServer):
    """FixtureServer + per-request latency + ``/__requests`` / ``/__health`` control endpoints."""

    def __init__(self, latency: tuple[float, float]) -> None:
        super().__init__()
        self.latency = latency
        self._rng = random.Random(7)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.read(1), timeout=5)
            if not first or first == b"\x16":  # TLS ClientHello → refuse (the crawler falls back to http)
                writer.close()
                return
            head = first + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            writer.close()
            return
        parts = head.split(b" ", 2)
        target = parts[1].decode("latin-1") if len(parts) == 3 else "/"
        if target.startswith("/__"):
            await self._control(target, writer)
            return
        await asyncio.sleep(self._rng.uniform(*self.latency))
        replay = asyncio.StreamReader()  # crawler requests are GET/HEAD: the head is the whole request
        replay.feed_data(head)
        replay.feed_eof()
        await super()._handle(replay, writer)

    async def _control(self, target: str, writer: asyncio.StreamWriter) -> None:
        path = target.split("?", 1)[0]
        if path == "/__requests":
            by_host = Counter(h for h, _p, _hd in self.requests)
            body = json.dumps(
                {
                    "total": len(self.requests),
                    "by_host": dict(by_host),
                    "paths": [f"{h}{p}" for h, p, _ in self.requests],
                }
            ).encode()
            ctype = "application/json"
        elif path == "/__health":
            body, ctype = b"ok", "text/plain"
        else:
            body, ctype = b"not found", "text/plain"
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: {ctype}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()

    async def start_on(self, port: int) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]


async def serve(out: Path, port: int, latency: tuple[float, float]) -> None:
    agencies = build_agencies()
    server = WorldServer(latency)
    for a in agencies:
        for path, html in site_pages(a).items():
            server.add(a.domain, path, html)
            server.add(f"www.{a.domain}", path, html)
    await server.start_on(port)
    target = server.target()
    write_files(out, agencies, target)
    deliverable = sum(1 for a in agencies if a.deliverable_founder)
    print(
        f"WORLD READY http://{target} agencies={len(agencies)} deliverable_founders={deliverable} out={out}",
        flush=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("out_dir", type=Path)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--latency", default="0.15,0.5", help="min,max seconds added to every page request")
    a = p.parse_args(argv)
    lo, hi = (float(x) for x in a.latency.split(","))
    try:
        asyncio.run(serve(a.out_dir.resolve(), a.port, (lo, hi)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
