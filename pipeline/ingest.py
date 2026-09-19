#!/usr/bin/env python3
"""Fetch and normalize source layers into data/interim/<layer_id>.parquet.

Every fetcher returns a DataFrame with at minimum `lat`, `lng` (points) or a
`wkt` column (lines/polygons), plus whatever attributes the scorer needs.
Fetchers are registered by layer id so the registry stays the single source
of truth about what exists; this module only says *how* to get it.
"""
from __future__ import annotations

import json
import math
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
                  page: int = 2000, geometry: bool = True,
                  bbox: tuple | None = None):
    """Page an ArcGIS FeatureServer layer, yielding GeoJSON features.

    ArcGIS caps a single response at maxRecordCount (2000 here), so anything
    national has to be walked with resultOffset.
    """
    offset = 0
    while True:
        q = {"where": where, "outFields": out_fields, "f": "geojson",
             "resultOffset": offset, "resultRecordCount": page,
             "returnGeometry": str(geometry).lower(), "outSR": "4326"}
        if bbox:
            # Server-side clip. Aqueduct is global; pulling all 17k basins to
            # throw most away is rude to the host and slow for us.
            q.update({"geometry": ",".join(str(v) for v in bbox),
                      "geometryType": "esriGeometryEnvelope",
                      "spatialRel": "esriSpatialRelIntersects", "inSR": "4326"})
        url = f"{base}/query?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as r:
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


def _line_vertices(geom: dict, every: int = 1, densify_km: float | None = None):
    """Flatten a (Multi)LineString into points.

    score.py measures distance to point features, so lines are represented by
    sampled points. Raw vertices are fine where the source geometry is already
    detailed (HIFLD transmission averages ~36 vertices per line), but EIA's
    pipeline geometry averages ~3, so a nearest-vertex distance there would be
    wildly wrong. Pass densify_km to interpolate along each segment.
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
        pts = part[::every] if every > 1 else part
        if not densify_km:
            out.extend(pts)
            continue
        for i in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[i][:2], pts[i + 1][:2]
            # Local equirectangular approximation is plenty for spacing a
            # sample; we are choosing point density, not measuring distance.
            midlat = math.radians((y1 + y2) / 2.0)
            dx = (x2 - x1) * 111.320 * math.cos(midlat)
            dy = (y2 - y1) * 110.574
            seg = math.hypot(dx, dy)
            n = max(1, int(seg // densify_km))
            for k in range(n):
                f = k / n
                out.append([x1 + (x2 - x1) * f, y1 + (y2 - y1) * f])
        out.append(pts[-1][:2])
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
    """Substations from OpenStreetMap, fetched in latitude bands.

    HIFLD's national substation layer is no longer public (the one copy still
    reachable holds 128 features, not ~80k), so this uses the OSM fallback the
    registry always listed.

    Each band is cached to its own parquet. Overpass rate-limits national
    queries hard and will 429 mid-run; without per-band caching a single
    failed band means refetching everything and getting throttled again.
    """
    # Tiles, not bands: (lat_lo, lat_hi, lng_lo, lng_hi). Overpass refused the
    # full-width northern band for over two hours of backoff, so that one is
    # split by longitude. Narrow queries succeed where wide ones are throttled.
    TILES = [(24, 33, -125, -66), (33, 38, -125, -66), (38, 43, -125, -66),
             (43, 50, -125, -105), (43, 50, -105, -90), (43, 50, -90, -66)]
    RAW.mkdir(parents=True, exist_ok=True)
    frames, missing = [], []

    for lo, hi, wlng, elng in TILES:
        full = (wlng, elng) == (-125, -66)
        cache = (RAW / f"osm_substations_{lo}_{hi}.parquet" if full
                 else RAW / f"osm_substations_{lo}_{hi}_{wlng}_{elng}.parquet")
        if cache.exists():
            frames.append(pd.read_parquet(cache))
            print(f"    tile {lo}-{hi} {wlng}..{elng}: cached ({len(frames[-1]):,})")
            continue
        query = (f'[out:json][timeout:600];'
                 f'nwr["power"="substation"]({lo},{wlng},{hi},{elng});'
                 f'out center tags;')
        body = urllib.parse.urlencode({"data": query}).encode()
        got = None
        for attempt in range(3):
            for ep in ("https://overpass-api.de/api/interpreter",
                       "https://overpass.kumi.systems/api/interpreter"):
                try:
                    req = urllib.request.Request(ep, data=body, headers={"User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=900) as r:
                        got = json.load(r).get("elements", [])
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"    tile {lo}-{hi} {wlng}..{elng} "
                          f"{ep.split('//')[1][:18]} try{attempt+1}: {type(e).__name__}")
            if got is not None:
                break
            # Overpass throttling clears on the order of minutes, not seconds.
            wait = 60 * (attempt + 1)
            print(f"    tile {lo}-{hi}: backing off {wait}s")
            time.sleep(wait)

        if got is None:
            missing.append((lo, hi, wlng, elng))
            continue

        rows = []
        for el in got:
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lng = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lng is None:
                continue
            t = el.get("tags", {})
            volts = [float(x) for x in re.findall(r"\d+", str(t.get("voltage", "")))]
            kv = max(volts) / 1000.0 if volts else None
            rows.append({"lat": lat, "lng": lng, "MAX_VOLT": kv,
                         "name": t.get("name"), "operator": t.get("operator")})
        band = pd.DataFrame(rows)
        band.to_parquet(cache, index=False, compression="zstd")
        print(f"    tile {lo}-{hi} {wlng}..{elng}: fetched {len(band):,}")
        frames.append(band)
        time.sleep(5)

    if missing:
        print(f"    !! tiles still missing: {missing} - rerun to fill them in")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()



CONUS_BBOX = (-125.0, 24.0, -66.0, 50.0)


@fetcher("water_stress")
def fetch_water_stress() -> pd.DataFrame:
    """WRI Aqueduct 4.0 baseline water stress, by hydrological basin.

    Uses WRI's own 261 MB download rather than an ArcGIS-hosted copy. The
    hosted services are partial: the best one returned 1,405 CONUS basins and
    left the whole northern tier (ND, MN, MT, NH, ME) with no value at all.
    The authoritative file has 68,506 basins globally, 4,267 over CONUS.

    Requires GDAL (ogr2ogr) to read the File Geodatabase.

    bws_score is 0-5, higher = more stressed.
    """
    import subprocess
    import zipfile

    RAW.mkdir(parents=True, exist_ok=True)
    zpath = RAW / "aqueduct40.zip"
    if not zpath.exists() or zpath.stat().st_size < 200_000_000:
        print("    downloading Aqueduct 4.0 (261 MB)")
        req = urllib.request.Request(
            "https://files.wri.org/aqueduct/aqueduct-4-0-water-risk-data.zip",
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=1800) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)

    exdir = RAW / "aqueduct40"
    if not exdir.exists():
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(exdir)

    gdb = next((p for p in exdir.rglob("*.gdb") if p.is_dir()), None)
    if gdb is None:
        raise RuntimeError("no .gdb found in Aqueduct archive")

    out = INTERIM / "aqueduct_conus.geojsonl"
    if not out.exists():
        subprocess.run([
            "ogr2ogr", "-f", "GeoJSONSeq", str(out), str(gdb), "baseline_annual",
            "-clipdst", "-125", "24", "-66", "50",
            "-select", "bws_score,bws_cat,bws_label,pfaf_id",
            "-nlt", "PROMOTE_TO_MULTI",
        ], check=True, capture_output=True)

    from shapely.geometry import shape as _shape
    rows = []
    with open(out) as fh:
        for line in fh:
            try:
                f = json.loads(line)
                geom = _shape(f["geometry"])
            except Exception:
                continue
            if geom.is_empty:
                continue
            p = f.get("properties") or {}
            v = p.get("bws_score")
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = None
            if v is not None and v < 0:
                v = None
            rows.append({"wkt": geom.wkt, "bws_score": v,
                         "bws_label": p.get("bws_label"), "pfaf_id": p.get("pfaf_id")})
    df = pd.DataFrame(rows)
    print(f"    basins: {len(df)}  with bws_score: {df['bws_score'].notna().sum()}")
    return df



# PAD-US designation codes that are genuinely undevelopable. Deliberately does
# NOT include NF (National Forest) or general BLM holdings: those are
# multiple-use lands that can and do host infrastructure under right-of-way, so
# excluding them would zero out most of the West for no defensible reason.
PROTECTED_DES = ["NP", "NWR", "NM", "WA", "WSA", "RNA", "ACEC", "MPA", "NT", "NLS"]


@fetcher("protected_lands")
def fetch_protected_lands() -> pd.DataFrame:
    """PAD-US protected areas that constitute a hard siting exclusion."""
    from shapely.geometry import shape as _shape

    BASE = ("https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services"
            "/Federal_Management_Agencies/FeatureServer/0")
    # A single OR'd WHERE over 75k large polygons times the server out. Split
    # into two simple IN queries and page small -- these geometries are big.
    des = ",".join(f"'{d}'" for d in PROTECTED_DES)
    queries = ["Own_Name IN ('NPS','FWS')", f"Des_Tp IN ({des})"]
    rows, seen = [], set()
    for where in queries:
        print(f"    query: {where[:60]}")
        for f in _arcgis_paged(BASE, out_fields="Own_Name,Des_Tp,Unit_Nm",
                               where=where, bbox=CONUS_BBOX, page=250):
            g = f.get("geometry")
            if not g:
                continue
            try:
                geom = _shape(g).buffer(0)
            except Exception:
                continue
            if geom.is_empty:
                continue
            p = f.get("properties", {}) or {}
            key = (p.get("Unit_Nm"), p.get("Des_Tp"), round(geom.area, 8))
            if key in seen:          # the two queries overlap on NPS wilderness
                continue
            seen.add(key)
            # Full-precision PAD-US boundaries serialize to ~157 MB of WKT and
            # make the point-in-polygon join crawl. The analysis grid is 5 km2
            # hexes, so ~100 m tolerance loses nothing that can affect a cell.
            geom = geom.simplify(0.001, preserve_topology=True)
            if geom.is_empty:
                continue
            rows.append({"wkt": geom.wkt, "own": p.get("Own_Name"),
                         "des": p.get("Des_Tp"), "name": p.get("Unit_Nm")})
    df = pd.DataFrame(rows)
    print(f"    protected polygons: {len(df)}")
    return df


@fetcher("tribal_lands")
def fetch_tribal_lands() -> pd.DataFrame:
    """Census TIGER 2024 American Indian / Alaska Native / Native Hawaiian areas.

    Carried as a jurisdictional FLAG, never a score penalty. Development on or
    near tribal land is a sovereignty and consultation question, not a
    desirability question.
    """
    from shapely.geometry import shape as _shape

    BASE = ("https://services1.arcgis.com/fBc8EJBxQRMcHlei/arcgis/rest/services"
            "/WASO_STLPG_tl_2024_us_aiannh/FeatureServer/0")
    rows = []
    for f in _arcgis_paged(BASE, out_fields="NAME,NAMELSAD,GEOID",
                           bbox=CONUS_BBOX, page=500):
        g = f.get("geometry")
        if not g:
            continue
        try:
            geom = _shape(g).buffer(0)
        except Exception:
            continue
        if geom.is_empty:
            continue
        p = f.get("properties", {}) or {}
        rows.append({"wkt": geom.wkt, "name": p.get("NAMELSAD") or p.get("NAME"),
                     "geoid": p.get("GEOID")})
    df = pd.DataFrame(rows)
    print(f"    tribal areas: {len(df)}")
    return df



@fetcher("slope")
def fetch_slope() -> pd.DataFrame:
    """Terrain slope from USGS 3DEP, as a GeoTIFF sampled later per cell.

    One exportImage call for all of CONUS rather than thousands of tiles.
    At 4000x1763 the pixel is roughly 1.3 x 1.6 km, so this measures REGIONAL
    terrain -- "is this mountainous" -- not the slope of a specific 50-acre
    pad. Good enough to screen out the Rockies; not a substitute for a site
    survey. 8000px wide returns HTTP 500 from the service.

    Returns an empty frame: the artifact is the .tif, which score.py samples.
    """
    import numpy as _np
    import rasterio as _rio

    RAW.mkdir(parents=True, exist_ok=True)
    dem = RAW / "conus_dem.tif"
    if not dem.exists() or dem.stat().st_size < 1_000_000:
        q = {"bbox": "-125,24,-66,50", "bboxSR": "4326", "imageSR": "4326",
             "size": "4000,1763", "format": "tiff", "pixelType": "F32", "f": "image"}
        url = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
               "3DEPElevation/ImageServer/exportImage?" + urllib.parse.urlencode(q))
        print("    requesting CONUS DEM (single exportImage call)")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=1200) as r:
            dem.write_bytes(r.read())

    with _rio.open(dem) as ds:
        a = ds.read(1).astype("float32")
        res_x, res_y = ds.res
        profile = ds.profile
    a[(a < -500) | (a > 6000)] = _np.nan

    midlat = 37.0
    mx = res_x * 111320 * _np.cos(_np.radians(midlat))
    my = res_y * 110574
    gy, gx = _np.gradient(a, my, mx)
    slope_pct = _np.hypot(gx, gy) * 100.0        # rise/run as percent

    profile.update(dtype="float32", count=1, nodata=_np.nan, compress="deflate")
    out = INTERIM / "slope.tif"
    with _rio.open(out, "w", **profile) as dst:
        dst.write(slope_pct.astype("float32"), 1)
    print(f"    wrote {out.name}  median slope "
          f"{_np.nanmedian(slope_pct):.2f}%  p95 {_np.nanpercentile(slope_pct,95):.2f}%")
    return pd.DataFrame()



@fetcher("policy_climate")
def fetch_policy_climate() -> pd.DataFrame:
    """Assemble the state policy layer from cached policy_scan.py output.

    Reads only what policy_scan already validated (every field carries >=1
    source URL). States with no cached record are simply absent, and score.py
    renormalizes around them rather than scoring them zero.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from policy_scan import STATES as _ST

    cache = RAW / "policy"
    if not cache.exists():
        raise RuntimeError("no policy cache - run pipeline/policy_scan.py first")

    rows = []
    for f in sorted(cache.glob("*.json")):
        rec = json.loads(f.read_text())
        fields = rec.get("fields") or {}
        if not fields:
            continue
        scores = [v["score"] for v in fields.values() if v.get("score") is not None]
        if not scores:
            continue
        code = rec.get("state") or f.stem
        fips = _ST.get(code)
        if not fips:
            continue
        row = {
            "join_key": fips, "state": code,
            "policy_score": sum(scores) / len(scores),
            "n_fields": len(scores),
            "n_sources": sum(len(v.get("sources", [])) for v in fields.values()),
            "as_of": rec.get("as_of"),
        }
        # Keep each field too: the UI shows the breakdown and the moratorium
        # flag needs its individual value, not the state average.
        for k, v in fields.items():
            row[k] = v.get("score")
        rows.append(row)
    df = pd.DataFrame(rows)
    print(f"    states with policy data: {len(df)} / {len(_ST)}")
    if len(df):
        print(f"    mean fields cited per state: {df['n_fields'].mean():.1f}/6")
    return df


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

# National layers must actually be national. An ArcGIS service whose name says
# "United States" may hold one state's data; that silently hands one region a
# bonus no other region can earn. Cheap to check, expensive to miss.
NATIONAL_MIN_CELLS = 150


def _check_coverage(lid: str, df: pd.DataFrame) -> None:
    if df is None or df.empty or not {"lat", "lng"}.issubset(df.columns):
        if df is not None and "wkt" in getattr(df, "columns", []):
            print(f"    coverage: {len(df)} polygons (extent checked at join time)")
        return
    cells = {(round(a), round(b)) for a, b in zip(df["lat"], df["lng"])}
    lng_span = float(df["lng"].max() - df["lng"].min())
    print(f"    coverage: {len(cells)} 1-deg cells, lng span {lng_span:.1f} deg")
    if len(cells) < NATIONAL_MIN_CELLS or lng_span < 40:
        print(f"    !! WARNING {lid} looks REGIONAL, not national "
              f"({len(cells)} cells, {lng_span:.1f} deg). Check the source URL.")


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
            if df is None or df.empty:
                tif = INTERIM / f"{lid}.tif"
                if tif.exists():
                    print(f"   -> raster artifact {tif.name} "
                          f"({tif.stat().st_size/1e6:.1f} MB)\n")
                    continue
            _check_coverage(lid, df)
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
