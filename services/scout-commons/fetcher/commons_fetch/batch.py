"""Batch homepage fetch for the release pipeline (Cloud Run job, one task per shard).

    python -m commons_fetch.batch --input domains.csv --out gs://bucket/releases/R/homepages \\
        --store-dir /fingerprints --workers 64

Each task takes the domains whose hash lands in its shard (CLOUD_RUN_TASK_INDEX of
CLOUD_RUN_TASK_COUNT), fetches each homepage politely (robots.txt, 1 s per-host gap,
400 KB cap, public addresses only), extracts visible text and tools, flags parked
pages, and writes Parquet parts. A rerun skips domains already written, so a
crashed or preempted task resumes where it stopped.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from . import http, tech
from .parked import reason as parked_reason
from .store import Store

TEXT_CHARS = tech.TEXT_CHARS
FLUSH_ROWS = 500
SCHEMA = pa.schema([
    ("domain", pa.string()), ("fetched_at", pa.int64()), ("outcome", pa.string()), ("status", pa.int32()),
    ("error", pa.string()), ("final_url", pa.string()), ("redirected_offsite", pa.bool_()),
    ("ctype", pa.string()), ("title", pa.string()), ("text", pa.string()), ("text_chars", pa.int32()),
    ("tech", pa.list_(pa.string())), ("parked", pa.string()), ("ms", pa.int32()),
])


def shard(domain: str, count: int) -> int:
    return int.from_bytes(hashlib.sha1(domain.encode()).digest()[:8], "big") % count


def _title(html: str) -> str | None:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else None


def fetch_domain(store: Store | None, domain: str, timeout: float = 15.0) -> dict:
    """One homepage. Tries https://d/, then https://www.d/ if d doesn't resolve, then http://d/."""
    t0 = time.monotonic()
    tries = [f"https://{domain}/"]
    page: dict = {}
    with http.client(timeout=timeout) as c:
        while tries:
            url = tries.pop(0)
            page = http.get_page(c, url)
            err = page["error"]
            if err == "dns" and not url.startswith("https://www."):
                tries = [f"https://www.{domain}/"] + [t for t in tries if "www." not in t]
            elif err in ("connect", "timeout") and url.startswith("https://"):
                tries = [url.replace("https://", "http://", 1)]
            else:
                break
    http.forget(domain, "www." + domain, urlsplit(page.get("url", "")).hostname or "")
    status, err, body = page["status"], page["error"], page["body"]
    row = {
        "domain": domain, "fetched_at": int(time.time()), "status": status, "error": err,
        "final_url": page["url"], "ctype": page["ctype"][:100] or None,
        "redirected_offsite": bool(page["url"]) and not http.same_site(page["url"], domain),
        "title": None, "text": None, "text_chars": 0, "tech": [], "parked": None,
    }
    if status == -1:
        row["outcome"] = "robots_disallowed"
    elif status == 0:
        row["outcome"] = {"dns": "no_dns", "blocked_ip": "blocked_ip"}.get(err, "unreachable")
    elif status >= 400:
        row["outcome"] = "http_error"
    elif not body:
        row["outcome"] = "not_html"
    else:
        text = tech.visible_text(body)[:TEXT_CHARS]
        row.update(title=_title(body), text=text, text_chars=len(text))
        if store is not None:
            row["tech"] = sorted(tech.detect(store, body, page["headers"]))
        row["parked"] = parked_reason(page["url"], text, domain)
        row["outcome"] = "parked" if row["parked"] else "ok"
    row["ms"] = int((time.monotonic() - t0) * 1000)
    return row


class Writer:
    def __init__(self, out: str, task: int) -> None:
        self.fs, self.root = pafs.FileSystem.from_uri(out) if "://" in out else (pafs.LocalFileSystem(), os.path.abspath(out))
        self.fs.create_dir(self.root, recursive=True)
        self.task, self.buf, self.lock = task, [], threading.Lock()
        self.prefix = f"part-{task:05d}-"
        self.seq = len(self._parts())

    def _parts(self) -> list[str]:
        sel = pafs.FileSelector(self.root, allow_not_found=True)
        return sorted(i.path for i in self.fs.get_file_info(sel) if os.path.basename(i.path).startswith(self.prefix)
                      and i.path.endswith(".parquet"))

    def done(self) -> set[str]:
        out: set[str] = set()
        for p in self._parts():
            out.update(pq.read_table(p, columns=["domain"], filesystem=self.fs).column("domain").to_pylist())
        return out

    def add(self, row: dict) -> None:
        with self.lock:
            self.buf.append(row)
            if len(self.buf) >= FLUSH_ROWS:
                self._flush()

    def _flush(self) -> None:
        if not self.buf:
            return
        path = f"{self.root}/{self.prefix}{self.seq:05d}.parquet"
        tmp = path + ".tmp"
        pq.write_table(pa.Table.from_pylist(self.buf, schema=SCHEMA), tmp, filesystem=self.fs, compression="zstd")
        self.fs.move(tmp, path)  # a part appears whole or not at all
        self.seq, self.buf = self.seq + 1, []

    def close(self) -> None:
        with self.lock:
            self._flush()


def read_domains(path: str) -> list[str]:
    if path.endswith(".parquet"):
        return pq.read_table(path, columns=["domain"]).column("domain").to_pylist()
    with open(path) as f:
        return [r["domain"].strip().lower() for r in csv.DictReader(f) if r.get("domain")]


def run(domains: list[str], out: str, task: int, count: int, workers: int, store: Store | None,
        timeout: float = 15.0, limit: int | None = None) -> dict:
    w = Writer(out, task)
    done = w.done()
    mine = [d for d in dict.fromkeys(domains) if shard(d, count) == task and d not in done]
    if limit:
        mine = mine[:limit]
    stats: Counter = Counter()
    t0 = time.monotonic()
    it = iter(mine)
    with ThreadPoolExecutor(workers) as ex:
        running = {ex.submit(fetch_domain, store, d, timeout) for d in [next(it) for _ in range(min(workers * 2, len(mine)))]}
        while running:
            finished, running = wait(running, return_when=FIRST_COMPLETED)
            for f in finished:
                try:
                    row = f.result()
                except Exception as e:  # noqa: BLE001 - one bad domain must not stop the shard
                    stats["crashed"] += 1
                    print("crash", repr(e)[:200], file=sys.stderr)
                    continue
                w.add(row)
                stats[row["outcome"]] += 1
                nxt = next(it, None)
                if nxt:
                    running.add(ex.submit(fetch_domain, store, nxt, timeout))
            n = sum(stats.values())
            if n and n % 1000 < len(finished):
                print(json.dumps({"task": task, "done": n, "of": len(mine), **stats}), file=sys.stderr, flush=True)
    w.close()
    el = time.monotonic() - t0
    summary = {"task": task, "count": count, "skipped_already_done": len(done), "fetched": sum(stats.values()),
               "seconds": round(el, 1), "per_second": round(sum(stats.values()) / el, 2) if el else None, **stats}
    with w.fs.open_output_stream(f"{w.root}/_stats-{task:05d}-{int(time.time())}.json") as f:
        f.write(json.dumps(summary).encode())
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", required=True, help="CSV with a domain column, or Parquet")
    ap.add_argument("--out", required=True, help="local dir or gs://bucket/prefix")
    ap.add_argument("--store-dir", help="dir holding tech/fingerprints.json; omit to skip tool detection")
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--task-index", type=int, default=int(os.environ.get("CLOUD_RUN_TASK_INDEX", 0)))
    ap.add_argument("--task-count", type=int, default=int(os.environ.get("CLOUD_RUN_TASK_COUNT", 1)))
    a = ap.parse_args(argv)
    store = Store(a.store_dir) if a.store_dir else None
    print(json.dumps(run(read_domains(a.input), a.out, a.task_index, a.task_count, a.workers, store, a.timeout, a.limit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
