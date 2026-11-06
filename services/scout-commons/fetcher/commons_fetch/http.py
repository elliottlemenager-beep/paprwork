"""Polite outbound HTTP for the Commons worker.

- Identifies itself with a User-Agent that links to the README.
- Honours robots.txt for page fetches (APIs are called directly).
- Spaces requests to the same host (COMMONS_HOST_GAP_S, default 1 s).
- Reads at most 400 KB of any page.
- Refuses non-public addresses on every hop, including redirects, so a
  submitted domain can't point the worker at internal services. The address
  that passed the check is the one we connect to (``PinnedTransport``), so a
  domain can't answer the check with a public address and the connection with
  an internal one (DNS rebinding).

Tests swap the network for an ``httpx.MockTransport`` via ``set_transport``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

UA = "PaprScoutCommons/0.1 (+https://github.com/Papr-ai/paprwork/tree/development/services/scout-commons)"
MAX_BYTES = 400_000
MIN_GAP_S = float(os.environ.get("COMMONS_HOST_GAP_S", "1.0"))
CHECK_IP = True

_transport: httpx.BaseTransport | None = None
_robots: dict[str, robotparser.RobotFileParser | None] = {}
_last: dict[str, float] = {}
_lock = threading.Lock()


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport
    _robots.clear()
    _last.clear()


def _resolve(host: str) -> list[str]:
    """All addresses for host (tests replace this)."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(i[4][0]).split("%", 1)[0] for i in infos]


def pick_public(host: str) -> str:
    """One address to connect to. Raises if host doesn't resolve or ANY address is non-public."""
    try:
        addrs = _resolve(host)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise httpx.ConnectError(f"unresolvable: {host}") from e
    if not addrs:
        raise httpx.ConnectError(f"unresolvable: {host}")
    if not all(ipaddress.ip_address(a).is_global for a in addrs):
        raise httpx.ConnectError("blocked: non-public address")
    v4 = [a for a in addrs if ":" not in a]
    return (v4 or addrs)[0]


class PinnedTransport(httpx.BaseTransport):
    """Resolves each hop once, checks it, then connects to that exact address.

    The URL host is swapped for the checked IP; Host header and TLS SNI keep the
    real name, so certificates still verify against it.
    """

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self.inner = inner or httpx.HTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not CHECK_IP:
            return self.inner.handle_request(request)
        host = request.url.host
        ip = pick_public(host)
        url = request.url.copy_with(host=ip)
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        ext = dict(request.extensions)
        if request.url.scheme == "https":
            ext["sni_hostname"] = host
        pinned = httpx.Request(request.method, url, headers=headers, stream=request.stream, extensions=ext)
        return self.inner.handle_request(pinned)  # httpx sets response.request back to the original

    def close(self) -> None:
        self.inner.close()


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        transport=PinnedTransport(_transport),
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": UA},
    )


def _read_capped(r: httpx.Response) -> bytes:
    buf = bytearray()
    for chunk in r.iter_bytes():
        buf += chunk
        if len(buf) >= MAX_BYTES:
            break
    return bytes(buf[:MAX_BYTES])


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _wait(host: str) -> None:
    if MIN_GAP_S <= 0:
        return
    with _lock:
        now = time.monotonic()
        slot = max(now, _last.get(host, 0.0) + MIN_GAP_S)
        _last[host] = slot
    if slot > now:
        time.sleep(slot - now)


def allowed(c: httpx.Client, url: str) -> bool:
    p = urlsplit(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
        try:
            _wait(p.netloc)
            with c.stream("GET", base + "/robots.txt") as r:
                if r.status_code >= 400:
                    rp = None  # no robots.txt: everything is allowed
                else:
                    rp.parse(_read_capped(r).decode("utf-8", "replace").splitlines())
        except httpx.HTTPError:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)


def get_text(c: httpx.Client, url: str) -> tuple[int, str, str]:
    """(status, final_url, text). Status 0 on a network error, -1 when robots.txt disallows."""
    if not allowed(c, url):
        return -1, url, ""
    _wait(urlsplit(url).netloc)
    try:
        with c.stream("GET", url) as r:
            body = _read_capped(r)
            return r.status_code, str(r.url), body.decode(r.encoding or "utf-8", "replace")
    except httpx.HTTPError:
        return 0, url, ""


def get_page(c: httpx.Client, url: str) -> dict:
    """Like get_text, plus headers and content type, for the batch fetcher.

    status: HTTP status, 0 = network error, -1 = robots.txt disallows.
    """
    if not allowed(c, url):
        return {"status": -1, "url": url, "headers": {}, "ctype": "", "body": "", "error": "robots"}
    _wait(urlsplit(url).netloc)
    try:
        with c.stream("GET", url) as r:
            ctype = r.headers.get("content-type", "")
            body = _read_capped(r) if "html" in ctype or not ctype else b""
            return {"status": r.status_code, "url": str(r.url), "headers": dict(r.headers), "ctype": ctype,
                    "body": body.decode(r.encoding or "utf-8", "replace"), "error": None}
    except httpx.TooManyRedirects:
        return {"status": 0, "url": url, "headers": {}, "ctype": "", "body": "", "error": "too_many_redirects"}
    except httpx.TimeoutException:
        return {"status": 0, "url": url, "headers": {}, "ctype": "", "body": "", "error": "timeout"}
    except httpx.HTTPError as e:
        msg = str(e)
        err = "blocked_ip" if "non-public" in msg else "dns" if "unresolvable" in msg else "connect"
        return {"status": 0, "url": url, "headers": {}, "ctype": "", "body": "", "error": err}


def get_json(c: httpx.Client, url: str, params: dict | None = None, headers: dict | None = None):
    """Parsed JSON for a 200 response, else None."""
    _wait(urlsplit(url).netloc)
    try:
        with c.stream("GET", url, params=params, headers=headers) as r:
            if r.status_code != 200:
                return None
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return None  # over the cap: refuse rather than parse a truncated document
    except httpx.HTTPError:
        return None
    try:
        return json.loads(bytes(body))
    except ValueError:
        return None


def forget(*hosts: str) -> None:
    """Drop cached robots.txt and pacing state for hosts (the batch calls this after each domain)."""
    with _lock:
        for h in hosts:
            _last.pop(h, None)
            for scheme in ("https", "http"):
                _robots.pop(f"{scheme}://{h}", None)
