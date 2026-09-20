#!/usr/bin/env python3
"""Stream scored H3 cells into a PMTiles archive via tippecanoe.

PMTiles is the whole reason this can be a static site: one file, served over
plain HTTP range requests, no tile server and no database at runtime. That is
what lets the finished tool live on GitHub Pages for $0.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import h3
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "out"
TILES = ROOT / "tiles"


def aggregate(df: pd.DataFrame, factor_cols: list[str], parent_res: int,
              flag_cols: list[str] = (), excl_cols: list[str] = ()) -> pd.DataFrame:
    """Roll res-7 cells up to a coarser H3 parent, averaging scores.

    Low zooms must show a CONTINUOUS surface. Letting tippecanoe thin 1.5M
    tiny hexes with --drop-densest-as-needed instead produces a speckled
    scatter of survivors, which reads as missing data rather than as a
    choropleth -- and breaks point queries, since most cells simply are not
    there. Aggregating preserves both the visual surface and hit-testing.
    """
    d = df.copy()
    d["parent"] = [h3.cell_to_parent(c, parent_res) for c in d["h3"]]
    agg = {"score": "mean", "factors_used": "max", "state_fips": "first"}
    if "county_names" in d.columns:
        agg["county_names"] = "first"
    for c in factor_cols:
        agg[c] = "mean"
    # A parent cell inherits a flag if ANY child carries it -- the coarse view
    # should not quietly drop a constraint.
    for c in list(flag_cols) + list(excl_cols):
        agg[c] = "max"
    out = d.groupby("parent", as_index=False).agg(agg)
    return out.rename(columns={"parent": "h3"})


def features(df: pd.DataFrame, factor_cols: list[str],
             flag_cols: list[str] = (), excl_cols: list[str] = ()):
    for row in df.itertuples(index=False):
        d = row._asdict()
        boundary = h3.cell_to_boundary(d["h3"])
        # 4dp ~= 11 m, far finer than a 1.2 km hex needs, and materially
        # smaller on the wire than 5dp.
        ring = [[round(lng, 4), round(lat, 4)] for lat, lng in boundary]
        ring.append(ring[0])
        # Integers, not floats: MVT varint-encodes small ints in ~1 byte where
        # a float costs 8. Across 1.5M features x 14 factors this dominates
        # archive size. 0-100 is finer than the model's real precision anyway.
        props = {"h3": d["h3"], "score": int(round(d["score"])),
                 "st": d["state_fips"], "nf": int(d["factors_used"])}
        # Human-readable geography. MVT dictionary-encodes repeated strings per
        # tile, so ~3,100 distinct county names cost far less than they look.
        cn = d.get("county_names")
        if cn:
            props["cnames"] = cn
        for c in factor_cols:
            v = d.get(c)
            if v is not None and v == v:
                props[c.replace("f_", "")] = int(round(float(v) * 100))
        for c in flag_cols:
            if bool(d.get(c)):
                props["fl_" + c.replace("flag_", "")] = 1
        for c in excl_cols:
            if bool(d.get(c)):
                props[c] = 1
        yield {"type": "Feature",
               "geometry": {"type": "Polygon", "coordinates": [ring]},
               "properties": props}


def main() -> int:
    if not shutil.which("tippecanoe"):
        print("tippecanoe not found on PATH (try /opt/homebrew/bin)")
        return 1

    reg = yaml.safe_load((ROOT / "sources" / "registry.yml").read_text())
    res = reg["meta"]["grid"]["resolution"]
    src = OUT / "scored.parquet"
    if not src.exists():
        print("no scored.parquet - run pipeline/score.py first")
        return 1

    df = pd.read_parquet(src)
    df = df[df["score"].notna()].copy()
    factor_cols = [c for c in df.columns if c.startswith("f_")]
    flag_cols = [c for c in df.columns if c.startswith("flag_")]
    excl_cols = [c for c in df.columns if c.startswith("x_")]

    # Resolve every county a hex touches into readable names for the UI.
    if "county_all" in df.columns:
        lookup = (df.dropna(subset=["county_fips", "county_name"])
                    .drop_duplicates("county_fips")
                    .set_index("county_fips")["county_name"].to_dict())
        def names(cell_all):
            if not cell_all:
                return None
            out = [lookup.get(f) for f in str(cell_all).split(",")]
            return "|".join(sorted({n for n in out if n})) or None
        df["county_names"] = df["county_all"].map(names)

    print(f"tiling {len(df):,} cells, {len(factor_cols)} factors, "
          f"{len(flag_cols)} flags, {len(excl_cols)} exclusions")

    TILES.mkdir(parents=True, exist_ok=True)

    # (h3 resolution, tippecanoe min zoom, tippecanoe max zoom)
    TIERS = [(4, 3, 5), (6, 6, 8), (res, 9, res + 2)]
    total_mb = 0.0
    for tier_res, zmin, zmax in TIERS:
        tdf = (df if tier_res == res
               else aggregate(df, factor_cols, tier_res, flag_cols, excl_cols))
        dest = TILES / f"score_r{tier_res}.pmtiles"
        cmd = ["tippecanoe", "-o", str(dest), "--force", "-l", "score",
               f"-Z{zmin}", f"-z{zmax}",
               "--simplification=10", "--maximum-tile-bytes=900000",
               "--no-feature-limit", "--no-tile-size-limit",
               "--hilbert", "-q", "/dev/stdin"]
        print(f"\n[tier r{tier_res}] z{zmin}-{zmax}  {len(tdf):,} cells")

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
        try:
            for feat in features(tdf, factor_cols, flag_cols, excl_cols):
                proc.stdin.write(json.dumps(feat, separators=(",", ":")) + "\n")
            proc.stdin.close()
        except BrokenPipeError:
            print("   tippecanoe closed the pipe early")
        rc = proc.wait()
        if rc != 0:
            print(f"   tippecanoe exited {rc}")
            return rc
        mb = dest.stat().st_size / 1e6
        total_mb += mb
        flag = "ok" if mb < 90 else "OVER 100MB LIMIT"
        print(f"   -> {dest.name}  {mb:.1f} MB  [{flag}]")

    print(f"\ntotal tile payload: {total_mb:.1f} MB across {len(TIERS)} archives")
    print("GitHub Pages: 100 MB per FILE is the hard limit; per-file sizes above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
