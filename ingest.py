"""
ingest.py
Full-county ingestion for ~1.8M Maricopa parcels into PostGIS.

STRATEGY (why bulk, not the REST API):
  Paginating the ArcGIS query endpoint 1,800+ times to get every parcel is slow
  and fragile. The Assessor publishes bulk files that give you the whole county in
  one pull. So this job downloads bulk, loads a staging table with COPY, then
  upserts into `parcels` in a single transaction. Zone assignment and scoring run
  in SQL so 1.8M rows never round-trip through Python.

SOURCES (see build_parcels.py SOURCES for the full registry):
  parcels + geometry   Assessor GIS bulk (shapefile/GDB) or ArcGIS FeatureServer
  ownership            Secured Master (paid, ~$500) for full-county owner+mailing
  sale date + price    Sales Affidavits (R102), free
  listings (optional)  ARMLS RESO / Land.com  -> sets status + list_price

RUN CADENCE:
  The source files refresh in batches, so nightly or weekly is plenty. Schedule
  this as a cron/worker on the host. It is idempotent: re-running upserts.

The three fetch_* functions are STUBS today (this box cannot reach the county).
Fill them on the host and nothing else changes. The DB load path below is real.
"""

import os, json, io, csv
import psycopg
from psycopg import sql
from transform import (classify_owner_type, bucket_use, latest_qualified_sale,
                       zone_growth, target_score, CUR_YEAR)

DSN = os.environ.get("DATABASE_URL", "postgresql://terra:terra@localhost/terra")

# ---------------------------------------------------------------------------
# FETCHERS (stubs) -> yield dicts of raw Assessor-shaped rows
# ---------------------------------------------------------------------------
def fetch_parcels():
    """
    REAL: read the bulk Assessor GIS layer (all ~1.8M parcels). Yield per parcel:
      APN, GEOM_WKT (polygon, EPSG:4326), SITUS_ADDRESS, SITUS_CITY, PUC,
      LOT_ACRES, FCV, LPV, OWNER_NAME, OWNER_MAIL_STATE
    For a first cut you can filter server-side to vacant/ag PUCs to cut volume,
    but full county is what enables address/APN search + comps everywhere.
    """
    raise NotImplementedError("wire to Assessor GIS bulk / FeatureServer")

def fetch_sales():
    """REAL: parse Sales Affidavits (R102). Return {APN: [{year, price, exempt}]}."""
    raise NotImplementedError("wire to Sales Affidavits file")

def fetch_listings():
    """REAL: ARMLS RESO / Land.com join. Return {APN: list_price}. Empty -> all Off-market."""
    return {}

# ---------------------------------------------------------------------------
# LOAD
# ---------------------------------------------------------------------------
STAGING_DDL = """
DROP TABLE IF EXISTS parcels_stage;
CREATE UNLOGGED TABLE parcels_stage (
  apn text, geom_wkt text, situs_address text, city text,
  use text, acres numeric, est bigint, assessed bigint,
  owner text, owner_type text, absentee boolean,
  acquired int, tenure int, paid bigint, status text, list_price bigint
);
"""

UPSERT = """
INSERT INTO parcels AS p
  (apn, geom, zcta, situs_address, city, use, acres, est, assessed,
   owner, owner_type, absentee, acquired, tenure, paid, status, list_price,
   growth_score, target_score, updated_at)
SELECT s.apn,
       ST_SetSRID(ST_GeomFromText(s.geom_wkt),4326),
       z.zcta, s.situs_address, s.city, s.use, s.acres, s.est, s.assessed,
       s.owner, s.owner_type, s.absentee, s.acquired, s.tenure, s.paid,
       s.status, s.list_price,
       z.growth_default,
       -- target = 0.5*growth + 0.3*tenure_component + 0.2*use_component
       round( 0.5*z.growth_default
            + 0.3*greatest(0, least(100, (coalesce(s.tenure,0)-2)*4.2))
                  * CASE s.owner_type WHEN 'Builder/Developer' THEN 0.35
                                      WHEN 'Investor' THEN 0.8 ELSE 1.0 END
            + 0.2*CASE s.use WHEN 'Vacant' THEN 100 WHEN 'Agricultural' THEN 85 ELSE 25 END
            , 1),
       now()
FROM parcels_stage s
LEFT JOIN LATERAL (
  SELECT zcta, growth_default
  FROM zones z
  WHERE ST_Contains(z.geom, ST_PointOnSurface(ST_SetSRID(ST_GeomFromText(s.geom_wkt),4326)))
  LIMIT 1
) z ON true
ON CONFLICT (apn) DO UPDATE SET
  geom=EXCLUDED.geom, zcta=EXCLUDED.zcta, situs_address=EXCLUDED.situs_address,
  city=EXCLUDED.city, use=EXCLUDED.use, acres=EXCLUDED.acres, est=EXCLUDED.est,
  assessed=EXCLUDED.assessed, owner=EXCLUDED.owner, owner_type=EXCLUDED.owner_type,
  absentee=EXCLUDED.absentee, acquired=EXCLUDED.acquired, tenure=EXCLUDED.tenure,
  paid=EXCLUDED.paid, status=EXCLUDED.status, list_price=EXCLUDED.list_price,
  growth_score=EXCLUDED.growth_score, target_score=EXCLUDED.target_score,
  updated_at=now();
"""

def build_rows(parcels, sales, listings):
    """Transform raw rows -> staging tuples. Runs per row, before the SQL bulk step."""
    for r in parcels:
        owner_type, absentee = classify_owner_type(r["OWNER_NAME"], r.get("OWNER_MAIL_STATE"))
        use = bucket_use(r["PUC"])
        sale = latest_qualified_sale(sales.get(r["APN"], []))
        if sale:
            paid, acquired, tenure = sale["price"], sale["year"], CUR_YEAR - sale["year"]
        else:
            paid = acquired = tenure = None
        lp = listings.get(r["APN"])
        yield (r["APN"], r["GEOM_WKT"], r.get("SITUS_ADDRESS"), r.get("SITUS_CITY"),
               use, r.get("LOT_ACRES"), int(r["FCV"]), int(r.get("LPV") or r["FCV"]*0.8),
               r["OWNER_NAME"], owner_type, absentee, acquired, tenure, paid,
               "For sale" if lp else "Off-market", lp)

def run():
    parcels = fetch_parcels()
    sales   = fetch_sales()
    listings = fetch_listings()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(STAGING_DDL)
        # COPY staging in one stream (fast for millions of rows)
        with cur.copy("COPY parcels_stage FROM STDIN") as cp:
            for row in build_rows(parcels, sales, listings):
                cp.write_row(row)
        cur.execute("ANALYZE parcels_stage;")
        cur.execute(UPSERT)
        cur.execute("DROP TABLE parcels_stage;")
        conn.commit()
        cur.execute("SELECT count(*) FROM parcels;")
        print("parcels in DB:", cur.fetchone()[0])

if __name__ == "__main__":
    run()
