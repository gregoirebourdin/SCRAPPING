"""Static, deterministic email data: disposable domains, free providers and role local parts.

These lists are curated snapshots (no network fetch at runtime). They only feed raw signals
(`disposable`, `free_provider`, `role_address`); status derivation lives in `scout.email.status`.
"""

from __future__ import annotations

import re

# Well-known disposable / temporary mailbox providers (DEA). Lowercase, ASCII (IDNA) form.
DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    """
    0-mail.com 0815.ru 0clickemail.com 0wnd.net 0wnd.org 10mail.org 10minutemail.co.uk 10minutemail.com
    10minutemail.de 10minutemail.net 10minutemail.org 1secmail.com 1secmail.net 1secmail.org 20minutemail.com
    20minutemail.it 2prong.com 30minutemail.com 33mail.com 4warding.com 4warding.net 4warding.org
    60minutemail.com 675hosting.com 675hosting.net 675hosting.org 6url.com 75hosting.com 75hosting.net
    75hosting.org 7tags.com 9ox.net a-bc.net abyssmail.com afrobacon.com ajaxapp.net amilegit.com anonbox.net
    anonymbox.com antichef.com antichef.net antispam.de armyspy.com beefmilk.com binkmail.com bio-muesli.net
    bobmail.info bofthew.com bootybay.de boun.cr bouncr.com boximail.com brefmail.com bsnow.net bugmenot.com
    bumpymail.com burnermail.io burnthespam.info burstmail.info byom.de cek.pm chammy.info chogmail.com
    clixser.com clrmail.com cool.fr.nf correo.blogos.net cosmorph.com courriel.fr.nf courrieltemporaire.com
    crapmail.org cust.in cuvox.de dacoolest.com dandikmail.com dayrep.com dcemail.com deadaddress.com
    deadspam.com despam.it despammed.com devnullmail.com dfgh.net digitalsanctuary.com discard.email
    discardmail.com discardmail.de disposableaddress.com disposableemailaddresses.com disposableinbox.com
    dispose.it disposeamail.com dispostable.com dodgeit.com dodgit.com dodgit.org donemail.ru dontreg.com
    dontsendmespam.de drdrb.com drdrb.net dropjar.com dropmail.me dump-email.info dumpandjunk.com dumpmail.de
    dumpyemail.com e4ward.com easytrashmail.com einrot.com email60.com emailfake.com emailgo.de emailias.com
    emailigo.de emailinfive.com emailmiser.com emailondeck.com emailsensei.com emailtemporario.com.br
    emailto.de emailwarden.com emailx.at.hm emailxfer.com ephemail.net etranquil.com etranquil.net
    etranquil.org explodemail.com fakeinbox.com fakeinformation.com fakemail.fr fakemail.net
    fakemailgenerator.com filzmail.com fizmail.com fleckens.hu flyspam.com fr33mail.info frapmail.com
    friendlymail.co.uk fuckingduh.com fudgerub.com fux0ringduh.com garliclife.com get1mail.com get2mail.fr
    getairmail.com getmails.eu getnada.com getonemail.com getonemail.net ghosttexter.de gishpuppy.com
    givmail.com great-host.in greensloth.com grr.la guerillamail.biz guerillamail.com guerillamail.de
    guerillamail.net guerillamail.org guerrillamail.biz guerrillamail.com guerrillamail.de guerrillamail.info
    guerrillamail.net guerrillamail.org guerrillamailblock.com gustr.com h8s.org haltospam.com
    harakirimail.com hatespam.org herp.in hidemail.de hidzz.com hmamail.com hochsitze.com hulapla.de
    ieatspam.eu ieatspam.info ihateyoualot.info imails.info imgof.com imstations.com inbax.tk inboxalias.com
    inboxbear.com inboxclean.com inboxclean.org inboxkitten.com incognitomail.com incognitomail.net
    incognitomail.org insorg-mail.info instant-mail.de ipoo.org jetable.com jetable.fr.nf jetable.net
    jetable.org jnxjn.com jourrapide.com junk1e.com kasmail.com kaspop.com keepmymail.com killmail.com
    killmail.net kir.ch.tc klassmaster.com klassmaster.net klzlk.com kulturbetrieb.info kurzepost.de
    letthemeatspam.com lhsdv.com litedrop.com lol.ovpn.to lookugly.com lortemail.dk lr78.com m4ilweb.info
    maboard.com mail-temporaire.fr mail.tm mail2rss.org mail333.com mail4trash.com mailbidon.com mailcatch.com
    maildrop.cc maileater.com mailexpire.com mailforspam.com mailfreeonline.com mailin8r.com mailinater.com
    mailinator.com mailinator.net mailinator.org mailinator2.com mailincubator.com mailismagic.com
    mailmetrash.com mailmoat.com mailnator.com mailnesia.com mailnull.com mailpoof.com mailscrap.com
    mailshell.com mailsiphon.com mailslapping.com mailslite.com mailtemp.info mailtothis.com mailzilla.com
    mailzilla.org makemetheking.com mbx.cc mega.zik.dj meinspamschutz.de meltmail.com messagebeamer.de
    mierdamail.com mintemail.com moakt.com moburl.com mohmal.com moncourrier.fr.nf monemail.fr.nf
    monmail.fr.nf mt2009.com mvrht.com mycleaninbox.net mypartyclip.de myphantomemail.com mytempemail.com
    mytempmail.com mytrashmail.com nada.email nepwk.com nervmich.net nervtmich.net netzidiot.de neverbox.com
    no-spam.ws nobulk.com noclickemail.com nogmailspam.info nomail.xl.cx nomail2me.com nomorespamemails.com
    nospam.ze.tc nospam4.us nospamfor.us nospamthanks.info notmailinator.com nowmymail.com nurfuerspam.de
    nwldx.com objectmail.com obobbo.com oneoffemail.com onewaymail.com oopi.org otherinbox.com ourklips.com
    outlawspam.com owlpic.com pancakemail.com pimpedupmyspace.com pjjkp.com pokemail.net politikerclub.de
    poofy.org pookmail.com proxymail.eu prtnx.com putthisinyourspamdatabase.com quickinbox.com rcpt.at
    recode.me recursor.net regbypass.com rejectmail.com rhyta.com rklips.com rmqkr.net robot-mail.com
    rppkn.com rtrtr.com s0ny.net safersignup.de safetymail.info safetypost.de sandelf.de saynotospams.com
    selfdestructingmail.com sendspamhere.com sharklasers.com shieldedmail.com shiftmail.com shitmail.me
    shortmail.net skeefmail.com slaskpost.se slopsbox.com smellfear.com snakemail.com sneakemail.com
    sofimail.com sofort-mail.de sogetthis.com soodonims.com spam.la spam.su spam4.me spamavert.com spambob.com
    spambob.net spambob.org spambog.com spambog.de spambog.ru spambox.info spambox.us spamcannon.com
    spamcannon.net spamcero.com spamcon.org spamcorptastic.com spamcowboy.com spamcowboy.net spamcowboy.org
    spamday.com spamex.com spamfree24.com spamfree24.de spamfree24.eu spamfree24.info spamfree24.net
    spamfree24.org spamgourmet.com spamgourmet.net spamgourmet.org spamherelots.com spamhereplease.com
    spamhole.com spamify.com spaminator.de spamkill.info spaml.com spaml.de spammotel.com spamobox.com
    spamoff.de spamslicer.com spamspot.com spamthis.co.uk spamthisplease.com spamtrail.com speed.1s.fr
    spoofmail.de stuffmail.de supergreatmail.com supermailer.jp superrito.com suremail.info tafmail.com
    tagyourself.com teewars.org teleworm.com teleworm.us temp-mail.io temp-mail.org tempail.com tempalias.com
    tempe-mail.com tempemail.biz tempemail.com tempemail.net tempinbox.co.uk tempinbox.com tempmail.dev
    tempmail.it tempmail.net tempmail2.com tempmaildemo.com tempmailer.com tempmailo.com tempomail.fr
    temporarily.de temporarioemail.com.br temporaryemail.net temporaryforwarding.com temporaryinbox.com
    tempr.email thankyou2010.com thisisnotmyrealemail.com throwam.com throwawayemailaddress.com tilien.com
    tmail.ws tmailinator.com tmpmail.net tmpmail.org toiea.com tradermail.info trash-amil.com trash-mail.at
    trash-mail.com trash-mail.de trash2009.com trashdevil.com trashdevil.de trashemail.de trashmail.at
    trashmail.com trashmail.de trashmail.me trashmail.net trashmail.org trashmail.ws trashmailer.com
    trashymail.com trashymail.net trbvm.com turual.com twinmail.de tyldd.com uggsrock.com upliftnow.com
    uplipht.com venompen.com veryrealemail.com viditag.com viewcastmedia.com viewcastmedia.net
    viewcastmedia.org vomoto.com vubby.com walala.org walkmail.net webemail.me webm4il.info
    weg-werf-email.de wegwerf-emails.de wegwerfadresse.de wegwerfemail.com wegwerfemail.de wegwerfmail.de
    wegwerfmail.info wegwerfmail.net wegwerfmail.org wh4f.org whyspam.me willselfdestruct.com winemaven.info
    wronghead.com wuzup.net wuzupmail.net xagloo.com xemaps.com xents.com xmaily.com xoxy.net yep.it
    yogamaven.com yopmail.com yopmail.fr yopmail.net yuurok.com zehnminutenmail.de zippymail.info zoemail.net
    zoemail.org
    """.split()
)

# Consumer / free webmail providers (a person's address there is not a business address).
FREE_PROVIDERS: frozenset[str] = frozenset(
    """
    gmail.com googlemail.com
    yahoo.com yahoo.fr yahoo.co.uk yahoo.de yahoo.es yahoo.it yahoo.ca yahoo.com.br yahoo.co.jp yahoo.co.in
    yahoo.com.au yahoo.com.mx yahoo.com.ar yahoo.nl yahoo.se yahoo.ie yahoo.gr yahoo.co.nz ymail.com
    rocketmail.com
    outlook.com outlook.fr outlook.de outlook.es outlook.it outlook.be outlook.com.br
    hotmail.com hotmail.fr hotmail.co.uk hotmail.de hotmail.es hotmail.it hotmail.be hotmail.ca
    hotmail.com.br hotmail.nl hotmail.ch
    live.com live.fr live.co.uk live.de live.be live.it live.nl live.ca live.com.au msn.com windowslive.com
    passport.com
    icloud.com me.com mac.com
    aol.com aol.fr aol.de aol.co.uk aim.com
    proton.me protonmail.com protonmail.ch pm.me tutanota.com tutanota.de tuta.io tutamail.com
    gmx.com gmx.fr gmx.de gmx.net gmx.at gmx.ch gmx.us gmx.co.uk web.de mail.com email.com usa.com post.com
    orange.fr wanadoo.fr free.fr sfr.fr neuf.fr cegetel.net club-internet.fr laposte.net bbox.fr
    numericable.fr numericable.com noos.fr aliceadsl.fr libertysurf.fr tiscali.fr nordnet.fr
    libero.it virgilio.it alice.it tin.it email.it inwind.it iol.it tiscali.it
    t-online.de freenet.de arcor.de posteo.de posteo.net mailbox.org
    yandex.com yandex.ru ya.ru mail.ru inbox.ru list.ru bk.ru rambler.ru
    zoho.com zohomail.com zohomail.eu fastmail.com fastmail.fm hushmail.com hey.com duck.com
    qq.com 163.com 126.com sina.com sohu.com naver.com daum.net hanmail.net rediffmail.com
    seznam.cz centrum.cz wp.pl o2.pl onet.pl interia.pl op.pl
    telenet.be skynet.be scarlet.be bluewin.ch hispeed.ch sunrise.ch
    ziggo.nl kpnmail.nl planet.nl home.nl hetnet.nl
    btinternet.com sky.com virginmedia.com talktalk.net ntlworld.com blueyonder.co.uk tiscali.co.uk
    comcast.net verizon.net att.net sbcglobal.net bellsouth.net cox.net charter.net earthlink.net juno.com
    netzero.net shaw.ca rogers.com sympatico.ca videotron.ca bigpond.com optusnet.com.au xtra.co.nz
    terra.com.br uol.com.br bol.com.br ig.com.br sapo.pt telefonica.net terra.es
    """.split()
)

# Free-provider brands registered under many country TLDs (yahoo.*, hotmail.*, gmx.* …).
_FREE_PROVIDER_LABELS: frozenset[str] = frozenset({"yahoo", "hotmail", "gmx", "ymail", "yandex"})

# Local parts that address a function/mailbox rather than a person.
ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    """
    contact contacts contactez-nous contact-us contactus info infos information informations hello hi hey
    bonjour salut hallo hola ciao welcome bienvenue kontakt contacto contatti
    support help helpdesk assistance aide hotline
    sales sale vente ventes commercial commerciale business b2b devis quote quotes estimate
    admin administration administratif administrator office bureau secretariat secretary team equipe staff
    all everyone general
    jobs job careers career recrutement recrutements recruitment recruiting emploi emplois candidature
    candidatures rh hr talent talents
    compta comptabilite accounting accounts accountant billing facturation factures facture invoice invoices
    finance payments paiement
    noreply no-reply donotreply do-not-reply nepasrepondre ne-pas-repondre mailer-daemon nobody bounce bounces
    marketing presse press media medias communication comm relations-presse pr newsletter news
    webmaster postmaster hostmaster abuse security privacy dpo rgpd gdpr legal juridique compliance
    service-client serviceclient service-clients customer-service customerservice customercare customer
    clients client sav accueil reception
    direction agence studio atelier boutique shop store magasin
    booking bookings reservation reservations resa rdv
    commande commandes order orders
    events event evenements evenement partenariat partenariats partners partner partnership
    it tech dev developers ops root sysadmin system
    feedback enquiries enquiry inquiries inquiry demande demandes question questions
    mail email courrier
    """.split()
)

# Leading tokens that make "info.paris", "contact-lyon", "sales.emea" … role addresses.
_ROLE_PREFIXES: frozenset[str] = frozenset(
    """
    contact info infos hello bonjour support sales vente ventes admin noreply jobs careers recrutement rh hr
    compta comptabilite billing facturation marketing presse press commercial devis webmaster postmaster abuse
    sav accueil booking reservation newsletter
    """.split()
)

_ROLE_COMPACT: frozenset[str] = frozenset(re.sub(r"[._-]", "", r) for r in ROLE_LOCAL_PARTS)
_SEP = re.compile(r"[._-]+")


def _parents(domain: str) -> list[str]:
    labels = domain.lower().strip(".").split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def is_disposable_domain(domain: str) -> bool:
    """True for a known disposable provider or one of its subdomains."""
    return any(d in DISPOSABLE_DOMAINS for d in _parents(domain))


def is_free_provider(domain: str) -> bool:
    """True for consumer webmail domains (gmail.com, orange.fr, yahoo.* …)."""
    d = domain.lower().strip(".")
    if d in FREE_PROVIDERS:
        return True
    labels = d.split(".")
    return 2 <= len(labels) <= 3 and labels[0] in _FREE_PROVIDER_LABELS


def is_role_local_part(local_part: str) -> bool:
    """True when the local part designates a function (contact@, info.paris@, service-client@…)."""
    lp = local_part.lower().split("+", 1)[0].strip()
    if not lp:
        return False
    if lp in ROLE_LOCAL_PARTS:
        return True
    if _SEP.sub("-", lp) in ROLE_LOCAL_PARTS or _SEP.sub("", lp) in _ROLE_COMPACT:
        return True
    tokens = [t for t in _SEP.split(lp) if t]
    return len(tokens) > 1 and tokens[0] in _ROLE_PREFIXES
