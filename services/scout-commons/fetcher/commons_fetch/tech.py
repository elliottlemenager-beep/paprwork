"""Technology detection: which tools a company's website uses (HubSpot, Intercom, Stripe ...).

Input is the company's homepage from the newest Common Crawl crawl that has it
(see pages.py), so the company's site isn't visited. If the archive has no
homepage, or the archived page shows nothing, the live homepage is read once
with the polite client (robots.txt, 1 s gap, 400 KB cap).

Fingerprints come from webappanalyzer (github.com/enthec/webappanalyzer, the
community fork of Wappalyzer's open fingerprints, about 7,600 technologies).
They are downloaded by `python -m commons.compute --tech-fingerprints` and
stored in tech/fingerprints.json; they are not copied into this repo. Only the
signals a saved page carries are used: script URLs, inline scripts, HTML, meta
tags, response headers and cookies. JavaScript-only and DNS fingerprints are
skipped. A short built-in list (CORE) covers sales tools that matter most and
the tag-manager IDs webappanalyzer reads only from JavaScript.

Results are kept per crawl in tech/<d>.json, so `added` and `removed` show what
changed between the two newest crawls that had a homepage.
"""

from __future__ import annotations

import re
import time

import httpx

from . import pages
from .http import client, get_text
from .store import Store

TTL_S = 30 * 86400
TEXT_CHARS = 20_000
WA = "https://raw.githubusercontent.com/enthec/webappanalyzer/main/src/"
KEEP_CRAWLS = 6
CORE: dict[str, tuple[str, str]] = {
    "HubSpot": (
        "Marketing automation",
        r"js\.hs-scripts\.com|js\.hsforms\.net|js\.hs-analytics\.net|js\.hscollectedforms",
    ),
    "Marketo": ("Marketing automation", r"munchkin\.marketo\.net|mktoForms2|marketo\.com/js"),
    "Pardot": ("Marketing automation", r"pi\.pardot\.com|go\.pardot\.com|piAId\s*="),
    "Salesforce": ("CRM", r"force\.com/servlet/servlet\.WebToLead|webto\.salesforce\.com"),
    "Intercom": ("Live chat", r"widget\.intercom\.io|js\.intercomcdn\.com"),
    "Drift": ("Live chat", r"js\.driftt\.com|drift\.com/include"),
    "Crisp": ("Live chat", r"client\.crisp\.chat"),
    "Zendesk": ("Live chat", r"static\.zdassets\.com|zopim\.com"),
    "Segment": ("Customer data platform", r"cdn\.segment\.com/analytics\.js|cdn\.segment\.io"),
    "Google Tag Manager": ("Tag manager", r"googletagmanager\.com/gtm\.js|\bGTM-[A-Z0-9]{5,8}\b"),
    "Google Analytics": (
        "Analytics",
        r"google-analytics\.com/(analytics|ga)\.js|googletagmanager\.com/gtag/js\?id=(G|UA)-",
    ),
    "Facebook Pixel": ("Advertising", r"connect\.facebook\.net/[^\"']*/fbevents\.js"),
    "LinkedIn Insight Tag": ("Advertising", r"snap\.licdn\.com/li\.lms-analytics|_linkedin_partner_id"),
    "6sense": ("Account-based marketing", r"j\.6sc\.co"),
    "Clearbit": ("Account-based marketing", r"tag\.clearbitscripts\.com|reveal\.clearbit\.com"),
    "Chili Piper": ("Scheduling", r"js\.chilipiper\.com"),
    "Calendly": ("Scheduling", r"assets\.calendly\.com"),
    "Stripe": ("Payments", r"js\.stripe\.com"),
    "Shopify": ("Ecommerce", r"cdn\.shopify\.com|myshopify\.com"),
    "Webflow": ("Website builder", r"data-wf-site=|webflow\.(js|com/css)"),
    "WordPress": ("CMS", r"/wp-content/|/wp-includes/"),
    "Framer": ("Website builder", r"framerusercontent\.com|framer\.com/m/"),
    "Hotjar": ("Analytics", r"static\.hotjar\.com"),
    "Mixpanel": ("Analytics", r"cdn\.mxpnl\.com|cdn4\.mxpnl\.com"),
    "Amplitude": ("Analytics", r"cdn\.amplitude\.com"),
}
_CORE = {n: (cat, re.compile(rx, re.I)) for n, (cat, rx) in CORE.items()}
_SRC = re.compile(r"<script[^>]+src=[\"']([^\"']+)", re.I)
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.I | re.S)
_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"(name|property|content)\s*=\s*[\"']([^\"']*)", re.I)
_COMPILED: tuple[float, dict] | None = None


def visible_text(html: str) -> str:
    """Title, meta description and body text of a page, without scripts, styles or markup."""
    h = re.sub(r"<(script|style|noscript|svg|template)\b.*?</\1>", " ", html, flags=re.I | re.S)
    desc = " ".join(
        v
        for k, v in re.findall(
            r"<meta[^>]+(?:name|property)=[\"'](description|og:description)[\"'][^>]*content=[\"']([^\"']*)", h, re.I
        )
    )
    t = re.sub(r"<[^>]+>", " ", h)
    t = re.sub(r"&[a-z#0-9]+;", " ", t)
    return re.sub(r"\s+", " ", desc + " " + t).strip()


def _rx(p: str) -> re.Pattern | None:
    p = p.split("\\;", 1)[0]
    if not p:
        return None
    try:
        return re.compile(p, re.I)
    except re.error:
        return None


def _list(v) -> list[str]:
    return [v] if isinstance(v, str) else [x for x in (v or []) if isinstance(x, str)]


def build_fingerprints(store: Store) -> dict:
    """Download webappanalyzer's fingerprints and keep only what a saved page can match."""
    techs: dict[str, dict] = {}
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        cats = {int(k): v["name"] for k, v in c.get(WA + "categories.json").json().items()}
        for letter in "_abcdefghijklmnopqrstuvwxyz":
            for name, t in c.get(WA + f"technologies/{letter}.json").json().items():
                keep = {k: t[k] for k in ("scriptSrc", "scripts", "html", "meta", "headers", "cookies") if t.get(k)}
                if keep:
                    keep["cats"] = [cats.get(i, "Other") for i in t.get("cats", [])]
                    keep["implies"] = [i.split("\\;", 1)[0] for i in _list(t.get("implies"))]
                    techs[name] = keep
    meta = {"source": "github.com/enthec/webappanalyzer (GPL-3.0)", "built_at": int(time.time()), "count": len(techs)}
    store.write("tech/fingerprints.json", {**meta, "techs": techs})
    global _COMPILED
    _COMPILED = None
    return meta


def _compiled(store: Store) -> dict:
    global _COMPILED
    p = store.root / "tech" / "fingerprints.json"
    mtime = p.stat().st_mtime if p.exists() else 0.0
    if _COMPILED and _COMPILED[0] == mtime:
        return _COMPILED[1]
    data = store.read("tech/fingerprints.json") or {"techs": {}}
    out = {}
    for name, t in data["techs"].items():
        c = {
            "src": [r for r in map(_rx, _list(t.get("scriptSrc"))) if r],
            "inline": [r for r in map(_rx, _list(t.get("scripts"))) if r],
            "html": [r for r in map(_rx, _list(t.get("html"))) if r],
            "meta": {k.lower(): [r for r in map(_rx, _list(v)) if r] for k, v in (t.get("meta") or {}).items()},
            "headers": {k.lower(): [r for r in map(_rx, _list(v)) if r] for k, v in (t.get("headers") or {}).items()},
            "cookies": [k.lower() for k in (t.get("cookies") or {})],
            "cats": t.get("cats", []),
            "implies": t.get("implies", []),
        }
        out[name] = c
    _COMPILED = (mtime, out)
    return out


def detect(store: Store, html: str, headers: dict[str, str] | None = None) -> dict[str, list[str]]:
    """{technology: [categories]} found in one page."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    srcs = _SRC.findall(html)
    inline = "\n".join(_INLINE.findall(html))[:300_000]
    metas: dict[str, str] = {}
    for tag in _META.findall(html):
        a = {k.lower(): v for k, v in _ATTR.findall(tag)}
        key = a.get("name") or a.get("property")
        if key and "content" in a:
            metas[key.lower()] = a["content"]
    cookie_names = {
        c.split("=", 1)[0].strip().lower() for c in re.split(r",(?=[^;,]+=)", headers.get("set-cookie", ""))
    }
    fp = _compiled(store)
    found: dict[str, list[str]] = {}
    for name, c in fp.items():
        hit = (
            any(r.search(s) for r in c["src"] for s in srcs)
            or any(m in metas and (not rs or any(r.search(metas[m]) for r in rs)) for m, rs in c["meta"].items())
            or any(h in headers and (not rs or any(r.search(headers[h]) for r in rs)) for h, rs in c["headers"].items())
            or any(k in cookie_names for k in c["cookies"])
            or any(r.search(inline) for r in c["inline"])
            or any(r.search(html) for r in c["html"])
        )
        if hit:
            found[name] = c["cats"]
    for name, (cat, rx) in _CORE.items():
        if name not in found and rx.search(html):
            found[name] = [cat]
    queue = list(found)
    while queue:  # "WooCommerce implies WordPress" etc.
        for imp in fp.get(queue.pop(), {}).get("implies", []):
            if imp in fp and imp not in found:
                found[imp] = fp[imp]["cats"]
                queue.append(imp)
    return dict(sorted(found.items()))


def compute_tech(store: Store, domain: str) -> dict:
    prev = store.read(f"tech/{domain}.json") or {}
    history: dict[str, dict] = dict(prev.get("history") or {})
    pg = pages.lookup(store, domain)
    home = (pg.get("pages") or {}).get("home")
    found, source, crawl, page_url, html = {}, None, None, None, ""
    with client(timeout=30) as c:
        if home:
            got = pages.fetch(home, c)
            if got:
                found, source, crawl, page_url = detect(store, got[1], got[0]), "archive", home["crawl"], home["url"]
                html = got[1]
        if not found:
            status, final, live = get_text(c, f"https://{domain}/")
            if status == 200 and live:
                found, source, crawl, page_url = detect(store, live), "live", "live-" + time.strftime("%Y-%m"), final
                html = live
    if html:  # ranking (rank.py) compares homepage text; keep it so the page is read once
        store.write(
            f"text/{domain}.json",
            {
                "domain": domain,
                "source": source,
                "crawl": crawl,
                "text": visible_text(html)[:TEXT_CHARS],
                "computed_at": int(time.time()),
            },
        )
    if not source:
        payload = {"status": "no_page", "domain": domain, "computed_at": int(time.time()), "pages_status": pg["status"]}
        store.write(f"tech/{domain}.json", payload)
        return payload
    history[crawl] = {"tech": sorted(found), "url": page_url}
    keep = sorted(history, reverse=True)[:KEEP_CRAWLS]
    history = {k: history[k] for k in keep}
    snaps = [history[k] for k in keep if not k.startswith("live-")]
    added = removed = None
    if len(snaps) >= 2:
        a, b = set(snaps[0]["tech"]), set(snaps[1]["tech"])
        added, removed = sorted(a - b), sorted(b - a)
    payload = {
        "status": "ready",
        "domain": domain,
        "source": source,
        "crawl": crawl,
        "page": page_url,
        "tech": [{"name": n, "categories": cats} for n, cats in found.items()],
        "added": added,
        "removed": removed,
        "history": history,
        "computed_at": int(time.time()),
    }
    store.write(f"tech/{domain}.json", payload)
    return payload
