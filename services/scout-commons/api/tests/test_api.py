"""Integration tests against the dev MongoDB + BigQuery. Needs MONGO_URI and gcloud ADC (or run on Cloud Run).

    MONGO_URI=... COMMONS_API_KEYS=test pytest -q api/tests
"""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("COMMONS_API_KEYS", "test")
from commons_api.main import app  # noqa: E402

c = TestClient(app)
H = {"X-API-Key": os.environ["COMMONS_API_KEYS"].split(",")[0]}


def post(path, body, **params):
    return c.post(path, json=body, headers=H, params=params)


def test_auth_required():
    r = c.post("/v1/commons/filter", json={})
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_fields_open_and_listed():
    d = c.get("/v1/commons/fields").json()["data"]
    names = {f["name"] for f in d["fields"]}
    assert {"tld", "authority", "inbound_sites"} <= names and d["release"]


def test_entity_by_domain_has_freshness():
    r = c.get("/v1/commons/entities/https://www.Gong.io/", headers=H, params={"fields": "name,authority"})
    rec = r.json()["data"]["results"][0]
    assert rec["name"] == "Gong" and rec["entity_id"].startswith("c_")
    for k in ("source", "as_of", "release", "stale", "provisional", "confidence", "coverage"):
        assert k in rec


def test_batch_entities_with_lookalikes():
    d = post("/v1/commons/entities", {"ids": ["gong.io", "clari.com", "not-a-real-domain-xyz.com"],
                                      "fields": ["name", "lookalikes"]}).json()["data"]
    assert len(d["results"]) == 2 and d["not_found"] == ["not-a-real-domain-xyz.com"]
    gong = next(r for r in d["results"] if r["domain"] == "gong.io")
    assert "outreach.io" in [x["domain"] for x in gong["lookalikes"][:20]]


def test_filter_unknown_field_400():
    r = post("/v1/commons/filter", {"filters": {"country": "US"}})
    assert r.status_code == 400 and r.json()["error"] == "unknown_field"


def test_filter_with_facets_and_cursor():
    d = post("/v1/commons/filter", {"filters": {"tld": "io", "inbound_sites": {"gte": 1000}},
                                    "sort": [{"field": "authority", "order": "desc"}],
                                    "facets": ["language"], "fields": ["name", "authority"]},
             max_results=5).json()["data"]
    assert len(d["results"]) == 5 and d["next_cursor"] and "language" in d["facets"]
    assert all(r["domain"].endswith(".io") for r in d["results"])
    ib = [r["authority"] for r in d["results"]]
    assert ib == sorted(ib, reverse=True)


def test_count_only():
    d = post("/v1/commons/filter", {"filters": {"tld": "io"}, "facets": ["has_text"]}, max_results=0).json()["data"]
    assert d["results"] == [] and d["total"] > 0


def test_search_seeds():
    r = post("/v1/commons/search", {"seeds": ["gong.io", "clari.com"], "explain": True}).json()
    doms = [x["record"]["domain"] for x in r["data"]["results"]]
    assert r["search_id"] and len(doms) == 20 and "gong.io" not in doms


def test_search_query_not_yet():
    r = post("/v1/commons/search", {"query": "sales call recording"})
    assert r.status_code == 501


def test_search_needs_query_or_seeds():
    assert post("/v1/commons/search", {}).status_code == 400
