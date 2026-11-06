"""Public contact details pulled out of free text and HTML, with no model.

Business-blind: it finds emails, phone numbers, websites and social handles in
whatever text a host hands it (a channel description, a profile bio, a page's HTML)
and knows nothing about who the text belongs to or why the host wants it.

Three things make naive regexes miss most contacts in real bios, and each is handled:

- Digits written in Persian or Arabic script (``۰۹۱۲``, ``٠٩١٢``) are folded to
  ASCII before any number is matched.
- Handles are written many ways: a full URL, ``instagram.com/x``, ``IG: @x`` or a
  bare ``@x`` next to a platform word. All of them land on the same handle.
- A phone number is only kept when it has a plausible length after its separators
  are removed, so a year range or a price list does not become a phone.
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import unquote, urlsplit

_DIGIT_FOLD = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
# A run of digits with the separators people actually type, optionally led by + or 00.
PHONE_RE = re.compile(r"(?<![\w/=])(?:\+|00)?\d[\d\s().\-]{6,18}\d(?![\w/])")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]{}،]+", re.IGNORECASE)
BARE_DOMAIN_RE = re.compile(
    r"(?<![@\w.])(?:[a-z0-9-]+\.)+(?:com|ir|net|org|io|me|co|info|academy|online|site|app|tr|ru|ae)"
    r"(?:/[^\s<>\"']*)?(?![\w])",
    re.IGNORECASE,
)

# Hosts that are a social profile rather than someone's own website.
SOCIAL_HOSTS: dict[str, str] = {
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "t.me": "telegram",
    "telegram.me": "telegram",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "wa.me": "whatsapp",
    "api.whatsapp.com": "whatsapp",
    "chat.whatsapp.com": "whatsapp",
    "linkedin.com": "linkedin",
    "tiktok.com": "tiktok",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "x.com": "x",
    "twitter.com": "x",
    "aparat.com": "aparat",
    "eitaa.com": "eitaa",
    "rubika.ir": "rubika",
}
# Hosts that hold several links of one person: worth visiting, but not their website.
LINK_HUB_HOSTS = {"linktr.ee", "linkin.bio", "bio.link", "beacons.ai", "taplink.cc", "zaya.io", "lnk.bio"}
_BARE_HUB_RE = re.compile(
    r"(?<![\w./])(?:%s)/[A-Za-z0-9_.\-]+" % "|".join(re.escape(h) for h in sorted(LINK_HUB_HOSTS)),
    re.IGNORECASE,
)
# Hosts that are never a person's website (media, shorteners, stores).
IGNORED_HOSTS = {"goo.gl", "bit.ly", "play.google.com", "apps.apple.com", "google.com", "gmail.com"}

# Platform words that turn a bare ``@name`` into a handle on that platform.
_PLATFORM_WORDS: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram", "insta", "ig", "اینستاگرام", "اینستا", "پیج"),
    "telegram": ("telegram", "tg", "تلگرام", "کانال"),
}
_HANDLE_RE = r"@?([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)"
_RESERVED_PATHS = {"p", "reel", "reels", "explore", "stories", "tv", "joinchat", "share", "s", "c", "channel", "user", "watch", "shorts", "playlist", "in", "company", "send"}


@dataclass
class Contacts:
    """Everything found, de-duplicated, in the order it first appeared."""

    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    link_hubs: list[str] = field(default_factory=list)
    social: dict[str, list[str]] = field(default_factory=dict)

    def merge(self, other: "Contacts") -> "Contacts":
        for name in ("emails", "phones", "websites", "link_hubs"):
            mine = getattr(self, name)
            for value in getattr(other, name):
                if value not in mine:
                    mine.append(value)
        for platform, handles in other.social.items():
            for handle in handles:
                self.add_social(platform, handle)
        return self

    def add_social(self, platform: str, handle: str) -> None:
        bucket = self.social.setdefault(platform, [])
        if handle and handle not in bucket:
            bucket.append(handle)


def fold_digits(text: str) -> str:
    """Persian and Arabic-Indic digits to ASCII; everything else unchanged."""
    return text.translate(_DIGIT_FOLD)


def normalize_phone(raw: str, *, default_country: str = "") -> str:
    """Digits only, led by ``+`` when the country is known; empty when implausible.

    ``default_country`` is a calling code without ``+`` (``"98"``). A number written
    with its trunk ``0`` (``0912...``) is rewritten under it; a number already
    carrying ``+`` or ``00`` keeps its own country.
    """
    text = fold_digits(raw).strip()
    international = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    if not international and default_country:
        if digits.startswith(default_country) and len(digits) >= 11:
            international = True
        elif digits.startswith("0"):
            digits = default_country + digits[1:]
            international = True
    if not 8 <= len(digits) <= 15:
        return ""
    if international:
        return "+" + digits
    return digits


def _host(url: str) -> str:
    try:
        host = urlsplit(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.").removeprefix("m.")


def _social_from_url(url: str) -> tuple[str, str] | None:
    """``("instagram", "name")`` for a profile URL, else None."""
    host = _host(url)
    platform = SOCIAL_HOSTS.get(host)
    if platform is None:
        for known, name in SOCIAL_HOSTS.items():
            if host.endswith("." + known):
                platform = name
                break
    if platform is None:
        return None
    path = unquote(urlsplit(url if "://" in url else "https://" + url).path)
    parts = [p for p in path.split("/") if p]
    if platform == "whatsapp":
        number = parts[0] if parts else ""
        query = urlsplit(url if "://" in url else "https://" + url).query
        match = re.search(r"phone=(\d+)", query)
        number = match.group(1) if match else number
        return ("whatsapp", normalize_phone("+" + number)) if number.isdigit() else ("whatsapp", "")
    if platform == "youtube":
        if parts and parts[0].startswith("@"):
            return ("youtube", parts[0])
        if len(parts) >= 2 and parts[0] in {"channel", "c", "user"}:
            return ("youtube", parts[1])
        return ("youtube", "")
    if platform == "linkedin" and len(parts) >= 2:
        return ("linkedin", f"{parts[0]}/{parts[1]}")
    if not parts or parts[0].lower() in _RESERVED_PATHS:
        return (platform, "")
    handle = parts[0].lstrip("@")
    if platform == "telegram" and handle.startswith("+"):
        return (platform, "")
    return (platform, handle.lower() if platform == "instagram" else handle)


def _clean_url(url: str) -> str:
    url = url.rstrip(".,;:!?…»)")
    if url.lower().startswith("www."):
        url = "https://" + url
    if "://" not in url:
        url = "https://" + url
    return url


def extract(text: str, *, default_country: str = "") -> Contacts:
    """Contacts in plain text (a bio, a description, a page's visible text)."""
    found = Contacts()
    if not text:
        return found
    text = fold_digits(html_lib.unescape(text))

    urls = [_clean_url(u) for u in URL_RE.findall(text)]
    urls += [_clean_url(u) for u in BARE_DOMAIN_RE.findall(text) if not any(u.lower() in x.lower() for x in urls)]
    urls += [_clean_url(u) for u in _BARE_HUB_RE.findall(text) if not any(u.lower() in x.lower() for x in urls)]
    for url in urls:
        _classify_url(found, url)

    # Emails after URLs so ``mailto``-free addresses are not mistaken for domains above.
    for email in EMAIL_RE.findall(text):
        email = email.lower().rstrip(".")
        if email not in found.emails and not email.endswith((".png", ".jpg", ".webp")):
            found.emails.append(email)

    without_urls = URL_RE.sub(" ", text)
    for raw in PHONE_RE.findall(without_urls):
        if re.fullmatch(r"\d{4}\s*[-–]\s*\d{4}", raw.strip()):
            continue  # a year range
        phone = normalize_phone(raw, default_country=default_country)
        if phone and phone not in found.phones:
            found.phones.append(phone)

    for platform, words in _PLATFORM_WORDS.items():
        pattern = r"(?:%s)\s*[:：\-]?\s*%s" % ("|".join(map(re.escape, words)), _HANDLE_RE)
        for match in re.finditer(pattern, text, re.IGNORECASE):
            handle = match.group(1)
            if "@" in text[match.start() : match.end()] and not handle.isdigit():
                found.add_social(platform, handle.lower() if platform == "instagram" else handle)
    return found


def _classify_url(found: Contacts, url: str) -> None:
    social = _social_from_url(url)
    host = _host(url)
    if social is not None:
        platform, handle = social
        found.add_social(platform, handle)
        return
    if not host or host in IGNORED_HOSTS or "." not in host:
        return
    if host in LINK_HUB_HOSTS:
        if url not in found.link_hubs:
            found.link_hubs.append(url)
        return
    if url not in found.websites:
        found.websites.append(url)


_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.IGNORECASE | re.DOTALL)


def extract_html(html: str, *, default_country: str = "") -> Contacts:
    """Contacts in a page: its ``mailto:``/``tel:``/profile links plus its visible text."""
    found = Contacts()
    if not html:
        return found
    for href in _HREF_RE.findall(html):
        href = html_lib.unescape(href).strip()
        lower = href.lower()
        if lower.startswith("mailto:"):
            email = unquote(href[7:]).split("?", 1)[0].strip().lower()
            if EMAIL_RE.fullmatch(email) and email not in found.emails:
                found.emails.append(email)
        elif lower.startswith("tel:"):
            phone = normalize_phone(unquote(href[4:]), default_country=default_country)
            if phone and phone not in found.phones:
                found.phones.append(phone)
        elif lower.startswith(("http://", "https://")) and _social_from_url(href) is not None:
            _classify_url(found, href)
    text = _TAG_RE.sub(" ", html)
    found.merge(extract(text, default_country=default_country))
    return found


def extract_all(texts: Iterable[str], *, default_country: str = "") -> Contacts:
    """One merged result for several texts of the same owner."""
    found = Contacts()
    for text in texts:
        found.merge(extract(text, default_country=default_country))
    return found
