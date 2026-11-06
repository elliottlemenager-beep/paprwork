import json

import httpx
import pyarrow.parquet as pq
import pytest

from commons_fetch import batch, http
from commons_fetch.parked import reason

PAGE = "<html><head><title>Acme Robotics</title></head><body>" + "Acme builds warehouse robots for mid-size retailers. " * 10 + "</body></html>"
PARKED = "<html><body>This domain is for sale! Make an offer on this domain today. " + "x " * 150 + "</body></html>"
SEEN = []


def handler(req: httpx.Request) -> httpx.Response:
    SEEN.append((str(req.url), req.headers.get("host"), req.extensions.get("sni_hostname")))
    host = req.headers["host"]
    if req.url.path == "/robots.txt":
        if host == "private.example":
            return httpx.Response(404)
        if host == "norobots.example":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(404)
    if host == "parked.example":
        return httpx.Response(200, html=PARKED)
    if host == "big.example":
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>" + b"a" * 2_000_000)
    if host == "json.example":
        return httpx.Response(200, content=b"[" + b"1," * 300_000 + b"1]")
    if host == "redir.example":
        return httpx.Response(302, headers={"location": "https://evil.example/"})
    if host == "gone.example":
        return httpx.Response(404, html="nope")
    return httpx.Response(200, html=PAGE)


ADDRS = {"private.example": ["10.0.0.5"], "evil.example": ["169.254.169.254"], "mixed.example": ["93.184.216.34", "127.0.0.1"]}


@pytest.fixture(autouse=True)
def net(monkeypatch):
    SEEN.clear()
    http.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(http, "MIN_GAP_S", 0)

    def resolve(host):
        if host == "nodns.example" or host == "www.nodns.example":
            raise OSError("no such host")
        return ADDRS.get(host, ["93.184.216.34"])

    monkeypatch.setattr(http, "_resolve", resolve)
    yield
    http.set_transport(None)


def test_connects_to_checked_ip_with_real_host_and_sni():
    row = batch.fetch_domain(None, "acme.example")
    assert row["outcome"] == "ok" and row["title"] == "Acme Robotics"
    url, host, sni = SEEN[-1]
    assert url.startswith("https://93.184.216.34/") and host == "acme.example" and sni == "acme.example"
    assert row["final_url"] == "https://acme.example/"


@pytest.mark.parametrize("dom", ["private.example", "mixed.example"])
def test_private_addresses_blocked(dom):
    row = batch.fetch_domain(None, dom)
    assert row["outcome"] == "blocked_ip"
    assert not any(h == dom for _, h, _ in SEEN)  # nothing ever sent to it


def test_redirect_to_metadata_ip_blocked():
    row = batch.fetch_domain(None, "redir.example")
    assert row["outcome"] == "blocked_ip"
    assert not any(h == "evil.example" for _, h, _ in SEEN)


def test_robots_respected():
    assert batch.fetch_domain(None, "norobots.example")["outcome"] == "robots_disallowed"


def test_no_dns_tries_www_then_gives_up():
    row = batch.fetch_domain(None, "nodns.example")
    assert row["outcome"] == "no_dns"


def test_page_capped():
    row = batch.fetch_domain(None, "big.example")
    assert row["text_chars"] <= batch.TEXT_CHARS


def test_get_json_refuses_over_cap():
    with http.client() as c:
        assert http.get_json(c, "https://json.example/x") is None


def test_parked_and_http_error():
    assert batch.fetch_domain(None, "parked.example")["outcome"] == "parked"
    assert batch.fetch_domain(None, "gone.example")["outcome"] == "http_error"


def test_parked_rules():
    assert reason("https://sedo.com/search?d=x", "whatever " * 50, "x.com") == "parking_host"
    assert reason("https://x.com/", "short", "x.com") == "thin"
    assert reason("https://x.com/", "We make robots. " * 30, "x.com") is None


def test_batch_shards_resumes_and_writes_parquet(tmp_path):
    doms = [f"d{i}.example" for i in range(40)] + ["parked.example"]
    total = 0
    for t in range(3):
        s = batch.run(doms, str(tmp_path), t, 3, workers=4, store=None)
        total += s["fetched"]
    assert total == len(doms)
    rows = pq.read_table(tmp_path).to_pylist()
    assert sorted(r["domain"] for r in rows) == sorted(doms)
    again = batch.run(doms, str(tmp_path), 0, 3, workers=4, store=None)
    assert again["fetched"] == 0 and again["skipped_already_done"] > 0
    stats = [json.loads(p.read_text()) for p in tmp_path.glob("_stats-*.json")]
    assert len(stats) == 4
