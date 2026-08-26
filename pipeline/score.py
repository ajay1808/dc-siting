#!/usr/bin/env python3
"""Reduce source layers onto the H3 grid and compute the suitability score.

Design notes
------------
* All distance work happens in EPSG:5070 (Conus Albers, equal-area, metres)
  so that "within 40 km" means the same thing in Maine and in Arizona.
* Nearest-neighbour work uses a KD-tree rather than SQL: 1.47M cells against
  a few thousand features resolves in well under a second, where a spatial
  cross join would not.
* Every factor writes BOTH its subscore and stays in the output, so the map
  can explain any cell's total instead of just asserting it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from pyproj import Transformer
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data" / "interim"
OUT = ROOT / "data" / "out"

TO_ALBERS = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)


def project(lng, lat):
    x, y = TO_ALBERS.transform(np.asarray(lng), np.asarray(lat))
    return np.column_stack([x, y])


# --- scoring primitives -----------------------------------------------------

def distance_decay(cells_xy, feat_xy, weights, decay_km, max_km, k=24):
    """Best weighted feature wins: max over features of w * exp(-d/decay)."""
    if len(feat_xy) == 0:
        return np.full(len(cells_xy), np.nan)
    tree = cKDTree(feat_xy)
    k = min(k, len(feat_xy))
    dist, idx = tree.query(cells_xy, k=k, distance_upper_bound=max_km * 1000)
    if k == 1:
        dist, idx = dist[:, None], idx[:, None]
    valid = np.isfinite(dist)
    dist = np.where(valid, dist, np.inf)
    w = np.where(valid, weights[np.clip(idx, 0, len(weights) - 1)], 0.0)
    decayed = w * np.exp(-(dist / 1000.0) / decay_km)
    decayed = np.where(valid, decayed, 0.0)
    return np.clip(decayed.max(axis=1), 0, 1)


def sum_within_radius(cells_xy, feat_xy, values, radius_km):
    """Total capacity/value inside a radius, log-compressed then normalized."""
    if len(feat_xy) == 0:
        return np.full(len(cells_xy), np.nan)
    ftree, ctree = cKDTree(feat_xy), cKDTree(cells_xy)
    pairs = ctree.query_ball_tree(ftree, r=radius_km * 1000)
    totals = np.array([values[p].sum() if p else 0.0 for p in pairs])
    lg = np.log1p(totals)
    hi = np.percentile(lg[lg > 0], 97) if (lg > 0).any() else 1.0
    return np.clip(lg / hi, 0, 1) if hi > 0 else np.zeros_like(lg)


def count_optimum(cells_xy, feat_xy, radius_km, optimum, falloff):
    """Some neighbours prove the fundamentals; too many mean grid contention."""
    if len(feat_xy) == 0:
        return np.full(len(cells_xy), np.nan)
    ftree, ctree = cKDTree(feat_xy), cKDTree(cells_xy)
    n = np.array([len(p) for p in ctree.query_ball_tree(ftree, r=radius_km * 1000)],
                 dtype=float)
    rise = np.clip(n / max(optimum, 1e-9), 0, 1)
    over = np.clip((n - optimum) / max(falloff, 1e-9), 0, None)
    return np.clip(rise * np.exp(-0.5 * over ** 2), 0, 1)


# --- factor computation -----------------------------------------------------

def compute_layer_subscore(layer, cells_xy):
    """Return (subscore array | None) for one registry layer."""
    lid = layer["id"]
    spec = layer.get("scoring")
    src = INTERIM / f"{lid}.parquet"
    if not spec or not src.exists():
        return None
    df = pd.read_parquet(src)
    if df.empty or not {"lat", "lng"}.issubset(df.columns):
        return None

    feat_xy = project(df["lng"].to_numpy(), df["lat"].to_numpy())
    method = spec.get("method")

    if method == "distance_decay":
        wf = spec.get("weight_field")
        if wf and wf in df.columns:
            raw = pd.to_numeric(df[wf], errors="coerce").fillna(0).to_numpy()
            hi = np.percentile(raw[raw > 0], 95) if (raw > 0).any() else 1.0
            w = np.clip(raw / hi, 0.05, 1.0) if hi > 0 else np.ones(len(df))
        else:
            w = np.ones(len(df))
        return distance_decay(cells_xy, feat_xy, w,
                              spec.get("decay_km", 20), spec.get("max_km", 60))

    if method == "capacity_within_radius":
        vf = spec.get("value_field")
        vals = (pd.to_numeric(df[vf], errors="coerce").fillna(0).to_numpy()
                if vf and vf in df.columns else np.ones(len(df)))
        return sum_within_radius(cells_xy, feat_xy, vals, spec.get("radius_km", 50))

    if method == "count_within_radius":
        return count_optimum(cells_xy, feat_xy, spec.get("radius_km", 50),
                             spec.get("optimum_count", 5), spec.get("falloff", 20))
    return None


def main() -> int:
    reg = yaml.safe_load((ROOT / "sources" / "registry.yml").read_text())
    cfg = yaml.safe_load((ROOT / "config" / "scoring.yml").read_text())
    res = reg["meta"]["grid"]["resolution"]

    grid = pd.read_parquet(OUT / f"grid_r{res}.parquet")
    print(f"grid: {len(grid):,} cells (H3 r{res})")
    cells_xy = project(grid["lng"].to_numpy(), grid["lat"].to_numpy())

    layer_scores: dict[str, np.ndarray] = {}
    for layer in reg["layers"]:
        s = compute_layer_subscore(layer, cells_xy)
        if s is None:
            continue
        layer_scores[layer["id"]] = s
        cov = float(np.isfinite(s).mean())
        print(f"  [layer] {layer['id']:<24} mean={np.nanmean(s):.3f} "
              f"max={np.nanmax(s):.3f} coverage={cov:6.1%}")

    combine_fn = {"max": np.fmax.reduce, "mean": lambda a: np.nanmean(a, axis=0),
                  "min": np.fmin.reduce, "product": lambda a: np.nanprod(a, axis=0)}

    factor_vals, weights = {}, {}
    for fid, f in cfg["factors"].items():
        avail = [layer_scores[i] for i in f["inputs"] if i in layer_scores]
        if not avail:
            continue
        arr = np.vstack(avail)
        factor_vals[fid] = (arr[0] if len(avail) == 1
                            else combine_fn[f.get("combine", "mean")](arr))
        weights[fid] = f["weight"]

    if not factor_vals:
        print("\nno factors computable yet - ingest more layers first")
        return 1

    print(f"\nfactors available: {len(factor_vals)}/{len(cfg['factors'])}"
          f"  ({', '.join(factor_vals)})")

    stack = np.vstack([factor_vals[f] for f in factor_vals])
    wvec = np.array([weights[f] for f in factor_vals])[:, None]
    present = np.isfinite(stack)
    wsum = (np.where(present, wvec, 0)).sum(axis=0)
    total = np.where(wsum > 0,
                     np.nansum(np.where(present, stack * wvec, 0), axis=0)
                     / np.where(wsum > 0, wsum, 1), np.nan)

    grid["score"] = np.round(total * 100, 1)
    grid["factors_used"] = present.sum(axis=0)
    for fid, v in factor_vals.items():
        grid[f"f_{fid}"] = np.round(v, 4)

    min_needed = cfg["missing_data"]["min_factors_required"]
    grid["provisional"] = grid["factors_used"] < min_needed

    dest = OUT / "scored.parquet"
    grid.to_parquet(dest, index=False, compression="zstd")
    s = grid["score"].dropna()
    print(f"\nscore distribution (PROVISIONAL - "
          f"{len(factor_vals)} of {len(cfg['factors'])} factors live):")
    for q in (5, 25, 50, 75, 95, 99):
        print(f"   p{q:<3} {np.percentile(s, q):6.1f}")
    print(f"   max  {s.max():6.1f}")
    print(f"\nwrote {dest} ({dest.stat().st_size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
