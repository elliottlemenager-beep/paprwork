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


def test_search_scores_ranked_not_tied():
    r = post("/v1/commons/search", {"seeds": ["gong.io"]}).json()["data"]["results"]
    s = [x["score"] for x in r]
    assert s == sorted(s, reverse=True) and len(set(s)) > 1 and all(0 < v <= 1 for v in s)  # per-seed rescale: top lookalike is exactly 1.0 (clean.py)


def test_search_unknown_seed_warns():
    r = post("/v1/commons/search", {"seeds": ["not-a-real-domain-xyz.com"]})
    assert r.status_code == 200
    assert [w["code"] for w in r.json()["warnings"]] == ["SEED_NOT_FOUND"]


def test_filter_returns_search_id():
    r = post("/v1/commons/filter", {"fields": ["name"]}, max_results=3).json()
    assert r["search_id"].startswith("q_")


def test_search_and_filter_log_query_and_candidates(monkeypatch):
    from commons_api import querylog
    rows = []
    monkeypatch.setattr(querylog._pool, "submit", lambda fn, q, c: rows.append((q, c)))
    r = post("/v1/commons/search", {"seeds": ["gong.io"]}, max_results=10).json()
    (q, cand), = rows
    assert q["search_id"] == cand["search_id"] == r["search_id"] and q["use_case"] == "commons_lookalike"
    assert [c["rank"] for c in cand["candidates"]] == list(range(1, len(r["data"]["results"]) + 1))
    assert cand["candidates"][0]["score"] == r["data"]["results"][0]["score"]
    rows.clear()
    f = post("/v1/commons/filter", {"fields": ["name"]}, max_results=3).json()
    assert rows[0][0]["use_case"] == "commons_filter" and rows[0][0]["search_id"] == f["search_id"]


def test_log_failure_never_breaks_response(monkeypatch):
    from commons_api import querylog
    monkeypatch.setattr(querylog.store, "mongo", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    querylog._write({"search_id": "x"}, {"search_id": "x"})  # swallowed, logged


class _Col(list):
    def find_one(self, q, proj=None):
        return next((d for d in self if all(d.get(k) == v for k, v in q.items())), None)
    def insert_one(self, d): self.append(d)
    def create_index(self, *a, **k): pass


def _fake_db(monkeypatch):
    from commons_api import querylog
    db = {"query_log": _Col([{"search_id": "q_1", "use_case": "commons_lookalike"}]),
          "retrieval_log": _Col([{"search_id": "q_1", "candidates": [{"entity_id": "c_a", "rank": 1}, {"entity_id": "c_b", "rank": 2}]}]),
          "outcome_log": _Col()}
    monkeypatch.setattr(querylog.store, "mongo", lambda: {querylog.store.DB: db})
    return db


def test_feedback_records_outcome_with_rank(monkeypatch):
    db = _fake_db(monkeypatch)
    r = post("/v1/feedback", {"search_id": "q_1", "outcome": "contacted", "entity_id": "c_b"}).json()
    assert r["data"] == {"recorded": True, "rank": 2}
    row, = db["outcome_log"]
    assert row["outcome"] == "contacted" and row["use_case"] == "commons_lookalike" and "user" not in str(row).lower()


def test_feedback_unknown_search_id_404(monkeypatch):
    _fake_db(monkeypatch)
    r = post("/v1/feedback", {"search_id": "q_nope", "outcome": "opened"})
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_feedback_rejects_bad_outcome():
    r = post("/v1/feedback", {"search_id": "q_1", "outcome": "liked"})
    assert r.status_code == 400


# Quality gate (Amir, #580 follow-up): big seeds must not be led by hubs/infra, and must surface real peers.
_NOISE = {"facebook.com", "google.com", "googletagmanager.com", "gstatic.com", "instagram.com", "youtube.com",
          "twitter.com", "linkedin.com", "pinterest.com", "apple.com", "cloudflare.com", "gravatar.com", "wp.com",
          "gmpg.org", "recaptcha.net", "hcaptcha.com", "paypalobjects.com", "cloudflarestream.com", "imgix.net"}
_PEERS = {"stripe.com": {"paypal.com", "adyen.com", "checkout.com", "square.com", "braintreepayments.com", "klarna.com"},
          "notion.so": {"trello.com", "asana.com", "clickup.com", "slack.com", "airtable.com", "miro.com"},
          "shopify.com": {"bigcommerce.com", "wix.com", "squarespace.com", "klaviyo.com", "woocommerce.com"}}


@pytest.mark.parametrize("seed", list(_PEERS))
def test_lookalike_quality_no_hubs_and_known_peers(seed):
    top = [x["record"]["domain"] for x in post("/v1/commons/search", {"seeds": [seed]}, max_results=20).json()["data"]["results"]]
    assert not _NOISE & set(top), f"hub/infra in top 20 for {seed}: {_NOISE & set(top)}"
    assert not [d for d in top if d.endswith((".gov", ".edu", ".mil"))]
    assert len(_PEERS[seed] & set(top)) >= 1, f"no known peer in top 20 for {seed}: {top}"
