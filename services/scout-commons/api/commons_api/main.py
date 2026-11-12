"""Scout Commons API (dev). Implements the v0.2 contract on MongoDB + precomputed lookalikes.

Built:   /fields, /filter (facets, sort, cursor), /entities (single + batch), /search with seeds (lookalikes).
Not yet: /search with free-text query (needs embeddings or Atlas Search), live fallback, /tasks, webhooks.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import ExecutionTimeout

from . import store
from .query import ApiError
from .records import known_field
from .registry import FIELDS  # noqa: F401

log = logging.getLogger("commons")
app = FastAPI(title="Scout Commons API", version="0.2.0-dev")
OPEN = {"/healthz", "/v1/commons/fields", "/docs", "/openapi.json"}


def ok(data, request: Request, code=200, warnings=None, notices=None, search_id=None):
    return JSONResponse({"code": code, "status": "success", "request_id": request.state.rid, "data": data,
                         "error": None, "details": None, "search_id": search_id,
                         "warnings": warnings or [], "notices": notices or []}, code)


def err(request: Request, status: int, code: str, message: str, path: str | None = None):
    rid = getattr(request.state, "rid", "r_" + uuid.uuid4().hex[:16])
    details = {"message": message} | ({"path": path} if path else {})
    return JSONResponse({"code": status, "status": "error", "request_id": rid, "data": None, "error": code,
                         "details": details, "search_id": None, "warnings": [], "notices": []}, status)


@app.middleware("http")
async def auth_and_id(request: Request, call_next):
    request.state.rid = "r_" + uuid.uuid4().hex[:16]
    keys = [k for k in os.environ.get("COMMONS_API_KEYS", "").split(",") if k]
    if request.url.path not in OPEN:
        sent = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not keys or not any(hmac.compare_digest(sent, k) for k in keys):   # no keys configured = closed
            return err(request, 401, "unauthorized", "Missing or invalid API key (X-API-Key header).")
    t = time.time()
    resp = await call_next(request)
    # Entity-only logging: path + timing, never user IDs next to domains (§ quotas/privacy).
    log.info(json.dumps({"rid": request.state.rid, "path": request.url.path, "status": resp.status_code,
                         "ms": round((time.time() - t) * 1000)}))
    resp.headers["x-request-id"] = request.state.rid
    return resp


@app.exception_handler(ApiError)
async def _api_err(request: Request, e: ApiError):
    return err(request, e.status, e.code, e.message, e.path)


@app.exception_handler(RequestValidationError)
async def _val_err(request: Request, e: RequestValidationError):
    first = e.errors()[0]
    path = "/" + "/".join(str(p) for p in first["loc"][1:]) if len(first["loc"]) > 1 else None
    code = "unknown_field" if first["type"] == "extra_forbidden" else "invalid_request"
    return err(request, 400, code, first["msg"], path)


@app.exception_handler(ExecutionTimeout)
async def _timeout(request: Request, e):
    return err(request, 503, "service_unavailable", "Query took too long; narrow the filters.")


@app.get("/healthz")
def healthz():
    return {"ok": True, "release": store.active_release()["release_id"]}


@app.get("/v1/commons/fields")
def fields(request: Request, entity: str = "company"):
    if entity != "company":
        raise ApiError(400, "invalid_request", "entity must be 'company' in Phase 1", "/entity")
    out = [{"name": f.name, "type": f.type, "operators": f.operators, "values": list(f.values) if f.values else None,
            "facetable": f.facetable, "sortable": f.sortable or f.name in ("authority",), "in_vector_store": False} for f in FIELDS.values()]
    return ok({"entity": "company", "release": store.active_release()["release_id"], "fields": out}, request)


def _check_fields(fs: list[str], base: str):
    for i, f in enumerate(fs):
        if not known_field(f):
            raise ApiError(400, "unknown_field", f"Unknown record field '{f}'", f"{base}/{i}")


def _cursor(c: str | None) -> int:
    if not c:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(c.encode()))["o"])
    except Exception:
        raise ApiError(400, "invalid_request", "Bad cursor", "/cursor")


from .routes import register  # noqa: E402  (routes need the helpers above)

register(app)
