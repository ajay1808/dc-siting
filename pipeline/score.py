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


def sum_within_radius(cells_xy, feat_xy, values, radius_km, clamp_total=None):
    """Total capacity/value inside a radius, log-compressed then normalized.

    Prefers an EXPLICIT clamp. Normalising to the observed 97th percentile
    makes the scale depend on the data, so one new gigawatt-scale project
    restates every other cell's score on the next refresh.
    """
    if len(feat_xy) == 0:
        return np.full(len(cells_xy), np.nan)
    ftree, ctree = cKDTree(feat_xy), cKDTree(cells_xy)
    pairs = ctree.query_ball_tree(ftree, r=radius_km * 1000)
    totals = np.array([values[p].sum() if p else 0.0 for p in pairs])
    lg = np.log1p(totals)
    if clamp_total:
        hi = np.log1p(float(clamp_total[1]))
    else:
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

def idw(cells_xy, feat_xy, values, k=6, power=2.0):
    """Inverse-distance-weighted interpolation from scattered stations.

    Climate normals are point observations, not a surface, so every cell gets
    a distance-weighted blend of its k nearest stations rather than the single
    nearest value (which produces visible Voronoi facets on the map).
    """
    if len(feat_xy) == 0:
        return np.full(len(cells_xy), np.nan)
    k = min(k, len(feat_xy))
    tree = cKDTree(feat_xy)
    dist, idx = tree.query(cells_xy, k=k)
    if k == 1:
        dist, idx = dist[:, None], idx[:, None]
    dist = np.maximum(dist, 1.0)              # avoid divide-by-zero at a station
    w = 1.0 / dist ** power
    return (values[idx] * w).sum(axis=1) / w.sum(axis=1)


def _cell_points(grid):
    """Shapely points for every cell centroid, built once and reused."""
    from shapely.geometry import Point as _Point
    if not hasattr(_cell_points, "_cache"):
        _cell_points._cache = [_Point(x, y)
                               for x, y in zip(grid["lng"], grid["lat"])]
    return _cell_points._cache


def polygon_join(df, grid, value_field=None):
    """Assign each cell the containing polygon's value, or a boolean mask.

    With value_field: returns float array (NaN where no polygon contains it).
    Without:          returns bool array, True where any polygon contains it.
    """
    from shapely import wkt as _wkt
    from shapely.strtree import STRtree

    sub = df if value_field is None else df[df[value_field].notna()]
    if sub.empty:
        return None
    geoms, vals = [], []
    for i, w in enumerate(sub["wkt"]):
        try:
            g = _wkt.loads(w)
        except Exception:
            continue
        # Only pay for repair when it is actually needed. buffer(0) on a large
        # invalid MultiPolygon costs seconds each, and GEOS "within" tests
        # against invalid geometry are pathologically slow -- that combination
        # stalled a full scoring run for 20+ minutes.
        if not g.is_valid:
            try:
                g = g.buffer(0)
            except Exception:
                continue
        if g.is_empty or not g.is_valid:
            continue
        geoms.append(g)
        if value_field is not None:
            vals.append(float(sub[value_field].iloc[i]))
    if not geoms:
        return None

    if value_field is None:
        # Returns the FRACTION of each cell covered, not a boolean.
        #
        # Binary any-overlap was wrong at this cell size: it zeroed The Dalles,
        # an operating Google campus, because the Columbia River Gorge scenic
        # area clips the cell -- even though no protected polygon contains the
        # centroid and the cell is 54% buildable. A 5 km2 hex that is 10%
        # national park is not unbuildable; one that is 95% park is.
        #
        # Rasterising then block-averaging gives coverage fraction cheaply.
        # GEOS "within" against PAD-US multipolygons with millions of vertices
        # is minutes; this is seconds and cannot change the answer materially.
        from rasterio.features import rasterize
        from rasterio.transform import from_origin

        FINE = 0.004                      # ~440 m burn
        K = 6                             # -> ~2.6 km blocks, about one cell
        west, south, east, north = -125.0, 24.0, -66.0, 50.0
        w = int((east - west) / FINE)
        h = int((north - south) / FINE)
        burned = rasterize(((g, 1) for g in geoms), out_shape=(h, w),
                           transform=from_origin(west, north, FINE, FINE),
                           fill=0, dtype="uint8", all_touched=False)
        h2, w2 = h // K, w // K
        frac = (burned[:h2 * K, :w2 * K]
                .reshape(h2, K, w2, K).mean(axis=(1, 3)).astype("float32"))
        del burned
        coarse = from_origin(west, north, FINE * K, FINE * K)
        inv = ~coarse
        lng = grid["lng"].to_numpy()
        lat = grid["lat"].to_numpy()
        col = np.floor(inv.a * lng + inv.b * lat + inv.c).astype(np.int64)
        row = np.floor(inv.d * lng + inv.e * lat + inv.f).astype(np.int64)
        ok = (row >= 0) & (row < h2) & (col >= 0) & (col < w2)
        out = np.zeros(len(lng), dtype="float32")
        out[ok] = frac[row[ok], col[ok]]
        return out

    # Rasterise the VALUE, same as the boolean path. Exact point-in-polygon
    # is fine for many small polygons but pathological for a few enormous
    # ones: eGRID's 27 continent-scale subregions make the STRtree bbox
    # prefilter useless, so nearly every one of 1.47M points runs a full
    # geometry test. Burning values at ~440 m cannot change the answer at a
    # 5 km2 cell and finishes in seconds.
    from rasterio.features import rasterize as _rasterize
    from rasterio.transform import from_origin as _from_origin

    FINE = 0.004
    west, south, east, north = -125.0, 24.0, -66.0, 50.0
    w = int((east - west) / FINE)
    h = int((north - south) / FINE)
    NODATA = np.float32(-9.99e30)
    # Ascending so that where polygons overlap (air_permitting designates the
    # same city for several pollutants) the WORST value is the one that lands.
    order = np.argsort(np.asarray(vals))
    shapes = ((geoms[i], float(vals[i])) for i in order)
    burned = _rasterize(shapes, out_shape=(h, w),
                        transform=_from_origin(west, north, FINE, FINE),
                        fill=float(NODATA), dtype="float32")
    inv = ~_from_origin(west, north, FINE, FINE)
    lng = grid["lng"].to_numpy(); lat = grid["lat"].to_numpy()
    col = np.floor(inv.a * lng + inv.b * lat + inv.c).astype(np.int64)
    row = np.floor(inv.d * lng + inv.e * lat + inv.f).astype(np.int64)
    ok = (row >= 0) & (row < h) & (col >= 0) & (col < w)
    out = np.full(len(lng), np.nan)
    sampled = np.full(len(lng), np.nan)
    sampled[ok] = burned[row[ok], col[ok]]
    good = np.isfinite(sampled) & (sampled > float(NODATA) / 2)
    out[good] = sampled[good]
    return out


def _unused_exact_polygon_join(geoms, vals, grid):
    """Kept for reference: the exact point-in-polygon path this replaced."""
    from shapely.strtree import STRtree
    tree = STRtree(geoms)
    pts = _cell_points(grid)
    ci, gi = tree.query(pts, predicate="within")

    out = np.full(len(pts), np.nan)
    varr = np.asarray(vals)
    seen = np.zeros(len(pts), dtype=bool)
    for i, j in zip(ci, gi):
        if not seen[i]:
            out[i] = varr[j]
            seen[i] = True
    return out


def _transform(vals, how):
    """Optional pre-normalisation transform. log1p for quantities spanning
    several orders of magnitude (queued MW runs from 0 to tens of GW per
    county), where a linear scale would crush everything but the top decile."""
    if how == "log1p":
        return np.log1p(np.clip(vals, 0, None))
    return vals


def _normalize(vals, clamp, invert):
    """Scale to 0..1 across an explicit clamp range.

    An explicit clamp (rather than the observed min/max) keeps the scale
    stable between refreshes -- otherwise one new outlier silently restates
    every other cell's score.
    """
    lo, hi = clamp if clamp else (np.nanmin(vals), np.nanmax(vals))
    if hi <= lo:
        return np.full(len(vals), np.nan)
    x = (np.clip(vals, lo, hi) - lo) / (hi - lo)
    return 1.0 - x if invert else x


def compute_layer_subscore(layer, cells_xy, grid):
    """Return (subscore array | None) for one registry layer."""
    lid = layer["id"]
    spec = layer.get("scoring")

    # --- raster layers: sample the value under each cell centroid ------------
    tif = INTERIM / f"{lid}.tif"
    if spec and tif.exists():
        import rasterio
        with rasterio.open(tif) as ds:
            band = ds.read(1).astype("float32")
            inv = ~ds.transform
            h, w = band.shape
        # rasterio's ds.sample() is a per-point Python generator: on 1.47M
        # cells it ran for 40+ minutes. Applying the inverse affine as array
        # maths and indexing the band directly does the same job in ~1 second.
        lng = grid["lng"].to_numpy()
        lat = grid["lat"].to_numpy()
        col = inv.a * lng + inv.b * lat + inv.c
        row = inv.d * lng + inv.e * lat + inv.f
        col = np.floor(col).astype(np.int64)
        row = np.floor(row).astype(np.int64)
        ok = (row >= 0) & (row < h) & (col >= 0) & (col < w)
        vals = np.full(len(lng), np.nan)
        vals[ok] = band[row[ok], col[ok]]
        vals[~np.isfinite(vals)] = np.nan
        method = spec.get("method", "normalize")
        print(f"      (raster sample covered {np.isfinite(vals).mean():.1%} of cells)")
        return _normalize(vals, spec.get("clamp"), method == "normalize_invert")

    # A layer may read another layer's data (the ISO queue yields both a supply
    # signal and a friction signal from one fetch).
    src = INTERIM / f"{layer.get('data_from', lid)}.parquet"
    if not spec or not src.exists():
        return None
    df = pd.read_parquet(src)
    if df.empty:
        return None

    # --- areal joins: county / state published statistics --------------------
    geom_kind = layer.get("geometry", "")
    if geom_kind in ("join_county", "join_state"):
        col = "county_fips" if geom_kind == "join_county" else "state_fips"
        if col not in grid.columns or "join_key" not in df.columns:
            return None
        vf = spec.get("value_field")
        cand = [c for c in df.columns if c not in ("join_key",)]
        if not vf:
            vf = next((c for c in ("price", "value", "score") if c in df.columns), None)
        if vf is None or vf not in df.columns:
            vf = next((c for c in cand if pd.api.types.is_numeric_dtype(df[c])), None)
        if vf is None:
            return None
        lut = df.dropna(subset=["join_key"]).set_index("join_key")[vf]
        vals = grid[col].map(lut).to_numpy(dtype=float)
        vals = _transform(vals, spec.get("transform"))
        method = spec.get("method", "normalize")
        if method == "ratio_normalize":
            return np.clip(vals, 0, 1)
        return _normalize(vals, spec.get("clamp"), method == "normalize_invert")

    # --- polygon layers: value of the containing polygon ---------------------
    if "wkt" in df.columns:
        vf = spec.get("value_field")
        if not vf or vf not in df.columns:
            return None
        out = polygon_join(df, grid, value_field=vf)
        if out is None:
            return None
        cov = np.isfinite(out).mean()
        # For some layers ABSENCE is meaningful, not missing. A cell in no
        # nonattainment area is in attainment -- the best possible value -- and
        # leaving it null dropped the factor for 91% of the country.
        fill = spec.get("fill_missing")
        if fill is not None:
            out = np.where(np.isfinite(out), out, float(fill))
            print(f"      (polygon join covered {cov:.1%}; "
                  f"remainder filled with {fill})")
        else:
            print(f"      (polygon join covered {cov:.1%} of cells)")
        method = spec.get("method", "normalize")
        return _normalize(out, spec.get("clamp"), method == "normalize_invert")

    if not {"lat", "lng"}.issubset(df.columns):
        return None

    feat_xy = project(df["lng"].to_numpy(), df["lat"].to_numpy())
    method = spec.get("method")

    if method == "distance_decay":
        wf = spec.get("weight_field")
        if wf and wf in df.columns:
            raw = pd.to_numeric(df[wf], errors="coerce").to_numpy(dtype=float)
            known = np.isfinite(raw) & (raw > 0)
            if known.any():
                hi = np.percentile(raw[known], 95)
                w = np.where(known, np.clip(raw / hi, 0.05, 1.0), np.nan)
            else:
                w = np.full(len(df), np.nan)
            # Untagged features get a CONSERVATIVE weight, not the median.
            # OSM taggers record voltage on big substations far more often than
            # on small ones, so the untagged population skews low -- using the
            # median promoted them to roughly 115 kV, inflating distribution
            # assets into transmission class. 0.25 is about sub-transmission.
            fallback = spec.get("untagged_weight", 0.25)
            w = np.where(np.isfinite(w), w, fallback)
        else:
            w = np.ones(len(df))
        return distance_decay(cells_xy, feat_xy, w,
                              spec.get("decay_km", 20), spec.get("max_km", 60))

    if method == "capacity_within_radius":
        vf = spec.get("value_field")
        vals = (pd.to_numeric(df[vf], errors="coerce").fillna(0).to_numpy()
                if vf and vf in df.columns else np.ones(len(df)))
        return sum_within_radius(cells_xy, feat_xy, vals, spec.get("radius_km", 50),
                                 spec.get("clamp_total_mw"))

    if method in ("idw", "idw_invert"):
        vf = spec.get("value_field")
        if not vf or vf not in df.columns:
            return None
        vals = pd.to_numeric(df[vf], errors="coerce").to_numpy(dtype=float)
        ok = np.isfinite(vals)
        if not ok.any():
            return None
        interp = idw(cells_xy, feat_xy[ok], vals[ok], k=spec.get("k", 6))
        # Regional point sets (CAISO/WEIM nodes cover only the West) must not
        # be extrapolated across the country: IDW always returns a value, so
        # without a cutoff every East Coast cell would inherit California
        # prices. Beyond max_km from the nearest point the layer is absent.
        mk = spec.get("max_km")
        if mk:
            d, _ = cKDTree(feat_xy[ok]).query(cells_xy, k=1)
            interp = np.where(d <= mk * 1000, interp, np.nan)
            print(f"      (idw within {mk} km covers {np.isfinite(interp).mean():.1%} of cells)")
        return _normalize(interp, spec.get("clamp"), method == "idw_invert")

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
        s = compute_layer_subscore(layer, cells_xy, grid)
        if s is None:
            continue
        layer_scores[layer["id"]] = s
        cov = float(np.isfinite(s).mean())
        print(f"  [layer] {layer['id']:<24} mean={np.nanmean(s):.3f} "
              f"max={np.nanmax(s):.3f} coverage={cov:6.1%}")

    def _first(a):
        """Take the first input that has a value, in declared order -- i.e. a
        preferred source with a fallback, not an average of the two."""
        out = np.full(a.shape[1], np.nan)
        for row in a:
            out = np.where(np.isfinite(out), out, row)
        return out

    combine_fn = {"max": np.fmax.reduce, "mean": lambda a: np.nanmean(a, axis=0),
                  "min": np.fmin.reduce, "product": lambda a: np.nanprod(a, axis=0),
                  "first": _first}

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

    # --- exclusions: multiply the weighted score ----------------------------
    excl_cfg = cfg.get("exclusions", {}) or {}
    by_id = {l["id"]: l for l in reg["layers"]}
    mult = np.ones(len(grid))
    for lid, rule in excl_cfg.items():
        # Raster-backed exclusion (e.g. wetland fraction from NLCD).
        etif = INTERIM / f"{lid}.tif"
        if etif.exists():
            import rasterio
            with rasterio.open(etif) as ds:
                band = ds.read(1).astype("float32")
                inv = ~ds.transform
                eh, ew = band.shape
            lngs = grid["lng"].to_numpy(); lats = grid["lat"].to_numpy()
            c2 = np.floor(inv.a * lngs + inv.b * lats + inv.c).astype(np.int64)
            r2 = np.floor(inv.d * lngs + inv.e * lats + inv.f).astype(np.int64)
            ok2 = (r2 >= 0) & (r2 < eh) & (c2 >= 0) & (c2 < ew)
            vals = np.full(len(lngs), np.nan)
            vals[ok2] = band[r2[ok2], c2[ok2]]
            mask = np.isfinite(vals) & (vals >= float(rule.get("threshold", 0.5)))
            m = float(rule.get("multiplier", 0.0))
            mult = np.where(mask, np.minimum(mult, m), mult)
            grid[f"x_{lid}"] = mask
            print(f"  [exclude] {lid:<22} {mask.sum():>9,} cells "
                  f"({mask.mean():5.1%})  multiplier={m}")
            continue

        layer = by_id.get(lid)
        src = INTERIM / f"{lid}.parquet"
        if layer is None or not src.exists():
            continue
        edf = pd.read_parquet(src)
        if "wkt" not in edf.columns:
            continue
        frac = polygon_join(edf, grid)
        if frac is None:
            continue
        m = float(rule.get("multiplier", 0.0))
        # Blend toward the exclusion multiplier in proportion to coverage:
        # 100% covered -> m, 0% -> untouched, linear between.
        cell_mult = 1.0 - frac * (1.0 - m)
        mult = np.minimum(mult, cell_mult)
        flagged = frac >= float(rule.get("flag_at", 0.5))
        grid[f"x_{lid}"] = flagged
        grid[f"xf_{lid}"] = np.round(frac, 3)
        print(f"  [exclude] {lid:<22} mean coverage {frac.mean():6.2%} | "
              f">={rule.get('flag_at',0.5):.0%} in {flagged.sum():,} cells "
              f"({flagged.mean():.1%})  multiplier={m}")
    total = total * mult

    # --- flags: surfaced, never folded into the score -----------------------
    for flag in cfg.get("flags", []):
        lid = flag.get("source")
        src = INTERIM / f"{lid}.parquet"
        if not src.exists():
            continue
        fdf = pd.read_parquet(src)
        if "wkt" in fdf.columns:
            field = flag.get("field")
            if field and field in fdf.columns:
                # A threshold flag must compare the polygon's VALUE. Using bare
                # membership here flagged every cell inside any basin -- 99.9%
                # of the grid -- instead of the genuinely water-stressed ones.
                vals = polygon_join(fdf, grid, value_field=field)
                fm = (np.isfinite(vals)
                      & (vals >= float(flag.get("threshold", 0)))) \
                    if vals is not None else None
            else:
                # polygon_join now returns coverage fraction, not a boolean.
                # Without this threshold the flag count came out fractional
                # (81,430.19 cells) and every partially-touched cell flagged.
                cov = polygon_join(fdf, grid)
                fm = (cov >= float(flag.get("threshold", 0.25))
                      if cov is not None else None)
        elif "join_key" in fdf.columns and flag.get("field"):
            field = flag["field"]
            if field not in fdf.columns:
                print(f"  [flag]    {flag['id']:<22} SKIPPED - "
                      f"'{field}' not in {lid}")
                continue
            col = ("county_fips" if by_id.get(lid, {}).get("geometry") == "join_county"
                   else "state_fips")
            lut = fdf.dropna(subset=["join_key"]).set_index("join_key")[field]
            vals = grid[col].map(lut).to_numpy(dtype=float)
            thr = float(flag.get("threshold", 0))
            # Some fields flag on a LOW value: the policy rubric scores
            # moratoria_active as 100 = no moratoria, so "flag it" means below.
            fm = (np.isfinite(vals) & (vals <= thr)
                  if flag.get("comparison") == "below"
                  else np.isfinite(vals) & (vals >= thr))
        else:
            continue
        if fm is None:
            continue
        grid[f"flag_{flag['id']}"] = fm
        print(f"  [flag]    {flag['id']:<22} {fm.sum():>9,} cells ({fm.mean():5.1%})")

    # Persist the multiplier itself. The map recomputes the weighted score
    # client-side when weights change, and without this it had no way to apply
    # exclusions -- Yosemite rendered as a developable ~16 instead of 0.
    grid["excl_mult"] = np.round(mult, 3)
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
