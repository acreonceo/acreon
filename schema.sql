-- Terra land-intelligence schema (PostGIS)
-- Holds all ~1.8M Maricopa parcels for search + comps.
-- Analytics (targets, forecasting) run on the vacant/ag subset via a partial index.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy search on address/owner/apn

-- ---------------------------------------------------------------------------
-- ZONES: ZCTA polygons + growth signals. Small table (~130 rows).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zones (
  zcta          text PRIMARY KEY,
  geom          geometry(MultiPolygon, 4326) NOT NULL,
  signals       jsonb NOT NULL,            -- {developable_land, permit_velocity, ... water_status}
  water_status  text NOT NULL,
  growth_default numeric                    -- gated composite at default weights, for fast paths
);
CREATE INDEX IF NOT EXISTS zones_geom_gix ON zones USING gist (geom);

-- ---------------------------------------------------------------------------
-- PARCELS: the full county. Geometry is the parcel polygon (from Assessor GIS);
-- centroid is generated for point rendering and fast spatial joins.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parcels (
  apn           text PRIMARY KEY,
  geom          geometry(Geometry, 4326) NOT NULL,
  centroid      geometry(Point, 4326)
                  GENERATED ALWAYS AS (ST_PointOnSurface(geom)) STORED,
  zcta          text REFERENCES zones(zcta),
  situs_address text,
  city          text,
  use           text,                       -- Vacant | Agricultural | Improved
  acres         numeric,
  est           bigint,                      -- full cash value (market proxy)
  assessed      bigint,
  owner         text,
  owner_type    text,                        -- Individual | Trust/LLC | Investor | Builder/Developer
  absentee      boolean,
  acquired      int,                         -- NULL when no qualified sale
  tenure        int,                         -- NULL when no qualified sale
  paid          bigint,                      -- NULL on exempt transfer
  status        text NOT NULL DEFAULT 'Off-market',   -- flips to 'For sale' only via listings join
  list_price    bigint,
  growth_score  numeric,                     -- gated zone growth at default weights
  target_score  numeric,                     -- off-market acquisition score at default weights
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Spatial: polygon index drives map tiles; centroid index drives point queries.
CREATE INDEX IF NOT EXISTS parcels_geom_gix     ON parcels USING gist (geom);
CREATE INDEX IF NOT EXISTS parcels_centroid_gix ON parcels USING gist (centroid);

-- Attribute filters used by the map + targets tab.
CREATE INDEX IF NOT EXISTS parcels_zcta_ix   ON parcels (zcta);
CREATE INDEX IF NOT EXISTS parcels_use_ix    ON parcels (use);
CREATE INDEX IF NOT EXISTS parcels_status_ix ON parcels (status);

-- Search: trigram indexes make APN / address / owner lookups fast and fuzzy.
CREATE INDEX IF NOT EXISTS parcels_apn_trgm   ON parcels USING gin (apn gin_trgm_ops);
CREATE INDEX IF NOT EXISTS parcels_addr_trgm  ON parcels USING gin (situs_address gin_trgm_ops);
CREATE INDEX IF NOT EXISTS parcels_owner_trgm ON parcels USING gin (owner gin_trgm_ops);

-- The analytics population: off-market vacant/ag land, ranked by target score.
-- Partial index keeps the acquisition-targets query fast without scanning 1.8M rows.
CREATE INDEX IF NOT EXISTS parcels_targets_ix
  ON parcels (target_score DESC)
  WHERE status = 'Off-market' AND use IN ('Vacant', 'Agricultural');

-- Low-zoom map performance: index the land subset by acreage so we can drop
-- tiny/irrelevant parcels when zoomed out.
CREATE INDEX IF NOT EXISTS parcels_land_acres_ix
  ON parcels (acres DESC)
  WHERE use IN ('Vacant', 'Agricultural');

-- Owner mailing address (added later for outreach export). Idempotent so existing
-- databases pick it up on the next boot.
ALTER TABLE parcels ADD COLUMN IF NOT EXISTS mail_address text;

-- Rebuilt model (economist review): per-parcel water state and carry, zone-level
-- developer price. Idempotent so existing databases pick these up on boot.
ALTER TABLE parcels ADD COLUMN IF NOT EXISTS water_state text;
ALTER TABLE parcels ADD COLUMN IF NOT EXISTS carry_rate numeric;
ALTER TABLE zones   ADD COLUMN IF NOT EXISTS dev_value_per_acre numeric;
CREATE INDEX IF NOT EXISTS parcels_water_state_ix ON parcels (water_state);

-- Historical development events, used to reconstruct the frontier and fit the
-- conversion hazard. Lightweight: only what the estimation needs.
CREATE TABLE IF NOT EXISTS built (
  apn        text PRIMARY KEY,
  centroid   geometry(Point,4326) NOT NULL,
  const_year int NOT NULL,
  acres      numeric
);
CREATE INDEX IF NOT EXISTS built_centroid_gix ON built USING gist (centroid);
CREATE INDEX IF NOT EXISTS built_year_ix ON built (const_year);

-- Fitted model coefficients and run metadata.
CREATE TABLE IF NOT EXISTS model_fit (
  key text PRIMARY KEY,
  payload jsonb NOT NULL,
  fitted_at timestamptz NOT NULL DEFAULT now()
);

-- Parcel-level screens and fitted hazard.
ALTER TABLE parcels ADD COLUMN IF NOT EXISTS edge_miles numeric;
ALTER TABLE parcels ADD COLUMN IF NOT EXISTS hazard_fitted numeric;
ALTER TABLE parcels ADD COLUMN IF NOT EXISTS landlocked boolean;
ALTER TABLE parcels ADD COLUMN IF NOT EXISTS flood_zone text;
CREATE INDEX IF NOT EXISTS parcels_landlocked_ix ON parcels (landlocked);
