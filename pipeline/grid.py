#!/usr/bin/env python3
"""Build the H3 analysis grid for CONUS.

The grid is the spine of the whole tool: every layer is reduced onto these
cells, the score is computed per cell, and the map renders these cells. Using
a uniform hex grid (rather than counties) means distance-decay scoring behaves
consistently everywhere and cells are directly comparable.

Resolution 7 => ~5.16 km2 per cell, ~1.22 km edge, ~1.6M cells over CONUS.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import h3
import pandas as pd
import yaml
from shapely.geometry import shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "out"
STATES_URL = "https://cdn.jsdelivr.net/npm/us-atlas@3/states-10m.json"

# CONUS = 50 states + DC, minus Alaska and Hawaii, minus territories.
NON_CONUS = {"02", "15", "60", "66", "69", "72", "78"}


def _decode_topojson(topo: dict, object_name: str) -> list[dict]:
    """Minimal TopoJSON -> GeoJSON feature decoder (quantized delta arcs)."""
    tr = topo.get("transform")
    sx, sy = (tr["scale"] if tr else (1.0, 1.0))
    tx, ty = (tr["translate"] if tr else (0.0, 0.0))

    def arc(idx: int) -> list[list[float]]:
        reverse = idx < 0
        if reverse:
            idx = ~idx
        pts, x, y = [], 0, 0
        for dx, dy in topo["arcs"][idx]:
            if tr:
                x += dx
                y += dy
                pts.append([x * sx + tx, y * sy + ty])
            else:
                pts.append([dx, dy])
        return pts[::-1] if reverse else pts

    def ring(arcs: list[int]) -> list[list[float]]:
        out: list[list[float]] = []
        for a in arcs:
            seg = arc(a)
            out.extend(seg[1:] if out else seg)
        return out

    def geom(g: dict):
        t = g["type"]
        if t == "Polygon":
            return {"type": "Polygon", "coordinates": [ring(r) for r in g["arcs"]]}
        if t == "MultiPolygon":
            return {"type": "MultiPolygon",
                    "coordinates": [[ring(r) for r in poly] for poly in g["arcs"]]}
        raise ValueError(f"unsupported topojson geometry: {t}")

    feats = []
    for g in topo["objects"][object_name]["geometries"]:
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        feats.append({"id": g.get("id"),
                      "props": g.get("properties", {}),
                      "geometry": geom(g)})
    return feats


def load_states() -> list[dict]:
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "us-states-10m.json"
    if not cache.exists():
        print(f"  fetching {STATES_URL}")
        req = urllib.request.Request(STATES_URL, headers={"User-Agent": "dc-siting/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            cache.write_bytes(r.read())
    topo = json.loads(cache.read_text())
    return _decode_topojson(topo, "states")


def build(resolution: int) -> pd.DataFrame:
    states = load_states()
    conus = [s for s in states if str(s["id"]).zfill(2) not in NON_CONUS]
    print(f"  states: {len(states)} total -> {len(conus)} CONUS")

    rows: dict[str, str] = {}
    for s in conus:
        fips = str(s["id"]).zfill(2)
        geom = shape(s["geometry"]).buffer(0)
        try:
            cells = h3.geo_to_cells(geom, resolution)
        except Exception:
            cells = set()
            polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
            for p in polys:
                cells |= set(h3.geo_to_cells(p, resolution))
        for c in cells:
            rows.setdefault(c, fips)
        print(f"    {fips} {s['props'].get('name','?'):<22} {len(cells):>8,} cells")

    cells = list(rows)
    lat, lng = zip(*(h3.cell_to_latlng(c) for c in cells)) if cells else ((), ())
    df = pd.DataFrame({"h3": cells, "state_fips": [rows[c] for c in cells],
                       "lat": lat, "lng": lng})
    return df


def main() -> int:
    reg = yaml.safe_load((ROOT / "sources" / "registry.yml").read_text())
    res = reg["meta"]["grid"]["resolution"]
    print(f"building H3 grid, resolution {res} "
          f"(~{h3.average_hexagon_area(res, unit='km^2'):.2f} km2/cell)")

    df = build(res)
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"grid_r{res}.parquet"
    df.to_parquet(dest, index=False, compression="zstd")

    area = h3.average_hexagon_area(res, unit="km^2")
    print(f"\ncells: {len(df):,}")
    print(f"approx area covered: {len(df) * area:,.0f} km2 (CONUS is ~8,080,000)")
    print(f"wrote {dest}  ({dest.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
