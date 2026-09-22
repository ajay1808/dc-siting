# DC Siting Command

Internal data-center siting tool. Open-source stack, open-government data,
static hosting, no backend.

Scores every ~5 km² hex cell in the continental US for data-center
suitability, and explains *why* each cell scored what it did.

## Status

| | |
|---|---|
| Grid | H3 resolution 7 — **1,467,441 CONUS cells** (~5.16 km² each) |
| Layers declared | 29 (all V1 = easy + medium tier) |
| Layers ingested | 26 |
| Factors live | **18 of 18** |
| Tile payload | 74.4 MB across 3 archives, largest 49 MB |

Scores are labelled PROVISIONAL until cooling, water, land and policy land.
The three heaviest factors (grid access, interconnection headroom, power
cost) are live, so rankings are already meaningful: the top states are
Indiana, Ohio, Texas, Illinois and Virginia, and known campuses score
Council Bluffs 72, New Albany 72, Omaha 71, Ashburn 67.

Santa Clara scores 38 with a power-cost subscore of 0.00. That is correct
behaviour, not a bug: California industrial power is the most expensive in
the country and the Valley's data centers exist for latency and legacy
reasons. The `edge_latency` profile ranks it far higher.

### Source substitutions made during ingest

| Layer | Registry primary | Actually used | Why |
|---|---|---|---|
| `transmission_lines` | EIA Energy Atlas | HIFLD Open FeatureServer | EIA's dcat feed does not expose the electric layers; HIFLD Open still serves transmission publicly (52,244 features). |
| `substations` | HIFLD | OpenStreetMap | HIFLD's national substation layer is no longer public — the only reachable copy holds 128 features, not ~80k. |
| `power_plants` | EIA v2 API | EIA-860M spreadsheet | The v2 API returns capacity but no coordinates. |
| `interconnection_queue` | LBNL "Queued Up" | EIA-860M *Planned* sheet | LBNL returns 403 to scripted clients. The 860M planned sheet is a narrower proxy and **understates queue contention**. |
| `retail_power_price` | EIA-861 by utility | EIA v2, state level | Utility service territory polygons are in the restricted HIFLD set. |
| `solar_wind_potential` | `developer.nrel.gov` | `developer.nlr.gov` | NREL became the National Laboratory of the Rockies; `nrel.gov` was retired 29 May 2026. Existing keys still work. |

## Architecture

```
sources/registry.yml   declarative manifest: every layer, source, licence, scoring rule
        |
pipeline/check.py      validate all endpoints before fetching anything
pipeline/grid.py       build the H3 CONUS grid
pipeline/ingest.py     fetch + normalize -> data/interim/<layer>.parquet
pipeline/score.py      reduce onto cells, weight, exclude -> data/out/scored.parquet
pipeline/tiles.py      3-tier multi-resolution PMTiles
        |
web/                   MapLibre GL + PMTiles, fully static
```

**Why this beats the reference product.** A commercial equivalent renders
GeoJSON as SVG via react-simple-maps, shipping 9–11 MB per layer into the
DOM and requiring a Supabase backend. This uses vector tiles over HTTP range
requests: no server, no database, and per-factor subscores are baked into the
tiles as integers, so switching weighting profiles is a GPU restyle with
**zero network requests**.

## Scoring

`total = 100 * Σ(wᵢ·subscoreᵢ) / Σ(wᵢ) * exclusion_multiplier`

Factors with no data for a cell drop out of both numerator and denominator —
missing data never reads as "unsuitable". Cells below
`min_factors_required` are marked provisional.

Four weighting profiles ship (`config/scoring.yml`): balanced, AI/HPC
training, edge/latency, and sustainability-weighted. All are recomputed
client-side from the same tiles.

Two deliberate modelling choices:

- **Poverty rate is a disclosure flag, not a scoring input.** Treating low
  income as a siting advantage is the pattern that generates environmental
  justice litigation and permit denials. It is surfaced for review, never
  silently rewarded.
- **Tribal land is a jurisdictional flag, not a penalty.** Development there
  is a sovereignty and consultation question, not a desirability question.

## Setup

See [docs/ACCOUNTS.md](docs/ACCOUNTS.md) — five free API keys, ~15 minutes.

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
brew install gdal tippecanoe duckdb pmtiles
cp .env.example .env    # then fill in keys
make check              # validate every source endpoint
make all                # grid -> ingest -> score -> tiles
make serve              # http://localhost:8099
```

`make serve` uses `http-server` specifically because PMTiles requires HTTP
range request support; Python's `http.server` does not provide it.

## Deployment

GitHub Actions builds and publishes to Pages monthly. The workflow hard-fails
if any tile exceeds Pages' 100 MB per-file limit or the site exceeds 1 GB,
rather than deploying something that 404s at runtime.

If tiles outgrow those limits, move the `.pmtiles` files to Cloudflare R2
(10 GB free, zero egress, range requests supported) and point the sources in
`web/src/app.js` at the R2 URL. Nothing else changes.


## Land cover: what it actually means here

Scored on **development cost and permitting risk**, not "developed vs not":

| Class | Score | Why |
|---|---|---|
| Developed, open space / low | 0.95 / 0.90 | Already serviced and disturbed; parcels big enough |
| Barren | 0.90 | Cheap, nothing to clear, no habitat |
| Shrub / grassland | 0.80 | Cheap clearing |
| Pasture / hay | 0.75 | Already disturbed agriculture |
| **Cultivated crops** | **0.50** | Looks ideal — flat, cleared — but prime-farmland conversion is the most locally contested change of use there is |
| Forest | 0.30–0.35 | Clearing cost, stormwater permitting, ESG exposure |
| **Developed, high intensity** | **0.20** | The *worst* developed class: no room, expensive land |
| Wetlands / open water | **exclusion** | Clean Water Act §404 — not a low score, a blocker |

Cells are scored by **areal fraction**, not the single class under the
centroid: at 5 km² "mostly cropland with 15% wetland" is a materially
different site from "all cropland".

## Security

The published site makes **no API calls** — every key is build-time only.
See [docs/SECURITY.md](docs/SECURITY.md); run `make audit` before publishing.


## Calibration

Weights are not hand-waved. `pipeline/calibrate.py` fits them against
`config/case_studies.yml`: **28 operating campuses** (Ashburn, Council Bluffs,
New Albany, Abilene, The Dalles …) that should score high, and **15 known-bad
sites** that should not.

The negatives do the real work. With positives alone an optimiser just
inflates whatever they happen to score well on, so the set deliberately spans
four failure modes: dense urban (great infrastructure, no room — the Manhattan
case), protected land, remote-no-grid, and hazard/terrain.

Guard rails, because 43 labelled points against 15 factors will overfit:
weights are non-negative, sum to 1, **floored at 0.015** so a real factor
cannot be zeroed, and regularised toward the hand-set prior.

Result: separation between good and bad sites improved 29.4 → 35.8, the worst
positive went 0.0 → 49.1, and the best negative fell 68.0 → 62.6.

Positives converge around 66 against a target of 80. The model genuinely
cannot reach 80 for every real campus — several sit in mediocre cells on some
factors — and the target was left honest rather than moved to flatter the fit.

Run `python pipeline/calibrate.py` to see the per-case table without changing
anything; add `--apply` to write the weights.


## Variable audit (2026-09-21)

Checked every variable for scale and semantics. Two findings mattered.

**Transmission is genuinely transmission.** Minimum 100 kV at the 1st
percentile, zero features below 69 kV, `VOLT_CLASS` spanning 100-161 up to
735+. No distribution contamination.

**Substations were 40% distribution.** OSM's `power=substation` covers
everything from a 500 kV transmission bus to a 13.8 kV pole-mounted
neighbourhood transformer, and the original ingest captured neither the
`substation=*` subtag nor a voltage floor. Worse, 25% had no voltage tag and
the scorer assigned them the *median* of known values — promoting distribution
assets to roughly 115 kV. Now the subtag is captured, `distribution`,
`minor_distribution`, `traction` and anything under 35 kV are dropped
(**30,791 of 77,794 removed**), and untagged features get a conservative
0.25 weight instead of the median.

Also fixed:

| Issue | Was | Now |
|---|---|---|
| Hazard clamp | `[0,60]` pinned 13.4% of counties at exactly 0 — all of hurricane Florida and wildfire California indistinguishable | `[0,95]` |
| Capacity normalisation | observed 97th percentile, so one new gigawatt project restated every cell | explicit `clamp_total_mw` |
| Gas pipelines | included gathering lines (raw wellhead gas, not deliverable) | dropped |
| `weight_curve` | declared in the registry, never read by any code | removed |

**Exclusions were missing from the rendered map.** The client recomputes the
weighted score when weights change, but had no access to the exclusion
multiplier — so Yosemite rendered as a developable ~16 instead of 0. The
multiplier is now baked per cell as `xm` and applied in both the GPU
expression and the JS recompute.


## Energy-analyst gap review (2026-09-22)

Four layers added, chosen for decision-changing value rather than novelty.
Full gap analysis including paid options in [RECOMMENDATIONS.md](RECOMMENDATIONS.md).

| Layer | Why |
|---|---|
| **Air permitting burden** | A campus needs 50-200 MW of backup generation. Inside a nonattainment area that plant triggers New Source Review, may need emission offsets bought in the same airshed, and faces caps on test-run hours. Weighted by classification and by whether the pollutant is one gensets actually emit. |
| **Brownfield interconnection** | A retiring thermal plant leaves an energised POI with transmission already sized. Currently the fastest route to large load in the US. Weighted by recency. |
| **Grid carbon intensity** | eGRID subregion CO2e/MWh before any PPA — 243 to 1,549 lb/MWh. |
| **Military installations** | Footprints are not developable (hard exclusion, 1.1% of cells). |

**The calibration independently upweighted air permitting** (0.040 → 0.067)
without being told to, which is the case studies confirming it is real signal
rather than a hypothesis.

It also drove **cooling, water and IXP proximity to the weight floor**. That is
worth stating plainly: real campuses sit in Prineville *and* Phoenix, so those
factors do not discriminate between sites that actually got built. Either the
case set is too coarse to resolve them, or they matter less to revealed siting
behaviour than engineering intuition suggests. The floor keeps them in the
model rather than letting a 43-point fit delete them.
