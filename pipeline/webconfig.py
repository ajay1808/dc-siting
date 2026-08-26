#!/usr/bin/env python3
"""Emit web/config.json so the map's profile weights stay in sync with
config/scoring.yml. Single source of truth: the YAML."""
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
cfg = yaml.safe_load((ROOT / "config" / "scoring.yml").read_text())
scored = ROOT / "data" / "out" / "scored.parquet"

live = []
if scored.exists():
    import pandas as pd
    live = [c[2:] for c in pd.read_parquet(scored, columns=None).columns
            if c.startswith("f_")]

out = {
    "resolution": cfg["grid"]["resolution"],
    "factors": {k: {"label": v["label"], "weight": v["weight"]}
                for k, v in cfg["factors"].items()},
    "profiles": {"default": {"label": "Balanced (default)",
                             "description": "Registry default weighting.",
                             "weights": {k: v["weight"] for k, v in cfg["factors"].items()}},
                 **{k: {"label": v["label"],
                        "description": v.get("description", ""),
                        "weights": v["weights"]}
                    for k, v in cfg["profiles"].items()}},
    "live_factors": live,
    "flags": cfg.get("flags", []),
}
dest = ROOT / "web" / "config.json"
dest.write_text(json.dumps(out, indent=1))
print(f"wrote {dest}  profiles={list(out['profiles'])}  live_factors={live}")
