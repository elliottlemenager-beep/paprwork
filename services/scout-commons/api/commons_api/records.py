"""Mongo document → API record. Every record carries the freshness fields (§7.3), whatever `fields` asks for."""

from __future__ import annotations

FRESHNESS = ("source", "as_of", "release", "stale", "provisional", "confidence", "coverage")
ALWAYS = ("type", "schema_version", "entity_id", "domain") + FRESHNESS

# Record field → Mongo path. Projection is built from this so we only pull what's asked for.
RECORD_PATHS = {
    "name": "name", "description": "description", "logo": None, "country": None, "state": None,
    "city": None, "industry": None, "size_band": None, "employees": None,
    "authority": "authority_pct", "authority_rank": "authority_rank", "inbound_sites": "inbound_sites",
    "outbound_sites": "outbound_sites", "tld": "tld", "kind": "kind", "language": "language",
    "homepage_url": "homepage_url", "homepage_status": "homepage_status",
    "homepage_source": "homepage_source", "name_confidence": "name_confidence",
    "lookalike_count": "lookalike_count", "has_text": "has_text",
    "uses": None, "open_jobs": None,
}
_INTERNAL = ("domain", "release_id", "as_of", "stale", "has_text", "lookalike_count")


def known_field(f: str) -> bool:
    return f == "*" or f == "lookalikes" or f in RECORD_PATHS or f in ALWAYS


def projection(fields: list[str]) -> dict | None:
    if "*" in fields:
        return None
    paths = {RECORD_PATHS[f] for f in fields if RECORD_PATHS.get(f)}
    return {p: 1 for p in paths | set(_INTERNAL)}


def _as_of(v) -> str | None:
    if v is None:
        return None
    s = str(v).replace(" ", "T")
    return s if s.endswith("Z") or "+" in s else s + "Z"


def shape(doc: dict, fields: list[str]) -> dict:
    has_ll = (doc.get("lookalike_count") or 0) > 0
    rec = {
        "type": "company", "schema_version": 1, "entity_id": doc["_id"], "domain": doc["domain"],
        "source": "batch", "as_of": _as_of(doc.get("as_of")), "release": doc.get("release_id"),
        "stale": bool(doc.get("stale", False)), "provisional": False, "confidence": "high",
        "coverage": "full" if doc.get("has_text") and has_ll else "partial",
    }
    want = RECORD_PATHS.keys() if "*" in fields else [f for f in fields if f in RECORD_PATHS]
    for f in want:
        p = RECORD_PATHS[f]
        rec[f] = ([] if f in ("uses", "open_jobs") else None) if p is None else doc.get(p)
    return rec
