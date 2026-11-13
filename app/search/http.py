"""Reading web addresses for the job search, safely.

Every address the search reads comes from somewhere outside the app: a job
feed, a community list, or a web page an agent found. Some of those could
point at this computer or at other devices on your home network (a router's admin
page, Job App's own server). So before any request, and again on every
redirect, the address is checked: only ordinary public web addresses are
read. Responses are capped in size, and nothing is ever sent except a plain
GET (or, for Workday's job search, a small JSON query).
"""

from __future__ import annotations

import asyncio
import gzip
import io
import ipaddress
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlsplit

from .. import __version__

log = logging.getLogger("jobapp.search.http")

USER_AGENT = f"JobApp/{__version__} (a personal job search tool running on one person's computer)"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".corp", ".intranet")


class FetchError(Exception):
    """A request did not produce a usable response.

    ``kind`` is one of: blocked, not_found, http, network, timeout, too_large.
    """

    def __init__(self, kind: str, url: str, message: str = "", status: Optional[int] = None):
        self.kind = kind
        self.url = url
        self.status = status
        super().__init__(message or kind)


@dataclass
class Response:
    url: str                       # where the response finally came from, after redirects
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8-sig", errors="replace"))
        except ValueError as exc:
            raise FetchError("http", self.url, "The response was not readable JSON.", self.status) from exc


class Web(Protocol):
    async def get(self, url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = 20) -> Response: ...
    async def post_json(self, url: str, body: dict, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = 20) -> Response: ...


# --- which addresses may be read -------------------------------------------------------


def _bad_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def check_public(url: str, resolve: Callable = socket.getaddrinfo) -> None:
    """Raise FetchError("blocked") unless the address is an ordinary public web address."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("blocked", url, "That is not a valid web address.") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("blocked", url, "Only http and https addresses are read.")
    if parts.username or parts.password:
        raise FetchError("blocked", url, "Addresses with a user name or password in them are not read.")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES) or "." not in host and ":" not in host:
        raise FetchError("blocked", url, "Addresses on this computer or your local network are never read.")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        addresses = [host]
    else:
        try:
            infos = resolve(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError) as exc:
            raise FetchError("network", url, f"The site {host} could not be found.") from exc
        addresses = [info[4][0] for info in infos]
    if not addresses or any(_bad_ip(address) for address in addresses):
        raise FetchError("blocked", url, "Addresses on this computer or your local network are never read.")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 6

    def __init__(self, resolve: Callable):
        super().__init__()
        self._resolve = resolve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl, self._resolve)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# --- the real reader --------------------------------------------------------------------


class SafeWeb:
    """Reads public web addresses. Blocking work runs in threads, a few at a time."""

    def __init__(self, *, concurrency: int = 8, per_host: int = 3, resolve: Callable = socket.getaddrinfo):
        self._resolve = resolve
        self._all = asyncio.Semaphore(concurrency)
        self._per_host_limit = per_host
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self.counts: Counter = Counter()
        self.bytes = 0

    def _host_gate(self, url: str) -> asyncio.Semaphore:
        host = (urlsplit(url).hostname or "").lower()
        if host not in self._hosts:
            self._hosts[host] = asyncio.Semaphore(self._per_host_limit)
        return self._hosts[host]

    def _fetch(self, url: str, data: Optional[bytes], headers: dict, max_bytes: int, timeout: float) -> Response:
        check_public(url, self._resolve)
        opener = urllib.request.build_opener(_CheckedRedirects(self._resolve))
        request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Accept-Encoding", "gzip")
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            raw = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            kind = "not_found" if exc.code in (404, 410) else "http"
            raise FetchError(kind, url, f"The site answered {exc.code}.", exc.code) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, FetchError):
                raise reason from exc
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise FetchError("timeout", url, "The site did not answer in time.") from exc
            raise FetchError("network", url, f"The site could not be reached ({reason}).") from exc
        except FetchError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise FetchError("timeout", url, "The site did not answer in time.") from exc
        except OSError as exc:
            raise FetchError("network", url, f"The site could not be reached ({exc}).") from exc
        with raw:
            body = raw.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError("too_large", url, "The page was larger than the app reads.")
            response_headers = {k.lower(): v for k, v in raw.headers.items()}
            if response_headers.get("content-encoding", "").lower() == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as unzip:
                    body = unzip.read(max_bytes * 8 + 1)
                if len(body) > max_bytes * 8:
                    raise FetchError("too_large", url, "The page was larger than the app reads.")
            return Response(url=raw.geturl(), status=raw.status, headers=response_headers, body=body)

    async def _run(self, url: str, data: Optional[bytes], headers: dict, max_bytes: int, timeout: float) -> Response:
        async with self._all, self._host_gate(url):
            started = time.monotonic()
            try:
                response = await asyncio.to_thread(self._fetch, url, data, headers, max_bytes, timeout)
            except FetchError as exc:
                self.counts[exc.kind] += 1
                log.debug("GET %s failed: %s (%.1fs)", url, exc.kind, time.monotonic() - started)
                raise
            self.counts["ok"] += 1
            self.bytes += len(response.body)
            return response

    async def get(self, url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = 20) -> Response:
        return await self._run(url, None, {"Accept": "text/html,application/json;q=0.9,*/*;q=0.5"}, max_bytes, timeout)

    async def post_json(self, url: str, body: dict, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = 20) -> Response:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        return await self._run(url, data, headers, max_bytes, timeout)
