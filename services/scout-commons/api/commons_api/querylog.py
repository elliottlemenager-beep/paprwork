"""Query + candidate logging (mirrors memory's QueryLog / MemoryRetrievalLog), keyed by search_id.

One `query_log` row per search/filter call, one `retrieval_log` row with EVERY returned candidate
(entity_id, rank, score) so hard negatives can be mined later. Written on a background thread so a
logging failure or slow Mongo write never touches the response.

Privacy (contract): entities only. No caller identity is stored here. Outcomes (step 2) will join on search_id.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import store

log = logging.getLogger("commons.querylog")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="querylog")
_indexed = False


def new_search_id() -> str:
    return "q_" + uuid.uuid4().hex[:16]


def _write(query_row: dict, cand_row: dict) -> None:
    global _indexed
    try:
        db = store.mongo()[store.DB]
        if not _indexed:
            db["query_log"].create_index("search_id", unique=True)
            db["retrieval_log"].create_index("search_id")
            db["query_log"].create_index([("use_case", 1), ("created_at", -1)])
            _indexed = True
        db["query_log"].insert_one(query_row)
        db["retrieval_log"].insert_one(cand_row)
    except Exception as e:  # never raise into the request path
        log.warning("querylog write failed: %s", e)


def record(*, search_id: str, request_id: str, use_case: str, endpoint: str, params: dict,
           candidates: list[dict], t0: float, total: int | None = None) -> None:
    """candidates: [{'entity_id', 'score'(optional)}] in returned order. Fire-and-forget."""
    now = datetime.now(timezone.utc)
    try:
        release = store.active_release()["release_id"]
    except Exception:
        release = None
    query_row = {"search_id": search_id, "request_id": request_id, "use_case": use_case, "endpoint": endpoint,
                 "params": params, "latency_ms": round((time.time() - t0) * 1000), "result_count": len(candidates),
                 "total": total, "release": release, "created_at": now}
    cand_row = {"search_id": search_id, "use_case": use_case, "release": release, "created_at": now,
                "candidates": [{"entity_id": c.get("entity_id"), "rank": i + 1, "score": c.get("score")}
                               for i, c in enumerate(candidates)]}
    _pool.submit(_write, query_row, cand_row)
