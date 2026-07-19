"""
Terra API. Serves the map (vector tiles) and the analytics (search, detail, targets)
straight from PostGIS. Every SQL statement here was validated against a live
PostGIS instance before shipping.
"""
import os, json, pathlib
from fastapi import FastAPI, Response, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from psycopg_pool import ConnectionPool

DSN = os.environ.get("DATABASE_URL", "postgresql://terra:terra@localhost/terra")
pool = ConnectionPool(DSN, min_size=1, max_size=10, open=True)

app = FastAPI(title="Terra Land Intelligence API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- SELF-INITIALIZATION -----------------------------------------------------
# On first boot against an empty database: create extensions + tables (idempotent)
# and seed the demo parcels so the live site works immediately. When the real
# county ingest runs later, it upserts over this. All best-effort: if seeding
# fails the API still starts and simply serves an empty map.
_here = pathlib.Path(__file__).resolve().parent

def _init_db():
    import transform as T
    ddl = (_here / "schema.sql").read_text()
    stmts = [s.strip() for s in "\n".join(
        l for l in ddl.splitlines() if not l.strip().startswith("--")
    ).split(";") if s.strip()]
    with pool.connection() as c:
        for s in stmts:
            c.execute(s)
        c.commit()
        zn = c.execute("SELECT count(*) FROM zones").fetchone()[0]
        if zn == 0:
            zones = json.load(open(_here / "scored_zctas.json"))["features"]
            with c.cursor() as cur:
                for z in zones:
                    sig = z["signals"]
                    cur.execute(
                        "INSERT INTO zones(zcta,geom,signals,water_status,growth_default) "
                        "VALUES(%s,ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)),%s::jsonb,%s,%s) "
                        "ON CONFLICT (zcta) DO NOTHING",
                        (z["zcta"], json.dumps(z["geometry"]), json.dumps(sig),
                         sig["water_status"], round(T.zone_growth(sig), 2)))
            c.commit()
        pn = c.execute("SELECT count(*) FROM parcels").fetchone()[0]
        if pn == 0 and (_here / "parcels.json").exists():
            parcels = json.load(open(_here / "parcels.json"))
            zsig = {z: json.loads(s) for z, s in
                    c.execute("SELECT zcta, signals::text FROM zones").fetchall()}
            with c.cursor() as cur:
                for p in parcels:
                    sig = zsig.get(p["zcta"])
                    if not sig:
                        continue
                    g = T.zone_growth(sig)
                    ts = round(T.target_score(g, p.get("tenure"), p["owner_type"], p["use"]), 1)
                    cur.execute(
                        "INSERT INTO parcels(apn,geom,zcta,situs_address,city,use,acres,est,assessed,"
                        "owner,owner_type,absentee,acquired,tenure,paid,status,list_price,growth_score,target_score) "
                        "VALUES(%s,ST_SetSRID(ST_Point(%s,%s),4326),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (apn) DO NOTHING",
                        (p["apn"], p["lon"], p["lat"], p["zcta"], p["address"], p["city"], p["use"],
                         p["acres"], p["est"], p["assessed"], p["owner"], p["owner_type"],
                         p.get("absentee", False), p.get("acquired"), p.get("tenure"), p.get("paid"),
                         p["status"], p.get("list_price"), round(g, 2), ts))
            c.commit()

@app.on_event("startup")
def _startup():
    try:
        _init_db()
    except Exception as e:
        print("init/seed skipped:", e)

def q1(sql, params=None):
    with pool.connection() as c, c.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()

def qall(sql, params=None):
    with pool.connection() as c, c.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

@app.get("/healthz")
def healthz():
    n = q1("SELECT count(*) FROM parcels")[0]
    return {"ok": True, "parcels": n}

# --- MAP TILES -------------------------------------------------------------
# Zoom LOD: when zoomed out, only render meaningful land (drops built-out noise
# and keeps tiles small across 1.8M parcels).
TILE_SQL = """
WITH b AS (SELECT ST_TileEnvelope(%(z)s,%(x)s,%(y)s) g)
SELECT ST_AsMVT(t,'parcels') FROM (
  SELECT p.apn, p.use, p.status, p.acres, p.target_score, p.growth_score,
         ST_AsMVTGeom(ST_Transform(p.geom,3857), b.g) geom
  FROM parcels p, b
  WHERE ST_Transform(p.geom,3857) && b.g
    AND ( %(z)s >= 13
          OR (p.use IN ('Vacant','Agricultural')
              AND p.acres >= CASE WHEN %(z)s < 10 THEN 20
                                  WHEN %(z)s < 12 THEN 5 ELSE 1 END) )
) t
"""

@app.get("/tiles/{z}/{x}/{y}.mvt")
def tiles(z: int, x: int, y: int):
    row = q1(TILE_SQL, {"z": z, "x": x, "y": y})
    data = row[0] if row and row[0] else b""
    return Response(bytes(data), media_type="application/vnd.mapbox-vector-tile")

# Zones are only ~130 rows: serve as one GeoJSON for the choropleth backdrop.
@app.get("/zones")
def zones():
    row = q1("""
      SELECT jsonb_build_object(
        'type','FeatureCollection',
        'features', jsonb_agg(jsonb_build_object(
          'type','Feature',
          'geometry', ST_AsGeoJSON(geom)::jsonb,
          'properties', jsonb_build_object('zcta',zcta,'growth',growth_default,
                                           'water_status',water_status,'signals',signals)))
      ) FROM zones""")
    return Response(json.dumps(row[0]), media_type="application/json")

# --- SEARCH + DETAIL -------------------------------------------------------
@app.get("/parcels/search")
def search(q: str = Query(..., min_length=2), limit: int = 12):
    like = f"%{q}%"
    return qall("""
      SELECT apn, situs_address, city, zcta, use, acres, est, owner, status, list_price
      FROM parcels
      WHERE apn ILIKE %s OR situs_address ILIKE %s OR owner ILIKE %s OR city ILIKE %s
      ORDER BY est DESC NULLS LAST LIMIT %s
    """, (like, like, like, like, limit))

@app.get("/parcels/{apn}")
def parcel(apn: str):
    rows = qall("""
      SELECT p.*, z.signals AS zone_signals
      FROM parcels p LEFT JOIN zones z ON z.zcta = p.zcta
      WHERE p.apn = %s
    """, (apn,))
    if not rows:
        raise HTTPException(404, "parcel not found")
    r = rows[0]
    r.pop("geom", None); r.pop("centroid", None)   # geometry not needed in detail JSON
    return r

# --- ACQUISITION TARGETS ---------------------------------------------------
@app.get("/targets")
def targets(use: str = "", owner_type: str = "", min_acres: float = 0,
            min_tenure: int = 0, min_growth: float = 0, water_status: str = "",
            limit: int = 100):
    where = ["status='Off-market'", "use IN ('Vacant','Agricultural')",
             "acres >= %s", "coalesce(tenure,0) >= %s", "growth_score >= %s"]
    params = [min_acres, min_tenure, min_growth]
    if use:          where.append("use = %s");         params.append(use)
    if owner_type:   where.append("owner_type = %s");  params.append(owner_type)
    if water_status: where.append("zcta IN (SELECT zcta FROM zones WHERE water_status=%s)"); params.append(water_status)
    params.append(limit)
    return qall(f"""
      SELECT apn, situs_address, city, zcta, use, acres, owner, owner_type,
             tenure, acquired, paid, est, growth_score, target_score,
             ST_X(centroid) lon, ST_Y(centroid) lat
      FROM parcels
      WHERE {' AND '.join(where)}
      ORDER BY target_score DESC
      LIMIT %s
    """, tuple(params))

# Serve the MapLibre frontend (single file, same origin as the API).
_here = pathlib.Path(__file__).resolve().parent
@app.get("/")
def index():
    return FileResponse(_here / "index.html")
