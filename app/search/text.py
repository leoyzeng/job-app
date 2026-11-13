"""Turning job pages into plain text, and the simple signals read from that text."""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Optional

# --- HTML to text -------------------------------------------------------------------------

_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe", "form", "button", "select"}
_LAYOUT = {"nav", "footer"}
_BLOCK = {
    "p", "div", "section", "article", "main", "header", "aside", "ul", "ol", "table", "tr", "h1", "h2",
    "h3", "h4", "h5", "h6", "br", "hr", "blockquote", "pre", "dl", "dt", "dd",
}


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.layout = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _LAYOUT:
            self.layout += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _LAYOUT:
            self.layout = max(0, self.layout - 1)
        elif tag in _BLOCK or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and not self.layout:
            self.parts.append(data)


def tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in (text or "").replace("\r", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line in ("", "-"):
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(markup: str) -> str:
    """Readable text of an HTML page or fragment. Scripts, styles, menus and footers are dropped."""
    if not markup:
        return ""
    parser = _TextParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - a broken page still gives what was read so far
        pass
    return tidy("".join(parser.parts))


def unescape_html(markup: str) -> str:
    """Greenhouse sends its HTML with the tags themselves escaped (&lt;p&gt;)."""
    return html.unescape(markup or "")


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ", int(limit * 0.8))
    return (cut[:space] if space > 0 else cut).rstrip() + " [...]"


def page_title(markup: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", markup or "", flags=re.S | re.I)
    return " ".join(html.unescape(match.group(1)).split()) if match else ""


# --- schema.org JobPosting embedded in pages -------------------------------------------------

_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", flags=re.S | re.I)


def _walk(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in value:
                yield from _walk(value[key])


def _is_posting(obj: dict) -> bool:
    kind = obj.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(k).lower() == "jobposting" for k in kinds)


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or value.get("@value") or "").strip()
    if isinstance(value, list):
        return ", ".join(t for t in (_text_of(v) for v in value) if t)
    return "" if value is None else str(value)


def _place(location: Any) -> str:
    places = []
    for item in location if isinstance(location, list) else [location]:
        if not isinstance(item, dict):
            if item:
                places.append(str(item))
            continue
        address = item.get("address", item)
        if isinstance(address, str):
            places.append(address)
            continue
        if isinstance(address, dict):
            parts = [_text_of(address.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")]
            text = ", ".join(p for p in parts if p)
            if text:
                places.append(text)
    return "; ".join(dict.fromkeys(places))


def _pay(salary: Any) -> str:
    if not isinstance(salary, dict):
        return _text_of(salary)
    currency = salary.get("currency") or ""
    value = salary.get("value")
    if isinstance(value, dict):
        low, high = value.get("minValue"), value.get("maxValue")
        single = value.get("value")
        unit = str(value.get("unitText") or "").lower()
        amount = f"{low}-{high}" if low and high else str(single or low or high or "")
        if not amount:
            return ""
        return f"{currency} {amount}{' per ' + unit if unit else ''}".strip()
    return _text_of(value)


def json_ld_postings(markup: str) -> list[dict]:
    """The JobPosting objects a page declares about itself, already tidied."""
    found = []
    for block in _LD.findall(markup or ""):
        try:
            data = json.loads(html.unescape(block.strip()))
        except ValueError:
            continue
        for obj in _walk(data):
            if not _is_posting(obj):
                continue
            description = obj.get("description") or ""
            found.append({
                "title": _text_of(obj.get("title")),
                "company": _text_of(obj.get("hiringOrganization")),
                "location": _place(obj.get("jobLocation")) or (
                    "Remote" if str(obj.get("jobLocationType", "")).upper() == "TELECOMMUTE" else ""),
                "description": html_to_text(description) if "<" in description else tidy(description),
                "posted_at": _text_of(obj.get("datePosted"))[:10],
                "valid_through": _text_of(obj.get("validThrough"))[:10],
                "employment_type": _text_of(obj.get("employmentType")),
                "pay": _pay(obj.get("baseSalary")),
            })
    return found


# --- signals ---------------------------------------------------------------------------------

_CLOSED = re.compile(
    r"(no longer accepting applications|no longer (?:open|available|active)|position (?:has been|is) (?:filled|closed)"
    r"|job (?:posting )?(?:is )?(?:closed|expired|not found)|this (?:job|position|posting|role) (?:has )?(?:expired|closed)"
    r"|the job you are looking for (?:is|was)|page (?:you requested )?(?:could not be|cannot be) found"
    r"|applications? (?:are|is) (?:now )?closed)",
    flags=re.I,
)


def looks_closed(text: str) -> bool:
    """True when the start of the page says the posting is gone."""
    return bool(_CLOSED.search((text or "")[:3000]))


_SCAM_SIGNS = [
    (re.compile(r"\b(?:application|training|registration|processing|onboarding) fee\b|\bpay (?:a|the) fee\b", re.I),
     "asks for a fee"),
    (re.compile(r"\b(?:whatsapp|telegram|signal app|google hangouts|wire transfer|western union|gift cards?)\b", re.I),
     "mentions contact by chat app or unusual payments"),
    (re.compile(r"\b(?:bank account|routing number|social security number|ssn)\b.{0,40}\b(?:send|provide|submit|email)\b"
                r"|\b(?:send|provide|submit|email)\b.{0,40}\b(?:bank account|routing number|social security number|ssn)\b",
                re.I | re.S),
     "asks for bank or ID details up front"),
    (re.compile(r"\b(?:send|email) (?:your )?(?:resume|cv) to [a-z0-9._%+-]+@(?:gmail|yahoo|hotmail|outlook|aol)\.com\b",
                re.I),
     "asks for applications to a personal email address"),
]


def scam_signs(text: str) -> list[str]:
    return [label for pattern, label in _SCAM_SIGNS if pattern.search(text or "")]


# --- places ------------------------------------------------------------------------------------

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}
_US_CODES = set(_US_STATES.values())
_US_WORDS = re.compile(
    r"\b(?:united states|usa|u\.s\.a?\.?|us|america|nationwide)\b"
    r"|\b(?:san francisco|new york city|nyc|seattle|boston|austin|los angeles|chicago|pittsburgh|bay area"
    r"|silicon valley|washington,? d\.?c\.?)\b",
    re.I,
)
# Countries and large cities outside the US that show up often on tech job boards.
_ELSEWHERE = re.compile(
    r"\b(?:canada|ontario|quebec|british columbia|alberta|toronto|vancouver|montreal|waterloo|ottawa|calgary"
    r"|united kingdom|uk|england|scotland|london|manchester|edinburgh|ireland|dublin|germany|berlin|munich"
    r"|france|paris|netherlands|amsterdam|spain|madrid|barcelona|italy|milan|switzerland|zurich|sweden"
    r"|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki|poland|warsaw|krakow|czech|prague|austria"
    r"|vienna|belgium|brussels|portugal|lisbon|romania|bucharest|ukraine|israel|tel aviv|india|bangalore"
    r"|bengaluru|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida|chennai|china|beijing|shanghai|shenzhen"
    r"|hong kong|taiwan|taipei|japan|tokyo|korea|seoul|singapore|australia|sydney|melbourne|new zealand"
    r"|mexico|mexico city|guadalajara|brazil|sao paulo|são paulo|argentina|buenos aires|colombia|bogota"
    r"|chile|santiago|philippines|manila|vietnam|indonesia|malaysia|kuala lumpur|thailand|bangkok"
    r"|uae|dubai|abu dhabi|saudi|riyadh|egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|cape town"
    r"|emea|apac|latam|europe)\b",
    re.I,
)
_STATE_CODE = re.compile(r"(?:,|\s-)\s*([A-Z]{2})\b")


def in_us(location: str) -> Optional[bool]:
    """True if the place is in the US, False if it is clearly elsewhere, None if it cannot tell."""
    text = (location or "").strip()
    if not text:
        return None
    lowered = text.lower()
    us = bool(_US_WORDS.search(text)) or any(name in lowered for name in _US_STATES)
    if not us:
        us = any(code in _US_CODES for code in _STATE_CODE.findall(text))
    if not us and re.fullmatch(r"(?:US|USA)[-\s,].*|.*[-\s,](?:US|USA)", text):
        us = True
    elsewhere = bool(_ELSEWHERE.search(text))
    if us:
        return True
    if elsewhere:
        return False
    return None
