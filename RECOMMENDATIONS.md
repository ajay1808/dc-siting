# Data gaps and what would close them

Written from the perspective of what an energy/siting analyst would actually
want. Split into **added**, **open-source gaps still worth doing**, and
**paid**, because the paid tier is where the genuinely decisive data lives and
it is worth being honest about that rather than pretending open data is
sufficient.

---

## Added in this pass

| Layer | Why it matters | Source |
|---|---|---|
| **Air permitting burden** | A campus needs 50–200 MW of backup generation. Inside a nonattainment area that plant triggers New Source Review, may need emission offsets bought in the same airshed, and faces hard caps on test-run hours. Almost no public siting map carries this. | [EPA Green Book](https://www.epa.gov/green-book) |
| **Brownfield interconnection** | A retiring thermal plant leaves an energised POI with transmission already sized. Re-using it is currently the fastest route to large load in the US. | EIA-860M, Retired sheet |
| **Grid carbon intensity** | CO2e/MWh before any PPA. Sets how much clean procurement a corporate commitment needs, and increasingly whether a project is approvable. | [EPA eGRID](https://www.epa.gov/egrid) |
| **Military installations** | Footprints are not developable. | DoD via ArcGIS |

---

## Open-source gaps still worth closing

Ordered by how much they would change an answer.

### 1. Real interconnection queue position — *the single biggest gap*
The model uses **planned generation additions** as a proxy. That is
generation-side and it understates contention badly. What actually decides a
project is the **load-side queue**: how many large-load requests are ahead of
you at that substation and what the study backlog looks like.

Each ISO/RTO publishes its own queue (PJM, MISO, ERCOT, SPP, CAISO, ISO-NE,
NYISO) in inconsistent formats. LBNL's *Queued Up* consolidates them but
returns 403 to scripted clients; the underlying data is on Zenodo and OEDI.
**This is where I would spend the next effort.**

### 2. Nodal wholesale prices (LMP)
Power cost currently joins at **state** level from EIA retail averages. Real
siting economics run on **nodal LMP** — basis differentials inside one state
routinely exceed the differences between states. ISOs publish day-ahead and
real-time LMP by node; the history is large but free.

### 3. Utility service territory + retail tariff
Which utility serves a parcel determines the tariff, the interconnection
process and who pays for upgrades. HIFLD's territory layer moved to restricted
access. EIA-861 has utility-level data without geometry; joining them is
doable but fiddly.

### 4. Transmission capacity and congestion
Proximity to a line says nothing about whether that line has **headroom**.
ISO congestion reports, ATC postings and NERC assessments would turn "near a
500 kV line" into "near a 500 kV line with capacity".

### 5. Water, properly
Currently WRI Aqueduct basin stress only. Missing:
- **USGS county water use** (no scriptable download found; manual)
- **Groundwater / aquifer depletion** (USGS)
- **US Drought Monitor**, weekly
- **Reclaimed water availability** — the preferred DC cooling source; EPA CWNS

### 6. Fiber routes
Currently a *proxy*: IXP proximity plus ACS household adoption. Actual
**long-haul fiber routes** are what matter. The academic *InterTubes* dataset
is the best open option; FCC's BDC has no scriptable path.

### 7. Labor
Construction and operations labor availability — electricians, millwrights,
DC technicians — is a real constraint on build schedule. BLS OES by metro is
free but returned 403 to scripted access; needs a manual pull.

### 8. Land cost
No proxy at all today. USDA NASS agricultural land values by county are free
via the QuickStats API and would give a usable floor. Actual parcel-level
pricing is paid (below).

### 9. Climate *projections*
Cooling and water are scored on **historical normals**. A 30-year asset should
be screened against projected wet-bulb and projected water stress — WRI
Aqueduct publishes future scenarios; NOAA/NCA publish downscaled projections.

### 10. Smaller additions
- FEMA NFHL floodplain polygons (registry entry exists; NRI only gives county risk)
- EPA brownfields / Superfund (redevelopment credits, pre-existing service)
- FAA airports and obstruction surfaces
- Opportunity Zones
- Wind resource (currently solar only — wind is too spatially spiky for the coarse sampling used)
- Rail access for heavy equipment
- State RPS / clean energy standards

---

## Paid data — where the decisive information actually is

Deliberately excluded from the build. Listed so the tradeoff is explicit.

| What | Why it beats the free version | Vendors |
|---|---|---|
| **Parcel boundaries & ownership** | The model scores 5 km² hexes. Real siting needs *this parcel*, its owner, acreage and assessed value. This is the biggest single step-change available. | Regrid, CoreLogic, ATTOM |
| **Data center inventory & pipeline** | OSM has 1,764 tagged US data centers; the real number including announced capacity is far higher, with MW, tenant and status. | DC Byte, Baxtel, datacenterHawk, 451 Research |
| **Utility interconnection capacity** | Actual available MW at a substation. Sometimes only obtainable by asking the utility directly. | Utility-specific; Grid Strategies, Enverus |
| **Nodal LMP history & forwards** | Curated, cleaned, with forward curves rather than raw ISO dumps. | Yes Energy, Velocity Suite (Hitachi), Enverus |
| **ASHRAE design conditions** | Design wet-bulb — the *right* variable for cooling. The model substitutes degree-days, which ignores humidity entirely. | ASHRAE handbook / licensed data |
| **Land cost & comparables** | Actual transaction comps rather than agricultural proxies. | CoStar, CBRE, JLL |
| **Fiber route & latency** | Carrier-level route geometry and measured latency. | TeleGeography, Cross River Fiber |
| **Environmental due diligence** | Phase I/II ESA, wetland delineation, cultural resources — all site-specific fieldwork. | Regional consultants |

### If you bought only one thing
**Parcel data.** Every other paid feed refines a score; parcels change what the
tool *is* — from "this 5 km² area looks promising" to "these seven parcels
are the candidates, here is who owns them". That is the difference between a
screening map and a siting tool.

---

## Known limits of what is built

Repeated here so they are not buried:

- **Cooling** uses degree-days, not design wet-bulb — humidity is invisible.
- **Interconnection** uses planned generation, not the queue — understates contention.
- **Power cost** joins at state level — intrastate spread is lost.
- **Broadband** measures adoption, not availability.
- **Slope** is sampled at ~1.4 km — screens out mountains, says nothing about a parcel.
- **Policy** is LLM-assembled with mandatory citations, and should be verified before relying on it.
- **Grid carbon** is a subregion average; marginal emissions differ from average.
- Scores are **relative**, not absolute. A 70 means "better than most of the country on these weights", not "viable".
