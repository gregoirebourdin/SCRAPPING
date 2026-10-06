"""Published email helpers: role/person/generic classification and person ↔ email matching."""

from __future__ import annotations

import re
from collections.abc import Iterable

from scout.extract.names import is_known_first_name
from scout.util.text import ascii_fold
from scout.util.urls import registrable_domain

ROLE_LOCALS = frozenset(
    """
contact contacts info infos information informations hello hi hey bonjour salut coucou allo support help aide
helpdesk sales sale vente ventes admin administration administratif office bureau team equipe jobs job career
careers carriere carrieres recrutement recruitment recruiting recrute rh hr drh people talent talents compta
comptabilite accounting accounts billing facturation facture factures invoice invoices paiement payment
payments noreply no-reply donotreply do-not-reply nepasrepondre ne-pas-repondre marketing presse press media
medias communication communications com commercial commerciale devis quote quotes booking bookings reservation
reservations resa rdv rendezvous welcome accueil secretariat secretary direction partenariat partenariats
partners partner partnership partnerships newsletter news webmaster postmaster hostmaster abuse privacy dpo
rgpd gdpr legal juridique service-client serviceclient service.client customer customers customerservice
customer-service customercare care client clients sav order orders commande commandes shop boutique store
studio agence agency mail email enquiries enquiry inquiries inquiry general hq headquarters it tech
technique dev developers events event evenements formation formations training ecole school projet projets
project projects bonjour.paris hola ciao hallo kontakt anfrage buero vertrieb verkauf
""".split()
)

_ROLE_PREFIXES = tuple(sorted({r for r in ROLE_LOCALS if len(r) >= 4}, key=len, reverse=True))


def _local(address: str) -> tuple[str, str]:
    addr = (address or "").strip().lower()
    local, _, domain = addr.rpartition("@")
    local = local.split("+", 1)[0]
    return local, domain


def classify_email(address: str) -> str:
    """'role' (contact@, info@, jobs@…), 'person' (jean.dupont@, jdupont@, marie@) or 'generic'."""
    local, domain = _local(address)
    if not local:
        return "generic"
    folded = ascii_fold(local)
    if folded in ROLE_LOCALS:
        return "role"
    tokens = [t for t in re.split(r"[._\-]", folded) if t]
    if tokens and tokens[0] in ROLE_LOCALS and (len(tokens) > 1 or folded.startswith(tokens[0])):
        if len(tokens) == 1 or not is_known_first_name(tokens[0]):
            return "role"  # contact-paris@, info.lyon@, jobs2024@
    if re.fullmatch(r"[a-z]+\d*", folded) and any(
        folded.startswith(p) and folded[len(p) :].isdigit() for p in _ROLE_PREFIXES
    ):
        return "role"
    label = (registrable_domain(domain) or domain).split(".", 1)[0]
    alpha = re.sub(r"[^a-z]", "", folded)
    if label and (
        alpha == re.sub(r"[^a-z]", "", label) or (len(alpha) >= 4 and alpha in label.replace("-", ""))
    ):
        return "generic"  # lumiere@agence-lumiere.fr
    if len(tokens) == 2 and all(t.isalpha() for t in tokens) and any(len(t) >= 2 for t in tokens):
        return "person"  # jean.dupont / j.dupont / jean-dupont
    if len(tokens) == 3 and all(t.isalpha() for t in tokens):
        return "person"  # jean.pierre.dupont
    if len(tokens) == 1 and folded.isalpha() and is_known_first_name(folded):
        return "person"  # marie@
    # Single opaque tokens ("jdupont", "lumierestudio") stay generic: only a known person's
    # name can prove they are personal (see match_email_to_person).
    return "generic"


def _variants(first: str) -> list[str]:
    f = ascii_fold(first).lower().strip()
    parts = [p for p in re.split(r"[\s\-']", f) if p]
    out = ["".join(parts), "-".join(parts)] if parts else []
    if len(parts) > 1:
        out.append(parts[0])
        out.append("".join(p[0] for p in parts))  # jean-pierre → jp
    return list(dict.fromkeys(v for v in out if v))


def _last_variants(last: str) -> list[str]:
    raw = ascii_fold(last).lower().strip()
    parts = [p for p in re.split(r"[\s\-']", raw) if p]
    if not parts:
        return []
    out = ["".join(parts), "-".join(parts)]
    core = [
        p
        for p in parts
        if p not in ("de", "du", "des", "la", "le", "van", "von", "der", "den", "d", "di", "da")
    ]
    if core and core != parts:
        out.append("".join(core))
    out.append(parts[-1])
    return list(dict.fromkeys(v for v in out if v))


def match_email_to_person(address: str, first: str | None, last: str | None) -> float:
    """0–1 likelihood that ``address`` belongs to the person (first.last=1.0 … initials=0.6)."""
    local, _domain = _local(address)
    local = ascii_fold(local)
    if not local or not (first or last):
        return 0.0
    firsts = _variants(first) if first else []
    lasts = _last_variants(last) if last else []
    best = 0.0
    seps = (".", "_", "-", "")
    for f in firsts:
        for la in lasts:
            for sep in seps:
                if local == f"{f}{sep}{la}":
                    best = max(best, 1.0)
                if local == f"{la}{sep}{f}":
                    best = max(best, 0.95)
                if local == f"{f[0]}{sep}{la}":
                    best = max(best, 0.9)
                if local == f"{f}{sep}{la[0]}":
                    best = max(best, 0.85)
                if local == f"{la}{sep}{f[0]}":
                    best = max(best, 0.8)
                if local == f"{f[0]}{sep}{la[0]}" and len(local) <= 3:
                    best = max(best, 0.6)
            if best < 0.85 and len(f) >= 3 and len(la) >= 3 and f in local and la in local:
                best = max(best, 0.85)
    for f in firsts:
        if local == f and local not in ROLE_LOCALS:
            best = max(best, 0.8)
    for la in lasts:
        if local == la and local not in ROLE_LOCALS and len(la) >= 3:
            best = max(best, 0.7)
    return best


def emails_on_domain(emails: Iterable[str], domain: str | None) -> list[str]:
    """Lowercased, de-duplicated addresses whose domain belongs to ``domain`` (subdomains included)."""
    target = registrable_domain(domain) if domain else None
    if not target:
        return []
    out: list[str] = []
    for e in emails or []:
        if not isinstance(e, str) or "@" not in e:
            continue
        addr = e.strip().lower()
        if registrable_domain(addr.rpartition("@")[2]) == target and addr not in out:
            out.append(addr)
    return out
