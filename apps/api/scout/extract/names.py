"""Person-name plausibility and splitting (deterministic, multilingual).

A built-in first-name gazetteer (FR/EN/ES/DE/IT/PT/NL/AR…, ascii-folded) is a strong positive
signal; business / UI vocabulary is a hard negative. UNKNOWN beats WRONG: when unsure, reject.
"""

from __future__ import annotations

import re

from scout.util.text import ascii_fold, collapse_ws, title_case_name

# ---- first-name gazetteer (lowercase, accent-free) -------------------------------------------

_FIRST_NAMES_RAW = """
aaron abdel abdallah abdelkader abdou abdoul abdoulaye abdul abel abigail adam adele adeline adil adrian
adriana adrien agathe agnes ahmed aicha aida aime aimee alain alan alba albane albert alberto alessandra
alessandro alessia alex alexander alexandra alexandre alexia alexis alfonso alfred ali alice alicia alina
aline alison amandine amaury amelie amelia amin amina amir amy ana anais andre andrea andreas andres andrew
angela angelique angelo anis anna annabelle anne annick annie anouk anthony antoine antoinette antonio
apolline arnaud arthur astrid audrey augustin aurelie aurelien aurore axel axelle aya aymeric baptiste barbara
bastien beatrice beatriz ben benedicte benjamin benoit bernard bertrand blanche blandine boris brigitte bruno
bryan camille capucine carine carla carlos carmen carole caroline catherine cecile cedric celia celine charles
charlie charline charlotte chloe christelle christian christina christine christophe christopher claire clara
clarisse claude claudia clement clementine clothilde clotilde colette coline coralie corentin corinne cyril
cyrille damien daniel daniela daniele danielle david davide deborah delphine denis denise diane didier dimitri
djamel dominique dorian dorothee dylan edith edouard elena eleonore eliane elias elie elisa elisabeth elise
eliott ella elodie eloise elsa emeline emilie emilien emma emmanuel emmanuelle enzo eric erik erwan estelle
esteban ethan etienne eugenie eva eve evelyne fabien fabienne fabrice farid fatima fatou felix fernand
fernando filipe flavie florence florent florian francesca francesco francine francis francisco franck francois
francoise frederic frederique gabriel gabrielle gael gaelle gaetan gaspard gauthier gautier genevieve geoffrey
georges gerald gerard geraldine germain gilbert gilles ginette giovanni giulia giuseppe gregoire gregory
guillaume gustave guy hamid hannah hassan hector helene henri herve hichem hugo hugues ibrahim ines ingrid
irene isabelle ismael jacqueline jacques jade james jan jane jean jeanne jennifer jeremie jeremy jerome
jessica joanna joao jocelyn joel johan johanna john jonathan jordan jose joseph josephine josiane jules julia
julian julie julien juliette justine karim karima karine kevin khadija laetitia lara laura laurence laurent
lea leila leo leon leonard leonie lilian liliane lina lionel lisa loic lola lorenzo louis louise luc luca
lucas lucie lucien lucile ludovic luis luisa lydie madeleine magali maite malik manon manuel manuela marc
marcel marco margaux margot maria marianne marie marine mario marion marius marjorie marc-antoine marlene
marta martin martine mathias mathieu mathilde matteo matthew matthias matthieu maud maurice max maxence maxime
melanie melissa michael michel michele michelle mickael miguel mila mireille mohamed mohammed monique morgan
morgane muriel myriam nadia nadine natacha natalia nathalie nathan nicolas nicole nina noah noemie norbert
oceane octave odile olivia olivier oscar pablo paola pascal pascale patricia patrick paul paula pauline pedro
perrine philippe pierre pierre-yves quentin rachel rachid rafael raphael raphaelle raymond rebecca regis remi
remy renaud rene richard robert robin rodolphe rodrigo roger romain romane rose roxane sabine sabrina samia
samir samuel sandra sandrine sara sarah sebastien serge severine simon simone sofia sofiane solene sonia
sophie stephane stephanie steve steven susana suzanne sylvain sylvie tanguy tatiana theo theophile therese
thibault thibaut thierry thomas timothee tom tristan valentin valentine valerie vanessa victor victoria
vincent virginie vivien wendy william xavier yann yannick yasmine yoann youssef yves yvette yvonne zoe abby
adrianna alana albertine aldo alfie alicja allison amanda amber andy angie annette april arianna ashley austin
barry bella beth betty bill billy bob bonnie brad brandon brenda brian brittany bruce caleb cameron carl carol
carolyn casey chad charlene chris christy cindy cody colin connor craig crystal curtis cynthia dale dan dana
danny darren dave dawn dean debbie debra dennis derek diana donald donna doris dorothy doug douglas dustin
earl ed eddie edward eileen elaine eleanor elizabeth ellen emily eric erica erin ethel evan frank fred gary
gavin gemma george gina glen glenn gloria gordon grace graham greg hailey harold harry harvey hazel heather
heidi helen henry holly howard ian isaac jack jackie jacob jake jamie janet janice jared jason jean-luc jeff
jeffrey jenna jenny jeremiah jesse jill jim jimmy joan joanne jody joe joey joshua joyce judith judy justin
karen kate katherine kathleen kathy katie kayla keith kelly ken kenneth kerry kim kimberly kirsten kristen
kurt kyle larry laurie leah lee leslie lewis lily linda lindsay lisa logan lori louis lucy luke lynn madison
mandy marcus margaret marilyn mark marsha marshall mary matt megan melinda melvin mia mike mildred molly
monica nancy natalie neil nicholas nick nora oliver olive owen pam pamela patsy peggy penny peter phil phillip
phyllis rachael ralph randy ray rebekah regina rhonda ricky rita rob roberta rodney ron ronald rosemary ross
ruby russell ruth ryan sally sam samantha sandy scott sean shane shannon sharon shawn sheila shirley stacy
stanley stuart sue susan tammy tara ted teresa terry tiffany tim timothy tina todd toby tony tracy travis
trevor troy tyler vicky virginia walter wayne wesley whitney abril agustin alejandra alejandro alvaro
ana-maria anibal antonia beatris blanca camila candela carolina cesar concepcion consuelo cristina cristobal
dario diego dolores eduardo elvira emilio enrique esperanza estefania esther eva-maria federico felipe fermin
francisca gabriela gerardo gonzalo guadalupe guillermo gustavo ignacio inmaculada isidro jaime javier jesus
jimena joaquin jorge josefa juan juana juanita julio leticia lorena lourdes lucia manolo marcos margarita
mariana marisol mercedes miriam montserrat natividad nerea nuria pilar ramon raquel ricardo roberto rocio rosa
rosario ruben salvador santiago sergio silvia soledad tomas valeria veronica vicente yolanda achim anja anke
annika antje armin bernd birgit bjorn burkhard christa christoph dagmar detlef dieter dirk elke ernst franz
friedrich gabriele gerhard gerd gisela gudrun gunter gunther hannelore hans hans-peter harald heike heiko
heinz helga helmut herbert holger horst ilse ingeborg ingo jens jorg jurgen karl karl-heinz katrin kerstin
klaus lars lothar lukas manfred markus marlies monika nadja niklas olaf otto petra rainer ralf reinhard renate
rolf rudolf silke stefan steffen sven thorsten tobias torsten udo ulrich ursula uta uwe volker waltraud werner
wilhelm wolfgang adriano alberta alessio alfredo andreina angelica annalisa antonella arturo benedetta bruna
carlo caterina cesare chiara claudio corrado cristiano dante donatella elisabetta emanuele enrico ernesto
fabio fabrizio federica filippo flavio franco gaetano gianluca gianni giacomo giada gino giorgia giorgio
girolamo giuliano giulio graziella guido ilaria leonardo letizia loredana luciana luciano lucio luigi marcello
marina mariangela massimo matilde maurizio michela mirko paolo pasquale patrizia piero pietro raffaele
raffaella renato riccardo rocco rosaria salvatore sandro serena silvio simona stefania stefano tiziana tommaso
umberto valerio vincenzo vittoria vittorio afonso anabela armando artur bernardo caio catarina conceicao diogo
duarte eduarda fabiana filomena goncalo graca guilherme helena henrique isabel joana joaquim leonor luana
marcelo mariana-joao matheus nuno paulo raimundo renata rui tiago vasco vitor abdelaziz abdellah abderrahmane
abdessamad adel ahmad aisha amal amine anas asma ayoub aziz bilal brahim driss fadwa faisal fares fatiha habib
hafida hakim halima hamza hanane hicham houda hussein idriss ilyas imane imen jamal jamila kamal khalid lamia
latifa mahdi mehdi meriem mohamed-amine mounir mourad mustapha nabil nadir najat naima nassim nawal noureddine
omar othmane rachida rania redouane reda riad saad said saida salah salim salma sami samira selim souad sultan
tarek walid yacine yamina yasmina yassine youcef younes youssra zakaria zineb zohra aart bram daan dirk-jan
eline femke floor geert hendrik inge jaap jeroen joost karel kees koen lieke lotte maarten marieke maartje
niels pieter roel ruud sanne sem stijn thijs wim wouter
"""
FIRST_NAMES: frozenset[str] = frozenset(t for t in _FIRST_NAMES_RAW.split() if t)

# Lowercase name particles (do not count as name tokens, never start a name).
PARTICLES = frozenset(
    {
        "de",
        "du",
        "des",
        "la",
        "le",
        "van",
        "von",
        "der",
        "den",
        "di",
        "da",
        "del",
        "dos",
        "das",
        "d",
        "ter",
        "ten",
        "zu",
        "y",
        "e",
        "bin",
        "ben",
        "al",
        "el",
        "st",
        "saint",
        "sainte",
    }
)

# Business / UI / title vocabulary that never appears in a person's name.
_STOPWORDS = frozenset(
    """
agence agency studio studios services service contact contacts equipe team teams marketing digital digitale
mentions legales legal cookies cookie savoir plus accueil home about blog nos notre our the and et les
solutions solution conseil consulting design communication web site page menu politique confidentialite
privacy policy terms conditions newsletter inscription connexion login panier cart shop boutique projet
projets project projects realisations references client clients temoignage temoignages avis review reviews
devis gratuit free offre offres tarifs prix pricing lire read more voir view all tous toutes decouvrir
discover learn en cliquez click here ici suivez follow nous us rejoignez join careers recrutement emploi jobs
job sas sarl eurl sasu ltd inc gmbh llc group groupe holding paris lyon marseille bordeaux toulouse nantes
lille nice rennes strasbourg montpellier rue avenue boulevard bd place cedex tel telephone email mail adresse
address copyright droits reserves rights reserved director directeur directrice fondateur fondatrice founder
founders ceo cto cfo coo cmo manager gerant gerante president presidente chef head responsable associate
associe associee partner partners cofondateur cofondatrice co assistant assistante developpeur developpeuse
developer designer graphiste consultant consultante stagiaire intern commercial commerciale comptable office
community social media creative creatif creative artistique art account sales ventes business developpement
development strategie strategy seo sea ux ui brand branding identite visuelle video photo photographie
production motion print event evenementiel merci bienvenue welcome hello bonjour lundi mardi mercredi jeudi
vendredi samedi dimanche monday tuesday wednesday thursday friday saturday sunday janvier fevrier mars avril
mai juin juillet aout septembre octobre novembre decembre january february march april june july august
september october november december google facebook instagram linkedin twitter youtube tiktok pinterest
wordpress shopify wix webflow mon ma mes votre vos your my un une a of for pour par with avec sur on in dans
au aux chez at by from to qui who what quoi pourquoi why how comment notre-equipe envoyer send submit valider
message formulaire form telecharger download inscrivez abonnez subscribe partager share retour back suivant
next precedent previous haut top bas fermer close ouvrir open rechercher search filtrer filter trier sort
categorie category tag archives actualites news article articles presse press evenements events formation
formations atelier ateliers programme agenda planning horaires ouverture ferme mentions-legales cgv cgu faq
aide help support centre center institut institute association federation universite university ecole school
college lycee hotel restaurant cafe bar boulangerie patisserie pharmacie clinique cabinet sante health
immobilier real estate finance banque bank assurance insurance transport logistique industrie industry
technologies technology tech software logiciel cloud data ai ia intelligence artificielle innovation lab labs
nord sud est ouest north south east west europe international national regional local new nouveau nouvelle top
premium pro plus expert experts experte specialiste specialist manager- ressources humaines human resources
operations operation directeur-general general generale executive officer chief vice senior junior lead
principal staff membre member members collaborateur collaborateurs talent talents recrute hiring lancement
launch produit produits product products offre-speciale maison atelier garage domaine chateau ferme galerie
librairie bistrot brasserie salon spa optique auto immo conseils creations editions productions films records
music musique sport fitness yoga coaching coach academy academie consultants avocats avocat notaire notaires
architecte architectes architecture interieur cuisine cuisines bois metal batiment construction renovation
travaux electricite plomberie paysage jardin jardins voyage voyages tourisme mode beaute bijoux fleurs
fleuriste traiteur vins caves cave epicerie marche market store magasin centre-ville ville city village
quartier lumiere lumieres soleil ocean mer montagne horizon nature
""".split()
)

_NAME_TOKEN_RE = re.compile(r"^[^\W\d_]+(?:['’\-][^\W\d_]+)*\.?$", re.UNICODE)
_INITIAL_RE = re.compile(r"^[^\W\d_]\.$", re.UNICODE)


def _fold(s: str) -> str:
    return ascii_fold(s).lower()


def is_known_first_name(token: str) -> bool:
    """Gazetteer lookup (accent/case-insensitive); hyphenated names match on their first part."""
    t = _fold(token).strip(".'’")
    if not t:
        return False
    if t in FIRST_NAMES:
        return True
    head = re.split(r"[-']", t, maxsplit=1)[0]
    return len(head) >= 2 and head in FIRST_NAMES


def has_known_first_name(name: str) -> bool:
    """A gazetteer first name among the first three name tokens ("Jean Dupont", "DUPONT Jean", "Marie-Claire
    Roux"). Headings and product names that pass the structural checks ("Related Websites", "Prompts Gpt")
    never do."""
    return any(is_known_first_name(t) for t in name_tokens(name or "")[:3])


def _token_ok(tok: str) -> bool:
    if not _NAME_TOKEN_RE.match(tok):
        return False
    letters = [c for c in tok if c.isalpha()]
    if not letters or not letters[0].isupper():
        return False
    if len(letters) == 1:
        return tok.endswith(".")  # middle initial "F."
    parts = re.split(r"['’\-]", tok.rstrip("."))
    for part in parts:
        if not part:
            return False
        if not part[0].isupper() and _fold(part) not in PARTICLES:
            return False
        # Reject mIxEd garbage like "PoWeR"; accept "McDonald", "DeVito", "DUPONT".
        if not (
            part.isupper() or part[1:].islower() or re.match(r"^(Mc|Mac|De|Di|Le|La|O)[A-Z][a-z]+$", part)
        ):
            return False
    return True


def _tokens(s: str) -> list[str]:
    return collapse_ws(s.replace(" ", " ")).split(" ") if s else []


def name_tokens(s: str) -> list[str]:
    """Tokens without particles."""
    return [t for t in _tokens(s) if _fold(t).strip(".'’") not in PARTICLES]


def is_plausible_person_name(s: str, *, require_known_first_name: bool = False) -> bool:
    """2–4 capitalized (or ALL CAPS) name tokens, no digits / business words / UI words.

    A known first name (gazetteer) is required when ``require_known_first_name`` is set; otherwise
    a gazetteer hit is not mandatory but every structural rule must pass.
    """
    if not s:
        return False
    s = collapse_ws(s)
    if len(s) < 4 or len(s) > 60:
        return False
    if re.search(r"[\d@/\\|:;!?()\[\]{}<>=+*#%&$€£\"«»“”_,]", s):
        return False
    toks = _tokens(s)
    core = name_tokens(s)
    if not (2 <= len(core) <= 4) or len(toks) > 6:
        return False
    if _fold(toks[0]).strip(".'’") in PARTICLES:
        return False
    for tok in toks:
        folded = _fold(tok).strip(".'’")
        if folded in PARTICLES:
            if tok[0].isupper() and tok not in (
                "De",
                "Van",
                "Von",
                "Da",
                "Di",
                "Del",
                "Le",
                "La",
                "Du",
                "Des",
                "Der",
                "Den",
                "El",
                "Al",
                "Ben",
                "Bin",
            ):
                return False
            continue
        if not _token_ok(tok):
            return False
        for piece in re.split(r"[-'’]", folded):
            if piece in _STOPWORDS:
                return False
    initials = [t for t in core if _INITIAL_RE.match(t)]
    if len(initials) > 1 or (core and _INITIAL_RE.match(core[-1]) and len(core) == 2):
        return False  # "Jean D." / "J. F." — not enough to identify a person
    if all(t.isupper() for t in core) and all(len(t) <= 3 for t in core):
        return False  # acronyms "SEO SEA", "RH DG"
    known = any(is_known_first_name(t) for t in core[:3])
    if require_known_first_name and not known:
        return False
    if not known:
        # Without a gazetteer hit, require "normal" name shapes: 2–3 tokens of ≥ 2 letters.
        if len(core) > 3 or any(len(t.strip(".")) < 2 for t in core):
            return False
    return True


# Words that make a "name" a business: legal forms and unambiguous organisation nouns (deliberately narrower
# than _STOPWORDS so real surnames such as "Bois" or "Paris" are never rejected).
_COMPANY_MARKERS = frozenset(
    """
agence agency studio studios group groupe holding consulting conseil conseils solutions services cabinet
association institut institute company compagnie societe entreprise enterprises entreprises labs lab
marketing digital communication media medias production productions editions academy academie
""".split()
)


def looks_like_company_name(name: str, company_name: str | None = None) -> bool:
    """True when ``name`` is an organisation rather than a person: a legal form or organisation noun,
    or the company's own name ("Agence Lumière", "Lumière SAS", "Lumière"). A sole proprietorship named
    after its owner ("Jean Dupont") still yields the person, because that name is a plausible person name."""
    from scout.util.text import LEGAL_FORMS, normalize_company_name, normalize_key

    key = normalize_key(name)
    if not key:
        return False
    tokens = set(key.split())
    if tokens & _COMPANY_MARKERS or tokens & {f for f in LEGAL_FORMS if len(f) >= 3}:
        return True
    if company_name:
        company = normalize_company_name(company_name)
        if company and normalize_company_name(name) == company and not is_plausible_person_name(name):
            return True
    return False


def person_name_score(s: str) -> float:
    """0–1 plausibility; ≥ 0.8 with a known first name, ~0.55 for structurally valid unknown names."""
    if not is_plausible_person_name(s):
        return 0.0
    core = name_tokens(s)
    if any(is_known_first_name(t) for t in core[:2]):
        return 0.9
    return 0.55


def split_name(full: str) -> tuple[str | None, str | None]:
    """Split a full name into (first, last).

    Handles registry style "DUPONT Jean" / "DUPONT Jean Pierre", "Dupont, Jean", particles
    ("Jean de La Fontaine" → ("Jean", "de La Fontaine")) and hyphenated first names.
    ALL CAPS tokens are title-cased.
    """
    s = collapse_ws(full or "").strip(" ,.")
    if not s:
        return None, None
    if "," in s:
        last, _, first = s.partition(",")
        first, last = collapse_ws(first), collapse_ws(last)
        if first and last:
            return _case(first.split(" ")[0]), _case(last)
    toks = _tokens(s)
    if len(toks) == 1:
        return _case(toks[0]), None

    # Registry style: leading ALL CAPS surname tokens followed by capitalized given names.
    caps_prefix = []
    for t in toks:
        letters = [c for c in t if c.isalpha()]
        if len(letters) >= 2 and t.isupper():
            caps_prefix.append(t)
        else:
            break
    rest = toks[len(caps_prefix) :]
    if caps_prefix and rest and all(not t.isupper() for t in rest if len(t) > 1):
        if not is_known_first_name(caps_prefix[0]) or any(is_known_first_name(t) for t in rest):
            return _case(rest[0]), _case(" ".join(caps_prefix))

    first = toks[0]
    idx = 1
    # "Jean Pierre Dupont": a second known first name is a middle name, unless it is the last token.
    while (
        idx < len(toks) - 1
        and _fold(toks[idx]) not in PARTICLES
        and (_INITIAL_RE.match(toks[idx]) or (is_known_first_name(toks[idx]) and not toks[idx].isupper()))
    ):
        idx += 1
    surname = " ".join(toks[idx:]) if idx < len(toks) else ""
    return _case(first), (_case(surname) if surname else None)


def _case(s: str) -> str:
    """Title-case ALL CAPS input (keeping particles lowercase); leave mixed case untouched."""
    letters = [c for c in s if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return title_case_name(s)
    return s


def display_name(full: str) -> str:
    """Human display form: "DUPONT Jean" → "Jean Dupont"; mixed case kept as written."""
    first, last = split_name(full)
    if first and last:
        return f"{first} {last}"
    return _case(collapse_ws(full))
