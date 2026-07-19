"""
load_zones.py
Loads the ZCTA zone polygons + growth signals into the `zones` table.
Run this ONCE before the first parcel ingest (parcels spatial-join to zones).
Input: scored_zctas.json produced by build_dataset.py.
"""
import os, json
import psycopg
from transform import zone_growth

DSN = os.environ.get("DATABASE_URL", "postgresql://terra:terra@localhost/terra")
SRC = os.environ.get("ZONES_JSON", "scored_zctas.json")

def run():
    feats = json.load(open(SRC))["features"]
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for z in feats:
            sig = z["signals"]
            cur.execute("""
                INSERT INTO zones (zcta, geom, signals, water_status, growth_default)
                VALUES (%s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)), %s::jsonb, %s, %s)
                ON CONFLICT (zcta) DO UPDATE SET
                  geom=EXCLUDED.geom, signals=EXCLUDED.signals,
                  water_status=EXCLUDED.water_status, growth_default=EXCLUDED.growth_default
            """, (z["zcta"], json.dumps(z["geometry"]), json.dumps(sig),
                  sig["water_status"], round(zone_growth(sig), 2)))
        conn.commit()
        cur.execute("SELECT count(*) FROM zones;")
        print("zones loaded:", cur.fetchone()[0])

if __name__ == "__main__":
    run()
