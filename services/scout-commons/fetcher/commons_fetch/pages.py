"""Company pages from the Common Crawl URL index, without visiting the company's site.

Each monthly crawl publishes `cluster.idx` (about 105 MB): the first URL key of
every block of its sorted URL index. We download it once per crawl, find the
block(s) that can hold a domain, and range-fetch only those blocks (about 250 KB
each) from data.commoncrawl.org. The shared index server is not used for
lookups: in testing it failed often, while the download CDN did not.

The newest crawl is read first. Page kinds it lacks (home, pricing, careers,
about) are filled from up to LOOKBACK older crawls. Homepages that redirect
(http to https, bare to www, / to /en/) are followed inside the index.

Stored per domain in pages/<d>.json for TTL_S. Holds only URLs and archive
offsets; `fetch()` downloads one page's bytes when a caller needs the content.
"""

from __future__ import annotations

import bisect
import gzip
import json
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from .http import MAX_BYTES, client
from .store import Store

CC = "https://data.commoncrawl.org/"
IDX = CC + "cc-index/collections/{crawl}/indexes/{file}"
LOOKBACK = 2
TTL_S = 30 * 86400
CRAWLS_TTL_S = 86400
KINDS = {
    "home": re.compile(r"^/(index\.(html?|php)|home/?|[a-z]{2}(-[a-z]{2})?/?)?$", re.I),
    "pricing": re.compile(r"^/([^/]+/)?(pricing|plans|price)(/|\.html?)?$", re.I),
    "careers": re.compile(r"^/([^/]+/)?(careers?|jobs|join-?us|work-with-us|hiring)(/|\.html?)?$", re.I),
    "about": re.compile(r"^/([^/]+/)?(about(-us)?|company|team|our-team|who-we-are)(/|\.html?)?$", re.I),
}

_IDX: dict[str, tuple[list[str], list[tuple[str, int, int]]]] = {}
_IDX_LOCK = threading.Lock()


def surt(domain: str) -> str:
    """lemlist.com -> 'com,lemlist)': the URL-index key prefix for the domain and its www host."""
    return ",".join(reversed(domain.lower().split("."))) + ")"


def crawls(store: Store, c: httpx.Client) -> list[str]:
    """Crawl ids, newest first (cached for a day)."""
    hit = store.read("pages/crawls.json")
    if hit and time.time() - hit.get("at", 0) < CRAWLS_TTL_S:
        return hit["crawls"]
    ids: list[str] = []
    try:
        r = c.get("https://index.commoncrawl.org/collinfo.json", timeout=20)
        if r.status_code == 200:
            ids = [x["id"] for x in r.json()]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        ids = []
    if not ids:  # the index server is down often; the CDN lists crawls too
        r = c.get(CC + "crawl-data/index.html", timeout=30)
        ids = sorted(set(re.findall(r"CC-MAIN-\d{4}-\d{2}", r.text)), reverse=True)
    if not ids:
        raise RuntimeError("no Common Crawl crawl list available")
    store.write("pages/crawls.json", {"at": int(time.time()), "crawls": ids})
    return ids


def cluster(store: Store, c: httpx.Client, crawl: str) -> tuple[list[str], list[tuple[str, int, int]]]:
    """(keys, locations) of a crawl's cluster.idx, downloaded once and kept in memory."""
    with _IDX_LOCK:
        if crawl in _IDX:
            return _IDX[crawl]
        path = store.root / "pages" / "idx" / f"{crawl}.idx"
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            with c.stream("GET", IDX.format(crawl=crawl, file="cluster.idx"), timeout=600) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            tmp.replace(path)
        keys, locs = [], []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                parts = ln.rstrip("\n").split("\t")
                if len(parts) >= 4:
                    keys.append(parts[0].split(" ", 1)[0])
                    locs.append((parts[1], int(parts[2]), int(parts[3])))
        _IDX[crawl] = (keys, locs)
        return _IDX[crawl]


def blocks(keys: list[str], locs: list[tuple[str, int, int]], prefix: str) -> list[tuple[str, int, int]]:
    """Index blocks that can hold keys starting with `prefix` (including the block before the first match)."""
    lo = max(bisect.bisect_left(keys, prefix) - 1, 0)
    hi = bisect.bisect_left(keys, prefix + "\uffff")
    return locs[lo : max(hi, lo + 1)]


def _range(c: httpx.Client, url: str, off: int, length: int) -> bytes | None:
    for attempt in range(5):
        try:
            r = c.get(url, headers={"Range": f"bytes={off}-{off + length - 1}"}, timeout=60)
            if r.status_code in (200, 206) and r.content[:2] == b"\x1f\x8b":
                return r.content
        except httpx.HTTPError:
            pass
        time.sleep(1.5 * (attempt + 1))  # the CDN answers 503 SlowDown when busy
    return None


def rows(store: Store, c: httpx.Client, crawl: str, domain: str) -> list[dict]:
    """Every URL-index row for the domain (and www.) in one crawl. Raises LookupError if a block failed."""
    keys, locs = cluster(store, c, crawl)
    pre = surt(domain)
    out = []
    for shard, off, n in blocks(keys, locs, pre):
        raw = _range(c, IDX.format(crawl=crawl, file=shard), off, n)
        if raw is None:
            raise LookupError(f"index block {shard}@{off} unavailable")
        for ln in gzip.decompress(raw).decode("utf-8", "replace").splitlines():
            if not ln.startswith(pre):
                continue
            try:
                key, ts, js = ln.split(" ", 2)
                row = json.loads(js)
            except ValueError:
                continue
            row["ts"], row["crawl"] = ts, crawl
            out.append(row)
    return out


def _norm(url: str) -> str:
    p = urlsplit(url)
    return (p.hostname or "").removeprefix("www.") + (p.path.rstrip("/") or "/")


def _rec(r: dict) -> dict:
    return {k: r.get(k) for k in ("url", "crawl", "ts", "filename", "offset", "length", "status")}


def pick(found: list[dict]) -> dict[str, dict]:
    """Best record per page kind: a 200 HTML page, shortest path, newest. Homepage redirects are followed."""
    ok = [r for r in found if r.get("status") == "200" and "html" in (r.get("mime-detected") or r.get("mime") or "")]
    best: dict[str, dict] = {}
    for kind, rx in KINDS.items():
        hits = [r for r in ok if rx.match(urlsplit(r["url"]).path or "/")]
        if hits:
            hits.sort(key=lambda r: (len(urlsplit(r["url"]).path), -int(r["ts"])))
            best[kind] = _rec(hits[0])
    if "home" not in best:
        by_url = {_norm(r["url"]): r for r in found}
        for r in found:
            if r.get("status", "").startswith("3") and KINDS["home"].match(urlsplit(r["url"]).path or "/"):
                cur, hops = r, 0
                while cur and cur.get("status", "").startswith("3") and cur.get("redirect") and hops < 4:
                    cur, hops = by_url.get(_norm(cur["redirect"])), hops + 1
                if cur and cur.get("status") == "200":
                    best["home"] = {**_rec(cur), "redirected_from": r["url"]}
                    break
    return best


def lookup(store: Store, domain: str) -> dict:
    with client(timeout=60) as c:
        ids = crawls(store, c)[: LOOKBACK + 1]
        found: dict[str, dict] = {}
        checked, failed, newest_count = [], [], None
        for crawl in ids:
            try:
                rs = rows(store, c, crawl, domain)
            except LookupError:
                failed.append(crawl)
                continue
            checked.append(crawl)
            if newest_count is None:
                newest_count = len(rs)
            for kind, rec in pick(rs).items():
                found.setdefault(kind, rec)
            if len(found) == len(KINDS):
                break
    status = "ready" if found else ("not_in_crawl" if checked else "error")
    payload = {
        "status": status,
        "domain": domain,
        "pages": found,
        "urls_in_newest_crawl": newest_count or 0,
        "crawls_checked": checked,
        "crawls_failed": failed,
        "computed_at": int(time.time()),
    }
    if status != "error":
        store.write(f"pages/{domain}.json", payload)
    return payload


def fetch(rec: dict, c: httpx.Client) -> tuple[dict[str, str], str] | None:
    """(response headers, html) for one archived page record."""
    raw = _range(c, CC + rec["filename"], int(rec["offset"]), int(rec["length"]))
    if raw is None:
        return None
    parts = gzip.decompress(raw).split(b"\r\n\r\n", 2)
    if len(parts) < 3:
        return None
    headers: dict[str, str] = {}
    for ln in parts[1].decode("latin-1").split("\r\n")[1:]:
        k, _, v = ln.partition(":")
        if k:
            headers[k.strip().lower()] = v.strip()
    body = parts[2]
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    return headers, body[:MAX_BYTES].decode("utf-8", "replace")
