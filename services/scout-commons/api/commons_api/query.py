"""Shared filter language (§7.1) → MongoDB query. Filters run inside the engine, never after retrieval."""

from __future__ import annotations

import re

from commons_contract.filters import AllOf, DatedFact, Exclude, Filters, NoneOf, Range

from .registry import FIELDS, HAS_FIELDS


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.path = status, code, message, path


_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def normalize_domain(v: str) -> str | None:
    d = v.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0]
    d = d.removeprefix("www.").rstrip(".")
    return d if _DOMAIN.match(d) else None


def _check_type(f, v, path):
    ok = {"keyword": isinstance(v, str), "bool": isinstance(v, bool),
          "number": isinstance(v, (int, float)) and not isinstance(v, bool)}[f.type]
    if not ok:
        raise ApiError(400, "invalid_request", f"{f.name} expects a {f.type} value", path)
    if f.values and isinstance(v, str) and v not in f.values:
        raise ApiError(400, "invalid_request", f"{f.name} must be one of {list(f.values)}", path)


def _cond(key: str, cond, path: str) -> dict:
    f = FIELDS.get(key)
    if f is None:
        raise ApiError(400, "unknown_field", f"Unknown filter field '{key}'. See GET /v1/commons/fields.", path)
    ops = f.operators
    if isinstance(cond, Range):
        if "range" not in ops:
            raise ApiError(400, "invalid_request", f"{key} doesn't support ranges", path)
        return {f.mongo: {f"${k}": v for k, v in cond.model_dump(exclude_none=True).items()}}
    if isinstance(cond, AllOf):
        raise ApiError(400, "invalid_request", f"{key} holds one value; 'all' needs a list field", path)
    if isinstance(cond, NoneOf):
        if "not" not in ops:
            raise ApiError(400, "invalid_request", f"{key} doesn't support 'not'", path)
        for v in cond.not_:
            _check_type(f, v, path)
        return {f.mongo: {"$nin": list(cond.not_)}}
    if isinstance(cond, DatedFact):
        raise ApiError(400, "invalid_request", f"{key} has no dated facts", path)
    if isinstance(cond, list):
        if "any" not in ops and f.type != "number":
            raise ApiError(400, "invalid_request", f"{key} doesn't support any-of lists", path)
        for v in cond:
            _check_type(f, v, path)
        return {f.mongo: {"$in": cond}}
    _check_type(f, cond, path)
    return {f.mongo: cond}


def _has(names, path: str) -> dict:
    out = {}
    for n in names:
        p = HAS_FIELDS.get(n) or (FIELDS[n].mongo if n in FIELDS else None)
        if p is None:
            raise ApiError(400, "unknown_field", f"'has' doesn't know field '{n}'", path)
        out[p] = {"$gt": 0} if p == "lookalike_count" else ({"$eq": True} if p == "has_text" else {"$nin": [None, ""]})
    return out


def to_mongo(filters: Filters | None, base: str = "/filters") -> dict:
    q: dict = {}
    for k, cond in (filters.root if filters else {}).items():
        part = _has(cond, f"{base}/has") if k == "has" else _cond(k, cond, f"{base}/{k}")
        for p, c in part.items():
            q.setdefault("$and", []).append({p: c}) if p in q else q.__setitem__(p, c)
    return q


def exclude_to_mongo(ex: Exclude | None) -> tuple[dict, list[str]]:
    """Returns (mongo clause, warnings codes)."""
    if not ex:
        return {}, []
    clauses, warn = [], []
    if ex.filters and ex.filters.root:
        clauses.append({"$nor": [to_mongo(ex.filters, "/exclude/filters")]})
    if ex.ids:
        ids = [i for i in ex.ids if i.startswith("c_")]
        doms = [d for d in (normalize_domain(i) for i in ex.ids if not i.startswith("c_")) if d]
        if ids:
            clauses.append({"_id": {"$nin": ids}})
        if doms:
            clauses.append({"domain": {"$nin": doms}})
    if ex.in_private_layer:
        warn.append("in_private_layer")
    return ({"$and": clauses} if clauses else {}), warn


def merge(*qs: dict) -> dict:
    qs = [q for q in qs if q]
    return {} if not qs else qs[0] if len(qs) == 1 else {"$and": qs}
