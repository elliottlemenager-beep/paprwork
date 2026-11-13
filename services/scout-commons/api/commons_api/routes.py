"""/filter, /entities, /search, /tasks."""

from __future__ import annotations

import base64
import json
import uuid

from fastapi import FastAPI, Query, Request
from pymongo.errors import ExecutionTimeout

from commons_contract.entities import EntitiesRequest
from commons_contract.filter import FilterRequest
from commons_contract.search import SearchRequest

from . import store
from .query import ApiError, exclude_to_mongo, merge, normalize_domain, to_mongo
from .records import projection, shape
from .registry import DEFAULT_SORT, FIELDS, SORT_ALIASES

MAX_TOTAL = 10_000
FACET_CAP = 10_000
COUNT_BUDGET_MS = 1500
FACET_BUDGET_MS = 2500
NOT_IN_PRIVATE = {"code": "IGNORED_FIELD", "path": "/exclude/in_private_layer",
                  "message": "in_private_layer isn't wired to Papr memory yet; it has no effect."}


def squash(raw: float) -> float:
    """Raw web-graph scores are unbounded (median ~0.07, max ~2.5). Map to 0-1 without ties at the top."""
    return raw / (raw + 0.25) if raw > 0 else 0.0


def _resolve(col, ids: list[str], proj) -> tuple[dict[str, dict], list[str]]:
    """ids or domains → {requested: doc}, not_found."""
    eids = [i for i in ids if i.startswith("c_")]
    doms = {i: normalize_domain(i) for i in ids if not i.startswith("c_")}
    found: dict[str, dict] = {}
    if eids:
        for d in col.find({"_id": {"$in": eids}}, proj, max_time_ms=3000):
            found[d["_id"]] = d
    valid = [d for d in doms.values() if d]
    if valid:
        by = {d["domain"]: d for d in col.find({"domain": {"$in": valid}}, proj, max_time_ms=3000)}
        for raw, d in doms.items():
            if d in by:
                found[raw] = by[d]
    return found, [i for i in ids if i not in found]


def register(app: FastAPI):
    from .main import _check_fields, _cursor, ok

    @app.post("/v1/commons/filter")
    def filter_(body: FilterRequest, request: Request, max_results: int = Query(100, ge=0, le=1000)):
        _check_fields(body.fields, "/fields")
        col, _ = store.companies()
        ex, warn = exclude_to_mongo(body.exclude)
        q = merge(to_mongo(body.filters), ex)
        sort = []
        for i, s in enumerate(body.sort):
            d = 1 if s.order == "asc" else -1
            if s.field in SORT_ALIASES:
                path, flip = SORT_ALIASES[s.field]
                sort.append((path, d * flip))
                continue
            f = FIELDS.get(s.field)
            if not f or not f.sortable:
                raise ApiError(400, "invalid_request", f"Can't sort by '{s.field}' (sortable: authority, authority_rank)", f"/sort/{i}/field")
            sort.append((f.mongo, d))
        sort = sort or DEFAULT_SORT
        offset = _cursor(body.cursor)
        if offset + max_results > MAX_TOTAL:
            raise ApiError(400, "invalid_request", "Paging stops at 10,000 rows; larger exports go through an export job.", "/cursor")
        results = []
        if max_results:
            docs = col.find(q, projection(body.fields)).sort(sort).skip(offset).limit(max_results).max_time_ms(8000)
            results = [shape(d, body.fields) for d in docs]
        # Counts/facets get a time budget: exact when an index covers the filter, otherwise an honest
        # lower bound + notice. Atlas Search (pending the disk decision) makes these exact and fast.
        notices, exact = [], True
        if not q:
            n, exact = col.estimated_document_count(), False
        else:
            try:
                n = col.count_documents(q, limit=MAX_TOTAL + 1, maxTimeMS=COUNT_BUDGET_MS)
            except ExecutionTimeout:
                n, exact = offset + len(results), False
                notices.append({"code": "COUNT_ESTIMATED", "path": "/filters",
                                "message": "Exact count timed out; total is a lower bound."})
        facets = {}
        for i, name in enumerate(body.facets):
            f = FIELDS.get(name)
            if not f or not f.facetable:
                raise ApiError(400, "invalid_request", f"'{name}' isn't facetable", f"/facets/{i}")
            pipe = [{"$match": q}, {"$sort": {"authority_rank": 1}}, {"$limit": FACET_CAP}, {"$group": {"_id": f"${f.mongo}", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}}, {"$limit": 50}]
            try:
                facets[name] = {str(r["_id"]): r["n"] for r in col.aggregate(pipe, maxTimeMS=FACET_BUDGET_MS)}
            except ExecutionTimeout:
                facets[name] = {}
                notices.append({"code": "FACET_UNAVAILABLE", "path": f"/facets/{i}",
                                "message": f"Facet '{name}' timed out for these filters."})
        nxt = None
        more = (offset + max_results < min(n, MAX_TOTAL)) if exact else True
        if max_results and len(results) == max_results and more and offset + max_results < MAX_TOTAL:
            nxt = base64.urlsafe_b64encode(json.dumps({"o": offset + max_results}).encode()).decode()
        if facets and (n > FACET_CAP or not exact):
            notices.append({"code": "FACETS_SAMPLED", "path": "/facets",
                            "message": f"Facet counts cover the top {FACET_CAP:,} matches by authority."})
        return ok({"results": results, "total": min(n, MAX_TOTAL), "total_is_estimate": n > MAX_TOTAL or not exact,
                   "facets": facets, "next_cursor": nxt}, request,
                  warnings=[NOT_IN_PRIVATE] if warn else [], notices=notices)

    def _entities(ids: list[str], fields: list[str], request: Request):
        _check_fields(fields, "/fields")
        col, rid = store.companies()
        want_ll = "lookalikes" in fields or "*" in fields
        found, missing = _resolve(col, ids, projection(fields))
        results = []
        lls = store.lookalikes(list({d["_id"] for d in found.values()}), rid) if want_ll else {}
        seen = set()
        for i in ids:
            d = found.get(i)
            if d is None or d["_id"] in seen:
                continue
            seen.add(d["_id"])
            rec = shape(d, fields)
            if want_ll:
                rec["lookalikes"] = [{"entity_id": x["entity_id"], "domain": x["domain"],
                                      "score": round(squash(x["score"]), 4), "via": "web_graph"}
                                     for x in lls.get(d["_id"], [])]
            results.append(rec)
        # Live fallback isn't built yet: misses come back as not_found, never silently dropped.
        return {"results": results, "not_found": missing, "pending": [], "task_id": None}

    @app.get("/v1/commons/entities/{entity_id:path}")
    def get_entity(entity_id: str, request: Request, fields: str = "*"):
        data = _entities([entity_id], fields.split(","), request)
        if not data["results"]:
            raise ApiError(404, "not_found", f"'{entity_id}' isn't in the release (live lookup not built yet).")
        return ok(data, request)

    @app.post("/v1/commons/entities")
    def get_entities(body: EntitiesRequest, request: Request):
        warns = []
        if body.updated_since:
            warns.append({"code": "IGNORED_FIELD", "path": "/updated_since",
                          "message": "One release so far; every record is returned."})
        return ok(_entities(body.ids, body.fields, request), request, warnings=warns)

    @app.post("/v1/commons/search")
    def search(body: SearchRequest, request: Request, max_results: int = Query(20, ge=10, le=200)):
        if body.query:
            raise ApiError(501, "not_implemented",
                           "Free-text search needs the embedding run (pending). Use seeds for lookalikes or /filter.", "/query")
        col, rid = store.companies()
        seeds, missing = _resolve(col, body.seeds, {"_id": 1, "domain": 1})
        seed_ids = list({d["_id"] for d in seeds.values()})
        warns = [{"code": "SEED_NOT_FOUND", "path": f"/seeds/{body.seeds.index(m)}",
                  "message": f"'{m}' isn't in the release; skipped."} for m in missing]
        lls = store.lookalikes(seed_ids, rid, k=200)
        # Multi-seed: average score across seeds (missing = 0), bonus for appearing under several seeds.
        agg: dict[str, dict] = {}
        for s in seed_ids:
            for x in lls.get(s, []):
                a = agg.setdefault(x["entity_id"], {"sum": 0.0, "hits": 0})
                a["sum"] += squash(x["score"]); a["hits"] += 1
        for s in seed_ids:
            agg.pop(s, None)
        scored = sorted(((e, a["sum"] / max(1, len(seed_ids)), a["hits"]) for e, a in agg.items()),
                        key=lambda t: -t[1])
        scored = [t for t in scored if t[1] >= body.min_score]
        ex, warn = exclude_to_mongo(body.exclude)
        q = merge(to_mongo(body.filters), ex, {"_id": {"$in": [e for e, _, _ in scored]}})
        docs = {d["_id"]: d for d in col.find(q, projection(body.fields), max_time_ms=5000)} if scored else {}
        results = []
        for e, sc, hits in scored:
            if e in docs:
                why = {"matched_text": None, "seed_overlap": round(hits / len(seed_ids), 2),
                       "filters_matched": list(body.filters.root.keys())} if body.explain else None
                results.append({"entity_id": e, "score": round(sc, 4), "why": why, "record": shape(docs[e], body.fields)})
            if len(results) >= max_results:
                break
        notices = []
        if seed_ids and not any(lls.get(s) for s in seed_ids):
            notices.append({"code": "NO_LOOKALIKES", "path": "/seeds",
                            "message": "These seeds have too few links for web-graph lookalikes; embedding lookalikes aren't live yet."})
        return ok({"results": results, "total_considered": len(scored), "routed": "precomputed",
                   "pending_seeds": [], "task_id": None}, request,
                  warnings=warns + ([NOT_IN_PRIVATE] if warn else []), notices=notices,
                  search_id="q_" + uuid.uuid4().hex[:16])

    @app.get("/v1/commons/tasks/{task_id}")
    def task(task_id: str, request: Request):
        raise ApiError(404, "not_found", "No live tasks yet (live fallback not built).")
