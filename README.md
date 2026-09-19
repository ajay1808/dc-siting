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
| Layers ingested | 16 |
| Factors live | 11 of 15 — **scores are provisional** |
| Tile payload | 61.2 MB across 3 archives, largest 42 MB |

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
