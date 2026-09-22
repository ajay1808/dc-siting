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

import numpy as np
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
            # OSM's substation subtag is the authoritative transmission vs
            # distribution signal. Without it, a 13.8 kV neighbourhood
            # distribution substation is indistinguishable from a 500 kV
            # transmission bus -- and for a 100 MW+ load they are not remotely
            # the same thing.
            rows.append({"lat": lat, "lng": lng, "MAX_VOLT": kv,
                         "sub_type": t.get("substation"),
                         "name": t.get("name"), "operator": t.get("operator")})
        band = pd.DataFrame(rows)
        band.to_parquet(cache, index=False, compression="zstd")
        print(f"    tile {lo}-{hi} {wlng}..{elng}: fetched {len(band):,}")
        frames.append(band)
        time.sleep(5)

    if missing:
        print(f"    !! tiles still missing: {missing} - rerun to fill them in")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)

    # Drop DISTRIBUTION substations. A 13.8 kV neighbourhood distribution
    # substation is worth essentially nothing to a 100 MW+ load, but the raw
    # OSM layer mixes them in with transmission buses: an audit found 5.2%
    # under 35 kV and 8.0% at 35-69 kV, and a quarter of all records untagged.
    before = len(df)
    kv = pd.to_numeric(df.get("MAX_VOLT"), errors="coerce")
    sub = df.get("sub_type").astype(str).str.lower() if "sub_type" in df else None

    is_dist = pd.Series(False, index=df.index)
    if sub is not None:
        is_dist |= sub.isin(["distribution", "minor_distribution", "traction"])
    is_dist |= kv.notna() & (kv < 35)          # explicit low voltage
    df = df[~is_dist].copy()

    # Untagged voltage: infer from the subtag where present, else mark it so
    # the scorer can weight it conservatively rather than assuming the median
    # (which previously promoted untagged records to ~115 kV).
    df["is_transmission"] = (sub.reindex(df.index).eq("transmission")
                             if sub is not None else False)
    kept_kv = pd.to_numeric(df["MAX_VOLT"], errors="coerce")
    print(f"    substations {before:,} -> {len(df):,} after dropping distribution "
          f"({before-len(df):,} removed)")
    print(f"    remaining with voltage: {kept_kv.notna().mean():.1%} | "
          f"tagged transmission: {df['is_transmission'].mean():.1%}")
    return df.reset_index(drop=True)



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



# NLCD palette index -> NLCD class code. MRLC's WMS returns a PALETTED image,
# not raw class values, so the index must be translated. The palette follows
# the canonical NLCD legend order (verified against the published RGB values).
NLCD_INDEX = {1:11, 2:12, 3:21, 4:22, 5:23, 6:24, 7:31, 8:32, 9:41, 10:42,
              11:43, 12:51, 13:52, 14:71, 15:72, 16:73, 17:74, 18:81, 19:82,
              20:90, 21:95}

# What land cover actually means for siting a large data center campus.
#
# The naive reading is "developed = good, undeveloped = bad". That is wrong in
# both directions, so this scores on development COST and PERMITTING RISK:
#
#  - Developed high-intensity is the WORST developed class, not the best:
#    parcels are small and expensive and there is no room for a campus.
#  - Developed low / open space is the best case: roads, power and fiber are
#    already there, the land is already disturbed, and parcels are big enough.
#  - Cultivated crops LOOK ideal (flat, cleared, cheap) but prime-farmland
#    conversion is the most locally contested change of use there is, so it is
#    scored well below pasture rather than alongside it.
#  - Forest carries clearing cost, stormwater and erosion permitting, and
#    growing ESG/carbon exposure.
#  - Wetlands are NOT a low score, they are an exclusion: Clean Water Act
#    section 404 permitting through USACE makes a large pad impractical.
NLCD_SUITABILITY = {
    11: 0.00,  # open water                    - exclusion
    12: 0.00,  # perennial ice / snow          - exclusion
    90: 0.00,  # woody wetlands                - CWA 404, exclusion
    95: 0.00,  # emergent herbaceous wetlands  - CWA 404, exclusion
    21: 0.95,  # developed, open space         - best: serviced and disturbed
    22: 0.90,  # developed, low intensity
    23: 0.55,  # developed, medium intensity   - infill only
    24: 0.20,  # developed, high intensity     - no room, expensive
    31: 0.90,  # barren land                   - cheap, nothing to clear
    52: 0.80,  # shrub / scrub
    71: 0.80,  # grassland / herbaceous
    81: 0.75,  # pasture / hay                 - already disturbed
    82: 0.50,  # cultivated crops              - farmland-conversion opposition
    41: 0.35,  # deciduous forest              - clearing + permitting + ESG
    42: 0.30,  # evergreen forest
    43: 0.32,  # mixed forest
    51: 0.80, 32: 0.85, 72: 0.75, 73: 0.70, 74: 0.70,   # AK classes, unused L48
}
# ONLY true wetlands trigger the exclusion. Open water (11) deliberately does
# NOT: a 5 km2 cell that is half river is still half buildable, and riverfront
# is actively desirable for cooling. Lumping water in here scored The Dalles,
# an operating Google campus on the Columbia, at exactly 0.
WETLAND_CLASSES = {90, 95}


@fetcher("land_cover")
def fetch_land_cover() -> pd.DataFrame:
    """NLCD 2021 land cover -> per-cell suitability and wetland fraction.

    Fetched via MRLC's WMS in tiles at ~500 m, translated from palette index to
    NLCD class, scored per pixel, then BLOCK-AVERAGED to roughly the analysis
    cell size. Averaging matters: a 5 km2 hex is heterogeneous, and "mostly
    cropland with 15% wetland" is a materially different site from "all
    cropland". Taking the single class under the centroid would throw that away.

    Writes two rasters:
      land_cover.tif          mean suitability 0-1  (buildability factor)
      land_cover_wetland.tif  TRUE wetland fraction (exclusion; excludes open
                              water, which is handled by suitability instead)
    """
    import io as _io
    import numpy as _np
    import rasterio as _rio
    from rasterio.transform import from_origin

    RES = 0.005                      # ~500 m
    W, S, E, N = -125.0, 24.0, -66.0, 50.0
    NX, NY = 4, 2                    # GeoServer caps a single GetMap request
    full_w = int((E - W) / RES)
    full_h = int((N - S) / RES)
    suit = _np.full((full_h, full_w), _np.nan, dtype="float32")
    wet = _np.full((full_h, full_w), _np.nan, dtype="float32")

    lut_s = _np.full(256, _np.nan, dtype="float32")
    lut_w = _np.full(256, _np.nan, dtype="float32")
    for idx, cls in NLCD_INDEX.items():
        lut_s[idx] = NLCD_SUITABILITY.get(cls, _np.nan)
        lut_w[idx] = 1.0 if cls in WETLAND_CLASSES else 0.0

    for ix in range(NX):
        for iy in range(NY):
            x0 = W + (E - W) * ix / NX
            x1 = W + (E - W) * (ix + 1) / NX
            y1 = N - (N - S) * iy / NY
            y0 = N - (N - S) * (iy + 1) / NY
            px = int((x1 - x0) / RES)
            py = int((y1 - y0) / RES)
            q = {"service": "WMS", "version": "1.1.1", "request": "GetMap",
                 "layers": "NLCD_2021_Land_Cover_L48", "srs": "EPSG:4326",
                 "bbox": f"{x0},{y0},{x1},{y1}", "width": str(px),
                 "height": str(py), "format": "image/geotiff"}
            url = ("https://www.mrlc.gov/geoserver/mrlc_display/wms?"
                   + urllib.parse.urlencode(q))
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=900) as r:
                blob = r.read()
            with _rio.open(_io.BytesIO(blob)) as ds:
                arr = ds.read(1)
            cx = int((x0 - W) / RES)
            cy = int((N - y1) / RES)
            suit[cy:cy + arr.shape[0], cx:cx + arr.shape[1]] = lut_s[arr]
            wet[cy:cy + arr.shape[0], cx:cx + arr.shape[1]] = lut_w[arr]
            print(f"    tile {ix},{iy}: {arr.shape[1]}x{arr.shape[0]}")
            time.sleep(1)

    # Block-average 500 m -> ~2 km so a centroid sample reads an areal mean.
    K = 4
    h2, w2 = full_h // K, full_w // K
    def block_mean(a):
        b = a[:h2 * K, :w2 * K].reshape(h2, K, w2, K)
        return _np.nanmean(_np.nanmean(b, axis=3), axis=1)
    suit_c = block_mean(suit)
    wet_c = block_mean(wet)

    prof = dict(driver="GTiff", height=h2, width=w2, count=1, dtype="float32",
                crs="EPSG:4326", transform=from_origin(W, N, RES * K, RES * K),
                nodata=_np.nan, compress="deflate")
    for name, arr in (("land_cover", suit_c), ("land_cover_wetland", wet_c)):
        with _rio.open(INTERIM / f"{name}.tif", "w", **prof) as dst:
            dst.write(arr.astype("float32"), 1)
    print(f"    mean suitability {_np.nanmean(suit_c):.3f} | "
          f"pixels >50% true wetland: {_np.nanmean(wet_c > 0.5):.2%}")
    return pd.DataFrame()



@fetcher("rail_highway")
def fetch_rail_highway() -> pd.DataFrame:
    """Interstate and primary highway access, from Census TIGER.

    Deliberately NOT the full 302k-segment rail network: for a data center the
    binding logistics constraint is heavy-haul road access for transformers,
    gensets and chillers, plus construction traffic. TIGER's primaryroads layer
    is one 38 MB national file covering interstates and primary arterials,
    which is the relevant subset.
    """
    import subprocess
    import zipfile

    from shapely.geometry import shape as _shape

    RAW.mkdir(parents=True, exist_ok=True)
    z = RAW / "tl_2024_us_primaryroads.zip"
    if not z.exists() or z.stat().st_size < 1_000_000:
        url = ("https://www2.census.gov/geo/tiger/TIGER2024/PRIMARYROADS/"
               "tl_2024_us_primaryroads.zip")
        print(f"    downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=900) as r, open(z, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)

    out = INTERIM / "primaryroads.geojsonl"
    if not out.exists():
        subprocess.run([
            "ogr2ogr", "-f", "GeoJSONSeq", str(out),
            f"/vsizip/{z}", "-t_srs", "EPSG:4326",
            "-clipdst", "-125", "24", "-66", "50",
            "-select", "FULLNAME,RTTYP",
        ], check=True, capture_output=True)

    rows = []
    with open(out) as fh:
        for line in fh:
            try:
                f = json.loads(line)
            except Exception:
                continue
            p = f.get("properties") or {}
            # RTTYP I = Interstate, U = US highway, S = State. Weight the
            # interstates highest: that is what an oversize load actually needs.
            w = {"I": 1.0, "U": 0.6, "S": 0.4}.get(p.get("RTTYP"), 0.3)
            for lng, lat in _line_vertices(f.get("geometry"), densify_km=3.0):
                rows.append({"lat": lat, "lng": lng, "road_class": w,
                             "name": p.get("FULLNAME")})
    df = pd.DataFrame(rows)
    return df[df.lat.between(24, 50) & df.lng.between(-125, -66)].reset_index(drop=True)



@fetcher("broadband_served")
def fetch_broadband_served() -> pd.DataFrame:
    """Household internet subscription rate by county, from ACS 2023.

    SUBSTITUTION: the registry asks for FCC Broadband Data Collection served /
    underserved BSL counts. FCC's bulk download requires an interactive
    session and its public API returned 403/405 to every documented endpoint,
    so there is no scripted path to it.

    ACS B28002 measures household internet SUBSCRIPTION, which is demand-side
    adoption rather than supply-side availability. For data center siting the
    two correlate through the same underlying fact -- whether real network
    infrastructure reaches the area -- but this will understate places with
    good infrastructure and low adoption. Weighted only 0.02 accordingly.
    """
    key = _env("CENSUS_API_KEY")
    if not key:
        raise RuntimeError("CENSUS_API_KEY not set")
    d = _get_json("https://api.census.gov/data/2023/acs/acs5", {
        "get": "NAME,B28002_001E,B28002_013E", "for": "county:*",
        "in": "state:*", "key": key})
    hdr, *rows = d
    df = pd.DataFrame(rows, columns=hdr)
    for c in ("B28002_001E", "B28002_013E"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["B28002_001E"] > 0]
    df["join_key"] = df["state"].str.zfill(2) + df["county"].str.zfill(3)
    # share WITH any internet subscription
    df["pct_connected"] = (1 - df["B28002_013E"] / df["B28002_001E"]) * 100
    out = df[["join_key", "pct_connected"]].dropna().reset_index(drop=True)
    print(f"    ACS B28002: {len(out)} counties, "
          f"median connected {out['pct_connected'].median():.1f}%")
    return out



def _manual_raster(layer_id: str, folder: str, what: str, where: str):
    """Consume a raster the user had to download by hand.

    Some federal hosts block scripted access outright (USFS returns 403 to any
    non-browser client; USGS exposes seismic hazard only as contour arcs, not a
    value surface). Rather than fake those layers, the pipeline looks for a
    file the user dropped in and skips the layer cleanly if it is absent.
    """
    import subprocess

    src = RAW / folder
    if not src.exists():
        raise RuntimeError(
            f"{layer_id}: no data at data/raw/{folder}/.\n"
            f"       MANUAL STEP - {what}\n"
            f"       {where}")
    cands = ([p for p in src.rglob("*.tif")] + [p for p in src.rglob("*.img")]
             + [p for p in src.rglob("*.gdb") if p.is_dir()])
    if not cands:
        raise RuntimeError(f"{layer_id}: nothing readable under data/raw/{folder}/")
    inp = cands[0]
    out = INTERIM / f"{layer_id}.tif"
    subprocess.run([
        "gdalwarp", "-t_srs", "EPSG:4326", "-te", "-125", "24", "-66", "50",
        "-ts", "3000", "0", "-r", "average", "-overwrite",
        "-of", "GTiff", "-co", "COMPRESS=DEFLATE", str(inp), str(out),
    ], check=True, capture_output=True)
    print(f"    reprojected {inp.name} -> {out.name}")
    return pd.DataFrame()


@fetcher("wildfire_risk")
def fetch_wildfire_risk() -> pd.DataFrame:
    return _manual_raster(
        "wildfire_risk", "wildfire",
        "download USFS Wildfire Hazard Potential 2023 (270 m)",
        "https://www.fs.usda.gov/rds/archive/catalog/RDS-2015-0047-4 "
        "-> unzip into data/raw/wildfire/")


@fetcher("seismic")
def fetch_seismic() -> pd.DataFrame:
    return _manual_raster(
        "seismic", "seismic",
        "download USGS NSHM PGA, 2% in 50 years, CONUS grid",
        "https://www.usgs.gov/programs/earthquake-hazards/"
        "seismic-hazard-maps-and-site-specific-data "
        "-> put the GeoTIFF in data/raw/seismic/")



# FEMA NRI hazard components relevant to a data center, and how much each
# should count. NRI's *_RISKS fields are 0-100 composite risk scores that
# already fold in exposure, frequency and community resilience.
#
# Weighted rather than max-pooled on purpose: tornado and hail are DESIGN
# problems (you build to them - tornado alley hosts plenty of campuses),
# whereas earthquake, flood and wildfire are SITING problems that change
# whether a site is viable or insurable at all.
NRI_HAZARDS = {
    "ERQK_RISKS": 1.00,   # earthquake  - structural, hardest to engineer around
    "IFLD_RISKS": 1.00,   # riverine flood
    "CFLD_RISKS": 1.00,   # coastal flood
    "WFIR_RISKS": 0.90,   # wildfire    - direct facility + transmission threat
    "HRCN_RISKS": 0.70,   # hurricane   - wind plus multi-day outage
    "LNDS_RISKS": 0.60,   # landslide
    "ISTM_RISKS": 0.45,   # ice storm   - grid outage driver
    "HWAV_RISKS": 0.40,   # heat wave   - cooling + grid stress
    "TRND_RISKS": 0.35,   # tornado     - design problem, not siting
}


@fetcher("hazard_nri")
def fetch_hazard_nri() -> pd.DataFrame:
    """FEMA National Risk Index, county level.

    Replaces the separate USGS seismic and USFS wildfire layers, both of which
    block scripted access (USFS 403s any non-browser client; USGS publishes
    seismic only as contour arcs, not a value surface). NRI is one public
    federal dataset that covers both, plus flood, hurricane and tornado, and
    joins straight onto county FIPS.
    """
    BASE = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services"
            "/National_Risk_Index_Counties/FeatureServer/0")
    cols = ["STCOFIPS"] + list(NRI_HAZARDS)
    rows = []
    for f in _arcgis_paged(BASE, out_fields=",".join(cols), geometry=False, page=1000):
        rows.append((f.get("properties") or {}))
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("NRI returned no rows")

    for c in NRI_HAZARDS:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")

    wsum = sum(NRI_HAZARDS.values())
    acc = None
    for c, w in NRI_HAZARDS.items():
        part = df[c].fillna(0) * w
        acc = part if acc is None else acc + part
    df["hazard_index"] = acc / wsum

    df["join_key"] = df["STCOFIPS"].astype(str).str.zfill(5)
    out = df[["join_key", "hazard_index"] + list(NRI_HAZARDS)].dropna(subset=["join_key"])
    print(f"    NRI counties: {len(out)}  hazard_index "
          f"median {out['hazard_index'].median():.1f} max {out['hazard_index'].max():.1f}")
    return out.reset_index(drop=True)



@fetcher("solar_wind_potential")
def fetch_solar_wind_potential() -> pd.DataFrame:
    """Solar resource (GHI/DNI) sampled on a coarse H3 grid, IDW'd later.

    NLR's API is per-point and rate limited to 1,000 requests/hour, so this
    samples at H3 resolution 3 (~651 CONUS cells, one rate window) rather than
    per analysis cell. Solar resource varies smoothly at continental scale, so
    interpolating from res 3 loses very little -- and this factor carries only
    0.02 weight, so a finer sample would not be a proportionate use of a free
    public API.

    Caveat: this is SOLAR only. Wind resource is spiky (ridgelines, gaps) and
    would not survive this interpolation; the NLR wind toolkit is a separate,
    heavier API. "Renewable potential" here therefore means solar.
    """
    import h3 as _h3

    key = _env("NREL_API_KEY")
    if not key:
        raise RuntimeError("NREL_API_KEY not set (note: NREL is now NLR)")

    cache = RAW / "nlr_solar.json"
    done = json.loads(cache.read_text()) if cache.exists() else {}

    # coarse cells covering CONUS
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grid import load_states, NON_CONUS
    from shapely.geometry import shape as _shape
    cells = set()
    for st in load_states():
        if str(st["id"]).zfill(2) in NON_CONUS:
            continue
        try:
            cells |= set(_h3.geo_to_cells(_shape(st["geometry"]).buffer(0), 3))
        except Exception:
            continue
    cells = sorted(cells)
    todo = [c for c in cells if c not in done]
    print(f"    coarse cells {len(cells)}, cached {len(done)}, to fetch {len(todo)}")

    for i, c in enumerate(todo):
        lat, lng = _h3.cell_to_latlng(c)
        url = ("https://developer.nlr.gov/api/solar/solar_resource/v1.json?"
               + urllib.parse.urlencode({"lat": round(lat, 4), "lon": round(lng, 4),
                                         "api_key": key}))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                o = (json.load(r).get("outputs") or {})
            ghi = (o.get("avg_ghi") or {}).get("annual")
            dni = (o.get("avg_dni") or {}).get("annual")
            if isinstance(ghi, (int, float)):
                done[c] = {"lat": lat, "lng": lng, "ghi": ghi, "dni": dni}
        except Exception as e:  # noqa: BLE001
            print(f"    {c}: {type(e).__name__}")
        if (i + 1) % 50 == 0:
            cache.write_text(json.dumps(done))
            print(f"    {i+1}/{len(todo)} fetched")
        time.sleep(1.2)          # stay well inside 1000/hr
    cache.write_text(json.dumps(done))

    df = pd.DataFrame([{"lat": v["lat"], "lng": v["lng"], "ghi": v["ghi"],
                        "dni": v.get("dni")} for v in done.values()])
    print(f"    solar points: {len(df)}  GHI {df['ghi'].min():.2f}-{df['ghi'].max():.2f}")
    return df


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



@fetcher("gas_pipelines")
def fetch_gas_pipelines() -> pd.DataFrame:
    """EIA natural gas interstate/intrastate transmission pipelines.

    NOTE: several ArcGIS services share this layer's name but hold only ONE
    state's data -- the first source tried here was Pennsylvania-only despite
    the national name. This one is national (~33k features, lng -151..-67).
    Always check extent before swapping it; run() warns on regional layers.

    EIA pipeline geometry averages ~3 vertices per feature, so raw vertices
    would put sample points hundreds of km apart. Densified to 4 km.
    """
    BASE = ("https://services.arcgis.com/RCbhhjhpPZMzteoU/arcgis/rest/services"
            "/NaturalGas_InterIntrastate_Pipelines_US_EIA/FeatureServer/0")
    rows = []
    for f in _arcgis_paged(BASE, out_fields="TYPEPIPE,Operator"):
        p = f.get("properties", {}) or {}
        # Gathering lines carry raw gas from wellheads to processing; they are
        # not deliverable supply and should not read as gas access.
        if str(p.get("TYPEPIPE", "")).strip().lower() == "gathering":
            continue
        for lng, lat in _line_vertices(f.get("geometry"), densify_km=4.0):
            rows.append({"lat": lat, "lng": lng, "typepipe": p.get("TYPEPIPE"),
                         "operator": p.get("Operator")})
    df = pd.DataFrame(rows)
    return df[df.lat.between(24, 50) & df.lng.between(-125, -66)].reset_index(drop=True)


NOAA_NORMALS_URL = ("https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/"
                    "archive/us-climate-normals_1991-2020_v1.0.1_annualseasonal_"
                    "multivariate_by-station_c20230404.tar.gz")


@fetcher("cooling_climate")
def fetch_cooling_climate() -> pd.DataFrame:
    """NOAA 1991-2020 annual cooling degree days, by station.

    SUBSTITUTION: the registry asks for design wet-bulb, the right variable for
    sizing evaporative cooling, but ASHRAE design conditions are paywalled.
    Annual CDD (base 65F) is the closest free proxy. It tracks cooling energy
    well but does NOT capture humidity, so arid and humid places with equal CDD
    score the same here when they should not.

    One 54 MB archive rather than 15,616 per-station requests.
    """
    import csv as _csv
    import io as _io
    import tarfile

    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "noaa_normals_annualseasonal.tar.gz"
    if not cache.exists():
        print("    downloading NOAA normals archive (~54 MB)")
        req = urllib.request.Request(NOAA_NORMALS_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=900) as r:
            cache.write_bytes(r.read())

    rows = []
    with tarfile.open(cache, "r:gz") as tf:
        members = [m for m in tf.getnames() if m.endswith(".csv")]
        for i, name in enumerate(members):
            try:
                txt = tf.extractfile(name).read().decode("utf8", "replace")
                rec = next(_csv.DictReader(_io.StringIO(txt)), None)
            except Exception:
                continue
            if not rec:
                continue
            try:
                lat = float(rec.get("LATITUDE", ""))
                lng = float(rec.get("LONGITUDE", ""))
                cdd = float(rec.get("ANN-CLDD-NORMAL", ""))
            except (TypeError, ValueError):
                continue
            if not (24 <= lat <= 50 and -125 <= lng <= -66) or cdd < 0:
                continue
            try:
                tavg = float(rec.get("ANN-TAVG-NORMAL", ""))
            except (TypeError, ValueError):
                tavg = None
            rows.append({"lat": lat, "lng": lng, "cdd65": cdd, "tavg": tavg,
                         "station": rec.get("STATION"), "name": rec.get("NAME")})
            if (i + 1) % 4000 == 0:
                print(f"    parsed {i+1}/{len(members)} stations -> {len(rows)} CONUS")
    return pd.DataFrame(rows)



# Ozone and PM nonattainment severity. A hyperscale campus needs 50-200 MW of
# diesel or gas backup, and in a nonattainment area that plant is a major
# permitting exposure: NSR/PSD review, emission offsets that must be bought in
# the same airshed, and hard caps on annual test-run hours. This is one of the
# most consequential siting constraints that almost no public map carries.
NAA_SEVERITY = {          # 1.0 = worst permitting burden
    "extreme": 1.00, "severe-17": 0.90, "severe-15": 0.90, "severe": 0.90,
    "serious": 0.70, "moderate": 0.50, "marginal": 0.30, "submarginal": 0.25,
    "subpart 1": 0.35, "incomplete data": 0.30, "primary": 0.60,
    "moderate<=": 0.50, "": 0.40,
}
# Backup generators emit NOx (an ozone precursor) and PM. CO/SO2/Pb
# nonattainment barely touches a data center.
NAA_POLLUTANT = {"ozone": 1.00, "pm2.5": 0.95, "pm-2.5": 0.95, "pm10": 0.60,
                 "pm-10": 0.60, "no2": 0.55, "so2": 0.30, "co": 0.25, "lead": 0.15}


@fetcher("air_permitting")
def fetch_air_permitting() -> pd.DataFrame:
    """EPA nonattainment / maintenance areas, scored by permitting burden.

    Emits `permit_difficulty` 0-1 per polygon: severity x pollutant relevance.
    The scorer inverts it, so clean-air counties score high.
    """
    from shapely.geometry import shape as _shape

    BASE = ("https://services.arcgis.com/cJ9YHowT8TU7DUyn/ArcGIS/rest/services"
            "/Nonattainment_Areas_and_Designations/FeatureServer/0")
    rows = []
    for f in _arcgis_paged(BASE, out_fields="pollutant_name,area_name,classification,"
                                            "current_status,state_name", page=200):
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
        pol = str(p.get("pollutant_name", "")).strip().lower()
        cls = str(p.get("classification", "")).strip().lower()
        status = str(p.get("current_status", "")).strip().lower()
        pw = next((v for k, v in NAA_POLLUTANT.items() if k in pol), 0.4)
        sw = next((v for k, v in NAA_SEVERITY.items() if k and k in cls), 0.4)
        # A maintenance area has attained the standard but still carries a
        # maintenance plan, so the burden is real but much lighter.
        if "maintenance" in status:
            sw *= 0.45
        # A revoked NAAQS leaves only anti-backsliding obligations, not live
        # permitting exposure. Counting it at full weight overstated the
        # burden across a lot of the Northeast and Midwest.
        if "revoked" in status:
            sw *= 0.40
        geom = geom.simplify(0.004, preserve_topology=True)
        if geom.is_empty:
            continue
        rows.append({"wkt": geom.wkt, "permit_difficulty": round(pw * sw, 4),
                     "pollutant": p.get("pollutant_name"),
                     "area": p.get("area_name"), "classification": p.get("classification"),
                     "status": p.get("current_status")})
    df = pd.DataFrame(rows)
    print(f"    nonattainment/maintenance areas: {len(df)} | "
          f"mean burden {df['permit_difficulty'].mean():.2f}")
    return df


EGRID_URL = ("https://www.epa.gov/system/files/documents/2025-06/"
             "egrid2023_data_rev2.xlsx")


@fetcher("grid_carbon")
def fetch_grid_carbon() -> pd.DataFrame:
    """EPA eGRID subregion CO2-equivalent output rate (lb/MWh).

    The carbon intensity of grid power where the site sits, before any PPA.
    Determines how much clean procurement a corporate commitment will require,
    and increasingly whether a project is approvable at all. Ranges from about
    430 lb/MWh (WECC California) to over 1,400 (upper Midwest coal).
    """
    import io as _io

    from shapely.geometry import shape as _shape

    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "egrid2023.xlsx"
    if not cache.exists() or cache.stat().st_size < 1_000_000:
        print("    downloading eGRID2023 (~21 MB)")
        req = urllib.request.Request(EGRID_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as r:
            cache.write_bytes(r.read())
    sr = pd.read_excel(cache, sheet_name="SRL23", skiprows=1, engine="openpyxl")
    rate = {str(k).strip(): float(v) for k, v in
            zip(sr["SUBRGN"], pd.to_numeric(sr["SRC2ERTA"], errors="coerce"))
            if pd.notna(v)}

    BASE = ("https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services"
            "/eGRID2023_Subregions/FeatureServer/0")
    rows = []
    for f in _arcgis_paged(BASE, out_fields="Subregion", page=100):
        g = f.get("geometry")
        if not g:
            continue
        try:
            geom = _shape(g).buffer(0)
        except Exception:
            continue
        code = str((f.get("properties") or {}).get("Subregion", "")).strip()
        if geom.is_empty or code not in rate:
            continue
        # Raw eGRID subregion geometry serialises to ~39 MB for 27 polygons.
        geom = geom.simplify(0.01, preserve_topology=True)
        if geom.is_empty:
            continue
        rows.append({"wkt": geom.wkt, "subregion": code,
                     "co2e_lb_mwh": rate[code]})
    df = pd.DataFrame(rows)
    print(f"    eGRID subregions matched: {len(df)} | "
          f"CO2e lb/MWh {df['co2e_lb_mwh'].min():.0f}-{df['co2e_lb_mwh'].max():.0f}")
    return df


@fetcher("retirement_opportunity")
def fetch_retirement_opportunity() -> pd.DataFrame:
    """Retired and retiring generators, as brownfield interconnection capacity.

    A retiring thermal plant leaves behind an energised point of interconnection
    with transmission already sized for its former output. Re-using that POI is
    currently the fastest route to large load in the US, so recent and imminent
    retirements are an asset rather than a liability. Weighted toward the recent
    ones: a plant retired in 1998 has usually had its interconnection released.
    """
    df = _eia860m_sheet("Retired")
    cap = pd.to_numeric(df.get("Nameplate Capacity (MW)"), errors="coerce").fillna(0)
    yr = pd.to_numeric(df.get("Retirement Year"), errors="coerce")
    recency = ((yr - 2015) / 12).clip(lower=0.15, upper=1.0).fillna(0.2)
    out = pd.DataFrame({
        "lat": df["lat"], "lng": df["lng"],
        "capacity_mw": (cap * recency).round(2),
        "raw_mw": cap, "retirement_year": yr,
        "plant_name": df.get("Plant Name"), "technology": df.get("Technology"),
        "plant_id": df.get("Plant ID"),
    })
    out = out.groupby("plant_id", as_index=False).agg(
        lat=("lat", "first"), lng=("lng", "first"),
        capacity_mw=("capacity_mw", "sum"), raw_mw=("raw_mw", "sum"),
        retirement_year=("retirement_year", "max"),
        plant_name=("plant_name", "first"), technology=("technology", "first"))
    print(f"    retired plants: {len(out)} | recency-weighted MW "
          f"{out.capacity_mw.sum():,.0f} of {out.raw_mw.sum():,.0f} raw")
    return out


@fetcher("military_land")
def fetch_military_land() -> pd.DataFrame:
    """DoD installation boundaries.

    Not developable, and the surrounding area carries airspace, security and
    encroachment considerations. Carried as an exclusion on the footprint
    itself rather than a penalty on the neighbourhood -- proximity to a base is
    not inherently bad for a data center, being inside one is disqualifying.
    """
    from shapely.geometry import shape as _shape

    BASE = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services"
            "/Military_Installations_byBranch/FeatureServer/0")
    rows = []
    for f in _arcgis_paged(BASE, out_fields="SITE_NAME,COMPONENT,STATE_TERR",
                           bbox=CONUS_BBOX, page=200):
        g = f.get("geometry")
        if not g:
            continue
        try:
            geom = _shape(g).buffer(0).simplify(0.002, preserve_topology=True)
        except Exception:
            continue
        if geom.is_empty:
            continue
        p = f.get("properties", {}) or {}
        rows.append({"wkt": geom.wkt, "name": p.get("SITE_NAME"),
                     "branch": p.get("COMPONENT")})
    df = pd.DataFrame(rows)
    print(f"    military installations: {len(df)}")
    return df



# ---------------------------------------------------------------------------
# County name -> FIPS, for sources that publish "County, ST" text rather than
# codes (ISO queues, some federal tables).
# ---------------------------------------------------------------------------
_COUNTY_LUT = None


def _norm_county(name: str) -> str:
    n = str(name).lower().strip()
    n = re.sub(r"\b(county|parish|borough|census area|municipality|city and borough)\b", "", n)
    n = n.replace("saint ", "st ").replace("st. ", "st ").replace("ste. ", "ste ")
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _county_lookup() -> dict:
    """(state_abbr, normalised name) -> 5-digit FIPS, from the us-atlas file
    the grid already uses, so joins agree with cell assignment."""
    global _COUNTY_LUT
    if _COUNTY_LUT is not None:
        return _COUNTY_LUT
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grid import _decode_topojson
    cache = RAW / "us-counties-10m.json"
    if not cache.exists():
        raise RuntimeError("run pipeline/counties.py first (needs us-counties-10m.json)")
    feats = _decode_topojson(json.loads(cache.read_text()), "counties")
    inv = {v: k for k, v in _state_fips().items()}
    lut = {}
    for f in feats:
        code = str(f["id"]).zfill(5)
        st = inv.get(code[:2])
        if st:
            lut[(st, _norm_county(f["props"].get("name", "")))] = code
    _COUNTY_LUT = lut
    return lut



_STATE_NAMES = {
    "alabama":"AL","arizona":"AZ","arkansas":"AR","california":"CA","colorado":"CO",
    "connecticut":"CT","delaware":"DE","district of columbia":"DC","florida":"FL",
    "georgia":"GA","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA",
    "kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD",
    "massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS",
    "missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH",
    "new jersey":"NJ","new mexico":"NM","new york":"NY","north carolina":"NC",
    "north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR","pennsylvania":"PA",
    "rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN",
    "texas":"TX","utah":"UT","vermont":"VT","virginia":"VA","washington":"WA",
    "west virginia":"WV","wisconsin":"WI","wyoming":"WY"}
# NYC boroughs are counties under different names.
_BOROUGHS = {"bronx":"bronx","the bronx":"bronx","brooklyn":"kings",
             "manhattan":"new york","staten island":"richmond","queens":"queens"}


def _state_abbr(v) -> str:
    """ISO queues report state as 'TX' or as 'Texas'. Truncating to two
    characters turned every ERCOT row into 'TE' and dropped all 1,778 Texas
    projects -- the single largest data center market -- from the queue layer."""
    t = str(v or "").strip()
    if len(t) == 2:
        return t.upper()
    return _STATE_NAMES.get(t.lower(), t.upper()[:2])


def _match_counties(lut, st, raw) -> list:
    raw = str(raw or "").strip()
    if not raw or raw.lower() in ("nan", "none"):
        return []
    # Try the whole string first so hyphenated names (Miami-Dade) survive,
    # then fall back to splitting multi-county entries.
    whole = _norm_county(_BOROUGHS.get(raw.lower(), raw))
    if (st, whole) in lut:
        return [lut[(st, whole)]]
    parts = [c.strip() for c in re.split(r"[,/;&-]| and ", raw) if c.strip()]
    out = []
    for c in parts:
        c = re.sub(r"^the\s+", "", c, flags=re.I)
        c = _BOROUGHS.get(c.lower(), c)
        code = lut.get((st, _norm_county(c)))
        if code:
            out.append(code)
    return out

def _county_centroids() -> pd.DataFrame:
    """County centroid points, for spreading county statistics over distance."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grid import _decode_topojson
    from shapely.geometry import shape as _shape
    feats = _decode_topojson(json.loads((RAW / "us-counties-10m.json").read_text()),
                             "counties")
    rows = []
    for f in feats:
        try:
            g = _shape(f["geometry"]).buffer(0)
        except Exception:
            continue
        if g.is_empty:
            continue
        c = g.representative_point()
        rows.append({"fips": str(f["id"]).zfill(5), "lat": c.y, "lng": c.x})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# INTERCONNECTION QUEUE -- consolidated from every ISO ourselves
# ---------------------------------------------------------------------------

@fetcher("iso_queue")
def fetch_iso_queue() -> pd.DataFrame:
    """Generation interconnection queues from all seven US ISOs, via gridstatus.

    LBNL's "Queued Up" consolidates these but blocks scripted access, so this
    consolidates them directly. gridstatus normalises each ISO's format into
    one schema (county, state, capacity, fuel, status, queue date).

    Coverage caveat that matters: this is the GENERATION queue, and only for
    ISO regions. The Southeast (Southern, TVA, Duke) and much of the West sit
    outside any ISO and publish only through utility OASIS pages; those
    counties get no value here and the factor drops out for them rather than
    reading as zero. LBNL's file (manual download) is the way to fill them.

    Emits one row per county with:
      active_mw        queued generation still in play -- supply that could
                       serve co-located load, and a sign planners expect
                       injection there
      withdrawn_share  share of all projects ever withdrawn, shrunk toward the
                       ISO-wide rate so a county with 2 projects cannot read
                       as 0% or 100% -- high withdrawal means expensive network
                       upgrades
    """
    import warnings
    warnings.filterwarnings("ignore")
    import gridstatus

    isos = [gridstatus.MISO, gridstatus.SPP, gridstatus.NYISO, gridstatus.ISONE,
            gridstatus.CAISO, gridstatus.Ercot]
    if _env("PJM_API_KEY"):
        isos.append(gridstatus.PJM)
    else:
        print("    PJM skipped - set PJM_API_KEY (free, https://dataminer2.pjm.com)")

    frames = []
    for cls in isos:
        try:
            iso = cls(api_key=_env("PJM_API_KEY")) if cls is gridstatus.PJM else cls()
            q = iso.get_interconnection_queue()
            q["iso"] = cls.__name__.upper()
            frames.append(q)
            print(f"    {cls.__name__:<6} {len(q):>6,} projects")
        except Exception as e:  # noqa: BLE001
            print(f"    {cls.__name__:<6} FAILED {type(e).__name__}: {str(e)[:70]}")
    if not frames:
        raise RuntimeError("no ISO queue retrieved")
    q = pd.concat(frames, ignore_index=True)

    status = q["Status"].astype(str).str.lower()
    q["is_active"] = status.str.contains("active|in progress|study|engineering|suspended|pending|confirmed|construction")
    q["is_withdrawn"] = status.str.contains("withdraw|cancel|deactivat|terminat")
    q["is_done"] = status.str.contains("operational|completed|in service|done")
    q["mw"] = pd.to_numeric(q.get("Capacity (MW)"), errors="coerce").fillna(0)

    lut = _county_lookup()
    rows, unmatched = [], 0
    for r in q.itertuples(index=False):
        st = _state_abbr(getattr(r, "State", ""))
        codes = _match_counties(lut, st, getattr(r, "County", ""))
        if not codes:
            unmatched += 1
            continue
        share = 1.0 / len(codes)            # split multi-county projects evenly
        for c in codes:
            rows.append({"join_key": c, "iso": r.iso, "mw": r.mw * share,
                         "active": r.is_active, "withdrawn": r.is_withdrawn,
                         "done": r.is_done})
    d = pd.DataFrame(rows)
    print(f"    matched {len(q)-unmatched:,}/{len(q):,} projects to a county "
          f"({unmatched:,} unmatched: blank or unparseable county)")

    # ERCOT's published queue lists only active and completed projects --
    # withdrawn ones are removed from the file. Its measured withdrawal rate is
    # therefore 0% by construction, and taking that at face value would make
    # all of Texas read as the lowest-friction interconnection market in the
    # country. Friction is only computed where the source reports withdrawals.
    reports_withdrawals = d.groupby("iso")["withdrawn"].any().to_dict()
    silent = sorted(k for k, v in reports_withdrawals.items() if not v)
    if silent:
        print(f"    friction not computable for {silent}: source omits withdrawn projects")

    # ISO-wide withdrawal rate as the prior for shrinkage
    iso_rate = d.groupby("iso")["withdrawn"].mean().to_dict()
    K = 8.0                                  # prior strength, in projects
    g = d.groupby("join_key")
    out = pd.DataFrame({
        "active_mw": g.apply(lambda x: x.loc[x.active, "mw"].sum()),
        "n_projects": g.size(),
        "n_withdrawn": g["withdrawn"].sum(),
        "iso": g["iso"].agg(lambda s: s.mode().iat[0]),
    }).reset_index()
    prior = out["iso"].map(iso_rate).fillna(d["withdrawn"].mean())
    out["withdrawn_share"] = ((out["n_withdrawn"] + K * prior)
                              / (out["n_projects"] + K)).round(4)
    out.loc[out["iso"].isin(silent), "withdrawn_share"] = np.nan
    print(f"    {len(out):,} counties | active queue {out.active_mw.sum()/1000:,.0f} GW")
    return out


# ---------------------------------------------------------------------------
# POWER COST -- utility level, replacing the state average
# ---------------------------------------------------------------------------

EIA861_URL = "https://www.eia.gov/electricity/data/eia861/zip/f8612024.zip"


@fetcher("utility_price")
def fetch_utility_price() -> pd.DataFrame:
    """Industrial price by retail utility, drawn on its service territory.

    State-average price hides the spread that actually matters: in Virginia,
    Dominion, Appalachian Power and the co-ops sit on different tariffs. EIA-861
    reports industrial revenue and sales per utility; dividing gives an average
    realised industrial price, which is drawn onto HIFLD service territories by
    EIA utility ID.

    Where territories overlap (co-ops frequently overlap IOUs in this layer)
    the scorer burns the HIGHER price last, i.e. it is conservative.
    Caveat: an average industrial price, not the large-load tariff a campus
    would actually negotiate; and in retail-choice states (TX, OH, PA, IL...)
    a campus buys at wholesale and this matters less.
    """
    import io as _io
    import zipfile

    from shapely.geometry import shape as _shape

    RAW.mkdir(parents=True, exist_ok=True)
    z = RAW / "eia861_2024.zip"
    if not z.exists() or z.stat().st_size < 1_000_000:
        req = urllib.request.Request(EIA861_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as r:
            z.write_bytes(r.read())
    zf = zipfile.ZipFile(z)
    name = next(n for n in zf.namelist()
                if n.startswith("Sales_Ult_Cust") and n.endswith(".xlsx") and "_CS" not in n)
    s = pd.read_excel(_io.BytesIO(zf.read(name)), header=[0, 1, 2], engine="openpyxl")

    def col(top, *want):
        for c in s.columns:
            if c[0].strip().upper() == top and all(w.lower() in " ".join(c).lower() for w in want):
                return s[c]
        raise KeyError((top, want))
    uid = pd.to_numeric(col("UTILITY CHARACTERISTICS", "utility number"), errors="coerce")
    rev = pd.to_numeric(col("INDUSTRIAL", "revenues"), errors="coerce")   # thousand $
    mwh = pd.to_numeric(col("INDUSTRIAL", "sales"), errors="coerce")      # MWh
    t = pd.DataFrame({"uid": uid, "rev": rev, "mwh": mwh}).dropna()
    t = t[(t.mwh > 1000)]                    # ignore utilities with trivial industrial load
    t = t.groupby("uid", as_index=False)[["rev", "mwh"]].sum()
    t["cents_kwh"] = (t.rev * 1000 / (t.mwh * 1000) * 100).round(3)
    price = dict(zip(t.uid.astype(int), t.cents_kwh))
    print(f"    EIA-861: industrial price for {len(price):,} utilities "
          f"(median {t.cents_kwh.median():.2f} c/kWh)")

    BASE = ("https://services6.arcgis.com/BAJNi3EgCdtQ1BCG/arcgis/rest/services"
            "/Electric_Retail_Service_Territories/FeatureServer/0")
    rows, missing = [], 0
    for f in _arcgis_paged(BASE, out_fields="ID,NAME,STATE,HOLDING_CO,CNTRL_AREA",
                           bbox=CONUS_BBOX, page=200):
        p = f.get("properties") or {}
        try:
            u = int(str(p.get("ID")).strip())
        except (TypeError, ValueError):
            continue
        if u not in price:
            missing += 1
            continue
        g = f.get("geometry")
        if not g:
            continue
        try:
            geom = _shape(g).buffer(0).simplify(0.004, preserve_topology=True)
        except Exception:
            continue
        if geom.is_empty:
            continue
        rows.append({"wkt": geom.wkt, "utility_id": u, "utility": p.get("NAME"),
                     "holding_co": p.get("HOLDING_CO"),
                     "balancing_area": p.get("CNTRL_AREA"),
                     "cents_kwh": price[u]})
    df = pd.DataFrame(rows)
    print(f"    territories priced: {len(df):,} ({missing:,} had no industrial sales)")
    return df


# ---------------------------------------------------------------------------
# LABOR
# ---------------------------------------------------------------------------

LABOR_NAICS = {
    "238210": ("electrical_contractors", 1.00),   # electricians: the binding trade
    "237130": ("line_construction", 0.80),        # power & comms line construction
    "238220": ("mechanical_contractors", 0.70),   # HVAC / plumbing: cooling plant
    "518210": ("dc_operations", 0.60),            # existing hosting/compute workforce
}


@fetcher("labor_pool")
def fetch_labor_pool() -> pd.DataFrame:
    """Skilled construction and operations labor, from Census County Business
    Patterns, spread over commuting distance by the scorer.

    Electricians are the binding trade on most campus builds: a 300 MW site can
    need several hundred on site at peak. Counts are weighted by how directly
    each trade gates a build, emitted at county centroids, and summed within
    commuting radius -- a crew does not stop at the county line.
    CBP suppresses small cells, so rural counties under-count slightly.
    """
    key = _env("CENSUS_API_KEY")
    if not key:
        raise RuntimeError("CENSUS_API_KEY not set")
    cents = _county_centroids().set_index("fips")
    tot = {}
    for code, (label, w) in LABOR_NAICS.items():
        d = _get_json("https://api.census.gov/data/2022/cbp", {
            "get": "EMP", "for": "county:*", "in": "state:*",
            "NAICS2017": code, "key": key})
        hdr, *rows = d
        i_emp, i_st, i_co = hdr.index("EMP"), hdr.index("state"), hdr.index("county")
        n = 0
        for r in rows:
            f = r[i_st].zfill(2) + r[i_co].zfill(3)
            e = pd.to_numeric(r[i_emp], errors="coerce")
            if pd.notna(e) and e > 0:
                tot[f] = tot.get(f, 0.0) + float(e) * w
                n += 1
        print(f"    NAICS {code} {label:<24} {n:>5,} counties")
        time.sleep(0.5)
    out = pd.DataFrame([{"fips": f, "weighted_workers": v} for f, v in tot.items()])
    out = out.join(cents, on="fips").dropna(subset=["lat", "lng"])
    out = out[out.lat.between(24, 50) & out.lng.between(-125, -66)]
    print(f"    labor points: {len(out):,} counties, "
          f"{out.weighted_workers.sum():,.0f} weighted workers")
    return out.reset_index(drop=True)



# ---------------------------------------------------------------------------
# WATER -- beyond baseline stress
# ---------------------------------------------------------------------------

@fetcher("reclaimed_water")
def fetch_reclaimed_water() -> pd.DataFrame:
    """Major municipal wastewater plants (EPA ECHO), by effluent flow.

    Treated effluent is the preferred cooling-water source for large campuses:
    it avoids competing with drinking-water supply, which is where most local
    opposition to data center water use starts.

    One CSV download, not paged JSON. The first version passed responseset=1,
    which is the PAGE SIZE -- one facility per request, 4,908 requests -- and
    ECHO's default columns omit latitude entirely, so every row was dropped.
    `qcolumns` selects the columns explicitly.

    Actual average flow is reported for about half the plants; the rest use
    design flow at 65% utilisation, a typical figure, and are flagged.
    """
    import io as _io

    q = _get_json("https://echodata.epa.gov/echo/cwa_rest_services.get_facilities",
                  {"output": "JSON", "p_maj": "Y", "p_pcomp": "POT"})
    qid = q["Results"]["QueryID"]
    meta = _get_json("https://echodata.epa.gov/echo/cwa_rest_services.metadata",
                     {"output": "JSON"})
    want = {"FacLat", "FacLong", "CWPName", "CWPState",
            "CWPTotalDesignFlowNmbr", "CWPActualAverageFlowNmbr"}
    ids = [c["ColumnID"] for c in meta["Results"]["ResultColumns"] if c["ObjectName"] in want]
    url = ("https://echodata.epa.gov/echo/cwa_rest_services.get_download?"
           + urllib.parse.urlencode({"qid": qid, "output": "CSV", "qcolumns": ",".join(ids)}))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = pd.read_csv(_io.BytesIO(r.read()), low_memory=False)
    actual = pd.to_numeric(d.get("CWPActualAverageFlowNmbr"), errors="coerce")
    design = pd.to_numeric(d.get("CWPTotalDesignFlowNmbr"), errors="coerce")
    flow = actual.where(actual > 0, design * 0.65)
    df = pd.DataFrame({"lat": pd.to_numeric(d["FacLat"], errors="coerce"),
                       "lng": pd.to_numeric(d["FacLong"], errors="coerce"),
                       "flow_mgd": flow, "flow_estimated": ~(actual > 0),
                       "name": d.get("CWPName"), "state": d.get("CWPState")})
    df = df.dropna(subset=["lat", "lng", "flow_mgd"])
    df = df[(df.flow_mgd > 0) & df.lat.between(24, 50) & df.lng.between(-125, -66)]
    # EPA's own file carries entry errors: Erwin WWTP, NC is listed at 650,000
    # MGD, alone twenty times all US municipal flow. Stickney (Chicago), the
    # largest plant in the world, is ~1,200 MGD, so anything above 1,500 is an
    # error. Dropping it brings the national total to ~31,500 MGD, which
    # matches published US municipal wastewater volume.
    bad = df.flow_mgd > 1500
    if bad.any():
        print(f"    dropped {bad.sum()} implausible flow value(s): "
              + ", ".join(f"{n.strip()} ({v:,.0f} MGD)" for n, v in
                          zip(df.loc[bad, "name"], df.loc[bad, "flow_mgd"])))
    df = df[~bad]
    print(f"    major POTWs: {len(df):,} | {df.flow_mgd.sum():,.0f} MGD "
          f"({df.flow_estimated.mean():.0%} from design flow)")
    return df.reset_index(drop=True)


@fetcher("drought_frequency")
def fetch_drought_frequency() -> pd.DataFrame:
    """US Drought Monitor, 2016-2025: average share of each county in severe
    (D2) or worse drought across ~520 weekly maps.

    Ten years rather than current conditions: a campus is a 30-year asset, and
    this week's drought map says little about the next decade. One request per
    state -- the API returns every county in a state at once.
    """
    frames = []
    for st in sorted(_state_fips()):
        url = ("https://usdmdataservices.unl.edu/api/CountyStatistics/"
               "GetDroughtSeverityStatisticsByAreaPercent?"
               + urllib.parse.urlencode({"aoi": st, "startdate": "1/1/2016",
                                         "enddate": "12/31/2025", "statisticsType": "1"}))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r:
                txt = r.read().decode("utf8", "replace")
            import io as _io
            d = pd.read_csv(_io.StringIO(txt), dtype={"FIPS": str})
            frames.append(d[["FIPS", "D2"]])
        except Exception as e:  # noqa: BLE001
            print(f"    {st} failed: {type(e).__name__}")
        time.sleep(0.8)
    d = pd.concat(frames, ignore_index=True)
    d["D2"] = pd.to_numeric(d["D2"], errors="coerce")
    out = d.groupby("FIPS", as_index=False)["D2"].mean()
    out = out.rename(columns={"FIPS": "join_key", "D2": "pct_area_severe_drought"})
    out["join_key"] = out["join_key"].str.zfill(5)
    print(f"    counties: {len(out):,} | median {out.pct_area_severe_drought.median():.1f}% "
          f"of area in D2+ on an average week")
    return out


@fetcher("water_stress_future")
def fetch_water_stress_future() -> pd.DataFrame:
    """WRI Aqueduct 4.0 projected baseline water stress, 2050, business-as-usual.

    The baseline layer scores today's stress; a campus built now operates
    through 2050. Same basin geometry as the baseline layer, read from the
    same WRI download.
    """
    import subprocess

    from shapely.geometry import shape as _shape

    exdir = RAW / "aqueduct40"
    gdb = next((p for p in exdir.rglob("*.gdb") if p.is_dir()), None)
    if gdb is None:
        raise RuntimeError("run water_stress first (downloads the Aqueduct archive)")
    out = INTERIM / "aqueduct_future_conus.geojsonl"
    if not out.exists():
        subprocess.run(["ogr2ogr", "-f", "GeoJSONSeq", str(out), str(gdb), "future_annual",
                        "-clipdst", "-125", "24", "-66", "50",
                        "-select", "bau50_ws_x_r,bau50_ws_x_s,pfaf_id",
                        "-nlt", "PROMOTE_TO_MULTI"], check=True, capture_output=True)
    rows = []
    with open(out) as fh:
        for line in fh:
            try:
                f = json.loads(line)
                g = _shape(f["geometry"]).buffer(0).simplify(0.004, preserve_topology=True)
            except Exception:
                continue
            p = f.get("properties") or {}
            v = pd.to_numeric(p.get("bau50_ws_x_s"), errors="coerce")
            if g.is_empty or pd.isna(v) or v < 0:
                continue
            rows.append({"wkt": g.wkt, "ws_2050": float(v)})
    df = pd.DataFrame(rows)
    print(f"    basins with 2050 projection: {len(df):,}")
    return df


# ---------------------------------------------------------------------------
# CLIMATE PROJECTIONS
# ---------------------------------------------------------------------------

@fetcher("climate_future")
def fetch_climate_future() -> pd.DataFrame:
    """NOAA/USGS CMRA county projections (LOCA-downscaled), mid-century.

    Cooling today is scored on 1991-2020 normals. This carries the forward
    view: projected cooling degree days and days above 95F for 2036-2065 under
    RCP4.5 (a middle scenario, not the worst case). A 30-year asset should be
    screened against the climate it will actually operate in.
    """
    BASE = ("https://services3.arcgis.com/0Fs3HcaFfvzXvm7w/arcgis/rest/services/"
            "Climate_Mapping_Resilience_and_Adaptation_(CMRA)_Climate_and_Coastal_"
            "Inundation_Projections/FeatureServer/0")
    fields = ["GEOID", "HISTORIC_MEAN_CDD", "RCP45MID_MEAN_CDD", "RCP85MID_MEAN_CDD",
              "HISTORIC_MEAN_TMAX95F", "RCP45MID_MEAN_TMAX95F", "RCP45MID_MEAN_TMAX100F"]
    rows = []
    for f in _arcgis_paged(BASE, out_fields=",".join(fields), geometry=False, page=1000):
        rows.append(f.get("properties") or {})
    df = pd.DataFrame(rows)
    for c in fields[1:]:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    df["join_key"] = df["GEOID"].astype(str).str.zfill(5)
    df = df.rename(columns={"RCP45MID_MEAN_CDD": "cdd_2050", "RCP45MID_MEAN_TMAX95F": "days95_2050",
                            "HISTORIC_MEAN_CDD": "cdd_hist", "RCP45MID_MEAN_TMAX100F": "days100_2050",
                            "RCP85MID_MEAN_CDD": "cdd_2050_rcp85"})
    df["cdd_change"] = df["cdd_2050"] - df["cdd_hist"]
    print(f"    counties: {len(df):,} | median CDD {df.cdd_hist.median():.0f} -> "
          f"{df.cdd_2050.median():.0f} by mid-century (RCP4.5)")
    return df[["join_key", "cdd_2050", "cdd_2050_rcp85", "cdd_hist", "cdd_change",
               "days95_2050", "days100_2050"]]


# ---------------------------------------------------------------------------
# LAND COST
# ---------------------------------------------------------------------------

FHFA_LAND_URL = "https://www.fhfa.gov/document/land-prices_2024_20_june.xlsx"


@fetcher("land_cost")
def fetch_land_cost() -> pd.DataFrame:
    """FHFA land price per acre by county (Davis, Larson, Oliner & Shui).

    Caveat worth stating: these are appraisal-based values for single-family
    residential land. A campus buys industrial or agricultural acreage, which
    is usually far cheaper per acre. It is used as a RELATIVE signal -- where
    land is expensive for housing it is expensive for everything -- not as a
    price estimate.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "fhfa_land_2024.xlsx"
    if not cache.exists():
        req = urllib.request.Request(FHFA_LAND_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as r:
            cache.write_bytes(r.read())
    d = pd.read_excel(cache, sheet_name="Cross-Section Counties ", header=1, engine="openpyxl")
    val = next(c for c in d.columns if "Per Acre" in str(c))
    out = pd.DataFrame({"join_key": pd.to_numeric(d["FIPS"], errors="coerce"),
                        "land_usd_acre": pd.to_numeric(d[val], errors="coerce")}).dropna()
    out["join_key"] = out["join_key"].astype(int).astype(str).str.zfill(5)
    print(f"    counties: {len(out):,} | median ${out.land_usd_acre.median():,.0f}/acre "
          f"(residential basis)")
    return out


# ---------------------------------------------------------------------------
# FIBER -- open proxies for long-haul routes
# ---------------------------------------------------------------------------

@fetcher("cable_landings")
def fetch_cable_landings() -> pd.DataFrame:
    """Submarine cable landing stations (TeleGeography's open map data).

    Matters for a specific class of site -- international connectivity,
    Virginia Beach, Jacksonville, Oregon coast -- rather than generally.
    """
    d = _get_json("https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json")
    rows = []
    for f in d.get("features", []):
        c = (f.get("geometry") or {}).get("coordinates") or []
        if len(c) >= 2:
            rows.append({"lat": c[1], "lng": c[0], "name": (f.get("properties") or {}).get("name")})
    df = pd.DataFrame(rows)
    df = df[df.lat.between(24, 50) & df.lng.between(-125, -66)].reset_index(drop=True)
    print(f"    CONUS landing stations: {len(df)}")
    return df


CLASS_I = ["BNSF", "UP", "CSXT", "NS", "CN", "CPKC", "CPRS", "KCS", "CP"]


@fetcher("rail_corridors")
def fetch_rail_corridors() -> pd.DataFrame:
    """Class I railroad main lines (NTAD North American Rail Network).

    The best open proxy for long-haul fiber. Durairajan et al.'s InterTubes
    study found US long-haul fiber runs overwhelmingly inside rail and highway
    rights-of-way. Filtered server-side to Class I owners so this is ~a fifth of
    the 302k-segment network rather than all of it.
    """
    BASE = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
            "NTAD_North_American_Rail_Network_Lines/FeatureServer/0")
    owners = ",".join(f"'{o}'" for o in CLASS_I)
    where = f"RROWNER1 IN ({owners}) AND COUNTRY='US'"
    rows = []
    for f in _arcgis_paged(BASE, out_fields="RROWNER1", where=where, page=2000):
        for lng, lat in _line_vertices(f.get("geometry"), densify_km=5.0):
            rows.append({"lat": lat, "lng": lng})
    df = pd.DataFrame(rows).drop_duplicates()
    df = df[df.lat.between(24, 50) & df.lng.between(-125, -66)].reset_index(drop=True)
    print(f"    Class I rail points: {len(df):,}")
    return df



# ---------------------------------------------------------------------------
# NODAL WHOLESALE PRICE + CONGESTION
# ---------------------------------------------------------------------------

CAISO_CONTOUR = ("https://wwwmobile.caiso.com/Web.Service.Chart/api/v3/"
                 "ChartService/PriceContourMap1")


def _sample_dates(n_per_month: int = 2, months: int = 12) -> list:
    """Spread sample days across the last year: the 5th and 20th of each month.

    Seasonal coverage matters more than volume -- summer congestion patterns
    look nothing like spring ones, and a single month would bake that in.
    """
    import datetime as _dt
    today = _dt.date.today().replace(day=1)
    out = []
    for k in range(1, months + 1):
        y, m = today.year, today.month - k
        while m <= 0:
            m += 12
            y -= 1
        for d in (5, 20)[:n_per_month]:
            out.append(_dt.date(y, m, d).isoformat())
    return sorted(out)


@fetcher("nodal_lmp")
def fetch_nodal_lmp() -> pd.DataFrame:
    """Average day-ahead LMP and its components at every CAISO / WEIM node.

    Genuinely nodal, which is the point: basis spread inside a single state
    routinely exceeds the difference between states, so a state or utility
    average hides exactly the signal a siting analyst wants.

    Two sources joined on node name:
      coordinates  CAISO's public price-contour map feed (~14,800 nodes). The
                   ISO's own map has to plot nodes somewhere; its data products
                   do not publish locations at all.
      prices       Historical day-ahead hourly LMP for ALL nodes via gridstatus
                   (CAISO OASIS), sampled on 24 days across the past year.

    Coverage: CAISO plus the Western Energy Imbalance Market footprint, i.e.
    much of the West. Other ISOs need a node-coordinate source (see
    RECOMMENDATIONS.md); until then the factor drops out there.

    Emits per node:
      lmp_mean         average day-ahead LMP, $/MWh
      congestion_mean  average congestion component, $/MWh. SIGNED: negative
                       means an export-constrained pocket (surplus generation),
                       which is GOOD for new load -- load there relieves the
                       constraint and buys cheap power.
    """
    import warnings
    warnings.filterwarnings("ignore")
    import gridstatus

    req = urllib.request.Request(CAISO_CONTOUR, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        contour = json.load(r)
    pts = {}
    for layer in contour.get("l", []):
        for m in layer.get("m", []):
            c = m.get("c") or []
            if m.get("t") == "Node" and len(c) == 2 and m.get("n"):
                pts[m["n"]] = (float(c[0]), float(c[1]), m.get("p"), m.get("a"))
    print(f"    contour feed: {len(pts):,} nodes with coordinates")

    cache = RAW / "caiso_dam_node_means.parquet"
    acc = None
    if cache.exists():
        acc = pd.read_parquet(cache)
        print(f"    cached price sample: {acc['n_days'].max()} days")
    else:
        iso = gridstatus.CAISO()
        parts = []
        for d in _sample_dates():
            try:
                df = iso.get_lmp(date=d, market="DAY_AHEAD_HOURLY", locations="ALL")
                g = df.groupby("Location")[["LMP", "Congestion"]].mean()
                g["day"] = d
                parts.append(g.reset_index())
                print(f"    {d}: {len(g):,} nodes")
            except Exception as e:  # noqa: BLE001
                print(f"    {d}: failed {type(e).__name__}")
            time.sleep(2)             # OASIS asks for spacing between requests
        allp = pd.concat(parts, ignore_index=True)
        acc = allp.groupby("Location").agg(
            lmp_mean=("LMP", "mean"), congestion_mean=("Congestion", "mean"),
            n_days=("day", "nunique")).reset_index()
        acc.to_parquet(cache, index=False)

    rows = []
    for r in acc.itertuples(index=False):
        p = pts.get(r.Location)
        if p is None:
            continue
        lat, lng, ptype, area = p
        rows.append({"lat": lat, "lng": lng, "node": r.Location, "node_type": ptype,
                     "area": area, "lmp_mean": round(r.lmp_mean, 3),
                     "congestion_mean": round(r.congestion_mean, 3),
                     "n_days": int(r.n_days)})
    out = pd.DataFrame(rows)
    out = out[out.lat.between(24, 50) & out.lng.between(-125, -66)].reset_index(drop=True)
    print(f"    nodes with price AND location: {len(out):,} | LMP "
          f"p5 ${out.lmp_mean.quantile(.05):.1f} p95 ${out.lmp_mean.quantile(.95):.1f} "
          f"| congestion p5 {out.congestion_mean.quantile(.05):+.1f} "
          f"p95 {out.congestion_mean.quantile(.95):+.1f}")
    return out


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


# Layers that are regional BY NATURE -- cable landings are coastal, CAISO nodes
# are western -- declare it here instead of tripping the national-coverage
# guard, which exists to catch sources that are regional BY MISTAKE.
REGIONAL_BY_DESIGN = {"cable_landings", "nodal_lmp"}


def _check_coverage(lid: str, df: pd.DataFrame) -> None:
    if lid in REGIONAL_BY_DESIGN:
        return
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


def audit_registry() -> list[str]:
    """Registry layers that feed a factor but have no fetcher.

    Added after six fetchers were silently deleted by an index-slice edit to
    this file. The stale parquets in data/interim kept scoring working, so
    nothing failed -- the pipeline had simply stopped being reproducible.
    """
    reg = yaml.safe_load((ROOT / "sources" / "registry.yml").read_text())
    # Layers with data_from reuse another layer's fetch and need no fetcher.
    need = {l["id"] for l in reg["layers"]
            if l.get("scoring") and not l.get("data_from")}
    return sorted(need - set(FETCHERS))


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
    gaps = audit_registry()
    if gaps:
        print(f"[registry] scoring layers with no fetcher: {gaps}")
    args = sys.argv[1:] or list(FETCHERS)
    sys.exit(run(args))
