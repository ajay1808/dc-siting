#!/usr/bin/env python3
"""Validate every endpoint in the source registry before any fetch runs.

Reports what actually resolves, so the pipeline never fails halfway through a
30GB download because a federal agency reorganized their portal.
"""
import concurrent.futures as cf
import sys
import urllib.request
import urllib.error
from pathlib import Path

import yaml

REG = Path(__file__).resolve().parents[1] / "sources" / "registry.yml"
UA = "dc-siting-registry-check/0.1 (+internal siting tool)"
TIMEOUT = 25
SKIP_FORMATS = {"overpass"}          # no fixed URL to probe


def probe(url: str):
    """HEAD, falling back to a ranged GET for servers that reject HEAD."""
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", UA)
        if method == "GET":
            req.add_header("Range", "bytes=0-2047")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue
            return e.code, e.reason
        except Exception as e:
            if method == "HEAD":
                continue
            return None, type(e).__name__
    return None, "unreachable"


def main():
    reg = yaml.safe_load(REG.read_text())
    jobs = []
    for layer in reg["layers"]:
        for ep in layer.get("endpoints", []):
            if ep.get("format") in SKIP_FORMATS:
                continue
            jobs.append((layer["id"], layer["tier"], ep["name"], ep["url"]))

    results = []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(probe, u): (lid, tier, n, u) for lid, tier, n, u in jobs}
        for fut in cf.as_completed(futs):
            lid, tier, n, u = futs[fut]
            status, err = fut.result()
            results.append((lid, n, u, status, err))

    results.sort(key=lambda r: (r[3] is None or r[3] >= 400, r[0]))
    ok = warn = bad = 0
    print(f"{'LAYER':<24}{'ENDPOINT':<22}{'STATUS':<10}NOTE")
    print("-" * 100)
    for lid, n, u, status, err in results:
        if status and status < 300:
            mark, ok = "OK", ok + 1
        elif status and status < 400:
            mark, warn = f"{status}", warn + 1
        else:
            mark, bad = f"{status or 'ERR'}", bad + 1
        print(f"{lid:<24}{n:<22}{mark:<10}{err[:44]}")
    print("-" * 100)
    print(f"reachable: {ok}   redirect: {warn}   FAILED: {bad}   total: {len(results)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
