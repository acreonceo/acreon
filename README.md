# Terra: Maricopa Land Intelligence

A cloud-hosted, auto-refreshing service over every Maricopa County parcel. It runs
appreciation forecasting and an off-market acquisition-targets engine on free county
data. Nothing runs on your PC. The site, the database, and the weekly data pull all
live on a host.

## What is real vs. what you wire

Verified working against a live PostGIS instance: the schema, the spatial queries,
vector-tile generation, fuzzy search, the targets ranking, and the API serving all of
it plus the map frontend. The transform logic (owner classification, land-use
bucketing, tenure, scoring) runs and is tested.

Stubbed, because this had to be built off-network: the three data pulls in
`pipeline/ingest.py` (`fetch_parcels`, `fetch_sales`, `fetch_listings`). They point at
the real sources. Wire them on the host and nothing downstream changes.

## The 1.8M decisions

**All parcels go in the database; analytics run on the land subset.** Every parcel
(~1.8M) is stored so address/APN/owner search and comps work everywhere. The
acquisition-targets and forecasting logic only touches off-market vacant and
agricultural land, which is isolated by a partial index so those queries never scan the
full county.

**Bulk ingestion, not REST pagination.** Pulling 1.8M parcels by hammering the ArcGIS
query endpoint is slow and fragile. `ingest.py` is built to load bulk files, COPY them
into a staging table, and upsert in one transaction. Zone assignment and scoring happen
in SQL so 1.8M rows never round-trip through Python.

**The map is vector tiles, not an embedded file.** The old single-file tool embedded
the parcels inline. That breaks past a few thousand. The frontend now renders
`ST_AsMVT` tiles from PostGIS with zoom-based level of detail: zoomed out it shows only
meaningful acreage, zoomed in it shows everything.

**Ownership for the full county points to the paid file.** You cannot API-call 1.8M
parcels for owner names, and the free downloads do not cover ownership comprehensively
for land. The Assessor Secured Master (about $500) is the realistic full-county
ownership source. Sale price and tenure still come free from the Sales Affidavits file.
Start free on a few ZIPs to validate; buy the Secured Master when you go county-wide.

## Data sources (see build_parcels.py for the full registry)

- Parcels + geometry + land use: Assessor GIS bulk / ArcGIS FeatureServer (free)
- Sale date + price (tenure, cost basis): Sales Affidavits R102 download (free)
- Ownership county-wide: Assessor Secured Master (~$500) or free downloads for a pilot
- Listings (optional for-sale flag): ARMLS RESO or Land.com, and note the IDX
  constraint. IDX is display-only, so listings are a display overlay, not a model input.

## Layout

```
db/schema.sql          PostGIS schema: zones, parcels, spatial + trigram + partial indexes
pipeline/transform.py  shared pure functions (classify, bucket, score)
pipeline/load_zones.py  loads ZCTA zones (run once, before parcels)
pipeline/ingest.py     full-county bulk ingest -> PostGIS (wire the 3 fetchers)
api/main.py            FastAPI: tiles, search, detail, targets, zones; serves the frontend
web/index.html         MapLibre client reading the API
Dockerfile             API + frontend image
docker-compose.yml     local: PostGIS + app
deploy/render.yaml     cloud: managed Postgres + web service + weekly ingest cron
```

## Run it locally

```
docker compose up -d db          # PostGIS; schema.sql auto-applies on first init
docker compose up -d app         # API + map at http://localhost:8080
# once the county fetchers are wired:
docker compose run --rm ingest   # loads zones, then parcels
```

## Deploy (via Claude Code on a networked machine)

This box cannot reach the county servers or stand up a public site, so the launch runs
where there is real internet.

1. Push the repo. Point Render at `deploy/render.yaml` (Blueprint).
2. On first DB create, enable extensions and apply the schema:
   `CREATE EXTENSION postgis; CREATE EXTENSION pg_trgm;` then run `db/schema.sql`.
3. Wire `pipeline/ingest.py` fetchers to the live sources (and drop the Secured Master
   credentials/path for full-county ownership).
4. The web service serves the map; the weekly cron refreshes the data.

## Refresh cadence

The source files update in batches, not real time, so the cron runs weekly. Ingest is
idempotent: it upserts, so a re-run costs nothing and never duplicates.

## Cost

Small managed Postgres plus a web service and a cron runs roughly $15 to $40 a month at
this scale; the full 1.8M with geometry may push the database to a larger tier. The
Secured Master is a separate one-time-per-refresh data cost if you go county-wide on
ownership.

## Scale note

At 519 sample rows the query planner sometimes chooses a sequential scan over the
trigram or partial indexes because the table is tiny. At 1.8M rows those indexes engage,
which is what the schema is built for. The tile, viewport, and targets queries were
written and checked against the indexed paths.
