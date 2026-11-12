"""MongoDB (records) + BigQuery (precomputed lookalikes, until they move to the serving store)."""

from __future__ import annotations

import os
import time
from functools import lru_cache

import pymongo
from google.cloud import bigquery

DB = os.environ.get("MONGO_DB", "scout_commons")
BQ_PROJECT = os.environ.get("BQ_PROJECT", "papr-scout-commons")
BQ_DATASET = os.environ.get("BQ_DATASET", "commons")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "us-west1")

_release_cache: dict = {"at": 0.0, "value": None}


@lru_cache(maxsize=1)
def mongo() -> pymongo.MongoClient:
    return pymongo.MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8000,
                               maxPoolSize=20, appname="scout-commons-api")


@lru_cache(maxsize=1)
def bq() -> bigquery.Client:
    return bigquery.Client(project=BQ_PROJECT, location=BQ_LOCATION)


def active_release() -> dict:
    """{'release_id', 'collection'} from the releases pointer, cached 60 s so a flip lands within a minute."""
    now = time.time()
    if _release_cache["value"] and now - _release_cache["at"] < 60:
        return _release_cache["value"]
    db = mongo()[DB]
    rid = db["releases"].find_one({"_id": "active"})["release_id"]
    meta = db["releases"].find_one({"_id": rid})
    val = {"release_id": rid, "collection": meta["collections"]["companies"]}
    _release_cache.update(at=now, value=val)
    return val


def companies():
    r = active_release()
    return mongo()[DB][r["collection"]], r["release_id"]


def lookalikes(seed_ids: list[str], release_id: str, k: int = 100) -> dict[str, list[dict]]:
    """Top-k precomputed lookalikes per seed. Table is clustered on seed_entity_id, so this bills ~10 MB."""
    if not seed_ids:
        return {}
    table = f"`{BQ_PROJECT}.{BQ_DATASET}.release_{release_id.replace('-', '_')}_lookalikes_serving`"
    sql = (f"SELECT seed_entity_id, ARRAY(SELECT AS STRUCT entity_id, domain, score, confidence, rank "
           f"FROM UNNEST(lookalikes) ORDER BY rank LIMIT @k) l FROM {table} "
           f"WHERE seed_entity_id IN UNNEST(@ids)")
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("ids", "STRING", seed_ids),
        bigquery.ScalarQueryParameter("k", "INT64", k)],
        maximum_bytes_billed=2 * 1024**3)
    rows = bq().query_and_wait(sql, job_config=cfg, wait_timeout=20)
    return {r["seed_entity_id"]: [dict(x) for x in r["l"]] for r in rows}
