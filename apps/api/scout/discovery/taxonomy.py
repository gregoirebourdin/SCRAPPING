"""Industry taxonomy: free-text industries → NAF codes, Maps queries, OSM tags, search keywords, YC tags.

`match_industries("agences marketing")` is deterministic (no AI): accent/case/plural-insensitive fuzzy matching
of the text's n-grams against every multilingual synonym.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from rapidfuzz import fuzz, process

from scout.discovery.naf import NAF_LABELS
from scout.util.text import normalize_key

LANGS = ("en", "fr", "de", "es", "it")


@dataclass(frozen=True)
class IndustryProfile:
    key: str
    labels: dict[str, list[str]]          # lang → synonyms (EN/FR/DE/ES/IT)
    naf_codes: list[str]                  # FR NAF rév. 2 primary codes
    maps_queries: dict[str, list[str]]    # lang → Google Maps queries
    osm_tags: list[tuple[str, str]]       # OSM (key, value) pairs
    search_keywords: dict[str, list[str]]  # lang → web-search keywords
    yc_industries: list[str]              # YC industries / tags (case-insensitive)
    local_business: bool                  # storefront / local service → Maps & OSM shine
    digital: bool                         # online-first business → web / YC / GitHub shine
    adjacent_naf_codes: list[str] = field(default_factory=list)  # used once the base plan is exhausted

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, IndustryProfile) and other.key == self.key

    @property
    def label(self) -> str:
        return self.labels["en"][0]

    def names(self, lang: str) -> list[str]:
        return self.labels.get(lang) or self.labels["en"]

    def maps(self, lang: str) -> list[str]:
        return self.maps_queries.get(lang) or self.maps_queries.get("en") or self.names(lang)[:2]

    def keywords(self, lang: str) -> list[str]:
        return self.search_keywords.get(lang) or self.search_keywords.get("en") or self.names(lang)[:2]

    def all_naf(self, *, adjacent: bool = False) -> list[str]:
        return list(dict.fromkeys([*self.naf_codes, *(self.adjacent_naf_codes if adjacent else [])]))


def _p(
    key: str,
    *,
    en: list[str],
    fr: list[str],
    de: list[str],
    es: list[str],
    it: list[str],
    naf: list[str],
    adj: list[str] | None = None,
    maps: dict[str, list[str]] | None = None,
    osm: list[tuple[str, str]] | None = None,
    kw: dict[str, list[str]] | None = None,
    yc: list[str] | None = None,
    local: bool = False,
    digital: bool = False,
) -> IndustryProfile:
    labels = {"en": en, "fr": fr, "de": de, "es": es, "it": it}
    return IndustryProfile(
        key=key,
        labels=labels,
        naf_codes=naf,
        maps_queries=maps or {lang: names[:2] for lang, names in labels.items()},
        osm_tags=osm or [],
        search_keywords=kw or {lang: names[:2] for lang, names in labels.items()},
        yc_industries=yc or [],
        local_business=local,
        digital=digital,
        adjacent_naf_codes=adj or [],
    )


PROFILES: tuple[IndustryProfile, ...] = (
    # --- marketing & creative services -----------------------------------------------------
    _p("marketing_agency",
       en=["marketing agency", "digital marketing agency", "growth marketing agency", "marketing firm", "inbound marketing agency"],
       fr=["agence marketing", "agence de marketing", "agence marketing digital", "agence webmarketing", "agence de growth marketing"],
       de=["Marketingagentur", "Online-Marketing-Agentur", "Agentur für digitales Marketing"],
       es=["agencia de marketing", "agencia de marketing digital"],
       it=["agenzia di marketing", "agenzia di marketing digitale"],
       naf=["73.11Z", "70.21Z"], adj=["73.12Z", "73.20Z", "70.22Z"],
       osm=[("office", "advertising_agency"), ("office", "marketing")], digital=True),
    _p("digital_agency",
       en=["digital agency", "digital studio", "digital transformation agency", "interactive agency"],
       fr=["agence digitale", "agence numérique", "agence de communication digitale", "agence marketing digital et web"],
       de=["Digitalagentur", "Agentur für Digitalisierung"],
       es=["agencia digital"], it=["agenzia digitale"],
       naf=["73.11Z", "62.01Z", "70.21Z"], adj=["62.02A", "74.10Z", "63.12Z"],
       osm=[("office", "advertising_agency"), ("office", "it")], digital=True),
    _p("communication_agency",
       en=["communication agency", "communications agency", "communications firm", "corporate communications agency"],
       fr=["agence de communication", "agence communication", "agence de com", "cabinet de communication"],
       de=["Kommunikationsagentur", "Agentur für Kommunikation"],
       es=["agencia de comunicación"], it=["agenzia di comunicazione"],
       naf=["70.21Z", "73.11Z"], adj=["73.12Z", "74.10Z", "70.22Z"],
       osm=[("office", "advertising_agency")], digital=True),
    _p("advertising_agency",
       en=["advertising agency", "ad agency", "creative agency", "media buying agency"],
       fr=["agence de publicité", "agence publicitaire", "agence créative", "régie publicitaire"],
       de=["Werbeagentur", "Kreativagentur", "Mediaagentur"],
       es=["agencia de publicidad", "agencia creativa"], it=["agenzia pubblicitaria", "agenzia creativa"],
       naf=["73.11Z", "73.12Z"], adj=["70.21Z", "74.10Z"],
       osm=[("office", "advertising_agency")], digital=True),
    _p("social_media_agency",
       en=["social media agency", "social media marketing agency", "community management agency", "instagram marketing agency", "tiktok agency"],
       fr=["agence social media", "agence réseaux sociaux", "agence de community management", "agence instagram", "agence tiktok"],
       de=["Social-Media-Agentur", "Agentur für soziale Medien"],
       es=["agencia de redes sociales", "agencia social media"], it=["agenzia social media", "agenzia di social media marketing"],
       naf=["73.11Z", "70.21Z"], adj=["63.12Z", "74.90B"],
       osm=[("office", "advertising_agency")], digital=True),
    _p("seo_sea_agency",
       en=["seo agency", "sea agency", "search engine optimization agency", "ppc agency", "google ads agency", "sem agency"],
       fr=["agence seo", "agence sea", "agence de référencement", "agence référencement naturel", "agence google ads"],
       de=["SEO-Agentur", "SEA-Agentur", "Suchmaschinenoptimierung Agentur"],
       es=["agencia seo", "agencia sem", "agencia de posicionamiento web"],
       it=["agenzia seo", "agenzia sem", "agenzia di posizionamento"],
       naf=["73.11Z", "62.01Z"], adj=["70.21Z", "73.12Z", "63.12Z"],
       osm=[("office", "advertising_agency")], digital=True),
    _p("web_agency",
       en=["web agency", "web design agency", "web design studio", "website design company", "web development agency", "web designer"],
       fr=["agence web", "agence de création de site internet", "création de site web", "agence de développement web", "webdesigner", "web designer"],
       de=["Webagentur", "Webdesign-Agentur", "Internetagentur", "Webdesigner"],
       es=["agencia web", "agencia de diseño web", "diseño web"], it=["web agency", "agenzia web", "agenzia di web design"],
       naf=["62.01Z", "73.11Z"], adj=["62.02A", "62.09Z", "63.12Z", "74.10Z"],
       osm=[("office", "it"), ("craft", "web_design")], digital=True),
    _p("branding_studio",
       en=["branding agency", "branding studio", "design studio", "graphic design studio", "brand design agency", "graphic designer"],
       fr=["agence de branding", "studio de design", "studio de design graphique", "agence de design", "graphiste", "studio graphique"],
       de=["Branding-Agentur", "Designstudio", "Grafikdesign Studio", "Designagentur"],
       es=["agencia de branding", "estudio de diseño", "estudio de diseño gráfico"],
       it=["agenzia di branding", "studio di design", "studio grafico"],
       naf=["74.10Z", "73.11Z"], adj=["70.21Z", "18.13Z"],
       osm=[("office", "graphic_design"), ("craft", "graphic_design")], digital=True),
    _p("pr_agency",
       en=["pr agency", "public relations agency", "public relations firm", "press relations agency"],
       fr=["agence rp", "agence de relations presse", "agence de relations publiques", "attaché de presse"],
       de=["PR-Agentur", "Agentur für Öffentlichkeitsarbeit", "Pressearbeit Agentur"],
       es=["agencia de relaciones públicas", "agencia de comunicación y prensa"],
       it=["agenzia di pubbliche relazioni", "ufficio stampa"],
       naf=["70.21Z"], adj=["73.11Z", "70.22Z"], osm=[("office", "advertising_agency")], digital=True),
    _p("event_agency",
       en=["event agency", "event management company", "event planner", "event production company", "corporate events agency"],
       fr=["agence événementielle", "agence evenementielle", "organisateur d'événements", "agence d'événementiel"],
       de=["Eventagentur", "Veranstaltungsagentur", "Eventmanagement"],
       es=["agencia de eventos", "organización de eventos"], it=["agenzia di eventi", "organizzazione eventi"],
       naf=["82.30Z", "93.29Z"], adj=["90.02Z", "73.11Z"],
       osm=[("office", "event_management")], local=True),
    _p("influencer_agency",
       en=["influencer marketing agency", "influencer agency", "creator agency", "talent management agency"],
       fr=["agence d'influence", "agence influenceurs", "agence de marketing d'influence", "agence de talents"],
       de=["Influencer-Agentur", "Influencer-Marketing-Agentur"],
       es=["agencia de influencers", "agencia de marketing de influencers"],
       it=["agenzia di influencer marketing", "agenzia influencer"],
       naf=["73.11Z", "70.21Z"], adj=["74.90B", "78.30Z"], digital=True),
    _p("video_production",
       en=["video production company", "video production agency", "film production company", "video agency", "videographer"],
       fr=["société de production vidéo", "production audiovisuelle", "agence vidéo", "boîte de production", "vidéaste"],
       de=["Videoproduktion", "Filmproduktion", "Produktionsfirma"],
       es=["productora audiovisual", "productora de vídeo", "producción de vídeo"],
       it=["casa di produzione video", "produzione video", "videomaker"],
       naf=["59.11B", "59.11A"], adj=["59.12Z", "59.11C"], osm=[("office", "media"), ("craft", "video")]),
    _p("photography_studio",
       en=["photography studio", "photographer", "photo studio", "commercial photographer"],
       fr=["studio photo", "photographe", "studio de photographie", "photographe professionnel"],
       de=["Fotostudio", "Fotograf"], es=["estudio de fotografía", "fotógrafo"], it=["studio fotografico", "fotografo"],
       naf=["74.20Z"], osm=[("craft", "photographer"), ("shop", "photo")], local=True),
    # --- tech --------------------------------------------------------------------------------
    _p("software_saas",
       en=["saas", "saas company", "saas startup", "software company", "software startup", "software publisher", "b2b saas", "tech startup", "startup"],
       fr=["éditeur de logiciel", "editeur logiciel", "startup saas", "entreprise saas", "logiciel saas", "start-up tech", "startup"],
       de=["Softwareunternehmen", "SaaS-Unternehmen", "Softwarehersteller", "Software-Startup"],
       es=["empresa de software", "empresa saas", "startup de software"],
       it=["azienda software", "azienda saas", "software house", "startup software"],
       naf=["58.29C", "62.01Z"], adj=["58.29A", "58.29B", "63.11Z", "62.02A"],
       osm=[("office", "it"), ("office", "software")],
       kw={"en": ["saas company", "software startup"], "fr": ["éditeur logiciel saas", "startup saas"],
           "de": ["SaaS Unternehmen", "Softwarefirma"], "es": ["empresa saas", "empresa de software"],
           "it": ["azienda saas", "software house"]},
       yc=["B2B", "SaaS", "Enterprise Software", "Productivity", "Engineering, Product and Design", "Developer Tools"],
       digital=True),
    _p("ai_company",
       en=["ai company", "ai startup", "artificial intelligence company", "machine learning company", "generative ai startup"],
       fr=["startup ia", "entreprise d'intelligence artificielle", "startup intelligence artificielle", "société ia"],
       de=["KI-Unternehmen", "KI-Startup"], es=["empresa de inteligencia artificial", "startup de ia"],
       it=["azienda di intelligenza artificiale", "startup ia"],
       naf=["62.01Z", "58.29C"], adj=["72.19Z", "63.11Z"],
       yc=["Artificial Intelligence", "AI", "Generative AI", "Machine Learning", "AI Assistant"], digital=True),
    _p("fintech",
       en=["fintech", "fintech startup", "fintech company", "payments company", "neobank"],
       fr=["fintech", "startup fintech", "néobanque"], de=["Fintech", "Fintech-Startup"],
       es=["fintech", "empresa fintech"], it=["fintech", "startup fintech"],
       naf=["66.19B", "62.01Z"], adj=["58.29C"],
       yc=["Fintech", "Payments", "Banking and Exchange", "Credit and Lending", "Neobank"], digital=True),
    _p("healthtech",
       en=["healthtech", "health tech startup", "digital health company", "medtech startup"],
       fr=["healthtech", "startup e-santé", "startup santé", "medtech"],
       de=["Healthtech", "Digital-Health-Startup"], es=["healthtech", "startup de salud digital"],
       it=["healthtech", "startup sanità digitale"],
       naf=["62.01Z", "58.29C"], adj=["72.19Z"],
       yc=["Healthcare IT", "Health Tech", "Digital Health", "Healthcare"], digital=True),
    _p("it_services",
       en=["it services company", "managed service provider", "msp", "it support company", "it consulting firm", "systems integrator"],
       fr=["esn", "ssii", "société de services informatiques", "infogérance", "prestataire informatique", "maintenance informatique"],
       de=["IT-Dienstleister", "IT-Systemhaus", "IT-Service", "Managed Service Provider"],
       es=["empresa de servicios informáticos", "consultora it", "soporte informático"],
       it=["società di servizi informatici", "assistenza informatica", "consulenza it"],
       naf=["62.02A", "62.03Z", "62.09Z"], adj=["62.02B", "95.11Z", "62.01Z"],
       osm=[("office", "it"), ("shop", "computer")], digital=True),
    _p("cybersecurity",
       en=["cybersecurity company", "cyber security firm", "security consulting firm", "penetration testing company", "infosec company"],
       fr=["entreprise de cybersécurité", "cybersécurité", "société de sécurité informatique", "pentest"],
       de=["Cybersecurity-Unternehmen", "IT-Sicherheit", "IT-Security-Firma"],
       es=["empresa de ciberseguridad", "ciberseguridad"], it=["azienda di cybersecurity", "sicurezza informatica"],
       naf=["62.02A", "62.09Z"], adj=["62.01Z", "58.29A"],
       osm=[("office", "it")], yc=["Security", "Cybersecurity"], digital=True),
    # --- professional services -----------------------------------------------------------------
    _p("consulting",
       en=["consulting firm", "management consulting firm", "consultancy", "strategy consulting firm", "business consultant"],
       fr=["cabinet de conseil", "conseil en management", "cabinet de conseil en stratégie", "consultant", "société de conseil"],
       de=["Unternehmensberatung", "Managementberatung", "Strategieberatung", "Beratungsunternehmen"],
       es=["consultora", "consultoría de gestión", "consultoría estratégica"],
       it=["società di consulenza", "consulenza aziendale", "consulenza direzionale"],
       naf=["70.22Z"], adj=["74.90B", "71.12B"], osm=[("office", "consulting")], digital=True),
    _p("recruitment_agency",
       en=["recruitment agency", "recruiting firm", "staffing agency", "headhunter", "executive search firm", "talent acquisition agency"],
       fr=["cabinet de recrutement", "agence de recrutement", "chasseur de têtes", "agence d'intérim", "cabinet de chasse"],
       de=["Personalvermittlung", "Personalberatung", "Headhunter", "Zeitarbeitsfirma"],
       es=["agencia de contratación", "empresa de selección de personal", "headhunter", "empresa de trabajo temporal"],
       it=["agenzia per il lavoro", "società di selezione del personale", "head hunter"],
       naf=["78.10Z", "78.20Z"], adj=["78.30Z", "70.22Z"],
       osm=[("office", "employment_agency")]),
    _p("accounting_firm",
       en=["accounting firm", "accountant", "chartered accountant", "cpa firm", "bookkeeping service", "tax advisor"],
       fr=["cabinet comptable", "expert comptable", "expert-comptable", "cabinet d'expertise comptable", "comptable"],
       de=["Steuerberater", "Steuerberatungskanzlei", "Wirtschaftsprüfer", "Buchhaltungsbüro"],
       es=["asesoría contable", "gestoría", "despacho contable", "contable"],
       it=["studio commercialista", "commercialista", "studio contabile"],
       naf=["69.20Z"], osm=[("office", "accountant"), ("office", "tax_advisor")], local=True),
    _p("law_firm",
       en=["law firm", "lawyer", "attorney", "solicitor", "legal practice", "barrister"],
       fr=["cabinet d'avocats", "avocat", "cabinet d'avocat", "avocats associés"],
       de=["Anwaltskanzlei", "Rechtsanwalt", "Kanzlei"], es=["bufete de abogados", "abogado", "despacho de abogados"],
       it=["studio legale", "avvocato"],
       naf=["69.10Z"], osm=[("office", "lawyer")], local=True),
    _p("notary",
       en=["notary", "notary office", "notary public"], fr=["notaire", "étude notariale", "office notarial"],
       de=["Notar", "Notariat"], es=["notaría", "notario"], it=["notaio", "studio notarile"],
       naf=["69.10Z"], osm=[("office", "notary")], local=True),
    _p("real_estate_agency",
       en=["real estate agency", "estate agent", "realtor", "real estate brokerage", "property agency"],
       fr=["agence immobilière", "agent immobilier", "agence immo", "mandataire immobilier"],
       de=["Immobilienmakler", "Immobilienagentur", "Maklerbüro"],
       es=["agencia inmobiliaria", "inmobiliaria"], it=["agenzia immobiliare", "agente immobiliare"],
       naf=["68.31Z"], adj=["68.32A"], osm=[("office", "estate_agent")], local=True),
    _p("architecture_firm",
       en=["architecture firm", "architect", "architectural practice", "architecture studio"],
       fr=["cabinet d'architecture", "architecte", "agence d'architecture"],
       de=["Architekturbüro", "Architekt"], es=["estudio de arquitectura", "arquitecto"],
       it=["studio di architettura", "architetto"],
       naf=["71.11Z"], adj=["71.12B"], osm=[("office", "architect")], local=True),
    _p("interior_design",
       en=["interior design studio", "interior designer", "interior architect", "interior decorator"],
       fr=["architecte d'intérieur", "décorateur d'intérieur", "studio de design d'intérieur", "agence d'architecture intérieure"],
       de=["Innenarchitekt", "Innenarchitektur", "Raumausstatter"],
       es=["interiorista", "diseño de interiores", "estudio de interiorismo"],
       it=["interior designer", "architetto d'interni", "studio di interior design"],
       naf=["74.10Z"], adj=["71.11Z", "43.39Z"], osm=[("shop", "interior_decoration")], local=True),
    _p("construction",
       en=["construction company", "builder", "general contractor", "building contractor", "home builder"],
       fr=["entreprise de construction", "entreprise du bâtiment", "constructeur de maisons", "entreprise de btp", "maçon"],
       de=["Bauunternehmen", "Baufirma", "Generalunternehmer"], es=["empresa de construcción", "constructora"],
       it=["impresa edile", "impresa di costruzioni"],
       naf=["41.20A", "41.20B", "43.99C"], adj=["43.39Z", "43.32A", "41.10A"],
       osm=[("craft", "builder"), ("office", "construction_company")], local=True),
    _p("plumber",
       en=["plumber", "plumbing company", "plumbing services"], fr=["plombier", "entreprise de plomberie", "plomberie"],
       de=["Klempner", "Sanitärinstallateur", "Installateur"], es=["fontanero", "fontanería"], it=["idraulico"],
       naf=["43.22A"], adj=["43.22B"], osm=[("craft", "plumber")], local=True),
    _p("electrician",
       en=["electrician", "electrical contractor", "electrical services"], fr=["électricien", "entreprise d'électricité"],
       de=["Elektriker", "Elektroinstallateur"], es=["electricista", "instalaciones eléctricas"], it=["elettricista"],
       naf=["43.21A"], osm=[("craft", "electrician")], local=True),
    _p("hvac",
       en=["hvac contractor", "hvac company", "heating and air conditioning", "heat pump installer"],
       fr=["chauffagiste", "climatisation", "entreprise de chauffage", "installateur pompe à chaleur", "frigoriste"],
       de=["Heizungsbauer", "Klimatechnik", "Heizung Sanitär"], es=["climatización", "calefacción", "instalador de aire acondicionado"],
       it=["termoidraulico", "climatizzazione", "impianti di riscaldamento"],
       naf=["43.22B"], adj=["43.22A"], osm=[("craft", "hvac")], local=True),
    _p("cleaning_services",
       en=["cleaning company", "cleaning services", "commercial cleaning company", "janitorial services", "office cleaning"],
       fr=["entreprise de nettoyage", "société de nettoyage", "nettoyage de bureaux", "entreprise de propreté"],
       de=["Reinigungsfirma", "Gebäudereinigung", "Reinigungsdienst"], es=["empresa de limpieza", "servicios de limpieza"],
       it=["impresa di pulizie", "servizi di pulizia"],
       naf=["81.21Z", "81.22Z"], adj=["81.29B"], osm=[("office", "cleaning"), ("craft", "cleaning")], local=True),
    # --- hospitality & travel ------------------------------------------------------------------
    _p("restaurant",
       en=["restaurant", "bistro", "brasserie", "eatery"], fr=["restaurant", "bistrot", "brasserie", "restauration"],
       de=["Restaurant", "Gaststätte", "Gasthaus"], es=["restaurante"], it=["ristorante", "trattoria", "osteria"],
       naf=["56.10A", "56.10C"], adj=["56.10B", "56.21Z"],
       osm=[("amenity", "restaurant")], local=True),
    _p("cafe_bar",
       en=["cafe", "coffee shop", "bar", "pub", "cocktail bar", "wine bar"],
       fr=["café", "bar", "coffee shop", "bar à vin", "bar à cocktails", "pub"],
       de=["Café", "Bar", "Kneipe", "Kaffeehaus"], es=["cafetería", "bar", "café"], it=["caffè", "bar", "caffetteria"],
       naf=["56.30Z"], osm=[("amenity", "cafe"), ("amenity", "bar"), ("amenity", "pub")], local=True),
    _p("hotel",
       en=["hotel", "boutique hotel", "guest house", "bed and breakfast", "inn"],
       fr=["hôtel", "hotel", "chambre d'hôtes", "maison d'hôtes"],
       de=["Hotel", "Pension", "Gasthof"], es=["hotel", "hostal"], it=["hotel", "albergo", "bed and breakfast"],
       naf=["55.10Z"], adj=["55.20Z"], osm=[("tourism", "hotel"), ("tourism", "guest_house")], local=True),
    _p("travel_agency",
       en=["travel agency", "tour operator", "travel agent", "destination management company"],
       fr=["agence de voyage", "agence de voyages", "voyagiste", "tour opérateur"],
       de=["Reisebüro", "Reiseveranstalter"], es=["agencia de viajes", "operador turístico"],
       it=["agenzia di viaggi", "tour operator"],
       naf=["79.11Z", "79.12Z"], adj=["79.90Z"], osm=[("shop", "travel_agency"), ("office", "travel_agent")], local=True),
    # --- health ------------------------------------------------------------------------------------
    _p("dentist",
       en=["dentist", "dental clinic", "dental practice", "orthodontist", "dental office"],
       fr=["dentiste", "cabinet dentaire", "chirurgien-dentiste", "chirurgien dentiste", "orthodontiste", "centre dentaire"],
       de=["Zahnarzt", "Zahnarztpraxis", "Kieferorthopäde"], es=["dentista", "clínica dental", "ortodoncista"],
       it=["dentista", "studio dentistico", "ortodontista"],
       naf=["86.23Z"], osm=[("amenity", "dentist"), ("healthcare", "dentist")], local=True),
    _p("medical_clinic",
       en=["medical clinic", "doctor", "general practitioner", "medical practice", "medical center", "physician"],
       fr=["médecin", "cabinet médical", "médecin généraliste", "centre médical", "clinique", "maison de santé"],
       de=["Arztpraxis", "Arzt", "Hausarzt", "Klinik", "Medizinisches Versorgungszentrum"],
       es=["clínica médica", "médico", "centro médico"], it=["studio medico", "medico", "clinica", "poliambulatorio"],
       naf=["86.21Z", "86.22C"], adj=["86.10Z", "86.22B"],
       osm=[("amenity", "doctors"), ("amenity", "clinic"), ("healthcare", "doctor")], local=True),
    _p("physiotherapist",
       en=["physiotherapist", "physical therapist", "physiotherapy clinic", "physio", "osteopath", "chiropractor"],
       fr=["kinésithérapeute", "kiné", "cabinet de kinésithérapie", "masseur kinésithérapeute", "ostéopathe"],
       de=["Physiotherapeut", "Physiotherapie", "Osteopath"], es=["fisioterapeuta", "fisioterapia", "osteópata"],
       it=["fisioterapista", "fisioterapia", "osteopata"],
       naf=["86.90E"], osm=[("healthcare", "physiotherapist"), ("healthcare", "osteopath")], local=True),
    _p("veterinarian",
       en=["veterinarian", "vet clinic", "veterinary clinic", "animal hospital", "vet"],
       fr=["vétérinaire", "clinique vétérinaire", "cabinet vétérinaire"],
       de=["Tierarzt", "Tierarztpraxis", "Tierklinik"], es=["veterinario", "clínica veterinaria"],
       it=["veterinario", "clinica veterinaria", "ambulatorio veterinario"],
       naf=["75.00Z"], osm=[("amenity", "veterinary")], local=True),
    _p("pharmacy",
       en=["pharmacy", "drugstore", "chemist"], fr=["pharmacie", "parapharmacie"],
       de=["Apotheke"], es=["farmacia"], it=["farmacia", "parafarmacia"],
       naf=["47.73Z"], osm=[("amenity", "pharmacy")], local=True),
    # --- beauty & wellness -----------------------------------------------------------------------
    _p("beauty_salon",
       en=["beauty salon", "beauty institute", "nail salon", "beautician", "aesthetic clinic"],
       fr=["institut de beauté", "salon de beauté", "esthéticienne", "onglerie", "centre esthétique"],
       de=["Kosmetikstudio", "Schönheitssalon", "Nagelstudio"], es=["salón de belleza", "centro de estética"],
       it=["centro estetico", "salone di bellezza", "estetista"],
       naf=["96.02B"], osm=[("shop", "beauty")], local=True),
    _p("hair_salon",
       en=["hair salon", "hairdresser", "barber", "barbershop", "hair stylist"],
       fr=["salon de coiffure", "coiffeur", "barbier", "coiffeuse"],
       de=["Friseur", "Friseursalon", "Barbershop"], es=["peluquería", "barbería", "peluquero"],
       it=["parrucchiere", "salone di parrucchiere", "barbiere"],
       naf=["96.02A"], osm=[("shop", "hairdresser")], local=True),
    _p("spa",
       en=["spa", "day spa", "wellness center", "massage parlour", "massage therapist"],
       fr=["spa", "centre de bien-être", "institut de massage", "hammam"],
       de=["Spa", "Wellnesscenter", "Massagepraxis"], es=["spa", "centro de bienestar", "centro de masajes"],
       it=["spa", "centro benessere", "centro massaggi"],
       naf=["96.04Z"], adj=["96.02B"], osm=[("shop", "massage"), ("leisure", "spa"), ("amenity", "spa")], local=True),
    _p("gym",
       en=["gym", "fitness studio", "fitness center", "health club", "crossfit box", "personal trainer"],
       fr=["salle de sport", "salle de fitness", "club de fitness", "coach sportif", "box de crossfit"],
       de=["Fitnessstudio", "Fitnesscenter", "Personal Trainer"], es=["gimnasio", "centro de fitness", "entrenador personal"],
       it=["palestra", "centro fitness", "personal trainer"],
       naf=["93.13Z"], adj=["93.11Z", "93.12Z"], osm=[("leisure", "fitness_centre")], local=True),
    _p("yoga_studio",
       en=["yoga studio", "pilates studio", "yoga school", "pilates"],
       fr=["studio de yoga", "studio de pilates", "cours de yoga", "école de yoga"],
       de=["Yogastudio", "Pilates-Studio", "Yogaschule"], es=["estudio de yoga", "estudio de pilates"],
       it=["studio yoga", "studio pilates", "scuola di yoga"],
       naf=["85.51Z", "93.13Z"], osm=[("sport", "yoga"), ("leisure", "fitness_centre")], local=True),
    # --- commerce --------------------------------------------------------------------------------
    _p("ecommerce_brand",
       en=["e-commerce brand", "ecommerce company", "online store", "dtc brand", "direct to consumer brand", "shopify store"],
       fr=["e-commerce", "boutique en ligne", "site e-commerce", "marque e-commerce", "marque dnvb", "pure player"],
       de=["Onlineshop", "E-Commerce-Unternehmen", "Online-Händler"], es=["tienda online", "comercio electrónico", "marca dtc"],
       it=["negozio online", "e-commerce", "brand dtc"],
       naf=["47.91B"], adj=["47.91A"], yc=["E-commerce", "Retail", "Consumer"], digital=True),
    _p("retail_shop",
       en=["retail store", "shop", "boutique", "clothing store", "gift shop", "concept store"],
       fr=["magasin", "boutique", "commerce de détail", "boutique de vêtements", "concept store"],
       de=["Einzelhandel", "Geschäft", "Boutique", "Bekleidungsgeschäft"], es=["tienda", "boutique", "comercio minorista"],
       it=["negozio", "boutique", "negozio di abbigliamento"],
       naf=["47.71Z", "47.78C"], adj=["47.19B", "47.59B", "47.77Z"],
       osm=[("shop", "clothes"), ("shop", "boutique"), ("shop", "gift"), ("shop", "jewelry")], local=True),
    _p("car_dealer",
       en=["car dealer", "car dealership", "used car dealer", "auto dealer"],
       fr=["concessionnaire automobile", "concession automobile", "garage automobile vente", "vendeur de voitures d'occasion"],
       de=["Autohaus", "Autohändler", "Gebrauchtwagenhändler"], es=["concesionario de coches", "concesionario"],
       it=["concessionaria auto", "concessionario auto", "autosalone"],
       naf=["45.11Z"], adj=["45.19Z"], osm=[("shop", "car")], local=True),
    _p("car_repair",
       en=["car repair shop", "auto repair", "garage", "mechanic", "body shop"],
       fr=["garage", "garagiste", "garage automobile", "mécanique automobile", "carrosserie"],
       de=["Autowerkstatt", "Kfz-Werkstatt", "Werkstatt"], es=["taller mecánico", "taller de coches"],
       it=["officina meccanica", "autofficina", "carrozzeria"],
       naf=["45.20A"], osm=[("shop", "car_repair")], local=True),
    # --- finance & insurance ------------------------------------------------------------------
    _p("insurance_broker",
       en=["insurance broker", "insurance agency", "insurance agent", "insurance brokerage"],
       fr=["courtier en assurance", "agence d'assurance", "agent général d'assurance", "cabinet de courtage"],
       de=["Versicherungsmakler", "Versicherungsagentur", "Versicherungsvertreter"],
       es=["corredor de seguros", "correduría de seguros", "agencia de seguros"],
       it=["broker assicurativo", "agenzia assicurativa", "agente assicurativo"],
       naf=["66.22Z"], osm=[("office", "insurance")], local=True),
    _p("financial_advisor",
       en=["financial advisor", "wealth management firm", "financial planner", "independent financial adviser", "asset management firm"],
       fr=["conseiller en gestion de patrimoine", "cgp", "cabinet de gestion de patrimoine", "conseiller financier", "gestion de patrimoine"],
       de=["Finanzberater", "Vermögensverwaltung", "Vermögensberater"],
       es=["asesor financiero", "gestión patrimonial", "gestora de patrimonios"],
       it=["consulente finanziario", "gestione patrimoniale", "wealth management"],
       naf=["66.19B", "66.30Z"], adj=["70.22Z"], osm=[("office", "financial_advisor"), ("office", "financial")], local=True),
    # --- education & community ----------------------------------------------------------------------
    _p("training_center",
       en=["training company", "training center", "training organization", "professional training provider", "school", "language school"],
       fr=["organisme de formation", "centre de formation", "école", "formation professionnelle", "école de langues"],
       de=["Bildungsträger", "Weiterbildung", "Schulungszentrum", "Sprachschule"],
       es=["centro de formación", "academia", "escuela de idiomas"], it=["ente di formazione", "centro di formazione", "scuola di lingue"],
       naf=["85.59A", "85.59B"], adj=["85.60Z"], osm=[("amenity", "training"), ("office", "educational_institution")]),
    _p("coaching",
       en=["business coach", "coaching company", "executive coach", "life coach", "leadership coaching"],
       fr=["coach", "coaching", "coach professionnel", "cabinet de coaching", "coach en entreprise"],
       de=["Business Coach", "Coaching", "Coach"], es=["coach", "coaching empresarial"], it=["coach", "coaching aziendale"],
       naf=["70.22Z", "85.59B"], adj=["96.09Z", "85.59A"], digital=True),
    _p("coworking",
       en=["coworking space", "coworking", "shared office", "business center", "flex office"],
       fr=["espace de coworking", "coworking", "centre d'affaires", "bureaux partagés", "tiers-lieu"],
       de=["Coworking Space", "Business Center", "Bürogemeinschaft"], es=["espacio de coworking", "centro de negocios"],
       it=["spazio coworking", "coworking", "business center"],
       naf=["68.20B", "82.11Z"], osm=[("amenity", "coworking_space"), ("office", "coworking")], local=True),
    _p("nonprofit",
       en=["nonprofit", "non-profit organization", "charity", "ngo", "association", "foundation"],
       fr=["association", "ong", "fondation", "association loi 1901", "organisme à but non lucratif"],
       de=["gemeinnütziger Verein", "Verein", "Stiftung", "NGO"], es=["asociación", "ong", "fundación", "organización sin ánimo de lucro"],
       it=["associazione", "onlus", "fondazione", "organizzazione no profit"],
       naf=["94.99Z"], adj=["88.99B"], osm=[("office", "association"), ("office", "ngo")]),
    # --- industry & trade --------------------------------------------------------------------------
    _p("manufacturing",
       en=["manufacturer", "manufacturing company", "factory", "industrial company", "machine shop"],
       fr=["fabricant", "entreprise industrielle", "usine", "industriel", "pme industrielle", "atelier de mécanique"],
       de=["Hersteller", "Produktionsunternehmen", "Fabrik", "Industrieunternehmen"],
       es=["fabricante", "empresa industrial", "fábrica"], it=["produttore", "azienda manifatturiera", "fabbrica"],
       naf=["25.62B", "25.11Z", "28.29B"], adj=["22.29A", "33.12Z"],
       osm=[("man_made", "works"), ("industrial", "factory")]),
    _p("wholesale_distribution",
       en=["wholesaler", "distributor", "wholesale company", "distribution company", "b2b distributor"],
       fr=["grossiste", "distributeur", "négociant", "commerce de gros", "centrale d'achat"],
       de=["Großhändler", "Großhandel", "Distributor"], es=["mayorista", "distribuidor", "distribuidora"],
       it=["grossista", "distributore", "commercio all'ingrosso"],
       naf=["46.90Z", "46.69B"], adj=["46.18Z", "46.49Z"], osm=[("shop", "wholesale")]),
    _p("logistics_transport",
       en=["logistics company", "freight forwarder", "trucking company", "transport company", "courier company", "3pl"],
       fr=["entreprise de transport", "transporteur", "société de logistique", "logisticien", "commissionnaire de transport", "transport routier"],
       de=["Spedition", "Logistikunternehmen", "Transportunternehmen", "Kurierdienst"],
       es=["empresa de transporte", "empresa de logística", "transitario"],
       it=["azienda di trasporti", "società di logistica", "spedizioniere", "corriere"],
       naf=["49.41A", "52.29B"], adj=["49.41B", "52.10B", "52.29A"],
       osm=[("office", "logistics"), ("office", "moving_company")]),
    _p("printing",
       en=["printing company", "print shop", "commercial printer", "printing services"],
       fr=["imprimerie", "imprimeur", "reprographie", "atelier d'impression"],
       de=["Druckerei", "Druckerei-Service"], es=["imprenta", "servicios de impresión"], it=["tipografia", "stamperia"],
       naf=["18.12Z"], adj=["18.13Z"], osm=[("craft", "printer"), ("shop", "copyshop")], local=True),
    _p("wedding_planner",
       en=["wedding planner", "wedding planning company", "wedding organizer"],
       fr=["wedding planner", "organisatrice de mariage", "organisateur de mariage", "agence de mariage"],
       de=["Hochzeitsplaner", "Hochzeitsplanerin"], es=["wedding planner", "organizador de bodas"],
       it=["wedding planner", "organizzatore di matrimoni"],
       naf=["96.09Z"], adj=["82.30Z", "93.29Z"], osm=[("office", "event_management")], local=True),
)

_BY_KEY: dict[str, IndustryProfile] = {p.key: p for p in PROFILES}

for _prof in PROFILES:  # import-time consistency check of the NAF table
    for _code in _prof.all_naf(adjacent=True):
        assert _code in NAF_LABELS, f"missing NAF label for {_code} ({_prof.key})"


def get_profile(key: str) -> IndustryProfile | None:
    return _BY_KEY.get(key)


# ---- matching ---------------------------------------------------------------------------------

_STOPWORDS = {
    "a", "an", "the", "of", "and", "or", "for", "in", "at", "on", "to", "with", "based", "company", "companies",
    "de", "du", "des", "d", "la", "le", "les", "l", "et", "ou", "en", "au", "aux", "pour", "dans", "sur", "un", "une",
    "der", "die", "das", "und", "fur", "im", "von", "el", "los", "las", "y", "del", "para", "il", "lo", "gli",
    "di", "della", "delle", "per", "con",
}


_NO_SINGULAR = {"saas", "paas", "iaas", "sas", "news", "plus", "bus", "gas"}


def _singular(tok: str) -> str:
    if len(tok) <= 3 or tok in _NO_SINGULAR or tok.endswith(("ss", "us", "is")):
        return tok
    if tok.endswith("ies"):
        return tok[:-3] + "y"
    if tok.endswith("aux"):
        return tok[:-3] + "al"
    if tok.endswith(("s", "x")):
        return tok[:-1]
    return tok


def _canon(text: str) -> str:
    toks = [_singular(t) for t in normalize_key(text).replace("-", " ").split() if t not in _STOPWORDS]
    return " ".join(toks)


def canonical(text: str) -> str:
    """Matching form of a phrase: unaccented, lowercase, stopwords removed, tokens singularized."""
    return _canon(text)


@lru_cache(maxsize=1)
def _synonym_index() -> tuple[list[str], list[tuple[IndustryProfile, str]]]:
    choices: list[str] = []
    owners: list[tuple[IndustryProfile, str]] = []
    seen: set[tuple[str, str]] = set()
    for prof in PROFILES:
        for lang, names in prof.labels.items():
            for n in names:
                c = _canon(n)
                if c and (prof.key, c) not in seen:
                    seen.add((prof.key, c))
                    choices.append(c)
                    owners.append((prof, lang))
    return choices, owners


@dataclass(frozen=True)
class IndustryMatch:
    profile: IndustryProfile
    score: float
    lang: str
    synonym: str


def match_industries_detailed(text: str, *, threshold: float = 88.0, limit: int = 5) -> list[IndustryMatch]:
    """Best synonym match per profile, best first. Short synonyms (≤ 4 chars) must match exactly."""
    return list(_match_cached(text or "", threshold, limit))


@lru_cache(maxsize=4096)
def _match_cached(text: str, threshold: float, limit: int) -> tuple[IndustryMatch, ...]:
    canon = _canon(text)
    if not canon:
        return ()
    toks = canon.split()
    grams = {" ".join(toks[i : i + n]) for n in range(1, min(5, len(toks)) + 1) for i in range(len(toks) - n + 1)}
    choices, owners = _synonym_index()
    best: dict[str, IndustryMatch] = {}
    for gram in grams:
        for _choice, score, idx in process.extract(
            gram, choices, scorer=fuzz.ratio, score_cutoff=threshold - 10, limit=None
        ):
            syn = choices[idx]
            if len(syn) <= 4 or len(gram) <= 4:
                if syn != gram:
                    continue
                score = 100.0
            n_syn = len(syn.split())
            if n_syn > 1 and n_syn == len(gram.split()):
                score = max(score, fuzz.token_sort_ratio(gram, syn))
            if score < threshold:
                continue
            prof, lang = owners[idx]
            # Prefer matches that explain more of the text ("social media agency" over "agency").
            weighted = score + 2.0 * n_syn
            cur = best.get(prof.key)
            if cur is None or weighted > cur.score:
                best[prof.key] = IndustryMatch(prof, weighted, lang, syn)
    ranked = sorted(best.values(), key=lambda m: -m.score)
    return tuple(ranked[:limit])


def match_industries(text: str, *, threshold: float = 88.0, limit: int = 5) -> list[IndustryProfile]:
    """'agences marketing' / 'marketing agencies' / 'dentistes à Lyon' / 'SaaS startups' → profiles, best first."""
    return [m.profile for m in match_industries_detailed(text, threshold=threshold, limit=limit)]


_FR_HINT = re.compile(r"[àâçéèêëîïôûùüÿœ]|\b(agence|agences|cabinet|entreprise|société|societe|à|des|les|du|une)\b", re.I)


def looks_french(text: str) -> bool:
    """Cheap language hint for industry phrases ('agences marketing à Lyon' → True)."""
    if not text:
        return False
    if _FR_HINT.search(text):
        return True
    return any(m.lang == "fr" and m.score >= 99 for m in match_industries_detailed(text, limit=3))
