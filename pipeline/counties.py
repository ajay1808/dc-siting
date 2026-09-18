#!/usr/bin/env python3
"""Attach county FIPS to every grid cell.

Several layers (ACS demographics, EIA retail price, FCC broadband) publish at
county or state granularity. Those have to be joined onto cells rather than
measured by distance, so each cell needs to know which county contains it.

Uses the same us-atlas TopoJSON the basemap comes from, so cell assignment and
the rendered boundaries can never disagree.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd
import yaml
from shapely.geometry import shape
from shapely.strtree import STRtree
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grid import _decode_topojson, NON_CONUS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "out"
URL = "https://cdn.jsdelivr.net/npm/us-atlas@3/counties-10m.json"


def main() -> int:
    reg = yaml.safe_load((ROOT / "sources" / "registry.yml").read_text())
    res = reg["meta"]["grid"]["resolution"]
    gridfile = OUT / f"grid_r{res}.parquet"
    grid = pd.read_parquet(gridfile)

    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "us-counties-10m.json"
    if not cache.exists():
        print(f"  fetching {URL}")
        req = urllib.request.Request(URL, headers={"User-Agent": "dc-siting/0.1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            cache.write_bytes(r.read())

    feats = _decode_topojson(json.loads(cache.read_text()), "counties")

    def clean(geom: dict):
        """Drop degenerate rings. us-atlas quantization can collapse a tiny
        island to fewer than the 4 coordinates a LinearRing requires."""
        t = geom["type"]
        if t == "Polygon":
            rings = [r for r in geom["coordinates"] if len(r) >= 4]
            return {"type": "Polygon", "coordinates": rings} if rings else None
        polys = []
        for poly in geom["coordinates"]:
            rings = [r for r in poly if len(r) >= 4]
            if rings:
                polys.append(rings)
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None

    geoms, fips = [], []
    skipped = 0
    for f in feats:
        code = str(f["id"]).zfill(5)
        if code[:2] in NON_CONUS:
            continue
        cg = clean(f["geometry"])
        if cg is None:
            skipped += 1
            continue
        try:
            g = shape(cg).buffer(0)
        except Exception:
            skipped += 1
            continue
        if g.is_empty:
            skipped += 1
            continue
        geoms.append(g)
        fips.append(code)
    if skipped:
        print(f"  skipped {skipped} degenerate county geometries")
    print(f"  counties (CONUS): {len(geoms)}")

    tree = STRtree(geoms)
    pts = [Point(x, y) for x, y in zip(grid["lng"], grid["lat"])]
    print(f"  assigning {len(pts):,} cells ...")

    assigned = [None] * len(pts)
    # STRtree.query with predicate does the point-in-polygon in bulk C code;
    # this is the difference between ~1 minute and ~1 hour in Python.
    cand_i, cand_j = tree.query(pts, predicate="within")
    for i, j in zip(cand_i, cand_j):
        if assigned[i] is None:
            assigned[i] = fips[j]

    grid["county_fips"] = assigned
    hit = grid["county_fips"].notna().mean()
    grid.to_parquet(gridfile, index=False, compression="zstd")
    print(f"  matched {hit:.1%} of cells to a county")
    print(f"  unmatched (coastal/border cells): {grid['county_fips'].isna().sum():,}")
    print(f"  wrote {gridfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
