#!/usr/bin/env python3
"""Emit web/config.json: weights, provenance and plain-English meaning.

Single source of truth is config/scoring.yml plus config/factor_meta.yml, so
what the UI claims about a factor cannot drift from what the pipeline does.
"""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
cfg = yaml.safe_load((ROOT / "config" / "scoring.yml").read_text())
meta = yaml.safe_load((ROOT / "config" / "factor_meta.yml").read_text()) or {}
scored = ROOT / "data" / "out" / "scored.parquet"

live = []
if scored.exists():
    import pyarrow.parquet as pq
    cols = pq.ParquetFile(scored).schema.names
    live = [c[2:] for c in cols if c.startswith("f_")]

calib = ROOT / "config" / "scoring.calibrated.yml"
calibrated = {}
if calib.exists():
    calibrated = (yaml.safe_load(calib.read_text()) or {}).get("fit", {})

factors = {}
for k, v in cfg["factors"].items():
    m = meta.get(k, {})
    factors[k] = {
        "label": v["label"],
        "weight": v["weight"],
        "inputs": v.get("inputs", []),
        "source": m.get("source", "—"),
        "source_url": m.get("source_url"),
        "detail": (m.get("detail") or "").strip(),
        "means": " ".join((m.get("means") or "").split()),
        "caveat": " ".join((m.get("caveat") or "").split()) or None,
        "live": k in live,
    }

out = {
    "resolution": cfg["grid"]["resolution"],
    "factors": factors,
    "profiles": {
        "default": {"label": "Balanced (calibrated)",
                    "description": "Fitted against real campuses and known-bad sites.",
                    "weights": {k: v["weight"] for k, v in cfg["factors"].items()}},
        **{k: {"label": v["label"], "description": v.get("description", ""),
               "weights": v["weights"]}
           for k, v in cfg["profiles"].items()},
    },
    "live_factors": live,
    "flags": cfg.get("flags", []),
    "exclusions": {k: {"multiplier": v.get("multiplier"),
                       "reason": " ".join((v.get("reason") or "").split())}
                   for k, v in (cfg.get("exclusions") or {}).items()},
    "calibration": calibrated,
}
dest = ROOT / "web" / "config.json"
dest.write_text(json.dumps(out, indent=1))
print(f"wrote {dest}  factors={len(factors)} live={len(live)}")
