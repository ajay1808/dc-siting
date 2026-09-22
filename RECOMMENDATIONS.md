# Data gaps: what is closed, what is left, and what costs money

Written from an energy/siting analyst's point of view. The honest headline
up front: open data now covers most of what a screening model needs, but the
three things that most often *decide* a real project — the load
interconnection queue, the specific parcel, and the utility's actual
available capacity — are still not public.

---

## Closed in this pass

| Gap | How it was closed | Coverage | What still isn't right |
|---|---|---|---|
| **Interconnection queue** | Consolidated all ISO queues ourselves with the open-source [`gridstatus`](https://github.com/gridstatus/gridstatus) library instead of relying on LBNL | 13,390 projects, 1,406 counties, 654 GW active (6 ISOs; PJM with a free key) | Generation queue, not load queue. Non-ISO Southeast/West missing. |
| **Queue friction** | Withdrawal rate per county, shrunk toward the ISO average | 5 ISOs | ERCOT publishes no withdrawn projects, so Texas has no friction value (by design — a 0% would be an artifact) |
| **Nodal LMP** | CAISO OASIS day-ahead prices for every node, joined to node coordinates from CAISO's public price-contour feed; 24 days sampled across a year | ~14,600 CAISO + Western EIM nodes | West only. Other ISOs need node coordinates. |
| **Transmission congestion** | The congestion component of the same nodal LMP, kept signed (negative = export-constrained = good for new load) | Same | Same |
| **Utility territory + price** | EIA-861 industrial revenue/sales by utility, drawn on HIFLD service territories | 1,070 priced territories; state price as fallback | Average industrial price, not a negotiated large-load tariff |
| **Water, properly** | Added 2050 projected stress (Aqueduct), 10-year drought frequency (US Drought Monitor), reclaimed-water supply (EPA ECHO, 32,700 MGD across 4,356 plants) | National | No groundwater or county withdrawal data |
| **Climate projections** | NOAA/USGS CMRA county projections: 2036-2065 cooling degree days and days above 95°F, RCP4.5 | 3,233 counties | Still dry-bulb, not wet-bulb |
| **Fiber** | Class I rail + interstate rights-of-way (where InterTubes found long-haul fiber runs), plus submarine cable landings | National | A proxy for where fiber usually is, not a route map |
| **Labor** | Census County Business Patterns: electricians, line crews, mechanical trades, hosting workforce, within 80 km | 2,563 counties | Establishment location, not worker residence |
| **Land cost** | FHFA county land prices per acre | 2,444 counties | Residential land basis — relative signal only |

### Data problems caught while building these

Each of these would have silently produced a plausible but wrong map:

- **ERCOT's state field reads `"Texas"`, not `"TX"`.** Truncating to two
  characters dropped all 1,778 Texas projects — the largest data center
  market — from the queue layer. Queue coverage went from 353 GW to 654 GW
  after the fix.
- **ERCOT omits withdrawn projects from its published queue**, so its
  withdrawal rate is 0% by construction. Texas would have ranked as the
  lowest-friction market in the country.
- **EPA lists Erwin WWTP (NC) at 650,000 MGD** — twenty times all US
  municipal flow. Dropped with a plausibility cap; the national total then
  matches published figures (~32,700 MGD).
- **ECHO's default CSV omits latitude.** Columns now requested explicitly.
- **LMP clamps guessed before looking at the data** ([15, 90] $/MWh) would
  have squeezed every node into ~15% of the scale. Set from the observed
  distribution instead ($26–40).
- **IDW has no natural edge**: without a distance cutoff, western node
  prices would have been extrapolated onto every East Coast cell.

---

## What is still open — and exactly what it would take

### 1. The load interconnection queue — *still the biggest gap*
What decides a data center is how many large-load requests are ahead of
yours at a given substation. Utilities mostly do not publish this. ERCOT is
the exception (its Large Load Interconnection Status report). Everything
modelled here is generation-side.

### 2. Non-ISO generation queues (Southeast, much of the West)
Southern Company, TVA, Duke, and western utilities outside CAISO publish
queues only through individual OASIS pages. **LBNL's *Queued Up* file
already consolidates them.** It blocks scripted access, so it needs a
one-off manual download (annual). Drop it in `data/raw/lbnl/` and the
ingest can be wired to it — the schema changes yearly, so it should be
parsed against the actual file rather than guessed.

### 3. Nodal prices outside the West
The method works; the missing piece is node **coordinates**. Each ISO's
public price-contour map has to plot nodes somewhere, so each is a candidate
source: SPP (`pricecontourmap.spp.org`), MISO's market displays, ERCOT's
LMP contour map, PJM Data Viewer. Historical prices for SPP, MISO, NYISO
and ERCOT are already available keylessly through `gridstatus`.

### 4. Utility available capacity
Whether *this* substation can take 300 MW is usually knowable only by
asking the utility. Some publish hosting-capacity maps for distribution,
almost none for transmission-level load.

### 5. Smaller open additions still worth doing
- USGS groundwater levels and trends
- FEMA NFHL floodplain polygons (NRI gives county risk only)
- EPA brownfields / Superfund (redevelopment credits, existing service)
- FAA airports and obstruction surfaces
- NREL wind resource (solar only today)
- State RPS / clean-energy standards

---

## Parcels

### Recommendation: support upload (done), buy Regrid if buying anything

The tool now accepts parcel polygons from **any** source. Upload a
shapefile, GeoJSON or CSV and it computes real acreage, scores each parcel
across every cell it covers (area-weighted, not just its centroid), carries
owner / APN / land use / value through to the ranking and CSV export, and
filters by minimum acreage. Column names from Regrid, county assessors and
most GIS exports are recognised automatically.

That makes the source a separate decision from the tool:

**If you buy one national dataset: [Regrid](https://regrid.com).**
Nationwide coverage with a standardised schema — owner, parcel number,
acreage, land use, zoning where available, assessed value — so a
multi-state search does not mean reconciling fifty county formats. It
sells per-county as well as nationally, has an API, and runs a program for
academic and nonprofit users. Its field names (`owner`, `parcelnumb`,
`ll_gisacre`, `usedesc`, `parval`) are recognised by the upload directly.
Alternatives: LightBox (ReportAll), CoreLogic, ATTOM — generally
enterprise-priced.

**Free statewide parcel layers exist in several data-center states** —
worth checking before buying: Texas (TxGIO StratMap), Virginia (VGIN),
North Carolina (NC OneMap), Indiana (IndianaMap), Wisconsin (statewide
parcel map), Oregon (ORMAP), Washington, Utah (UGRC), Montana, Florida
(FDOR), Massachusetts (MassGIS), Arkansas. Verify current availability;
Ohio, Georgia, Arizona and Iowa are county-by-county.

---

## Paid data — where the decisive information actually is

| What | Why it beats the open version | Vendors |
|---|---|---|
| **Parcels** | Turns "this 5 km² area" into "these seven parcels, here is who owns them" | Regrid, LightBox, CoreLogic, ATTOM |
| **Data center inventory & pipeline** | OSM has ~1,760 tagged US facilities; the real count with MW, tenant and status is far higher | DC Byte, Baxtel, datacenterHawk, 451 Research |
| **Nodal LMP, all ISOs, cleaned** | History, forwards and node maps in one place | Yes Energy, Enverus, Hitachi Velocity Suite, gridstatus.io |
| **Interconnection intelligence** | Load queues, study results, upgrade cost allocations | Enverus, Grid Strategies, Interconnection.fyi |
| **ASHRAE design conditions** | Design wet-bulb — the variable that actually sizes cooling | ASHRAE |
| **Land comps** | Real transaction prices for industrial acreage | CoStar, brokerage research |
| **Fiber routes & latency** | Actual route geometry and measured latency | TeleGeography, carrier data |

### If you bought only one thing
**Parcels.** Every other feed refines a score. Parcels change what the tool
*is* — from a screening map into a siting tool.
