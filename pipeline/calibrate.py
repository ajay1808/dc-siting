#!/usr/bin/env python3
"""Fit the default factor weights against hand-picked case studies.

Hand-set weights are just an opinion. This makes them answerable to reality:
proven campuses should score near `target.positive`, clearly-wrong sites near
`target.negative`, and the optimiser finds the non-negative weights that best
achieve that.

Guard rails, because 43 labelled points against 15 factors will overfit if you
let it:
  * weights are constrained non-negative and to sum to 1, so the result stays
    an interpretable weighting rather than a regression with sign flips;
  * the loss is regularised toward the hand-set prior by `prior_strength`, so
    the data nudges the weights rather than replacing them;
  * exclusion multipliers are applied exactly as in scoring, so an excluded
    negative cannot drag the fit around.

Writes config/scoring.calibrated.yml and a readable report. It does NOT
silently overwrite scoring.yml -- run with --apply for that.
"""
from __future__ import annotations

import sys
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import yaml
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "out"


def load_cases(grid: pd.DataFrame, factors: list[str], res: int):
    cs = yaml.safe_load((ROOT / "config" / "case_studies.yml").read_text())
    idx = grid.set_index("h3")
    xcols = [c for c in grid.columns if c.startswith("x_")]

    rows = []
    for label, key in (("pos", "positives"), ("neg", "negatives")):
        for c in cs[key]:
            cell = h3.latlng_to_cell(c["lat"], c["lng"], res)
            if cell not in idx.index:
                print(f"  ! {c['name']}: outside grid, skipped")
                continue
            r = idx.loc[cell]
            feats = [r.get(f"f_{f}", np.nan) for f in factors]
            # replicate the exclusion multiplier used in scoring
            mult = 1.0
            for xc in xcols:
                if bool(r.get(xc, False)):
                    mult = min(mult, 0.0 if xc == "x_protected_lands" else 0.15)
            rows.append({"name": c["name"], "kind": label, "mult": mult,
                         "feats": np.array(feats, dtype=float),
                         "why": c.get("why") or c.get("note", "")})
    return cs["targets"], rows


def score_of(w, feats, mult):
    present = np.isfinite(feats)
    if not present.any():
        return np.nan
    den = w[present].sum()
    if den <= 0:
        return np.nan
    return 100.0 * float((w[present] * feats[present]).sum() / den) * mult


def main() -> int:
    apply = "--apply" in sys.argv
    cfg = yaml.safe_load((ROOT / "config" / "scoring.yml").read_text())
    reg = yaml.safe_load((ROOT / "sources" / "registry.yml").read_text())
    res = reg["meta"]["grid"]["resolution"]

    grid = pd.read_parquet(OUT / "scored.parquet")
    factors = [f for f in cfg["factors"] if f"f_{f}" in grid.columns]
    missing = [f for f in cfg["factors"] if f not in factors]
    print(f"calibrating over {len(factors)} live factors"
          + (f" (not live, held at prior: {missing})" if missing else ""))

    targets, cases = load_cases(grid, factors, res)
    pos = [c for c in cases if c["kind"] == "pos"]
    neg = [c for c in cases if c["kind"] == "neg"]
    print(f"cases: {len(pos)} positive, {len(neg)} negative")

    w_prior = np.array([cfg["factors"][f]["weight"] for f in factors], dtype=float)
    w_prior = w_prior / w_prior.sum()
    tp, tn = float(targets["positive"]), float(targets["negative"])
    lam = float(targets.get("prior_strength", 0.25))

    P = np.vstack([c["feats"] for c in pos]); Pm = np.array([c["mult"] for c in pos])
    N = np.vstack([c["feats"] for c in neg]); Nm = np.array([c["mult"] for c in neg])

    def batch(w, F, M):
        pres = np.isfinite(F)
        Fz = np.where(pres, F, 0.0)
        den = (pres * w).sum(axis=1)
        den = np.where(den > 0, den, np.nan)
        return 100.0 * (Fz * w).sum(axis=1) / den * M

    def loss(w):
        sp = batch(w, P, Pm); sn = batch(w, N, Nm)
        lp = np.nanmean((sp - tp) ** 2)
        ln = np.nanmean((sn - tn) ** 2)
        # scaled so prior_strength ~1 genuinely competes with the data term
        return lp + ln + lam * 1e4 * float(((w - w_prior) ** 2).sum())

    cons = [{"type": "eq", "fun": lambda w: w.sum() - 1.0}]
    # Floor every weight. An unbounded fit drove network_latency to exactly
    # 0.000 and cooling to 0.005 -- that is 43 labelled points overfitting 15
    # factors, not a finding. A factor that is real should keep some weight
    # even when the case set cannot resolve it.
    bnds = [(0.015, 0.30)] * len(factors)
    best = minimize(loss, w_prior, method="SLSQP", bounds=bnds,
                    constraints=cons, options={"maxiter": 800, "ftol": 1e-9})
    w = np.clip(best.x, 0, None); w = w / w.sum()

    sp0, sn0 = batch(w_prior, P, Pm), batch(w_prior, N, Nm)
    sp1, sn1 = batch(w, P, Pm), batch(w, N, Nm)
    print(f"\n{'':<26}{'before':>18}{'after':>14}")
    print(f"{'positives mean':<26}{np.nanmean(sp0):>18.1f}{np.nanmean(sp1):>14.1f}   (target {tp:.0f})")
    print(f"{'negatives mean':<26}{np.nanmean(sn0):>18.1f}{np.nanmean(sn1):>14.1f}   (target {tn:.0f})")
    print(f"{'separation':<26}{np.nanmean(sp0)-np.nanmean(sn0):>18.1f}{np.nanmean(sp1)-np.nanmean(sn1):>14.1f}")
    worst = np.nanmin(sp1)
    print(f"{'worst positive':<26}{np.nanmin(sp0):>18.1f}{worst:>14.1f}")
    print(f"{'best negative':<26}{np.nanmax(sn0):>18.1f}{np.nanmax(sn1):>14.1f}")

    print("\nweight changes:")
    order = np.argsort(-(np.abs(w - w_prior)))
    for i in order:
        d = w[i] - w_prior[i]
        if abs(d) < 0.002:
            continue
        print(f"  {factors[i]:<24}{w_prior[i]:.3f} -> {w[i]:.3f}  ({d:+.3f})")

    print("\nper-case (after):")
    for c, s_before, s_after in sorted(
            list(zip(pos, sp0, sp1)) + list(zip(neg, sn0, sn1)),
            key=lambda t: -t[2]):
        tag = "+" if c["kind"] == "pos" else "-"
        print(f"  {tag} {c['name']:<26}{s_before:6.1f} -> {s_after:6.1f}   {c['why'][:34]}")

    dest = ROOT / "config" / "scoring.calibrated.yml"
    dest.write_text(yaml.safe_dump(
        {"calibrated_weights": {f: round(float(x), 4) for f, x in zip(factors, w)},
         "fit": {"positives_mean": round(float(np.nanmean(sp1)), 2),
                 "negatives_mean": round(float(np.nanmean(sn1)), 2),
                 "n_positive": len(pos), "n_negative": len(neg),
                 "prior_strength": lam}}, sort_keys=False))
    print(f"\nwrote {dest.relative_to(ROOT)}")

    if apply:
        txt = (ROOT / "config" / "scoring.yml").read_text()
        for f, x in zip(factors, w):
            import re
            txt = re.sub(rf"(  {f}:\n    label: [^\n]*\n    weight: )[0-9.]+",
                         rf"\g<1>{x:.4f}", txt)
        (ROOT / "config" / "scoring.yml").write_text(txt)
        print("applied to config/scoring.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
