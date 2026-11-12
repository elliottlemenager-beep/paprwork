"""Filterable fields for the active release. GET /v1/commons/fields and filter validation both read this.

Only fields that actually exist in the cc-2026q3 records are listed. Contract fields that aren't built yet
(country, size_band, industry, uses, open_jobs...) are rejected as unknown_field until they ship.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class F:
    name: str
    type: str                      # keyword | number | bool
    path: str | None = None        # Mongo path when it differs from name
    values: tuple[str, ...] | None = None
    facetable: bool = False
    sortable: bool = False
    ops: tuple[str, ...] = field(default=())

    @property
    def mongo(self) -> str:
        return self.path or self.name

    @property
    def operators(self) -> list[str]:
        if self.ops:
            return list(self.ops)
        return {"keyword": ["eq", "any", "not", "has"], "number": ["eq", "range", "has"],
                "bool": ["eq", "has"]}[self.type]


FIELDS: dict[str, F] = {f.name: f for f in [
    F("domain", "keyword"),
    F("tld", "keyword", facetable=True),
    F("kind", "keyword", facetable=True),
    F("language", "keyword", facetable=True),
    F("name_confidence", "keyword", values=("high", "medium", "low"), facetable=True),
    F("homepage_status", "keyword", facetable=True),
    F("homepage_source", "keyword", values=("commoncrawl", "live_fetch"), facetable=True),
    F("has_text", "bool", facetable=True),
    F("authority", "number", path="authority_pct"),
    F("authority_rank", "number", sortable=True),
    F("inbound_sites", "number"),
    F("outbound_sites", "number"),
    F("lookalike_count", "number"),
]}

# Fields that `has` may check (record fields, not only filter fields).
HAS_FIELDS = {"name": "name", "description": "description", "homepage": "homepage_url",
              "lookalikes": "lookalike_count", "text": "has_text"}

DEFAULT_SORT = [("authority_rank", 1)]
# Only indexed sorts are allowed (authority_rank is unique per company, so no tiebreaker is needed).
# authority (desc) is the same order as authority_rank (asc); clients can sort on either.
SORT_ALIASES = {"authority": ("authority_rank", -1)}
