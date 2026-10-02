"""Keyword lexicons and fingerprints used across discovery, qualification and extraction.

Everything is plain Python so it can be tuned without touching the pipeline.  Patterns are compiled
case-insensitively; ``\\b`` word boundaries are added where it matters.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------------------------------
# Acquisition services (what the agency sells).  label → regex patterns
# ---------------------------------------------------------------------------------------------------
SERVICES: dict[str, list[str]] = {
    "meta ads": [r"\b(facebook|fb|meta|instagram|ig) ads?\b", r"\bmeta advertising\b", r"\bfacebook advertising\b", r"\bpaid social\b"],
    "google ads": [r"\bgoogle ads?\b", r"\badwords\b", r"\bsearch ads\b", r"\bppc\b", r"\bpay[- ]per[- ]click\b"],
    "youtube ads": [r"\byoutube ads?\b", r"\byoutube advertising\b", r"\bvideo ads\b"],
    "tiktok ads": [r"\btiktok ads?\b", r"\btik ?tok advertising\b"],
    "paid ads": [r"\bpaid (ads|advertising|traffic|media|acquisition|campaigns?)\b", r"\bmedia buy(ing|er)\b", r"\bad spend\b", r"\broas\b", r"\bcpa\b", r"\bcost per (lead|acquisition)\b"],
    "funnels": [r"\bfunnels?\b", r"\bsales funnel\b", r"\bfunnel (build|design|strateg)", r"\bwebinar funnel\b", r"\bvsl\b", r"\blanding pages?\b"],
    "lead generation": [r"\blead gen(eration)?\b", r"\bbooked calls?\b", r"\bappointments? (booked|setting)\b", r"\bqualified leads\b", r"\bbook(ed)? (strategy|discovery|sales) calls\b"],
    "launches": [r"\b(course|product|program|digital) launch(es)?\b", r"\blaunch (strategy|marketing|campaign)\b", r"\bevergreen\b"],
    "email marketing": [r"\bemail marketing\b", r"\bemail (sequences?|automation|campaigns)\b", r"\bnurture sequence\b"],
    "cro": [r"\bconversion rate optimi[sz]ation\b", r"\bcro\b", r"\bsplit[- ]test", r"\ba/b test"],
    "growth marketing": [r"\bgrowth (marketing|partner|agency)\b", r"\bperformance marketing\b", r"\bcustomer acquisition\b", r"\bscal(e|ing) (your|their|our) (business|coaching|offer|program)"],
    "organic content": [r"\borganic (growth|content|marketing)\b", r"\bcontent marketing\b", r"\bshort[- ]form (video|content)\b"],
    "seo": [r"\bseo\b", r"\bsearch engine optimi[sz]ation\b"],
    "appointment setting": [r"\bappointment sett(ing|ers?)\b", r"\bdm setting\b", r"\bsetters?\b", r"\bclosers?\b", r"\bsales team\b"],
    "copywriting": [r"\bcopywriting\b", r"\bdirect response\b"],
    "ghl / automation": [r"\bgo ?high ?level\b", r"\bghl\b", r"\bmarketing automation\b", r"\bcrm (setup|build)"],
}

# Core acquisition signal used for ICP fit: any of these labels count as "acquisition"
ACQUISITION_LABELS = {"meta ads", "google ads", "youtube ads", "tiktok ads", "paid ads", "funnels", "lead generation", "launches", "growth marketing", "appointment setting"}

# ---------------------------------------------------------------------------------------------------
# ICP: who they serve
# ---------------------------------------------------------------------------------------------------
ICP: dict[str, list[str]] = {
    "coaches": [r"\bcoach(es|ing)?\b", r"\bcoaching (business|industry|clients?|offers?|programs?)\b"],
    "course creators": [r"\bcourse creators?\b", r"\bonline courses?\b", r"\bdigital courses?\b", r"\bcourse (launch|business|sales)\b", r"\be-?learning\b"],
    "infopreneurs": [r"\binfo ?preneurs?\b", r"\binfo[- ]?products?\b", r"\binformation products?\b", r"\bknowledge (business|entrepreneurs?|commerce)\b", r"\bedupreneurs?\b", r"\bdigital products?\b", r"\beducation business(es)?\b", r"\bonline education\b", r"\bexpert(s)? (business|industry)\b"],
    "consultants": [r"\bconsultants?\b", r"\bconsulting (business|firm|offer)\b"],
    "high ticket": [r"\bhigh[- ]ticket\b", r"\bpremium (offer|program)s?\b", r"\b\$\d{1,2}k (offers?|programs?|clients?)\b"],
    "memberships": [r"\bmemberships?\b", r"\bmembership sites?\b", r"\bcommunit(y|ies)\b", r"\bmasterminds?\b"],
    "personal brands": [r"\bpersonal brands?\b", r"\bthought leaders?\b", r"\bcreators?\b", r"\binfluencers?\b", r"\bexperts?\b", r"\bauthors?\b", r"\bspeakers?\b"],
    "webinars": [r"\bwebinars?\b", r"\bmasterclass(es)?\b", r"\bchallenges?\b", r"\bworkshops?\b", r"\bevergreen webinar\b", r"\bvsl\b"],
    "platforms": [r"\bkajabi\b", r"\bclickfunnels\b", r"\bteachable\b", r"\bthinkific\b", r"\bkartra\b", r"\bgo ?high ?level\b", r"\bsysteme\.io\b", r"\bskool\b", r"\bpodia\b", r"\bcircle\.so\b", r"\bmighty networks\b", r"\bsamcart\b", r"\bthrivecart\b"],
}

# The ICP labels that define "infopreneur / coach" (must be present for qualification)
CORE_ICP_LABELS = {"coaches", "course creators", "infopreneurs", "high ticket", "memberships", "webinars"}

COACH_ROLE_WORDS = [
    "business coach", "life coach", "executive coach", "mindset coach", "health coach", "fitness coach", "wellness coach",
    "relationship coach", "dating coach", "career coach", "leadership coach", "money coach", "financial coach",
    "sales coach", "marketing coach", "real estate coach", "trading coach", "trading educator", "forex educator",
    "ecommerce coach", "amazon fba coach", "spiritual coach", "transformational coach", "confidence coach",
    "parenting coach", "nutrition coach", "performance coach", "productivity coach", "public speaking coach",
    "course creator", "online course creator", "infopreneur", "digital course creator", "online educator",
    "consultant", "mentor", "trainer", "author", "speaker", "founder of", "creator of", "ceo of", "host of",
    "coaching program", "signature program", "mastermind", "membership", "academy", "bootcamp", "masterclass",
    "online program", "group program", "certification program", "online course", "coach",
]
COACH_ROLE_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(COACH_ROLE_WORDS, key=len, reverse=True)) + r")\b", re.I)

# ---------------------------------------------------------------------------------------------------
# Agency-ness / anti-signals
# ---------------------------------------------------------------------------------------------------
AGENCY_SIGNALS = [
    r"\bagency\b", r"\bour (clients|team|services|work|process|case studies)\b", r"\bwe (help|work with|partner with|scale|generate|build|manage|run)\b",
    r"\bdone[- ]for[- ]you\b", r"\bbook (a|your) (call|demo|strategy session|free consultation)\b", r"\bapply (now|to work with us)\b",
    r"\bcase stud(y|ies)\b", r"\bclient (results|success|stories|testimonials|wins)\b", r"\bmanaged (ad|campaigns?)\b", r"\bresults\b",
]
AGENCY_WORDS_RE = re.compile(r"\b(agency|agencies|studio|collective|consultancy|partners?|media|marketing|growth|digital|ads?|funnels?|labs?)\b", re.I)

# Sites that are not agencies: software, job boards, blogs, education platforms, personal portfolios
NON_AGENCY_SIGNALS = [
    (r"\b(start (your )?free trial|pricing plans?|per month|/mo\b|sign up free|free plan)\b", "saas"),
    (r"\b(apply for this job|job description|open positions|careers at)\b", "job_board"),
    (r"\b(top|best) \d+ (marketing |ads? |facebook ads? )?agenc(y|ies)\b", "listicle"),
    (r"\b(we are a directory|agency directory|find an agency|compare agencies)\b", "directory"),
    (r"\b(domain (is )?for sale|buy this domain|this domain may be for sale|parked (free )?courtesy|coming soon|under construction|website is being built)\b", "parked"),
    (r"\b(enroll now|join the course|course curriculum|modules? \d|lesson \d)\b", "course_platform"),
]

PARKED_SIGNALS = re.compile(
    r"(domain (is )?for sale|buy this domain|this domain may be for sale|parked (free )?courtesy|sedo\.com|dan\.com|afternic|hugedomains|"
    r"godaddy\.com/forsale|namecheap\.com/domains|is parked|coming soon|under construction|website is being built|"
    r"account suspended|this site can.t be reached|default web page|apache2 (debian|ubuntu) default|welcome to nginx|"
    r"index of /|site not found|no longer available|has expired|renew(al)? (now|your domain)|wix\.com/.*premium|"
    r"this website is temporarily unavailable|website expired|hosting account)",
    re.I,
)

LISTICLE_TITLE_RE = re.compile(
    r"(\b(top|best|leading|\d{1,3})\b.*\b(agenc(y|ies)|companies|firms|partners|experts|freelancers|services)\b)|(\bagenc(y|ies)\b.*\b(to (hire|work with|know)|in \d{4}|list|roundup)\b)",
    re.I,
)

# ---------------------------------------------------------------------------------------------------
# Tech / platform fingerprints (regex on raw HTML)
# ---------------------------------------------------------------------------------------------------
TECH_FINGERPRINTS: dict[str, list[str]] = {
    "clickfunnels": [r"clickfunnels\.com", r"cf2\.?pages", r"data-cf-", r"myclickfunnels", r"cfcdn\.com", r"id=\"cf-"],
    "kajabi": [r"kajabi", r"mykajabi\.com", r"kajabi-cdn", r"kajabi-storefront"],
    "gohighlevel": [r"leadconnectorhq\.com", r"msgsndr\.com", r"gohighlevel", r"stcdn\.leadconnectorhq", r"funnel-preview"],
    "kartra": [r"kartra\.com", r"app\.kartra", r"kartra-"],
    "leadpages": [r"leadpages\.net", r"lpages\.co", r"leadpages"],
    "systeme.io": [r"systeme\.io", r"systeme-io"],
    "webflow": [r"webflow\.com", r"data-wf-", r"webflow\.js"],
    "wordpress": [r"wp-content", r"wp-includes", r"wp-json"],
    "elementor": [r"elementor"],
    "squarespace": [r"squarespace", r"static1\.squarespace"],
    "wix": [r"wix\.com", r"wixstatic", r"wix-code"],
    "hubspot": [r"hs-scripts\.com", r"hubspot", r"hsforms"],
    "unbounce": [r"unbounce"],
    "instapage": [r"instapage"],
    "teachable": [r"teachable\.com", r"teachablecdn"],
    "thinkific": [r"thinkific"],
    "podia": [r"podia\.com"],
    "samcart": [r"samcart"],
    "thrivecart": [r"thrivecart"],
    "stripe": [r"js\.stripe\.com", r"checkout\.stripe"],
    "calendly": [r"calendly\.com"],
    "acuity": [r"acuityscheduling", r"squarespace\.com/scheduling"],
    "typeform": [r"typeform\.com"],
    "convertkit": [r"convertkit", r"kit\.com/forms", r"ck\.page"],
    "activecampaign": [r"activehosted\.com", r"activecampaign"],
    "mailchimp": [r"list-manage\.com", r"mailchimp"],
    "klaviyo": [r"klaviyo"],
    "manychat": [r"manychat"],
    "wistia": [r"wistia"],
    "vimeo": [r"player\.vimeo\.com"],
    "vidalytics": [r"vidalytics"],
    "meta pixel": [r"connect\.facebook\.net", r"fbq\(\s*['\"]init"],
    "google ads tag": [r"googleads\.g\.doubleclick|gtag\(\s*['\"]config['\"]\s*,\s*['\"]AW-"],
    "tiktok pixel": [r"analytics\.tiktok\.com"],
    "hotjar": [r"hotjar"],
    "intercom": [r"intercom"],
    "drift": [r"drift\.com"],
    "hyros": [r"hyros"],
    "wicked reports": [r"wickedreports"],
    "funnelish": [r"funnelish"],
    "groovefunnels": [r"groove(pages|funnels|\.cm)"],
    "skool": [r"skool\.com"],
    "circle": [r"circle\.so"],
    "mighty networks": [r"mn\.co|mightynetworks"],
    "stan store": [r"stan\.store"],
    "framer": [r"framer\.com|framerusercontent"],
    "shopify": [r"cdn\.shopify\.com"],
}
TECH_RE = {k: re.compile("|".join(v), re.I) for k, v in TECH_FINGERPRINTS.items()}

FUNNEL_PLATFORMS = {"clickfunnels", "kajabi", "gohighlevel", "kartra", "leadpages", "systeme.io", "unbounce", "instapage", "funnelish", "groovefunnels", "samcart", "thrivecart", "teachable", "thinkific", "podia", "webflow", "wordpress", "elementor", "squarespace", "wix", "hubspot", "framer", "stan store", "skool"}

# ---------------------------------------------------------------------------------------------------
# Funnel types.  Patterns matched on URL path + anchor text + page title/H1.
# ---------------------------------------------------------------------------------------------------
FUNNEL_TYPES: dict[str, list[str]] = {
    "webinar": [r"\bwebinar", r"\bmasterclass", r"\bfree training", r"\btraining\b", r"\bworkshop", r"\bregister\b", r"\bsave (my|your) seat", r"\bon[- ]demand", r"\bwatch now"],
    "vsl": [r"\bvsl\b", r"\bvideo sales", r"\bwatch (the|this) (free )?video", r"\bcase[- ]study video"],
    "application": [r"\bapply\b", r"\bapplication", r"\bbook (a|your|my) (call|session|consult)", r"\bstrategy (call|session)", r"\bschedule (a|your) call", r"\bdiscovery call", r"\bclarity call", r"\bfree consult", r"\bcalendly\.com", r"\bbreakthrough (call|session)", r"\bget started"],
    "lead_magnet": [r"\bfree (guide|ebook|e-book|checklist|template|pdf|download|cheat ?sheet|toolkit|blueprint|playbook|workbook|report|gift)", r"\bdownload\b", r"\bopt[- ]?in", r"\bfreebie", r"\bgrab (the|your) free"],
    "challenge": [r"\b\d+[- ]day challenge", r"\bchallenge\b", r"\bbootcamp"],
    "quiz": [r"\bquiz\b", r"\bassessment\b", r"\bscorecard\b", r"\btake the (test|quiz)"],
    "low_ticket": [r"\b\$\d{1,2}(\.\d\d)?\b", r"\btripwire", r"\bmini[- ]course", r"\bworkshop replay", r"\bbundle\b", r"\bself[- ]paced"],
    "course_sales": [r"\benroll", r"\bjoin (the|now|today)\b", r"\bcourse\b", r"\bacademy\b", r"\bprogram\b", r"\bmembership\b", r"\bmastermind", r"\bcertification", r"\bdoors (are )?(open|closing)", r"\bwaitlist"],
    "book_funnel": [r"\bfree (\+ shipping|book)", r"\bget (the|my|your) book", r"\bbook funnel"],
    "newsletter": [r"\bnewsletter", r"\bsubscribe\b", r"\bjoin \d+[,.]?\d* (subscribers|readers)"],
    "community": [r"\bskool\.com", r"\bcircle\.so", r"\bfacebook\.com/groups", r"\bjoin (the|our) (free )?(community|group)"],
    "link_in_bio": [r"linktr\.ee", r"beacons\.ai", r"stan\.store", r"bio\.link", r"linkin\.bio"],
}
FUNNEL_TYPE_RE = {k: re.compile("|".join(v), re.I) for k, v in FUNNEL_TYPES.items()}

# Pages on a client site that most likely *are* the funnel entry
FUNNEL_PATH_HINTS = re.compile(
    r"(webinar|masterclass|training|workshop|register|apply|application|book|call|schedule|consult|free|guide|download|optin|opt-in|"
    r"challenge|quiz|assessment|enroll|join|program|course|academy|mastermind|membership|offer|special|vsl|watch|start|waitlist|bootcamp|coaching|work-with|workwith)",
    re.I,
)

EXTERNAL_FUNNEL_HOSTS = re.compile(
    r"(clickfunnels\.com|mykajabi\.com|kajabi\.com|systeme\.io|leadpages\.(co|net)|lpages\.co|kartra\.com|gohighlevel\.com|msgsndr\.com|"
    r"funnelish\.com|groovepages\.com|samcart\.com|thrivecart\.com|teachable\.com|thinkific\.com|podia\.com|stan\.store|skool\.com|"
    r"calendly\.com|acuityscheduling\.com|typeform\.com|ck\.page|convertkit\.com|kit\.com|webinarjam\.com|everwebinar\.com|"
    r"demio\.com|zoom\.us/webinar|hubspotpagebuilder|instapage\.com|unbounce\.com|carrd\.co|linktr\.ee|beacons\.ai)",
    re.I,
)

PRICE_RE = re.compile(r"(\$|€|£)\s?\d{1,3}(?:[,.]\d{3})*(?:\.\d{2})?(?:\s?(k|/mo|/month|per month))?", re.I)

# ---------------------------------------------------------------------------------------------------
# Client / case-study extraction
# ---------------------------------------------------------------------------------------------------
CLIENT_CONTEXT_RE = re.compile(
    r"\b(client|clients|case stud(y|ies)|testimonial|results?|success stor(y|ies)|we helped|helped|worked with|working with|partnered with|"
    r"scaled|generated|grew|took|brought|launched|from \$|to \$|roas|revenue|in sales|per month|in \d+ days|booked calls|leads)\b",
    re.I,
)
CASE_STUDY_PATH_RE = re.compile(r"(case[-_ ]?stud|client|results|success|testimonial|portfolio|our-work|work/|wins|stories|review|proof|partners?/)", re.I)
MONEY_RESULT_RE = re.compile(r"(\$|€|£)\s?\d[\d,.]*\s?(k|m|million|thousand)?\b|\b\d+(\.\d+)?x\s?(roas|return)\b|\b\d{2,3}%\b", re.I)

# Names we must never report as clients (platforms, generic words)
NAME_STOPWORDS = {
    "facebook", "instagram", "google", "youtube", "tiktok", "linkedin", "meta", "kajabi", "clickfunnels", "teachable",
    "thinkific", "podia", "kartra", "gohighlevel", "highlevel", "calendly", "zoom", "shopify", "amazon", "apple", "microsoft",
    "hubspot", "mailchimp", "convertkit", "activecampaign", "stripe", "paypal", "wordpress", "webflow", "squarespace", "wix",
    "chatgpt", "openai", "forbes", "entrepreneur", "inc", "business insider", "the new york times", "usa today", "cnn",
    "the agency", "our team", "our clients", "the client", "case study", "case studies", "testimonials", "results",
    "privacy policy", "terms of service", "learn more", "read more", "book a call", "contact us", "about us", "get started",
    "home", "services", "blog", "faq", "faqs", "login", "sign up", "sign in", "apply now", "free training", "watch now",
    "united states", "united kingdom", "canada", "australia", "new york", "los angeles", "london", "sydney", "toronto",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "october", "november", "december", "ceo", "founder", "coach",
    "digital marketing", "marketing", "ads", "ads manager", "business manager", "meta ads", "facebook ads", "google ads",
}

# ---------------------------------------------------------------------------------------------------
# Query matrix
# ---------------------------------------------------------------------------------------------------
SERVICE_QUERY_TERMS = [
    "facebook ads agency", "meta ads agency", "paid ads agency", "ppc agency", "youtube ads agency", "google ads agency",
    "tiktok ads agency", "funnel agency", "sales funnel agency", "lead generation agency", "performance marketing agency",
    "media buying agency", "paid traffic agency", "webinar funnel agency", "launch agency", "growth marketing agency",
    "paid social agency", "digital marketing agency", "funnel building agency", "clickfunnels agency", "kajabi marketing agency",
    "ads management for", "done for you ads", "ad agency", "marketing agency", "growth partner", "advertising agency",
    "evergreen funnel agency", "high ticket ads agency", "appointment setting agency", "gohighlevel agency",
]

ICP_QUERY_TERMS = [
    "coaches", "online coaches", "course creators", "info products", "infopreneurs", "digital products", "online courses",
    "coaching businesses", "high ticket coaches", "consultants and coaches", "experts and coaches", "personal brands",
    "knowledge businesses", "membership sites", "masterminds", "thought leaders", "business coaches", "life coaches",
    "fitness coaches", "health coaches", "mindset coaches", "relationship coaches", "dating coaches", "real estate coaches",
    "trading educators", "financial educators", "course launches", "online educators", "info product businesses",
    "online course businesses", "coaching programs", "digital course creators", "education businesses", "creators and coaches",
    "spiritual coaches", "wellness coaches", "executive coaches", "career coaches", "money coaches", "ecommerce coaches",
]

PHRASE_QUERIES = [
    "we help coaches scale with facebook ads", "we help course creators scale their launches", "scale your coaching business with paid ads agency",
    "agency that helps course creators get leads", "coaching funnel agency case study", "high ticket coaching ads agency results",
    "info product marketing agency case study", "course launch agency case study", "evergreen webinar funnel agency clients",
    "done for you facebook ads for coaches", "done for you funnels for coaches", "paid ads for online coaches agency",
    "meta ads agency for course creators case study", "youtube ads agency for coaches case study", "lead generation for coaches agency testimonials",
    "we help coaches and consultants book more calls", "we scale coaches to 7 figures with ads", "ads agency for experts and coaches",
    "funnel agency for kajabi course creators", "clickfunnels certified partner coaches", "marketing agency specializing in coaches",
    "marketing agency specializing in online courses", "marketing agency specializing in info products", "coaching industry marketing agency",
    "agency for coaches book a call case studies", "we help online course creators generate leads with paid ads",
    "agency helping coaches generate booked calls with meta ads", "paid media agency for digital course launches",
    "performance marketing agency for coaches and course creators", "growth partner for coaches and consultants",
    "facebook ads management for coaches and consultants", "marketing agency for mastermind and membership businesses",
    "launch strategist agency for course creators", "we build webinar funnels for coaches", "youtube ads for course creators agency",
    "tiktok ads agency for creators and coaches", "ads agency for online education businesses", "coaching business lead generation agency",
    "agency for high ticket coaches and consultants", "we help experts scale their online programs", "online course marketing agency clients",
    "we help infopreneurs scale", "agency for infopreneurs", "paid ads agency for infopreneurs", "funnel agency for infopreneurs",
]

LISTICLE_QUERIES = [
    "best facebook ads agencies for coaches", "top marketing agencies for course creators", "best funnel agencies for coaches",
    "top paid ads agencies for coaches and consultants", "best agencies for online course launches", "top lead generation agencies for coaches",
    "best youtube ads agencies for course creators", "best marketing agencies for info products", "top agencies for coaching businesses",
    "best marketing agencies for coaches 2025", "best marketing agencies for coaches 2026", "top facebook ads agencies for course creators 2025",
    "best growth marketing agencies for coaches", "best meta ads agencies for coaches", "top webinar funnel agencies",
    "best agencies for high ticket coaches", "best ad agencies for online educators", "top digital marketing agencies for coaches uk",
    "best facebook ads agencies for coaches australia", "best marketing agencies for coaches canada", "best kajabi agencies",
    "best clickfunnels agencies", "best gohighlevel agencies for coaches", "top launch agencies for course creators",
]

SEARCH_MODIFIERS = ['"case study"', '"clients"', '"results"', '"book a call"', '"testimonials"']
