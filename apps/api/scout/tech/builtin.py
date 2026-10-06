"""Built-in technology fingerprints (fallback when the wappalyzergo service is unavailable).

Signals: response headers, ``<meta name="generator">``, the cached home ``head_html`` (head +
script/link/iframe tags + short inline scripts), link URLs, and (weakly) visible text.
Each detection keeps the verbatim fragment that matched as evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scout.tech.types import DetectedTech

_I = re.IGNORECASE

CONF_META = 0.95
CONF_HEADER = 0.9
CONF_HTML = 0.9
CONF_LINK = 0.8
CONF_WEAK = 0.6
CONF_TEXT = 0.6
CONF_IMPLIED = 0.8


@dataclass(frozen=True)
class Signature:
    name: str
    category: str
    html: tuple[re.Pattern[str], ...] = ()
    weak: tuple[re.Pattern[str], ...] = ()
    headers: tuple[tuple[str, re.Pattern[str] | None], ...] = ()
    meta: tuple[re.Pattern[str], ...] = ()
    links: tuple[re.Pattern[str], ...] = ()
    text: tuple[re.Pattern[str], ...] = ()
    implies: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    version: str | None = None  # fixed version label (e.g. "GA4")


def _sig(
    name: str,
    category: str,
    *,
    html: tuple[str, ...] = (),
    weak: tuple[str, ...] = (),
    headers: tuple[tuple[str, str | None], ...] = (),
    meta: tuple[str, ...] = (),
    links: tuple[str, ...] = (),
    text: tuple[str, ...] = (),
    implies: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    version: str | None = None,
) -> Signature:
    return Signature(
        name=name,
        category=category,
        html=tuple(re.compile(p, _I) for p in html),
        weak=tuple(re.compile(p, _I) for p in weak),
        headers=tuple((h.lower(), re.compile(p, _I) if p else None) for h, p in headers),
        meta=tuple(re.compile(p, _I) for p in meta),
        links=tuple(re.compile(p, _I) for p in links),
        text=tuple(re.compile(p, _I) for p in text),
        implies=implies,
        aliases=aliases,
        version=version,
    )


SIGNATURES: tuple[Signature, ...] = (
    # ---- CMS / site builders / e-commerce ------------------------------------------------------
    _sig("WordPress", "CMS", html=(r"/wp-content/(?:themes|plugins|uploads)/", r"/wp-includes/", r"wp-emoji-release\.min\.js", r"/wp-json/"),
         meta=(r"WordPress ?(?P<version>\d+(?:\.\d+)*)?",), headers=(("link", r"api\.w\.org"), ("x-pingback", r"xmlrpc\.php")),
         aliases=("wp",)),
    _sig("WooCommerce", "Ecommerce", html=(r"wp-content/plugins/woocommerce", r"woocommerce-(?:layout|general|smallscreen|no-js)", r"wc-blocks", r"\bwc_add_to_cart_params\b"),
         meta=(r"WooCommerce ?(?P<version>\d+(?:\.\d+)*)?",), implies=("WordPress",), aliases=("woo", "woo commerce")),
    _sig("Elementor", "Page builder", html=(r"wp-content/plugins/elementor", r"elementor-frontend", r"elementor-kit-\d+", r"\belementor-default\b"),
         meta=(r"Elementor ?(?P<version>\d+(?:\.\d+)*)?",), implies=("WordPress",)),
    _sig("Divi", "Page builder", html=(r"wp-content/themes/Divi", r"\bet_pb_", r"divi-style", r"\bet-divi\b", r"et_divi_theme"),
         meta=(r"Divi v\.?(?P<version>\d+(?:\.\d+)*)",), implies=("WordPress",)),
    _sig("Yoast SEO", "SEO", html=(r"yoast-schema-graph", r"optimized with the Yoast SEO", r"wp-content/plugins/wordpress-seo"), implies=("WordPress",)),
    _sig("Contact Form 7", "Forms", html=(r"wp-content/plugins/contact-form-7",), implies=("WordPress",)),
    _sig("WPML", "Translation", html=(r"wp-content/plugins/sitepress-multilingual-cms",), implies=("WordPress",)),
    _sig("Shopify", "Ecommerce", html=(r"cdn\.shopify\.com", r"cdn\.shopifycdn\.net", r"[\w-]+\.myshopify\.com", r"\bShopify\.(?:theme|shop|routes)\b", r"shopify-section", r"shopify-payment-button"),
         headers=(("x-shopid", None), ("x-shopify-stage", None), ("x-sorting-hat-shopid", None), ("powered-by", r"shopify")),
         text=(r"powered by shopify",), aliases=("shopify plus",)),
    _sig("Webflow", "Website builder", html=(r"data-wf-page=", r"data-wf-site=", r"assets\.website-files\.com", r"cdn\.prod\.website-files\.com", r"webflow\.(?:js|min\.js)"),
         meta=(r"\bWebflow\b",)),
    _sig("Webflow Ecommerce", "Ecommerce", html=(r"w-commerce-", r"data-wf-ecommerce", r"wf-commerce"), implies=("Webflow",)),
    _sig("Wix", "Website builder", html=(r"static\.wixstatic\.com", r"static\.parastorage\.com", r"wixBiSession", r"_wixCIDX", r"wix-code"),
         headers=(("x-wix-request-id", None),), meta=(r"Wix\.com Website Builder",)),
    _sig("Squarespace", "Website builder", html=(r"static1\.squarespace\.com", r"squarespace-cdn\.com", r"Static\.SQUARESPACE_CONTEXT", r"\bsqs-block\b"),
         meta=(r"Squarespace",), headers=(("server", r"squarespace"),)),
    _sig("Squarespace Commerce", "Ecommerce", html=(r"sqs-add-to-cart-button", r"squarespace-commerce", r"sqs-commerce"), implies=("Squarespace",)),
    _sig("Framer", "Website builder", html=(r"framerusercontent\.com", r"framer\.com/m/", r"data-framer-", r"events\.framer\.com"),
         meta=(r"Framer ?(?P<version>[\w.]+)?",)),
    _sig("Bubble", "No-code", html=(r"_bubble_page_load_data", r"bubble_session_uid", r"cdn\.bubble\.io", r"\.bubbleapps\.io"), meta=(r"\bBubble\b",)),
    _sig("PrestaShop", "Ecommerce", html=(r"/modules/ps_", r"var prestashop\s*=", r"prestashop"), meta=(r"PrestaShop",)),
    _sig("Magento", "Ecommerce", html=(r"Mage\.Cookies", r"/static/version\d+/frontend/", r"mage/cookies", r"data-mage-init", r"x-magento-init"),
         headers=(("x-magento-cache-debug", None), ("x-magento-tags", None)), meta=(r"Magento",), aliases=("adobe commerce",)),
    _sig("BigCommerce", "Ecommerce", html=(r"cdn\d*\.bigcommerce\.com", r"bigcommerce\.com/s-", r"\bBCData\b")),
    _sig("Odoo", "ERP", html=(r"odoo\.define", r"data-oe-model", r"/website/static/src/", r"/web/assets/\d"), meta=(r"\bOdoo\b",)),
    _sig("Joomla", "CMS", html=(r"/media/jui/", r"/media/system/js/", r"option=com_"), meta=(r"Joomla!? ?(?P<version>\d+(?:\.\d+)*)?",)),
    _sig("Drupal", "CMS", html=(r"Drupal\.settings", r"/sites/default/files/", r"\bdrupal\.js", r"data-drupal-"),
         meta=(r"Drupal ?(?P<version>\d+)?",), headers=(("x-drupal-cache", None), ("x-generator", r"drupal"))),
    _sig("Ghost", "CMS", html=(r"ghost-portal", r"/ghost/api/", r"\.ghost\.io"), meta=(r"Ghost ?(?P<version>\d+(?:\.\d+)*)?",)),
    _sig("Hostinger Website Builder", "Website builder", html=(r"zyrosite\.com", r"userapp\.zyrosite", r"builder\.hostinger"),
         meta=(r"Hostinger Website Builder", r"\bZyro\b"), aliases=("zyro",)),
    _sig("GoDaddy Website Builder", "Website builder", html=(r"img1\.wsimg\.com/isteam", r"websites\.godaddy\.com", r"img1\.wsimg\.com/blobby"),
         meta=(r"Starfield Technologies", r"Go ?Daddy Website Builder"), aliases=("godaddy builder", "godaddy")),
    _sig("Jimdo", "Website builder", html=(r"jimcdn\.com", r"jimdostatic\.com", r"jimdo-dolphin", r"\.jimdo(?:site)?\.com"), meta=(r"Jimdo",)),
    _sig("Weebly", "Website builder", html=(r"editmysite\.com", r"_W\.configDomain", r"weebly\.com"), meta=(r"Weebly",)),
    _sig("Substack", "Publishing", html=(r"substackcdn\.com", r"substack\.com/(?:embed|api)"), links=(r"https?://[\w-]+\.substack\.com",)),
    _sig("Kajabi", "Online courses", html=(r"kajabi-cdn\.com", r"kajabi-storefronts", r"\.mykajabi\.com"), meta=(r"Kajabi",)),
    _sig("Teachable", "Online courses", html=(r"teachablecdn\.com", r"fedora\.teachablecdn", r"\.teachable\.com")),
    _sig("Podia", "Online courses", html=(r"cdn\.podia\.com", r"podia-assets", r"\.podia\.com"), links=(r"\.podia\.com",)),
    _sig("Systeme.io", "Funnels", html=(r"systeme\.io",), links=(r"systeme\.io",), aliases=("systeme", "systemeio")),
    _sig("ClickFunnels", "Funnels", html=(r"clickfunnels\.com", r"cfimg\.com", r"myclickfunnels\.com", r"cf-builder")),
    _sig("Leadpages", "Landing pages", html=(r"leadpages\.(?:net|co|io)", r"\blp-pom\b")),
    _sig("Unbounce", "Landing pages", html=(r"unbounce\.com", r"ubembed\.com", r"\bub-emb", r"unbouncepages\.com")),
    # ---- CRM / marketing automation / email ---------------------------------------------------
    _sig("HubSpot", "Marketing automation", html=(r"js(?:-[a-z0-9]+)?\.hs-scripts\.com", r"js(?:-[a-z0-9]+)?\.hs-analytics\.net", r"js\.hsadspixel\.net", r"js\.hs-banner\.com", r"js\.usemessages\.com", r"_hsq\.push", r"hs-script-loader", r"js\.hubspot\.com"),
         headers=(("x-hs-hub-id", None),), links=(r"meetings\.hubspot\.com", r"share\.hsforms\.com")),
    _sig("HubSpot Forms", "Forms", html=(r"js(?:-[a-z0-9]+)?\.hsforms\.net", r"hbspt\.forms\.create", r"\bhs-form\b"), implies=("HubSpot",)),
    _sig("Salesforce", "CRM", html=(r"service\.force\.com", r"salesforce\.com/embeddedservice", r"my\.salesforce\.com", r"salesforce-sites\.com", r"servlet\.WebToLead", r"\.force\.com/")),
    _sig("Pardot", "Marketing automation", html=(r"pi\.pardot\.com", r"pardot\.com/pd\.js", r"\bpiAId\s*=", r"go\.pardot\.com"), implies=("Salesforce",),
         aliases=("salesforce pardot", "account engagement")),
    _sig("Pipedrive", "CRM", html=(r"leadbooster-chat\.pipedrive\.com", r"webforms\.pipedrive\.com", r"pipedriveLeadboosterConfig"), links=(r"pipedrive\.com",)),
    _sig("Zoho CRM", "CRM", html=(r"crm\.zoho\.(?:com|eu|in)/crm/WebToLeadForm", r"zohocrm"), aliases=("zoho",)),
    _sig("Klaviyo", "Email marketing", html=(r"static\.klaviyo\.com", r"klaviyo\.com/onsite", r"\b_learnq\b", r"a\.klaviyo\.com")),
    _sig("Mailchimp", "Email marketing", html=(r"chimpstatic\.com", r"list-manage\.com", r"mc\.us\d+\.list-manage", r"\bmc-validate\b", r"mailchimp\.com/"),
         links=(r"list-manage\.com", r"eepurl\.com", r"mailchi\.mp")),
    _sig("Brevo", "Email marketing", html=(r"sibforms\.com", r"sendinblue\.com", r"brevo\.com", r"sib-conversations", r"sibautomation\.com"),
         links=(r"sibforms\.com", r"brevo\.com"), aliases=("sendinblue",)),
    _sig("ActiveCampaign", "Marketing automation", html=(r"trackcmp\.net", r"activehosted\.com", r"activecampaign\.com", r"diffuser-cdn\.app-us1\.com")),
    _sig("Lemlist", "Sales engagement", html=(r"lemlist\.com", r"lemlst\.com"), links=(r"lemlist\.com",)),
    _sig("Zapier", "Automation", html=(r"interfaces\.zapier\.com", r"zapier\.com/(?:hooks|interfaces|apps/embed)", r"zapier-interfaces"), links=(r"zapier\.com/(?:hooks|interfaces)",)),
    # ---- chat / support / scheduling / forms -------------------------------------------------
    _sig("ManyChat", "Chatbot", html=(r"widget\.manychat\.com", r"\bmcwidget\b", r"mccdn\.me", r"manychat\.com"), links=(r"manychat\.com",)),
    _sig("Intercom", "Live chat", html=(r"widget\.intercom\.io", r"js\.intercomcdn\.com", r"intercomSettings", r"Intercom\(\s*['\"]boot")),
    _sig("Crisp", "Live chat", html=(r"client\.crisp\.chat", r"\$crisp\b", r"CRISP_WEBSITE_ID")),
    _sig("Zendesk", "Customer support", html=(r"static\.zdassets\.com", r"\bze-snippet\b", r"\bzEmbed\b", r"zopim"), links=(r"\.zendesk\.com",)),
    _sig("Drift", "Live chat", html=(r"js\.driftt\.com", r"drift\.com/include", r"\bdrift\.load\(")),
    _sig("Tidio", "Live chat", html=(r"code\.tidio\.co", r"tidioChatApi")),
    _sig("LiveChat", "Live chat", html=(r"cdn\.livechatinc\.com", r"livechatinc\.com")),
    _sig("Tawk.to", "Live chat", html=(r"embed\.tawk\.to",), aliases=("tawk",)),
    _sig("Smartsupp", "Live chat", html=(r"smartsuppchat\.com", r"\bsmartsupp\b")),
    _sig("Gorgias", "Customer support", html=(r"config\.gorgias\.chat", r"gorgias\.chat")),
    _sig("Zoho SalesIQ", "Live chat", html=(r"salesiq\.zoho\.", r"zoho\.com/salesiq")),
    _sig("Freshworks", "Customer support", html=(r"wchat\.freshchat\.com", r"widget\.freshworks\.com", r"freshdesk\.com"), aliases=("freshchat", "freshdesk")),
    _sig("Messenger Chat Plugin", "Live chat", html=(r"xfbml\.customerchat", r"\bfb-customerchat\b")),
    _sig("WhatsApp", "Messaging", links=(r"wa\.me/", r"api\.whatsapp\.com/send", r"whatsapp\.com/send"), aliases=("whatsapp business",)),
    _sig("Calendly", "Scheduling", html=(r"assets\.calendly\.com", r"calendly\.com/assets", r"Calendly\.init"), links=(r"calendly\.com/",)),
    _sig("Acuity Scheduling", "Scheduling", html=(r"acuityscheduling\.com",), links=(r"acuityscheduling\.com",)),
    _sig("Typeform", "Forms", html=(r"embed\.typeform\.com", r"typeform\.com/to/", r"form\.typeform\.com"), links=(r"typeform\.com/to/", r"form\.typeform\.com")),
    _sig("Tally", "Forms", html=(r"tally\.so/widgets", r"tally\.so/embed", r"tally\.so/r/"), links=(r"tally\.so/",)),
    # ---- JS frameworks / libraries -------------------------------------------------------------
    _sig("Next.js", "JavaScript framework", html=(r"/_next/static", r"__NEXT_DATA__", r"next-head-count", r"id=\"__next\""),
         headers=(("x-powered-by", r"next\.js"), ("x-nextjs-cache", None), ("x-nextjs-prerender", None), ("x-nextjs-stale-time", None)),
         implies=("React",), aliases=("nextjs", "next")),
    _sig("Nuxt.js", "JavaScript framework", html=(r"/_nuxt/", r"__NUXT__", r"data-n-head", r"id=\"__nuxt\""), headers=(("x-powered-by", r"nuxt"),),
         implies=("Vue.js",), aliases=("nuxt", "nuxtjs")),
    _sig("Gatsby", "Static site generator", html=(r"id=\"___gatsby\"", r"/page-data/", r"gatsby-(?:image|focus-wrapper|chunk)"),
         meta=(r"Gatsby ?(?P<version>\d+(?:\.\d+)*)?",), implies=("React",), aliases=("gatsbyjs",)),
    _sig("React", "JavaScript framework", html=(r"react(?:-dom)?(?:\.production)?(?:\.min)?\.js", r"data-reactroot", r"/react@(?P<version>\d+(?:\.\d+)*)"),
         weak=(r"_reactListening", r"__REACT_DEVTOOLS"), aliases=("reactjs", "react.js")),
    _sig("Vue.js", "JavaScript framework", html=(r"vue(?:\.runtime)?(?:\.global)?(?:\.prod)?(?:\.min)?\.js", r"data-v-[0-9a-f]{8}", r"__vue_app__", r"/vue@(?P<version>\d+(?:\.\d+)*)"),
         weak=(r"data-server-rendered",), aliases=("vue", "vuejs")),
    _sig("Angular", "JavaScript framework", html=(r"ng-version=\"(?P<version>\d+(?:\.\d+)*)\"", r"angular(?:\.min)?\.js", r"\bng-app\b"), aliases=("angularjs",)),
    _sig("Svelte", "JavaScript framework", html=(r"__sveltekit", r"data-sveltekit", r"/_app/immutable/"), weak=(r"\bsvelte-[a-z0-9]{6}\b",), aliases=("sveltekit",)),
    _sig("jQuery", "JavaScript library", html=(r"jquery[.-](?P<version>\d+\.\d+(?:\.\d+)?)(?:\.slim)?(?:\.min)?\.js", r"jquery(?:\.slim)?(?:\.min)?\.js")),
    _sig("Bootstrap", "UI framework", html=(r"bootstrap(?:\.bundle)?(?:\.min)?\.(?:css|js)",)),
    _sig("GSAP", "JavaScript library", html=(r"gsap(?:\.min)?\.js", r"greensock", r"TweenMax")),
    _sig("Google Fonts", "Font scripts", html=(r"fonts\.googleapis\.com", r"fonts\.gstatic\.com")),
    _sig("Font Awesome", "Font scripts", html=(r"font-?awesome", r"kit\.fontawesome\.com", r"use\.fontawesome\.com")),
    _sig("Weglot", "Translation", html=(r"cdn\.weglot\.com", r"weglot\.com")),
    _sig("Elfsight", "Widgets", html=(r"apps\.elfsight\.com", r"\belfsight-app")),
    # ---- analytics / tag managers / advertising -----------------------------------------------
    _sig("Google Analytics", "Analytics", html=(r"googletagmanager\.com/gtag/js\?id=G-[A-Z0-9]+", r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]G-[A-Z0-9]+"),
         version="GA4", aliases=("ga4", "google analytics 4", "ga")),
    _sig("Google Analytics", "Analytics", html=(r"google-analytics\.com/(?:ga|analytics)\.js", r"['\"]UA-\d{4,10}-\d{1,4}['\"]", r"googletagmanager\.com/gtag/js\?id=UA-"),
         version="UA", aliases=("universal analytics",)),
    _sig("Google Tag Manager", "Tag manager", html=(r"googletagmanager\.com/gtm\.js", r"googletagmanager\.com/ns\.html\?id=GTM-", r"['\"]GTM-[A-Z0-9]{4,9}['\"]"), aliases=("gtm",)),
    _sig("Google Ads", "Advertising", html=(r"googleadservices\.com", r"gtag/js\?id=AW-", r"['\"]AW-\d{6,12}", r"googleads\.g\.doubleclick\.net"), aliases=("adwords", "google adwords")),
    _sig("Google AdSense", "Advertising", html=(r"pagead2\.googlesyndication\.com", r"\badsbygoogle\b")),
    _sig("Meta Pixel", "Advertising", html=(r"connect\.facebook\.net/[\w_]+/fbevents\.js", r"fbq\(\s*['\"]init['\"]", r"facebook\.com/tr\?id="),
         aliases=("facebook pixel", "fb pixel", "meta ads pixel")),
    _sig("TikTok Pixel", "Advertising", html=(r"analytics\.tiktok\.com", r"\bttq\.load\("), aliases=("tiktok ads",)),
    _sig("LinkedIn Insight Tag", "Advertising", html=(r"snap\.licdn\.com/li\.lms-analytics", r"_linkedin_partner_id", r"px\.ads\.linkedin\.com"),
         aliases=("linkedin insight", "linkedin pixel")),
    _sig("Pinterest Tag", "Advertising", html=(r"s\.pinimg\.com/ct/core\.js", r"\bpintrk\("), aliases=("pinterest pixel",)),
    _sig("Microsoft Advertising", "Advertising", html=(r"bat\.bing\.com/bat\.js", r"\buetq\b"), aliases=("bing ads", "bing uet")),
    _sig("Snap Pixel", "Advertising", html=(r"sc-static\.net/scevent\.min\.js", r"\bsnaptr\("), aliases=("snapchat pixel",)),
    _sig("X Pixel", "Advertising", html=(r"static\.ads-twitter\.com/uwt\.js", r"\btwq\("), aliases=("twitter pixel",)),
    _sig("Criteo", "Advertising", html=(r"static\.criteo\.net", r"criteo\.com/js")),
    _sig("Hotjar", "Analytics", html=(r"static\.hotjar\.com", r"hotjar\.com/c/hotjar-", r"_hjSettings", r"\bhjid\s*:")),
    _sig("Microsoft Clarity", "Analytics", html=(r"clarity\.ms/tag", r"['\"]clarity['\"]\s*,\s*['\"]script['\"]"), aliases=("clarity",)),
    _sig("Matomo", "Analytics", html=(r"matomo\.js", r"piwik\.js", r"_paq\.push", r"\.matomo\.cloud"), aliases=("piwik",)),
    _sig("Plausible", "Analytics", html=(r"plausible\.io/js",)),
    _sig("Segment", "Customer data platform", html=(r"cdn\.segment\.com", r"analytics\.load\(\s*['\"]\w+")),
    _sig("Mixpanel", "Analytics", html=(r"cdn\.mxpnl\.com", r"cdn4\.mxpnl", r"mixpanel\.init\(")),
    _sig("Amplitude", "Analytics", html=(r"cdn\.amplitude\.com", r"amplitude\.getInstance", r"amplitude\.init\(")),
    _sig("Heap", "Analytics", html=(r"cdn\.heapanalytics\.com", r"heap\.load\(")),
    _sig("Contentsquare", "Analytics", html=(r"t\.contentsquare\.net",)),
    _sig("AB Tasty", "A/B testing", html=(r"try\.abtasty\.com",)),
    _sig("Sentry", "Error monitoring", html=(r"browser\.sentry-cdn\.com", r"\bSentry\.init\(", r"js\.sentry-cdn\.com")),
    # ---- payments ------------------------------------------------------------------------------
    _sig("Stripe", "Payment", html=(r"js\.stripe\.com", r"checkout\.stripe\.com"), links=(r"buy\.stripe\.com", r"checkout\.stripe\.com", r"billing\.stripe\.com")),
    _sig("PayPal", "Payment", html=(r"paypal\.com/sdk/js", r"paypalobjects\.com"), links=(r"paypal\.(?:com|me)/",)),
    # ---- consent ---------------------------------------------------------------------------------
    _sig("Axeptio", "Cookie consent", html=(r"static\.axept\.io", r"axeptioSettings", r"client\.axept\.io")),
    _sig("Didomi", "Cookie consent", html=(r"sdk\.privacy-center\.org", r"didomiConfig", r"didomi\.io")),
    _sig("OneTrust", "Cookie consent", html=(r"cdn\.cookielaw\.org", r"\boptanon", r"onetrust")),
    _sig("Cookiebot", "Cookie consent", html=(r"consent\.cookiebot\.com", r"id=\"Cookiebot\"", r"CookieConsent\.renew")),
    _sig("Trustpilot", "Reviews", html=(r"widget\.trustpilot\.com", r"trustpilot-widget", r"tp\.widget\.bootstrap"), aliases=("trustpilot widget",)),
    # ---- media / maps / captcha -----------------------------------------------------------------
    _sig("YouTube", "Video", html=(r"youtube(?:-nocookie)?\.com/embed/", r"youtube\.com/iframe_api", r"i\.ytimg\.com"), aliases=("youtube embed",)),
    _sig("Vimeo", "Video", html=(r"player\.vimeo\.com", r"vimeocdn\.com")),
    _sig("Wistia", "Video", html=(r"fast\.wistia\.(?:com|net)",)),
    _sig("Google Maps", "Maps", html=(r"maps\.googleapis\.com/maps/api", r"google\.[a-z.]+/maps/embed", r"maps\.google\.[a-z.]+/maps\?[^\"']*output=embed", r"google\.com/maps/d/embed"),
         aliases=("google maps embed",)),
    _sig("reCAPTCHA", "Security", html=(r"google\.com/recaptcha", r"recaptcha/api\.js", r"gstatic\.com/recaptcha", r"\bg-recaptcha\b", r"recaptcha/enterprise\.js"), aliases=("recaptcha",)),
    _sig("hCaptcha", "Security", html=(r"hcaptcha\.com/1/api\.js", r"js\.hcaptcha\.com", r"\bh-captcha\b")),
    _sig("Cloudflare Turnstile", "Security", html=(r"challenges\.cloudflare\.com/turnstile",), aliases=("turnstile",)),
    # ---- hosting / CDN / servers --------------------------------------------------------------------
    _sig("Cloudflare", "CDN", headers=(("server", r"cloudflare"), ("cf-ray", None), ("cf-cache-status", None)),
         html=(r"/cdn-cgi/(?:challenge-platform|scripts|l/email-protection|apps)",)),
    _sig("Vercel", "Hosting", headers=(("x-vercel-id", None), ("x-vercel-cache", None), ("server", r"^vercel"))),
    _sig("Netlify", "Hosting", headers=(("x-nf-request-id", None), ("server", r"netlify"))),
    _sig("Amazon CloudFront", "CDN", headers=(("x-amz-cf-id", None), ("via", r"cloudfront"))),
    _sig("Fastly", "CDN", headers=(("x-fastly-request-id", None), ("x-served-by", r"^cache-"))),
    _sig("LiteSpeed", "Web server", headers=(("server", r"litespeed"), ("x-litespeed-cache", None))),
    _sig("Nginx", "Web server", headers=(("server", r"^nginx(?:/(?P<version>[\d.]+))?"),)),
    _sig("Apache", "Web server", headers=(("server", r"^apache(?:/(?P<version>[\d.]+))?"),), aliases=("apache http server",)),
    _sig("PHP", "Programming language", headers=(("x-powered-by", r"php(?:/(?P<version>[\d.]+))?"),)),
)


def _norm_alias(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _build_known() -> dict[str, str]:
    known: dict[str, str] = {}
    for sig in SIGNATURES:
        for alias in (sig.name, *sig.aliases):
            for key in {alias.lower(), _norm_alias(alias), _norm_alias(alias).replace(" ", "")}:
                if key:
                    known.setdefault(key, sig.name)
    return known


# Lowercased name / alias → canonical technology name (used by the enrichment planner).
KNOWN_TECHNOLOGIES: dict[str, str] = _build_known()
CATEGORIES: dict[str, str] = {sig.name: sig.category for sig in SIGNATURES}


def canonical_tech_name(name: str) -> str | None:
    """Canonical technology name for a user/AI-provided label, or None when unknown."""
    if not name:
        return None
    for key in (name.lower().strip(), _norm_alias(name), _norm_alias(name).replace(" ", "")):
        if key in KNOWN_TECHNOLOGIES:
            return KNOWN_TECHNOLOGIES[key]
    return None


# ---- detection ---------------------------------------------------------------------------------

_GENERATOR_RES = (
    re.compile(r"<meta[^>]+name=[\"']generator[\"'][^>]*content=[\"']([^\"']+)", _I),
    re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*name=[\"']generator[\"']", _I),
)


@dataclass
class _Acc:
    sig: Signature
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    hits: int = 0

    def add(self, conf: float, evidence: str, version: str | None) -> None:
        self.hits += 1
        self.confidence = max(self.confidence, conf) if self.hits == 1 else min(0.99, max(self.confidence, conf) + 0.03)
        if evidence and evidence not in self.evidence:
            self.evidence.append(evidence)
        if version and version not in self.versions:
            self.versions.append(version)


def _snippet(hay: str, m: re.Match[str], pad: int = 40) -> str:
    a, b = max(0, m.start() - pad), min(len(hay), m.end() + pad)
    return re.sub(r"\s+", " ", hay[a:b]).strip()[:200]


def _version(m: re.Match[str]) -> str | None:
    v = m.groupdict().get("version")
    return v or None


def _link_urls(links: dict[str, Any] | None) -> str:
    if not isinstance(links, dict):
        return ""
    urls: list[str] = []
    social = links.get("social")
    if isinstance(social, dict):
        urls.extend(str(v) for v in social.values())
    for key in ("internal", "external", "people_profiles"):
        for item in links.get(key) or []:
            if isinstance(item, dict) and item.get("url"):
                urls.append(str(item["url"]))
            elif isinstance(item, str):
                urls.append(item)
    return "\n".join(urls)


def detect(
    headers: dict[str, Any] | None,
    head_html: str | None,
    html_text: str | None,
    links: dict[str, Any] | None,
) -> list[DetectedTech]:
    """Fingerprint technologies from cached home-page signals. Deterministic, no network."""
    hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    html = head_html or ""
    text = (html_text or "")[:60_000]
    link_blob = _link_urls(links)
    generators = [m.group(1) for rx in _GENERATOR_RES for m in rx.finditer(html)]
    acc: dict[str, _Acc] = {}

    def hit(sig: Signature, conf: float, evidence: str, version: str | None) -> None:
        a = acc.setdefault(sig.name, _Acc(sig))
        a.add(conf, evidence, version or sig.version)

    for sig in SIGNATURES:
        for name, rx in sig.headers:
            val = hdrs.get(name)
            if val is None:
                continue
            if rx is None:
                hit(sig, CONF_HEADER, f"{name}: {val[:100]}", None)
            elif (m := rx.search(val)) is not None:
                hit(sig, CONF_HEADER, f"{name}: {val[:100]}", _version(m))
        for rx in sig.meta:
            for gen in generators:
                if (m := rx.search(gen)) is not None:
                    hit(sig, CONF_META, f'<meta name="generator" content="{gen[:100]}">', _version(m))
        for rx in sig.html:
            if (m := rx.search(html)) is not None:
                hit(sig, CONF_HTML, _snippet(html, m), _version(m))
        for rx in sig.weak:
            if (m := rx.search(html)) is not None:
                hit(sig, CONF_WEAK, _snippet(html, m), _version(m))
        for rx in sig.links:
            if (m := rx.search(link_blob)) is not None:
                line_start = link_blob.rfind("\n", 0, m.start()) + 1
                line_end = link_blob.find("\n", m.end())
                hit(sig, CONF_LINK, link_blob[line_start : line_end if line_end >= 0 else None][:200], None)
        for rx in sig.text:
            if (m := rx.search(text)) is not None:
                hit(sig, CONF_TEXT, _snippet(text, m), None)

    # implied technologies (WooCommerce → WordPress, Next.js → React …)
    by_name = {s.name: s for s in SIGNATURES}
    for name in list(acc):
        for implied in acc[name].sig.implies:
            if implied not in acc and implied in by_name:
                a = acc.setdefault(implied, _Acc(by_name[implied]))
                a.add(CONF_IMPLIED, f"implied by {name}", None)

    out = [
        DetectedTech(
            name=a.sig.name,
            category=a.sig.category,
            version="+".join(sorted(a.versions)) if a.versions else None,
            confidence=round(a.confidence, 3),
            evidence=" | ".join(a.evidence[:3])[:400],
        )
        for a in acc.values()
    ]
    out.sort(key=lambda t: (-t.confidence, t.name))
    return out
