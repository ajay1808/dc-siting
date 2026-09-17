# Accounts & API Keys — everything you need to register for

All free. Nothing here requires a paid plan or a credit card.
Work top-down; the pipeline degrades gracefully and will skip layers whose
key is absent, so you can start the build before finishing this list.

When you have a value, put it in `.env` at the repo root (already gitignored):

```
EIA_API_KEY=...
CENSUS_API_KEY=...
NREL_API_KEY=...
PEERINGDB_API_KEY=...
ANTHROPIC_API_KEY=...
```

---

## 1. EIA — Energy Information Administration  *(required, 2 min)*
Unlocks: power plants, retail industrial electricity price, transmission,
substations, gas pipelines, balancing authorities.

1. Go to <https://www.eia.gov/opendata/register.php>
2. Enter name + email. Key is emailed instantly.
3. `EIA_API_KEY=` in `.env`

**This is the single highest-value key.** It also gives a public route to
transmission/substation data via the EIA Energy Atlas, which is what lets us
avoid the HIFLD access problem entirely (see #6).

---

## 2. US Census Bureau  *(required, 2 min)*
Unlocks: county demographics, poverty rate (EJ screening), population.

1. Go to <https://api.census.gov/data/key_signup.html>
2. Organization can be anything (e.g. "Internal Research").
3. Key emailed instantly — **click the activation link in the email**, it is
   not live until you do.
4. `CENSUS_API_KEY=` in `.env`

---

## 3. NREL Developer Network  *(recommended, 2 min)*
Unlocks: solar (NSRDB) and wind resource potential.

1. Go to <https://developer.nrel.gov/signup/>
2. Key shown on screen immediately.
3. `NREL_API_KEY=` in `.env`

Note: NREL's docs host was unreachable from this machine during endpoint
checks. If `developer.nrel.gov` also fails for you, it may be network-level;
this layer carries only 0.03 weight so it is safe to defer.

---

## 4. PeeringDB  *(recommended, 5 min)*
Unlocks: internet exchange points — the highest-value layer the commercial equivalent lacks.

1. Register at <https://www.peeringdb.com/register>
   - Personal/affiliate registration is fine; you do **not** need to affiliate
     with an organization for read access.
   - Account approval can take up to a day if you request org affiliation.
2. Once logged in: **Profile → API Keys → Add API Key** (read-only permission).
3. `PEERINGDB_API_KEY=` in `.env`

Anonymous access to `/api/ix` works at a lower rate limit, so the pipeline
will run without this — the key just makes refreshes faster.

---

## 5. Anthropic API  *(required only for the policy layer, 5 min)*
Unlocks: the AI-assembled state policy/legislation layer.

1. Go to <https://console.anthropic.com/>
2. Sign up, then **Settings → API Keys → Create Key**.
3. Add credit under **Billing** — the policy scan is cheap: ~50 states x a few
   research calls per month. Budget roughly $5–15/month.
4. `ANTHROPIC_API_KEY=` in `.env`

Your Claude Code subscription is separate and does **not** provide API
credits — this needs its own key.

---

## 6. HIFLD / ArcGIS  *(probably NOT needed — try last)*
Historically the source for transmission lines, substations, and electric
service territories. Access to the electric layers has been progressively
restricted to authorized government/industry users.

**Plan: skip this.** The pipeline's primary endpoints for those three layers
point at the EIA Energy Atlas instead, which is fully public. Only pursue
HIFLD if EIA coverage proves insufficient:

1. <https://hifld-geoplatform.opendata.arcgis.com/> — open layers need no login
2. Restricted layers require a HIFLD Secure account, which needs government or
   critical-infrastructure sponsorship. Not obtainable for general internal use.

Fallback if both fail: OpenStreetMap `power=line` / `power=substation` with
`voltage` tags. US coverage is now good, and the pipeline has an Overpass
path already wired for it.

---

## 7. GitHub  *(you have this)*
Needed for: hosting. Create an empty **public or private** repo named
`dc-siting`. Public is simpler — GitHub Pages on private repos requires a
paid plan.

```bash
gh repo create dc-siting --public --source=. --remote=origin
```

Then in the repo: **Settings → Pages → Source: GitHub Actions**.

---

## 8. Cloudflare R2  *(only if we exceed GitHub limits — decide later)*
Do **not** set this up yet. We will only need it if total tile output exceeds
what GitHub Pages comfortably serves (100 MB per file / ~1 GB per repo).
The pipeline reports total tile size at the end of every build, so we will
know before you have to decide.

If needed: <https://dash.cloudflare.com/> → R2 → free tier is 10 GB storage
with **zero egress fees**, which is the standard way to host PMTiles.

---

## Summary — do these five now

| # | Service | Time | Blocking? |
|---|---------|------|-----------|
| 1 | EIA | 2 min | Yes — most layers |
| 2 | Census | 2 min | Yes — demographics |
| 3 | NREL | 2 min | No |
| 4 | PeeringDB | 5 min | No |
| 5 | Anthropic | 5 min | Only policy layer |

Everything else in the registry — Climate TRACE, WRI Aqueduct, FEMA, USGS,
NPS/PAD-US, USFWS, BLM, FCC, NOAA, LBNL, OSM, EPA, BTS — needs **no account
at all**.
