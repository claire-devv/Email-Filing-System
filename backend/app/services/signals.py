"""
Centralized extraction of typed, normalized "learning signals" from an email.

This is the single source of truth for *what is discriminating* about a filing email,
used identically at classification time (to look up learned mappings and rank entities)
and at review time (to record what was learned). Keeping one definition is what makes
learning generic: there are no per-example keyword patches — every email is reduced to
the same typed signals, normalized so trivial format differences ("N" vs "North",
"St" vs "Street", a unit number) never break a match.

Signal types, most→least discriminating:
  - address : house number + normalized street name (e.g. "1339 north front")
  - org     : a company/entity name (e.g. "the springs retreat llc")
  - email   : a non-forwarder participant address (e.g. "jordan@jscre.com")
  - domain  : a non-free-mail, non-forwarder participant domain (e.g. "jscre.com")
  - keyword : fallback subject token (weak; only used when nothing better matches)
"""
from __future__ import annotations

import re
from email.utils import getaddresses

# Public free-mail providers are shared by millions of unrelated senders, so a
# domain-level signal for them would wrongly generalize one client to every sender on
# that provider. We still learn the *exact* free-mail sender address, never the domain.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "ymail.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net", "zoho.com",
    "mail.com", "yandex.com",
}

# Street-direction prefixes, normalized to a single canonical full word so "N Front" and
# "North Front" produce the same address key — while "N" and "S" stay distinct.
_DIRECTIONALS = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
    "no": "north", "so": "south",
    "north": "north", "south": "south", "east": "east", "west": "west",
}

# Street-type suffixes dropped from the address key (a property is identified by number +
# street name; "Ave"/"Avenue"/"St" are noise that varies between how people write it).
_STREET_SUFFIXES = {
    "st", "street", "ave", "avenue", "av", "blvd", "boulevard", "rd", "road",
    "dr", "drive", "ln", "lane", "ct", "court", "pl", "place", "ter", "terrace",
    "way", "cir", "circle", "pkwy", "parkway", "hwy", "highway", "sq", "square",
    "row", "aly", "alley", "pike", "plz", "plaza", "trl", "trail",
}

# Unit/suite markers; everything from here on in an address is dropped from the key.
_UNIT_MARKERS = {"unit", "apt", "apartment", "suite", "ste", "fl", "floor", "rm", "room", "#"}

# Company-name tails that mark an organization/entity phrase.
_ORG_SUFFIXES = [
    "llc", "l.l.c.", "lp", "l.p.", "llp", "inc", "inc.", "incorporated", "corp",
    "corp.", "company", "trust", "partners", "holdings", "properties", "property",
    "associates", "group", "ventures", "capital", "realty", "retreat", "apartments",
    "plaza", "management",
]

_ORG_SUFFIX_RE = r"(?:LLC|L\.L\.C\.|LP|L\.P\.|LLP|Inc\.?|Incorporated|Corp\.?|Company|Trust|Partners|Holdings|Properties|Property|Associates|Group|Ventures|Capital|Realty|Retreat|Apartments|Plaza|Management)"

# Generic subject words that identify a document type, not an entity. Kept here so the
# weak `keyword` fallback never stores cross-entity noise. address/org/email/domain
# signals are preferred over keywords precisely so this list does not need to grow.
_STOPWORDS = {
    "about", "attached", "email", "from", "fwd", "receipt", "subject", "that", "this",
    "with", "amortization", "executed", "invoice", "lease", "please", "update", "open",
    "balances", "balance", "completed", "complete", "statement", "new", "bill", "due",
    "regarding", "review", "final", "copy", "report", "rent", "roll",
}


def is_free_mail_domain(domain: str | None) -> bool:
    return (domain or "").strip().lower() in FREE_MAIL_DOMAINS


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_org(value: str) -> str:
    return _norm_ws(value).lower().strip(" .,-")


def address_key(house_number: str, middle: str) -> str:
    """
    Build the canonical address key from a house number and the run of words after it.
    Directionals are expanded to full words; the street suffix and any unit are dropped.
    "1339 N Front St" and "1339 North Front Street" both -> "1339 north front".
    "1416 Frankford 402" -> "1416 frankford".  "754 S 4th St" -> "754 south 4th".
    """
    words = [w for w in re.split(r"[\s.,]+", middle.lower()) if w]
    out: list[str] = []
    has_street_word = False
    for word in words:
        if word in _UNIT_MARKERS or word.startswith("#"):
            break  # unit/suite marker -> stop; the rest is not part of the identity
        if word in _STREET_SUFFIXES:
            break  # street type -> identity is complete
        if not out and word in _DIRECTIONALS:
            out.append(_DIRECTIONALS[word])
            continue
        # A bare trailing number after the street name is a unit (e.g. "Frankford 402").
        if out and word.isdigit():
            break
        out.append(word)
        has_street_word = True
    # Require an actual street-name word — "1339 north" alone (number + direction) is not a
    # usable identity and would only add noise.
    if not has_street_word:
        return ""
    core = " ".join(out).strip()
    return f"{house_number} {core}".strip() if core else ""


def extract_addresses(text: str) -> list[str]:
    """Extract normalized address keys from free text (subjects + bodies)."""
    if not text:
        return []
    keys: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key and len(key) >= 5 and key not in seen:
            seen.add(key)
            keys.append(key)

    # 1) Full street address with a recognized suffix (most reliable).
    suffix_alt = "|".join(sorted(_STREET_SUFFIXES, key=len, reverse=True))
    full = re.compile(
        rf"\b(\d{{1,6}})\s+((?:[NSEW]|NE|NW|SE|SW|north|south|east|west)\.?\s+)?"
        rf"([A-Za-z0-9][A-Za-z0-9'.\-]*(?:\s+[A-Za-z0-9'.\-]+){{0,3}}?)\s+"
        rf"(?:{suffix_alt})\b",
        re.IGNORECASE,
    )
    for m in full.finditer(text):
        add(address_key(m.group(1), f"{m.group(2) or ''}{m.group(3)}"))

    # 2) "<number> <Word>" with no suffix — common in subjects ("1322 Frankford",
    #    "1416 Frankford 402"). Capture number + the immediate street word(s).
    loose = re.compile(r"\b(\d{2,6})\s+((?:[NSEW]\.?\s+)?[A-Za-z][A-Za-z'\-]{2,}(?:\s+\d{1,4})?)\b")
    for m in loose.finditer(text):
        add(address_key(m.group(1), m.group(2)))

    return keys


def extract_orgs(text: str) -> list[str]:
    """Extract normalized organization/entity names ending in LLC/LP/Inc/etc."""
    if not text:
        return []
    pattern = re.compile(rf"\b([A-Z][A-Za-z0-9&.,'\- ]{{2,70}}?{_ORG_SUFFIX_RE})\b")
    out: list[str] = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        value = normalize_org(m.group(1))
        # Drop a leading connector the greedy capture may include ("for The Springs ...").
        value = re.sub(r"^(?:for|to|the attached|attached|re|fwd)\s+", "", value).strip()
        if value and len(value) >= 5 and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def subject_keywords(subject: str | None, limit: int = 5) -> list[str]:
    """Weak fallback tokens from the subject (used only when no stronger signal matches)."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]{3,}", subject or "")
    output: list[str] = []
    for word in words:
        value = word.lower().strip(".-")
        if value in _STOPWORDS or value in output:
            continue
        output.append(value)
        if len(output) >= limit:
            break
    return output


def extract_participant_addresses(
    sender: str | None,
    recipient: str | None,
    cc: str | None,
    body_text: str | None,
    forwarder_domains: set[str],
) -> list[tuple[str, str]]:
    """
    All (email, domain) participants from the envelope and every From/To/Cc line in the
    forwarded chain, excluding forwarder/relay domains (e.g. the RRES filing inbox).
    """
    raw = [sender or "", recipient or "", cc or ""]
    for m in re.finditer(r"(?im)^(?:From|To|Cc|Bcc)\s*:\s*(.+)$", body_text or ""):
        raw.append(m.group(1))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    # Parse each value individually. Python 3.13 hardened getaddresses() so that a single
    # list mixing display-name addresses with empty strings can return [('', '')] and drop
    # everything; isolating each value sidesteps that and is robust across versions.
    for value in raw:
        if not value or not value.strip():
            continue
        for _name, addr in getaddresses([value]):
            addr = (addr or "").strip().lower()
            if not addr or "@" not in addr or addr in seen:
                continue
            domain = addr.split("@", 1)[1]
            if domain in forwarder_domains:
                continue
            seen.add(addr)
            out.append((addr, domain))
    return out


def extract_signals(
    *,
    sender: str | None,
    recipient: str | None = None,
    cc: str | None = None,
    subject: str | None = None,
    body_text: str | None = None,
    forwarder_domains: set[str] | None = None,
) -> list[dict]:
    """
    Reduce an email to a de-duplicated list of typed signals: {"type", "value"}.
    Used both to look up learned mappings (classification) and to record them (review).
    """
    forwarder_domains = {d.strip().lower() for d in (forwarder_domains or set())}
    text = "\n".join([subject or "", body_text or ""])
    signals: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(stype: str, value: str) -> None:
        value = (value or "").strip().lower()
        if not value:
            return
        key = (stype, value)
        if key in seen:
            return
        seen.add(key)
        signals.append({"type": stype, "value": value})

    for key in extract_addresses(text):
        add("address", key)
    for org in extract_orgs(text):
        add("org", org)
    for addr, domain in extract_participant_addresses(sender, recipient, cc, body_text, forwarder_domains):
        add("email", addr)
        if domain and not is_free_mail_domain(domain):
            add("domain", domain)
    for kw in subject_keywords(subject):
        add("keyword", kw)
    return signals
