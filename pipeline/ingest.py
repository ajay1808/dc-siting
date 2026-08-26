#!/usr/bin/env python3
"""Fetch and normalize source layers into data/interim/<layer_id>.parquet.

Every fetcher returns a DataFrame with at minimum `lat`, `lng` (points) or a
`wkt` column (lines/polygons), plus whatever attributes the scorer needs.
Fetchers are registered by layer id so the registry stays the single source
of truth about what exists; this module only says *how* to get it.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
UA = "dc-siting/0.1 (internal siting research tool)"

FETCHERS: dict[str, callable] = {}


def fetcher(layer_id: str):
    def deco(fn):
        FETCHERS[layer_id] = fn
        return fn
    return deco


def _env(name: str) -> str | None:
    envfile = ROOT / ".env"
    if envfile.exists() and name not in os.environ:
        for line in envfile.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return os.environ.get(name) or None


def _get_json(url: str, params: dict | None = None, tries: int = 3) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries}: {url} :: {last}")


# ---------------------------------------------------------------------------
# NETWORK
# ---------------------------------------------------------------------------

@fetcher("ixp")
def fetch_ixp() -> pd.DataFrame:
    """PeeringDB facilities — coordinates, exchange count, network count.

    Works anonymously; an API key only raises the rate limit. Facilities
    (not the /api/ix records) are what carry lat/lng, and their ix_count /
    net_count is a good proxy for how valuable the interconnect point is.
    """
    key = _env("PEERINGDB_API_KEY")
    rows, skip, page = [], 0, 250
    while True:
        url = "https://www.peeringdb.com/api/fac"
        q = {"country": "US", "limit": page, "skip": skip}
        full = f"{url}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(
            full, headers={"User-Agent": UA, "Accept": "application/json"})
        if key:
            req.add_header("Authorization", f"Api-Key {key}")
        with urllib.request.urlopen(req, timeout=90) as r:
            batch = json.load(r).get("data", [])
        rows.extend(batch)
        print(f"    peeringdb fac: +{len(batch)} (total {len(rows)})")
        if len(batch) < page:
            break
        skip += page
        time.sleep(1.0)

    df = pd.DataFrame(rows)
    df = df[df["latitude"].notna() & df["longitude"].notna()]
    out = pd.DataFrame({
        "name": df["name"],
        "lat": pd.to_numeric(df["latitude"], errors="coerce"),
        "lng": pd.to_numeric(df["longitude"], errors="coerce"),
        "state": df.get("state"),
        "city": df.get("city"),
        "net_count": pd.to_numeric(df.get("net_count"), errors="coerce").fillna(0),
        "ix_count": pd.to_numeric(df.get("ix_count"), errors="coerce").fillna(0),
        "carrier_count": pd.to_numeric(df.get("carrier_count"), errors="coerce").fillna(0),
        "substations": df.get("diverse_serving_substations"),
        "voltage_services": df.get("available_voltage_services").astype(str),
        "org": df.get("org_name"),
    })
    return out.dropna(subset=["lat", "lng"]).reset_index(drop=True)


@fetcher("existing_datacenters")
def fetch_existing_datacenters() -> pd.DataFrame:
    """Data centers from OpenStreetMap via Overpass."""
    query = """
    [out:json][timeout:180];
    area["ISO3166-1"="US"][admin_level=2]->.us;
    (
      nwr["telecom"="data_center"](area.us);
      nwr["building"="data_center"](area.us);
      nwr["man_made"="data_center"](area.us);
    );
    out center tags;
    """
    endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    data = None
    for ep in endpoints:
        try:
            body = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(ep, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.load(r)
            print(f"    overpass ok via {ep}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"    overpass failed {ep}: {type(e).__name__}")
    if data is None:
        raise RuntimeError("all overpass endpoints failed")

    rows = []
    for el in data.get("elements", []):
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        t = el.get("tags", {})
        rows.append({"name": t.get("name") or t.get("operator") or "unnamed",
                     "operator": t.get("operator"), "lat": lat, "lng": lng})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------

def run(layer_ids: list[str]) -> int:
    reg = yaml.safe_load((ROOT / "sources" / "registry.yml").read_text())
    known = {l["id"]: l for l in reg["layers"]}
    INTERIM.mkdir(parents=True, exist_ok=True)

    failed = []
    for lid in layer_ids:
        if lid not in known:
            print(f"[skip] {lid}: not in registry")
            continue
        if lid not in FETCHERS:
            print(f"[todo] {lid}: no fetcher implemented yet")
            continue
        print(f"[fetch] {lid} ({known[lid]['name']})")
        try:
            df = FETCHERS[lid]()
            dest = INTERIM / f"{lid}.parquet"
            df.to_parquet(dest, index=False, compression="zstd")
            print(f"   -> {len(df):,} rows  {dest.name}  "
                  f"({dest.stat().st_size/1e6:.2f} MB)\n")
        except Exception as e:  # noqa: BLE001
            print(f"   !! FAILED: {type(e).__name__}: {e}\n")
            failed.append(lid)
    if failed:
        print(f"failed layers: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    args = sys.argv[1:] or list(FETCHERS)
    sys.exit(run(args))
