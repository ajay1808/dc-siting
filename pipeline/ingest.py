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
import re
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



def _arcgis_paged(base: str, out_fields: str = "*", where: str = "1=1",
                  page: int = 2000, geometry: bool = True):
    """Page an ArcGIS FeatureServer layer, yielding GeoJSON features.

    ArcGIS caps a single response at maxRecordCount (2000 here), so anything
    national has to be walked with resultOffset.
    """
    offset = 0
    while True:
        q = {"where": where, "outFields": out_fields, "f": "geojson",
             "resultOffset": offset, "resultRecordCount": page,
             "returnGeometry": str(geometry).lower(), "outSR": "4326"}
        url = f"{base}/query?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=180) as r:
            fc = json.load(r)
        feats = fc.get("features", [])
        if not feats:
            return
        yield from feats
        print(f"    arcgis: +{len(feats)} (offset {offset})")
        if len(feats) < page:
            return
        offset += page
        time.sleep(0.3)


def _line_vertices(geom: dict, every: int = 1):
    """Flatten a (Multi)LineString into vertices.

    score.py measures distance to point features, so lines are represented by
    their vertices. Transmission vertex spacing is already fine enough that a
    nearest-vertex distance is a good proxy for nearest-line distance.
    """
    if not geom:
        return []
    t = geom.get("type")
    if t == "LineString":
        parts = [geom["coordinates"]]
    elif t == "MultiLineString":
        parts = geom["coordinates"]
    else:
        return []
    out = []
    for part in parts:
        out.extend(part[::every] if every > 1 else part)
    return out


# ---------------------------------------------------------------------------
# ENERGY / GRID
# ---------------------------------------------------------------------------

@fetcher("transmission_lines")
def fetch_transmission_lines() -> pd.DataFrame:
    """HIFLD Open electric power transmission lines (~52k features, no auth).

    EIA's Energy Atlas was the registry's primary, but its dcat feed does not
    expose the electric layers; HIFLD Open still serves this one publicly.
    """
    BASE = ("https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services"
            "/Electric_Power_Transmission_Lines/FeatureServer/0")
    rows = []
    for f in _arcgis_paged(BASE, out_fields="VOLTAGE,VOLT_CLASS,OWNER,STATUS,TYPE"):
        p = f.get("properties", {}) or {}
        v = p.get("VOLTAGE")
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = None
        # HIFLD encodes unknown voltage as -999999.
        if v is not None and v < 0:
            v = None
        for lng, lat in _line_vertices(f.get("geometry")):
            rows.append({"lat": lat, "lng": lng, "VOLTAGE": v,
                         "volt_class": p.get("VOLT_CLASS"),
                         "owner": p.get("OWNER"), "status": p.get("STATUS")})
    df = pd.DataFrame(rows)
    return df[(df.lat.between(24, 50)) & (df.lng.between(-125, -66))].reset_index(drop=True)


@fetcher("substations")
def fetch_substations() -> pd.DataFrame:
    """Substations from OpenStreetMap.

    HIFLD's national substation layer is no longer public (the one copy still
    reachable holds 128 features, not ~80k), so this uses the OSM fallback the
    registry always listed. US coverage of power=substation with voltage tags
    is now good.
    """
    rows = []
    for lo, hi in [(24, 33), (33, 38), (38, 43), (43, 50)]:
        query = (f"[out:json][timeout:600];"
                 f"nwr[\"power\"=\"substation\"]({lo},-125,{hi},-66);out center tags;")
        body = urllib.parse.urlencode({"data": query}).encode()
        for ep in ("https://overpass-api.de/api/interpreter",
                   "https://overpass.kumi.systems/api/interpreter"):
            try:
                req = urllib.request.Request(ep, data=body, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=600) as r:
                    els = json.load(r).get("elements", [])
                print(f"    osm substations lat {lo}-{hi}: {len(els)}")
                for el in els:
                    lat = el.get("lat") or (el.get("center") or {}).get("lat")
                    lng = el.get("lon") or (el.get("center") or {}).get("lon")
                    if lat is None or lng is None:
                        continue
                    t = el.get("tags", {})
                    volts = [float(x) for x in re.findall(r"\\d+", str(t.get("voltage", "")))]
                    kv = max(volts) / 1000.0 if volts else None
                    rows.append({"lat": lat, "lng": lng, "MAX_VOLT": kv,
                                 "name": t.get("name"), "operator": t.get("operator")})
                break
            except Exception as e:  # noqa: BLE001
                print(f"    overpass {ep} failed: {type(e).__name__}")
        time.sleep(3)
    return pd.DataFrame(rows)



EIA860M_URL = ("https://www.eia.gov/electricity/data/eia860m/xls/"
               "july_generator2026.xlsx")


def _eia860m_sheet(sheet: str) -> pd.DataFrame:
    """Download EIA-860M once and return one sheet, cached on disk.

    The EIA v2 JSON API carries generator capacity but no coordinates, so the
    860M spreadsheet is the only free source that gives capacity AND lat/lng
    together. Note EIA publishes a stub file for the newest month or two
    before the real release lands; check file size if bumping the URL.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "eia860m.xlsx"
    if not cache.exists() or cache.stat().st_size < 1_000_000:
        print(f"    downloading {EIA860M_URL}")
        req = urllib.request.Request(EIA860M_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=300) as r:
            cache.write_bytes(r.read())
    df = pd.read_excel(cache, sheet_name=sheet, skiprows=2, engine="openpyxl")
    df = df.rename(columns={"Latitude": "lat", "Longitude": "lng"})
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lng"] = pd.to_numeric(df["lng"], errors="coerce")
    df = df.dropna(subset=["lat", "lng"])
    return df[df.lat.between(24, 50) & df.lng.between(-125, -66)].reset_index(drop=True)


@fetcher("power_plants")
def fetch_power_plants() -> pd.DataFrame:
    df = _eia860m_sheet("Operating")
    cap = pd.to_numeric(df.get("Nameplate Capacity (MW)"), errors="coerce").fillna(0)
    out = pd.DataFrame({
        "lat": df["lat"], "lng": df["lng"], "capacity_mw": cap,
        "plant_name": df.get("Plant Name"), "plant_id": df.get("Plant ID"),
        "technology": df.get("Technology"), "state": df.get("Plant State"),
        "ba": df.get("Balancing Authority Code"),
    })
    # 860M is generator-level; collapse to plants so capacity is not double counted
    # by the radius sum and a 12-unit site is not 12 nearest neighbours.
    agg = out.groupby("plant_id", as_index=False).agg(
        lat=("lat", "first"), lng=("lng", "first"),
        capacity_mw=("capacity_mw", "sum"), plant_name=("plant_name", "first"),
        technology=("technology", "first"), state=("state", "first"), ba=("ba", "first"))
    return agg


@fetcher("interconnection_queue")
def fetch_interconnection_queue() -> pd.DataFrame:
    """Planned generator additions from EIA-860M.

    The registry's primary source (LBNL 'Queued Up') returns 403 to scripted
    clients. EIA-860M's Planned sheet is the closest free substitute: it is
    generation that has cleared enough process to have a location and an
    in-service date, so it indexes where new capacity is actually arriving.
    It is NOT the full interconnection queue and will understate contention.
    """
    df = _eia860m_sheet("Planned")
    cap = pd.to_numeric(df.get("Nameplate Capacity (MW)"), errors="coerce").fillna(0)
    out = pd.DataFrame({
        "lat": df["lat"], "lng": df["lng"], "capacity_mw": cap,
        "plant_name": df.get("Plant Name"), "plant_id": df.get("Plant ID"),
        "technology": df.get("Technology"), "state": df.get("Plant State"),
        "q_year": pd.to_numeric(df.get("Operating Year"), errors="coerce"),
    })
    return out.groupby("plant_id", as_index=False).agg(
        lat=("lat", "first"), lng=("lng", "first"),
        capacity_mw=("capacity_mw", "sum"), plant_name=("plant_name", "first"),
        technology=("technology", "first"), state=("state", "first"),
        q_year=("q_year", "min"))



@fetcher("retail_power_price")
def fetch_retail_power_price() -> pd.DataFrame:
    """Industrial retail electricity price by state, latest annual (cents/kWh).

    EIA-861 resolves to utility, but utility service territory polygons are
    part of the restricted HIFLD set, so V1 joins at state level. That is
    coarse -- intrastate spread is real -- but it is honest and it is free.
    """
    key = _env("EIA_API_KEY")
    if not key:
        raise RuntimeError("EIA_API_KEY not set")
    d = _get_json("https://api.eia.gov/v2/electricity/retail-sales/data/", {
        "api_key": key, "frequency": "annual", "data[]": "price",
        "facets[sectorid][]": "IND", "sort[0][column]": "period",
        "sort[0][direction]": "desc", "length": "5000"})
    rows = d["response"]["data"]
    df = pd.DataFrame(rows)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["price"])
    latest = df["period"].max()
    df = df[df["period"] == latest]
    df = df[df["stateid"].str.len() == 2]

    fips = _state_fips()
    df["join_key"] = df["stateid"].map(fips)
    df = df.dropna(subset=["join_key"])
    print(f"    EIA retail industrial price, period {latest}, {len(df)} states")
    return df[["join_key", "price", "stateid"]].reset_index(drop=True)


@fetcher("county_demographics")
def fetch_county_demographics() -> pd.DataFrame:
    """ACS 5-year county poverty rate and median household income.

    Poverty rate is carried for EJ disclosure, not as a positive score input.
    """
    key = _env("CENSUS_API_KEY")
    if not key:
        raise RuntimeError("CENSUS_API_KEY not set")
    url = "https://api.census.gov/data/2023/acs/acs5"
    d = _get_json(url, {"get": "NAME,B17001_002E,B17001_001E,B19013_001E,B01003_001E",
                        "for": "county:*", "in": "state:*", "key": key})
    hdr, *rows = d
    df = pd.DataFrame(rows, columns=hdr)
    for c in ["B17001_002E", "B17001_001E", "B19013_001E", "B01003_001E"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["join_key"] = df["state"].str.zfill(2) + df["county"].str.zfill(3)
    df["pct_poverty"] = (df["B17001_002E"] / df["B17001_001E"] * 100).round(2)
    out = df[["join_key", "pct_poverty", "NAME"]].copy()
    out["median_income"] = df["B19013_001E"].where(df["B19013_001E"] > 0)
    out["population"] = df["B01003_001E"]
    out = out.dropna(subset=["pct_poverty"])
    print(f"    ACS 2023: {len(out)} counties")
    return out.reset_index(drop=True)


def _state_fips() -> dict:
    return {
        "AL":"01","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10",
        "DC":"11","FL":"12","GA":"13","ID":"16","IL":"17","IN":"18","IA":"19",
        "KS":"20","KY":"21","LA":"22","ME":"23","MD":"24","MA":"25","MI":"26",
        "MN":"27","MS":"28","MO":"29","MT":"30","NE":"31","NV":"32","NH":"33",
        "NJ":"34","NM":"35","NY":"36","NC":"37","ND":"38","OH":"39","OK":"40",
        "OR":"41","PA":"42","RI":"44","SC":"45","SD":"46","TN":"47","TX":"48",
        "UT":"49","VT":"50","VA":"51","WA":"53","WV":"54","WI":"55","WY":"56",
    }


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
