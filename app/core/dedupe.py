"""Recognising the same job when it turns up twice.

Two keys are made for every job:

* ``url:<normalised link>``: the same posting reached through different
  tracking links is one job.
* ``key:<company>|<title>|<location>``: the same job found on two sites is
  one job.

A job that matches an existing record on either key is a duplicate, whatever
the existing record's status. That is what keeps skipped and applied jobs from
coming back (PLAN.md R7).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

# Query parameters that say where a visitor came from, not which job it is.
_TRACKING = {
    "gh_src", "lever-source", "lever-origin", "source", "src", "ref", "referrer",
    "referer", "trk", "trackingid", "refid", "gclid", "fbclid", "msclkid",
    "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi", "jobsource", "sid",
}

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "plc", "lp", "llp",
}


class BadUrl(ValueError):
    pass


def normalize_url(url: str) -> str:
    """Reduce a link to the parts that identify the posting."""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise BadUrl("Enter a full link that starts with https://")
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError as exc:
        raise BadUrl("That link has an invalid port number.") from exc
    default_port = {"http": 80, "https": 443}[parts.scheme.lower()]
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path)
    path = path.rstrip("/")
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING and not k.lower().startswith("utm_")
    ]
    query = urlencode(sorted(kept))
    return netloc + path + (f"?{query}" if query else "")


def text_key(text: str) -> str:
    """Lowercase, accents removed, punctuation and extra spaces dropped."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^a-z0-9]+", " ", folded.lower())
    return " ".join(folded.split())


def company_key(company: str) -> str:
    words = text_key(company).split()
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def make_dedupe_keys(url: str, company: str, title: str, location: str = "") -> list[str]:
    keys = [f"url:{normalize_url(url)}"]
    comp, role = company_key(company), text_key(title)
    if comp and role:
        keys.append(f"key:{comp}|{role}|{text_key(location)}")
    return keys


def job_id_for(url: str) -> str:
    digest = hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()
    return f"j_{digest[:8]}"
