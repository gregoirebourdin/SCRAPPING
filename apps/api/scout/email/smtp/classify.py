"""SMTP reply classification — RFC 3463 enhanced status codes first, wording second.

The one rule that matters: never confuse "this mailbox does not exist" with "the server refused US"
(our IP, HELO, sender, reputation, rate). Only an explicit user-unknown answer becomes ``rejected``;
everything infrastructural becomes ``blocked``; every 4xx becomes ``temporary``; anything unclear is
``unknown``. ``rejected`` is the only result that may ever lead to an INVALID verdict.

RCPT replies (``classify_rcpt_reply``), in order:

1. 250 / 251 → ``accepted``; other 2xx/3xx (e.g. 252 "cannot VRFY") → ``unknown``.
2. 4xx (421, 450, 451, 452, greylisting, rate limits, "try again later") → ``temporary`` — never a verdict.
3. 5xx:
   a. ``X.7.x`` (security / policy) → ``blocked``.
   b. Microsoft 365 Directory-Based Edge Blocking, exactly ``5.4.1`` + "Recipient address rejected:
      Access denied" → ``rejected`` (the tenant's directory has no such recipient). Any other 5.4.x is
      a routing problem → ``unknown``. Tenants without DBEB accept every RCPT, which the catch-all
      probes then expose.
   c. Our sender refused ("Sender address rejected", ``X.1.7`` / ``X.1.8``) → ``blocked``.
   d. Policy wording (Spamhaus & other RBLs, rDNS/PTR, "client host rejected", HELO, reputation,
      relaying denied, "access denied", banned/blocked/blacklisted…) → ``blocked``.
   e. "try again later" / "temporarily" / rate-limit wording → ``temporary``.
   f. ``X.1.1`` / ``X.1.10`` (and ``X.1.0`` / ``X.1.3`` / ``X.1.6``) → ``rejected``;
      ``X.2.1`` mailbox disabled → ``rejected``; ``X.2.2`` / 552 mailbox full → the mailbox EXISTS →
      ``accepted`` (detail "mailbox full"); ``X.1.2`` (domain not served there) → ``unknown``.
   g. User-unknown wording ("user unknown", "no such user", "does not exist", "recipient rejected",
      550 "mailbox unavailable", …) → ``rejected``.
   h. Anything else — notably a bare 550 without detail (common on Microsoft 365) → ``unknown``.

Session replies (greeting / EHLO / MAIL FROM): 2xx/3xx → ``ok``, 4xx → ``temporary``, 5xx → ``policy_block``
(the server refuses to talk to us: that is about our infrastructure, never about a mailbox).
Exceptions: connect timeout / refused / DNS failure / dropped connection → ``infra_failure``.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from random import Random

import aiosmtplib

from scout.db.enums import MailProvider, SmtpResult
from scout.email.contracts import SessionOutcome

MAX_REPLY_CHARS = 500  # replies are stored/logged truncated (some servers send kilobytes of text)

# ---- enhanced status codes ------------------------------------------------------------------

_ENHANCED = re.compile(r"(?<![\d.])([245])\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")

_USER_UNKNOWN_ENHANCED = frozenset({"1.1", "1.10", "1.0", "1.3", "1.6"})
_SENDER_ENHANCED = frozenset({"1.7", "1.8"})


def parse_enhanced(message: str) -> str | None:
    """First RFC 3463 enhanced status code in a reply (``"5.1.1"``), or None."""
    m = _ENHANCED.search(message or "")
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else None


def _subject_detail(enhanced: str | None) -> str | None:
    """``"5.1.10"`` → ``"1.10"`` (class-independent part)."""
    return enhanced.split(".", 1)[1] if enhanced else None


# ---- wording ----------------------------------------------------------------------------------


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(parts), re.IGNORECASE)


_DBEB = _rx(r"recipient address rejected:?\s*access denied")

_SENDER_HINTS = _rx(
    r"sender address rejected",
    r"sender (address )?(is )?(rejected|refused|denied|invalid|unknown|not allowed)",
    r"sender verif",
    r"unverified (sender|address)",
    r"mail from .{0,40}(rejected|refused|denied)",
)

_POLICY_HINTS = _rx(
    r"spamhaus",
    r"spamcop",
    r"barracuda",
    r"sorbs",
    r"uceprotect",
    r"abuseat",
    r"\bsurbl\b",
    r"\b(rbl|dnsbl|rdns|ptr)\b",
    r"\bblock(ed|list| list)\b",
    r"\bblacklist(ed)?\b",
    r"\bblack list\b",
    r"\bdeny ?list(ed)?\b",
    r"\bbanned\b",
    r"\blisted (at|on|in|by)\b",
    r"reputation",
    r"client host rejected",
    r"helo command rejected",
    r"\b(helo|ehlo)\b",
    r"reverse (dns|hostname|lookup)",
    r"cannot find your (reverse )?hostname",
    r"access denied",
    r"relay access denied",
    r"relaying (denied|not (allowed|permitted))",
    r"unable to relay",
    r"(not permitted|not allowed|denied) to relay",
    r"relay not permitted",
    r"we do not relay",
    r"administrative prohibition",
    r"\bprohibited\b",
    r"not authori[sz]ed to send",
    r"\byour ip\b",
    r"\bsending ip\b",
    r"\bip address\b",
    r"dynamic ip",
    r"\bresidential\b",
    r"dial-?up",
    r"unsolicited",
    r"\bspam\b",
    r"\bpolicy\b",
)

_TEMPORARY_HINTS = _rx(
    r"gr[ae]y ?list",
    r"try (again )?later",
    r"please try again",
    r"temporar",
    r"rate.?limit",
    r"too many",
    r"throttl",
    r"slow down",
    r"server busy",
    r"\bdeferr?(ed|al)?\b",
)

_USER_UNKNOWN_HINTS = _rx(
    r"user ?unknown",
    r"unknown user",
    r"no such (user|recipient|mailbox|address|account|person)",
    r"(does|do)\s*(n[o']t|not) exist",
    r"doesn'?t exist",
    r"\bnot exist",
    r"(user|mailbox|recipient|address|account) not found",
    r"unknown (recipient|mailbox|address|account|local.?part)",
    r"recipient unknown",
    r"invalid (recipient|mailbox|address|user)",
    r"(recipient|mailbox|address|user) (is )?invalid",
    r"no mailbox",
    r"not a valid (mailbox|recipient|user|address)",
    r"user not local",
    r"mailbox name not allowed",
    r"(does not|doesn'?t) have an? .{0,40}account",
    r"recipient (address )?rejected",
    r"address rejected",
    r"undeliverable",
    r"not our customer",
    r"no longer (with|employed|available|active|valid)",
    r"(account|mailbox|user) (is |has been )?(disabled|deactivated|inactive|suspended|closed)",
)

_MAILBOX_UNAVAILABLE = _rx(r"mailbox (is )?unavailable")
_MAILBOX_FULL_HINTS = _rx(r"quota", r"mailbox (is )?full", r"storage", r"insufficient (space|storage)")
_GREYLIST_HINTS = _rx(r"gr[ae]y ?list")


class ReplyKind(StrEnum):
    accepted = "accepted"
    mailbox_full = "mailbox_full"  # X.2.2 / 552: the mailbox exists
    user_unknown = "user_unknown"
    mailbox_disabled = "mailbox_disabled"  # X.2.1
    directory_reject = "directory_reject"  # Microsoft 365 DBEB 5.4.1
    greylisted = "greylisted"
    rate_limited = "rate_limited"
    temporary = "temporary"
    policy = "policy"  # our IP / HELO / reputation / relay refused
    sender_rejected = "sender_rejected"  # our MAIL FROM refused
    domain_not_served = "domain_not_served"  # X.1.2 / X.4.x routing
    ambiguous = "ambiguous"


@dataclass(frozen=True)
class ReplyClass:
    result: SmtpResult
    kind: ReplyKind
    code: int | None
    enhanced: str | None
    reason: str  # short human explanation

    @property
    def is_mailbox_verdict(self) -> bool:
        """Accepted or rejected for the mailbox itself (not infrastructure, not temporary)."""
        return self.result in (SmtpResult.accepted, SmtpResult.rejected)


def _clip(message: str | None) -> str:
    return (message or "").replace("\r", " ").strip()[:MAX_REPLY_CHARS]


_ECHOED = re.compile(r"<[^<>]*>|[^\s<>()\[\]]+@[^\s<>()\[\]]+")


def _wording(message: str) -> str:
    """The reply text without the addresses the server echoes back (``<user@domain>``, ``user@domain``)."""
    return _ECHOED.sub(" ", message)


def classify_rcpt_reply(code: int, message: str, *, provider: MailProvider | None = None) -> ReplyClass:
    """Classify one RCPT TO reply (see module doc for the ordered rules)."""
    msg = _clip(message)
    enh = parse_enhanced(msg)
    words = _wording(
        msg
    )  # keyword rules never look inside echoed addresses (a domain may contain "greylist")
    sub = _subject_detail(enh)

    def rc(result: SmtpResult, kind: ReplyKind, reason: str) -> ReplyClass:
        return ReplyClass(result, kind, code, enh, reason)

    if code in (250, 251):
        return rc(SmtpResult.accepted, ReplyKind.accepted, "recipient accepted")
    if 200 <= code < 400:
        return rc(SmtpResult.unknown, ReplyKind.ambiguous, f"{code}: neither accepted nor refused")
    if 400 <= code < 500:
        if _GREYLIST_HINTS.search(words):
            return rc(SmtpResult.temporary, ReplyKind.greylisted, "greylisting: retry later")
        if code == 421 or _rx(r"rate.?limit", r"too many", r"throttl", r"busy").search(words):
            return rc(
                SmtpResult.temporary, ReplyKind.rate_limited, "rate limited / service busy: retry later"
            )
        return rc(SmtpResult.temporary, ReplyKind.temporary, f"{code}: temporary failure, retry later")
    if not 500 <= code < 600:
        return rc(SmtpResult.unknown, ReplyKind.ambiguous, f"unexpected reply code {code}")

    # ---- 5xx -------------------------------------------------------------------------------
    if sub is not None and sub.startswith("7."):
        return rc(SmtpResult.blocked, ReplyKind.policy, f"{enh}: security/policy refusal (our side)")
    if sub == "4.1" and _DBEB.search(words):
        # Microsoft 365 Directory-Based Edge Blocking: the recipient is not in the tenant directory.
        return rc(
            SmtpResult.rejected,
            ReplyKind.directory_reject,
            "5.4.1 directory-based edge blocking: no such recipient",
        )
    if (sub in _SENDER_ENHANCED) or _SENDER_HINTS.search(words):
        return rc(SmtpResult.blocked, ReplyKind.sender_rejected, "our sender address was refused")
    if _POLICY_HINTS.search(words):
        return rc(SmtpResult.blocked, ReplyKind.policy, "policy / reputation refusal (our side)")
    if _TEMPORARY_HINTS.search(words):
        return rc(
            SmtpResult.temporary, ReplyKind.rate_limited, "permanent code with temporary wording: retry later"
        )
    if sub in _USER_UNKNOWN_ENHANCED:
        return rc(SmtpResult.rejected, ReplyKind.user_unknown, f"{enh}: no such mailbox")
    if sub == "2.1":
        return rc(SmtpResult.rejected, ReplyKind.mailbox_disabled, f"{enh}: mailbox disabled")
    if sub == "2.2" or (
        code == 552 and (sub is None or sub.startswith("2.")) and _MAILBOX_FULL_HINTS.search(words)
    ):
        return rc(SmtpResult.accepted, ReplyKind.mailbox_full, "mailbox full: the mailbox exists")
    if sub is not None and (sub == "1.2" or sub.startswith("4.")):
        return rc(SmtpResult.unknown, ReplyKind.domain_not_served, f"{enh}: routing / domain not served here")
    if _USER_UNKNOWN_HINTS.search(words) or (code == 550 and _MAILBOX_UNAVAILABLE.search(words)):
        return rc(SmtpResult.rejected, ReplyKind.user_unknown, "server says the mailbox does not exist")
    if provider == MailProvider.microsoft_365:
        return rc(
            SmtpResult.unknown, ReplyKind.ambiguous, f"{code} without detail on Microsoft 365: inconclusive"
        )
    return rc(SmtpResult.unknown, ReplyKind.ambiguous, f"{code} without a recognisable reason: inconclusive")


def classify_rcpt(code: int, message: str, *, provider: MailProvider | None = None) -> SmtpResult:
    """Shorthand: the ``SmtpResult`` of a RCPT reply."""
    return classify_rcpt_reply(code, message, provider=provider).result


def is_greylisting(message: str) -> bool:
    return bool(_GREYLIST_HINTS.search(_wording(message or "")))


def is_policy_reply(code: int, message: str) -> bool:
    """5xx reply that is about us (policy, reputation, sender, HELO) rather than about a mailbox."""
    if not 500 <= code < 600:
        return False
    sub = _subject_detail(parse_enhanced(message))
    if sub == "4.1" and _DBEB.search(message or ""):
        return False  # Microsoft 365 directory-based edge blocking: about the recipient, not us
    return bool(
        (sub is not None and sub.startswith("7."))
        or sub in _SENDER_ENHANCED
        or _SENDER_HINTS.search(message or "")
        or _POLICY_HINTS.search(message or "")
    )


def classify_session_reply(code: int, message: str = "") -> SessionOutcome:
    """Greeting / EHLO / HELO / MAIL FROM reply → what it says about OUR ability to probe."""
    if 200 <= code < 400:
        return SessionOutcome.ok
    if 400 <= code < 500:
        return SessionOutcome.temporary
    if 500 <= code < 600:
        return SessionOutcome.policy_block
    return SessionOutcome.infra_failure


@dataclass(frozen=True)
class SessionFailure:
    outcome: SessionOutcome
    result: SmtpResult  # what unanswered recipients get
    detail: str


def classify_exception(exc: BaseException, *, stage: str = "connect") -> SessionFailure:
    """A session-level exception (connect, greeting, EHLO, MAIL FROM, mid-RCPT) → outcome + per-address result.

    Infrastructure problems map to ``blocked`` / ``timeout`` / ``unknown`` — never ``rejected``.
    """
    if isinstance(exc, aiosmtplib.SMTPResponseException):  # includes SMTPConnectResponseError
        code, message = exc.code, _clip(exc.message)
        outcome = classify_session_reply(code, message)
        result = {
            SessionOutcome.temporary: SmtpResult.temporary,
            SessionOutcome.policy_block: SmtpResult.blocked,
        }.get(outcome, SmtpResult.unknown)
        return SessionFailure(outcome, result, f"{stage}: {code} {message}".strip())
    if isinstance(exc, aiosmtplib.SMTPTimeoutError | TimeoutError):
        return SessionFailure(SessionOutcome.infra_failure, SmtpResult.timeout, f"timeout ({stage}): {exc}")
    if isinstance(exc, aiosmtplib.SMTPServerDisconnected):
        return SessionFailure(
            SessionOutcome.infra_failure, SmtpResult.unknown, f"disconnected ({stage}): {exc}"
        )
    if isinstance(exc, aiosmtplib.SMTPConnectError | OSError):
        return SessionFailure(
            SessionOutcome.infra_failure, SmtpResult.blocked, f"connect failed ({stage}): {exc}"
        )
    return SessionFailure(
        SessionOutcome.infra_failure, SmtpResult.unknown, f"{stage}: {type(exc).__name__}: {exc}"
    )


# ---- providers whose RCPT answers say nothing -------------------------------------------------

UNINFORMATIVE_PROVIDERS = frozenset(
    {MailProvider.secure_gateway, MailProvider.amazon_ses, MailProvider.mailgun}
)
# Consumer webmail (Yahoo / AOL / Verizon) accept at RCPT and bounce later.
_ACCEPT_THEN_BOUNCE_MX = re.compile(r"(^|\.)(yahoodns\.net|yahoo\.com|aol\.com|verizon\.net|aim\.com)$")
_GATEWAY_MX = re.compile(
    r"(^|\.)(mimecast(-offshore)?\.(com|co\.za)|pphosted\.com|ppe-hosted\.com|barracudanetworks\.com|"
    r"messagelabs\.com|iphmx\.com|sophos\.com|mailcontrol\.com|hornetsecurity\.com|fireeyecloud\.com|"
    r"vadesecure\.com|antispamcloud\.com|spamexperts\.(com|eu|net)|mailinblack\.com)$"
)


def _clean_host(host: str) -> str:
    return (host or "").strip().rstrip(".").lower()


def uninformative_accepts(provider: MailProvider | None, mx_hosts: Sequence[str]) -> bool:
    """Whether a 250 at RCPT proves nothing (accept-then-bounce consumer MX or a secure gateway)."""
    if provider in UNINFORMATIVE_PROVIDERS:
        return True
    hosts = [_clean_host(h) for h in mx_hosts if h]
    return any(_ACCEPT_THEN_BOUNCE_MX.search(h) or _GATEWAY_MX.search(h) for h in hosts)


def provider_from_mx(mx_hosts: Sequence[str], *, domain: str | None = None) -> MailProvider:
    """Provider behind MX hosts (delegates to the domain-intelligence detector when available)."""
    try:
        from scout.email.intel.providers import detect_provider
    except ImportError:  # pragma: no cover - intel layer always present in production
        pass
    else:
        return detect_provider(list(mx_hosts), domain=domain)
    hosts = [_clean_host(h) for h in mx_hosts if h]
    if any(_GATEWAY_MX.search(h) for h in hosts):
        return MailProvider.secure_gateway
    if any(h.endswith(("google.com", "googlemail.com")) for h in hosts):
        return MailProvider.google_workspace
    if any(h.endswith("protection.outlook.com") for h in hosts):
        return MailProvider.microsoft_365
    return MailProvider.unknown if hosts else MailProvider.none


# ---- random catch-all probe addresses ----------------------------------------------------------

_ONSETS = (
    "b", "c", "d", "f", "g", "j", "k", "l", "m", "n", "p", "r", "s", "t", "v", "z",
    "br", "dr", "gr", "kr", "tr", "st", "ch", "sh", "th", "pl", "fl", "cl",
)  # fmt: skip
_VOWELS = ("a", "e", "i", "o", "u", "a", "e", "i", "o", "ou", "ai", "ea")
_CODAS = ("", "", "", "", "n", "r", "l", "s", "m", "x", "nd", "rt")
_SHAPES = ("first.last", "flast", "first_last", "firstlast", "first-last", "lastf")
# Generated local parts must never look like a probe or a role mailbox.
_FORBIDDEN = re.compile(r"test|catch|scout|probe|spam|abuse|admin|info|post|noreply|random|fake|verif")


def _pseudo_name(rng: Random, lo: int, hi: int) -> str:
    """A pronounceable, name-like token (consonant-vowel syllables) of length lo..hi."""
    target = rng.randint(lo, hi)
    out = ""
    while len(out) < target:
        out += rng.choice(_ONSETS) + rng.choice(_VOWELS)
    out = out[:target]
    if rng.random() < 0.5:
        out = (out[:-1] + rng.choice(_CODAS))[:target] or out
    return out


def random_local_part(shape: str, rng: Random | None = None) -> str:
    """A plausible-looking but (with overwhelming probability) nonexistent local part.

    Never looks like a test ("test", "catchall", "scout-…", hex blobs): servers and gateways that
    special-case obvious probes would otherwise skew catch-all detection.
    """
    r = rng or secrets.SystemRandom()
    while True:
        first = _pseudo_name(r, 5, 7)
        last = _pseudo_name(r, 6, 9)
        local = {
            "first.last": f"{first}.{last}",
            "flast": f"{first[0]}{last}",
            "first_last": f"{first}_{last}",
            "firstlast": f"{first}{last}",
            "first-last": f"{first}-{last}",
            "lastf": f"{last}{first[0]}",
        }.get(shape, f"{first}.{last}")
        if not _FORBIDDEN.search(local):
            return local


def random_probe_addresses(domain: str, n: int = 2, *, rng: Random | None = None) -> list[str]:
    """``n`` improbable addresses of different shapes (first.last, flast, …) for catch-all detection."""
    r = rng or secrets.SystemRandom()
    out: list[str] = []
    for i in range(max(0, n)):
        for _ in range(5):  # avoid duplicates (practically impossible)
            addr = f"{random_local_part(_SHAPES[i % len(_SHAPES)], r)}@{domain}"
            if addr not in out:
                out.append(addr)
                break
    return out
