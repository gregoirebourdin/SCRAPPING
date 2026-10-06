"""Labelled, deterministic email-resolution scenarios for the benchmark harness (docs/EMAIL_ENGINE.md).

Each synthetic domain has a ground truth: its real convention, which mailboxes exist, whether it is
catch-all, how its mail server behaves (normal, greylisting, accept-all gateway…) and which addresses
are publicly observable (company website, imports). Targets are people whose address must be found.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from scout.db.enums import EmailEvidenceSource, MailProvider
from scout.email.patterns import GLOBAL_PRIORS, render

FIRST_NAMES = [
    "Marie",
    "Thomas",
    "Camille",
    "Lucas",
    "Julie",
    "Hugo",
    "Emma",
    "Louis",
    "Léa",
    "Nathan",
    "Chloé",
    "Antoine",
    "Sarah",
    "Paul",
    "Manon",
    "Pierre",
    "Laura",
    "Julien",
    "Inès",
    "Maxime",
    "Clara",
    "Alexandre",
    "Anaïs",
    "Romain",
    "John",
    "Emily",
    "Michael",
    "Olivia",
    "David",
    "Sophie",
    "James",
    "Anna",
    "Daniel",
    "Jean-Pierre",
    "Marc",
    "Élodie",
]
LAST_NAMES = [
    "Martin",
    "Bernard",
    "Dubois",
    "Durand",
    "Lefebvre",
    "Moreau",
    "Laurent",
    "Simon",
    "Michel",
    "Garcia",
    "David",
    "Bertrand",
    "Roux",
    "Vincent",
    "Fournier",
    "Morel",
    "Girard",
    "André",
    "Mercier",
    "Dupont",
    "Lambert",
    "Bonnet",
    "François",
    "Martinez",
    "Smith",
    "Johnson",
    "Brown",
    "Taylor",
    "Wilson",
    "de la Fontaine",
    "Le Gall",
    "N'Diaye",
]

# Domain kinds and their share in the default mix.
DEFAULT_MIX: dict[str, float] = {
    "published": 0.12,  # target address published on the company website
    "convention_strong": 0.30,  # ≥ 4 named colleague samples observed
    "convention_weak": 0.12,  # a single named sample
    "unknown": 0.22,  # nothing observed
    "catch_all": 0.10,  # accepts every RCPT
    "greylist": 0.06,  # first RCPT attempt answered 450
    "gateway": 0.05,  # secure gateway: accept-all at RCPT
    "no_mx": 0.03,  # domain receives no mail
}


@dataclass(frozen=True)
class BenchPerson:
    first: str
    last: str
    true_address: str | None  # None: no mailbox (left the company, no account)


@dataclass(frozen=True)
class Observation:
    first: str | None
    last: str | None
    address: str
    source: EmailEvidenceSource
    source_url: str | None = None


@dataclass
class BenchDomain:
    domain: str
    kind: str
    provider: MailProvider
    true_pattern: str | None
    accepts_mail: bool
    catch_all: bool
    behaviour: str  # normal | greylist_first | accept_all_gateway
    targets: list[BenchPerson] = field(default_factory=list)
    observed: list[Observation] = field(default_factory=list)
    mailboxes: set[str] = field(default_factory=set)
    company_size_max: int = 30
    country: str = "FR"


@dataclass
class BenchDataset:
    seed: int
    domains: list[BenchDomain]

    @property
    def targets(self) -> int:
        return sum(len(d.targets) for d in self.domains)


def _pick_pattern(rnd: random.Random) -> str:
    patterns, weights = zip(*GLOBAL_PRIORS.items(), strict=True)
    while True:
        p = rnd.choices(patterns, weights=weights)[0]
        if p != "{f}{l}":  # too rare/ambiguous to be a meaningful ground truth
            return p


def _address(pattern: str, first: str, last: str, domain: str) -> str | None:
    locals_ = render(pattern, first, last)
    return f"{locals_[0]}@{domain}" if locals_ else None


def _unique_people(rnd: random.Random, n: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    firsts_used: set[str] = set()
    while len(out) < n:
        f, l_ = rnd.choice(FIRST_NAMES), rnd.choice(LAST_NAMES)
        if (f, l_) in seen or f in firsts_used:
            continue  # distinct first names keep {first}@ conventions unambiguous inside one company
        seen.add((f, l_))
        firsts_used.add(f)
        out.append((f, l_))
    return out


def generate(
    n_domains: int = 200, *, seed: int = 7, mix: dict[str, float] | None = None, mailbox_rate: float = 0.92
) -> BenchDataset:
    """Deterministic dataset: same seed → same domains, people, truths."""
    rnd = random.Random(seed)
    kinds, weights = zip(*(mix or DEFAULT_MIX).items(), strict=True)
    domains: list[BenchDomain] = []
    for i in range(n_domains):
        kind = rnd.choices(kinds, weights=weights)[0]
        domain = f"bench-{kind.replace('_', '-')}-{i:04d}.example"
        n_targets = min(12, max(1, int(rnd.paretovariate(1.6))))
        n_colleagues = {
            "convention_strong": rnd.randint(4, 10),
            "convention_weak": 1,
            "published": rnd.randint(1, 4),
        }.get(kind, 0)
        people = _unique_people(rnd, n_targets + n_colleagues)
        targets_names, colleague_names = people[:n_targets], people[n_targets:]
        pattern = _pick_pattern(rnd)
        provider = {
            "gateway": MailProvider.secure_gateway,
            "no_mx": MailProvider.none,
        }.get(
            kind,
            rnd.choice(
                [
                    MailProvider.google_workspace,
                    MailProvider.microsoft_365,
                    MailProvider.ovh,
                    MailProvider.ionos,
                ]
            ),
        )
        d = BenchDomain(
            domain=domain,
            kind=kind,
            provider=provider,
            true_pattern=pattern if kind != "no_mx" else None,
            accepts_mail=kind != "no_mx",
            catch_all=kind in ("catch_all", "gateway"),
            behaviour={"greylist": "greylist_first", "gateway": "accept_all_gateway"}.get(kind, "normal"),
            company_size_max=rnd.choice([5, 10, 30, 50, 200]),
            country=rnd.choice(["FR", "FR", "FR", "BE", "GB", "US", "DE"]),
        )
        for f, l_ in colleague_names:
            addr = _address(pattern, f, l_, domain)
            if addr and d.accepts_mail:
                d.mailboxes.add(addr)
                d.observed.append(
                    Observation(f, l_, addr, EmailEvidenceSource.website, f"https://{domain}/team")
                )
        for f, l_ in targets_names:
            addr = _address(pattern, f, l_, domain) if d.accepts_mail else None
            exists = bool(addr) and rnd.random() < mailbox_rate
            if exists and addr:
                d.mailboxes.add(addr)
            d.targets.append(BenchPerson(f, l_, addr if exists else None))
            if kind == "published" and exists and addr and rnd.random() < 0.8:
                d.observed.append(
                    Observation(f, l_, addr, EmailEvidenceSource.website, f"https://{domain}/contact")
                )
        if d.accepts_mail and rnd.random() < 0.5:
            d.observed.append(
                Observation(
                    None, None, f"contact@{domain}", EmailEvidenceSource.website, f"https://{domain}/"
                )
            )
            d.mailboxes.add(f"contact@{domain}")
        domains.append(d)
    return BenchDataset(seed=seed, domains=domains)
