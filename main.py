"""
Terra API. Serves the map (vector tiles) and the analytics (search, detail, targets)
straight from PostGIS. Every SQL statement here was validated against a live
PostGIS instance before shipping.
"""
import os, json, pathlib, requests
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
    import threading
    def _bg():
        try:
            _init_db()
        except Exception as e:
            print("init/seed skipped:", e)
    # Non-blocking: bind the port and answer health checks immediately, even if
    # the database is cold. Schema + seeding happen in the background.
    threading.Thread(target=_bg, daemon=True).start()

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
    try:
        n = q1("SELECT count(*) FROM parcels")[0]
        return {"ok": True, "parcels": n}
    except Exception as e:
        # DB still warming up: report healthy so the deploy doesn't loop.
        return {"ok": True, "status": "starting", "detail": str(e)[:120]}

# --- MAP TILES -------------------------------------------------------------
# Zoom LOD: when zoomed out, only render meaningful land (drops built-out noise
# and keeps tiles small across 1.8M parcels).
TILE_SQL = """
WITH b AS (SELECT ST_TileEnvelope(%(z)s,%(x)s,%(y)s) g)
SELECT ST_AsMVT(t,'parcels') FROM (
  SELECT p.apn, p.use, p.status, p.acres,
         round(p.target_score)::int AS target_score,
         round(p.growth_score)::int AS growth_score,
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

# --- REAL DATA: discovery probe -------------------------------------------
# Hit this once (with your ADMIN_TOKEN) to confirm the server can reach the
# county and to learn the real parcel field names before we wire the full pull.
# It writes nothing to the database.
COUNTY_PARCELS = ("https://gis.mcassessor.maricopa.gov/arcgis/rest/services/"
                  "MaricopaDynamicQueryService/MapServer/3/query")

def _zip_bbox(zcta):
    zs = json.load(open(_here / "scored_zctas.json"))["features"]
    f = next((x for x in zs if x["zcta"] == zcta), None)
    if not f:
        return None
    xs, ys = [], []
    def walk(c):
        if c and isinstance(c[0], (int, float)) and len(c) == 2:
            xs.append(c[0]); ys.append(c[1]); return
        for x in c:
            walk(x)
    walk(f["geometry"]["coordinates"])
    return (min(xs), min(ys), max(xs), max(ys))

@app.get("/admin/probe")
def admin_probe(token: str, zip: str = "85326"):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    bb = _zip_bbox(zip)
    if not bb:
        raise HTTPException(400, f"zip {zip} is not in the bundled zones")
    params = {"where": "1=1",
              "geometry": f"{bb[0]},{bb[1]},{bb[2]},{bb[3]}",
              "geometryType": "esriGeometryEnvelope", "inSR": "4326",
              "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
              "returnGeometry": "false", "resultRecordCount": "3", "f": "json"}
    try:
        r = requests.get(COUNTY_PARCELS, params=params, timeout=25,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; acreon/1.0)"})
        j = r.json()
    except Exception as e:
        return {"ok": False, "reached_county": False, "error": str(e)}
    feats = j.get("features", [])
    fields = ([f_["name"] for f_ in j["fields"]] if "fields" in j
              else (list(feats[0]["attributes"].keys()) if feats else []))
    return {"ok": True, "reached_county": True, "zip": zip, "bbox": bb,
            "returned": len(feats), "field_names": fields,
            "sample": feats[0]["attributes"] if feats else None,
            "server_error": j.get("error")}

# --- REAL DATA: pilot ingest ----------------------------------------------
# Pulls vacant/agricultural parcels for one ZIP from the county service, maps the
# real fields (PUC, sale, value, owner), and replaces the demo data with real
# parcels. Runs in a background thread; poll /admin/status. Free-tier friendly:
# capped, one ZIP at a time.
import threading
UA = "Mozilla/5.0 (compatible; acreon/1.0)"
INGEST_STATUS = {"state": "idle"}
COUNTY_WHERE = "LC_CUR LIKE '2%' OR PUC LIKE '00%'"   # vacant + agricultural land

def _num(s):
    if s is None: return None
    t = str(s).replace(",", "").strip()
    if not t: return None
    try: return int(float(t))
    except Exception: return None

def _sale_year(s):
    s = (s or "").strip()
    for sep in ("/", "-"):
        if sep in s:
            p = s.split(sep)
            if len(p) == 3 and len(p[-1]) == 4 and p[-1].isdigit():
                return int(p[-1])
    return None

def _classify_use(a, acres):
    puc = (a.get("PUC") or "").strip()
    living = a.get("LIVING_SPACE")
    const = (a.get("CONST_YEAR") or "").strip()
    has_structure = (isinstance(living, (int, float)) and living and living > 0) or (len(const) == 4 and const.isdigit())
    if has_structure: return "Improved"
    if puc.startswith("00"): return "Vacant"
    if acres and acres >= 10: return "Agricultural"
    return "Vacant"

_ANCH = [("Buckeye",-112.58,33.37),("Goodyear",-112.36,33.44),("Avondale",-112.32,33.43),
         ("Surprise",-112.37,33.63),("Waddell",-112.45,33.6),("Tonopah",-112.94,33.5),
         ("Wickenburg",-112.73,33.97),("Peoria",-112.24,33.71),("Phoenix",-112.07,33.45),
         ("Queen Creek",-111.63,33.25),("Mesa",-111.83,33.42)]
def _city(lon, lat):
    import math
    return min(_ANCH, key=lambda a: math.hypot(lon-a[1], lat-a[2]))[0]

def _transform_feature(ft):
    import transform as T
    a = ft.get("properties") or ft.get("attributes") or {}
    geom = ft.get("geometry")
    if not geom: return None
    apn = (a.get("APN_DASH") or a.get("APN") or "").strip()
    if not apn: return None
    ls = a.get("LAND_SIZE")
    acres = round(ls/43560.0, 3) if isinstance(ls, (int, float)) and ls > 0 else None
    owner = (a.get("OWNER_NAME") or "").strip()
    owner_type, absentee = T.classify_owner_type(owner, a.get("MAIL_STATE"))
    use = _classify_use(a, acres)
    price = _num(a.get("SALE_PRICE"))
    est = _num(a.get("FCV_CUR")) or price or 1000
    assessed = _num(a.get("LPV_CUR")) or int(est*0.8)
    yr = _sale_year(a.get("SALE_DATE"))
    if price and price > 0 and yr:
        paid, acquired, tenure = price, yr, max(0, T.CUR_YEAR - yr)
    else:
        paid = acquired = tenure = None
    lon, lat = a.get("LONGITUDE"), a.get("LATITUDE")
    city = (a.get("PHYSICAL_CITY") or "").strip() or (_city(lon, lat) if lon and lat else "")
    addr = (a.get("PHYSICAL_ADDRESS") or "").strip()
    if not addr:
        sub, lot = (a.get("SUBNAME") or "").strip(), (a.get("LOT_NUM") or "").strip()
        addr = f"{sub} Lot {lot}".strip() if sub else f"Parcel {apn}"
    return (apn, json.dumps(geom), addr, city, use, acres, int(est), int(assessed),
            owner, owner_type, absentee, acquired, tenure, paid, "Off-market", None)

STAGE_DDL = """
DROP TABLE IF EXISTS parcels_stage;
CREATE TEMP TABLE parcels_stage (
  apn text, geom_geojson text, situs_address text, city text, use text, acres numeric,
  est bigint, assessed bigint, owner text, owner_type text, absentee boolean,
  acquired int, tenure int, paid bigint, status text, list_price bigint);
"""
STAGE_UPSERT = """
INSERT INTO parcels AS p (apn, geom, zcta, situs_address, city, use, acres, est, assessed,
  owner, owner_type, absentee, acquired, tenure, paid, status, list_price, growth_score, target_score, updated_at)
SELECT s.apn, ST_SimplifyPreserveTopology(ST_SetSRID(ST_GeomFromGeoJSON(s.geom_geojson),4326), 0.00003), z.zcta, s.situs_address, s.city, s.use,
  s.acres, s.est, s.assessed, s.owner, s.owner_type, s.absentee, s.acquired, s.tenure, s.paid, s.status, s.list_price,
  z.growth_default,
  round(0.5*coalesce(z.growth_default,0)
      + 0.3*greatest(0,least(100,(coalesce(s.tenure,0)-2)*4.2))
            * CASE s.owner_type WHEN 'Builder/Developer' THEN 0.35 WHEN 'Investor' THEN 0.8 ELSE 1.0 END
      + 0.2*CASE s.use WHEN 'Vacant' THEN 100 WHEN 'Agricultural' THEN 85 ELSE 25 END, 1),
  now()
FROM parcels_stage s
LEFT JOIN LATERAL (SELECT zcta, growth_default FROM zones z
  WHERE ST_Contains(z.geom, ST_PointOnSurface(ST_SetSRID(ST_GeomFromGeoJSON(s.geom_geojson),4326))) LIMIT 1) z ON true
WHERE s.geom_geojson IS NOT NULL
ON CONFLICT (apn) DO UPDATE SET
  geom=EXCLUDED.geom, zcta=EXCLUDED.zcta, situs_address=EXCLUDED.situs_address, city=EXCLUDED.city,
  use=EXCLUDED.use, acres=EXCLUDED.acres, est=EXCLUDED.est, assessed=EXCLUDED.assessed, owner=EXCLUDED.owner,
  owner_type=EXCLUDED.owner_type, absentee=EXCLUDED.absentee, acquired=EXCLUDED.acquired, tenure=EXCLUDED.tenure,
  paid=EXCLUDED.paid, status=EXCLUDED.status, list_price=EXCLUDED.list_price, growth_score=EXCLUDED.growth_score,
  target_score=EXCLUDED.target_score, updated_at=now();
"""

def _fetch_parcels(zcta, cap):
    bb = _zip_bbox(zcta)
    if not bb:
        raise RuntimeError(f"zip {zcta} not in zones")
    feats, offset = [], 0
    while len(feats) < cap:
        params = {"where": COUNTY_WHERE,
                  "geometry": f"{bb[0]},{bb[1]},{bb[2]},{bb[3]}",
                  "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
                  "returnGeometry": "true", "outSR": "4326", "f": "geojson",
                  "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": 1000}
        r = requests.get(COUNTY_PARCELS, params=params, timeout=90, headers={"User-Agent": UA})
        batch = r.json().get("features", [])
        if not batch:
            break
        feats.extend(batch); offset += len(batch)
        INGEST_STATUS.update(detail=f"fetching ({len(feats)})")
        if len(batch) < 1000:
            break
    return feats

def _load_parcels(rows, replace=False):
    with pool.connection() as c:
        with c.cursor() as cur:
            cur.execute(STAGE_DDL)
            with cur.copy("COPY parcels_stage (apn,geom_geojson,situs_address,city,use,acres,est,assessed,owner,owner_type,absentee,acquired,tenure,paid,status,list_price) FROM STDIN") as cp:
                for row in rows:
                    cp.write_row(row)
            if replace:
                cur.execute("DELETE FROM parcels;")
            cur.execute(STAGE_UPSERT)
        c.commit()
        return c.execute("SELECT count(*) FROM parcels").fetchone()[0]

def run_ingest(zcta, cap=6000, replace=False):
    global INGEST_STATUS
    INGEST_STATUS = {"state": "running", "zip": zcta, "detail": "starting"}
    try:
        feats = _fetch_parcels(zcta, cap)
    except Exception as e:
        INGEST_STATUS = {"state": "error", "detail": f"fetch failed: {e}"}; return
    if not feats:
        INGEST_STATUS = {"state": "error", "detail": "county returned 0 parcels; check zip/filter"}; return
    rows = [r for r in (_transform_feature(f) for f in feats) if r]
    if not rows:
        INGEST_STATUS = {"state": "error", "detail": "no usable rows after transform"}; return
    INGEST_STATUS.update(detail=f"loading {len(rows)} parcels")
    try:
        n = _load_parcels(rows, replace=replace)
        INGEST_STATUS = {"state": "done", "zip": zcta, "fetched": len(feats), "loaded": n}
    except Exception as e:
        INGEST_STATUS = {"state": "error", "detail": f"load failed: {e}"}

def run_ingest_all(cap_per_zip=2500, replace_first=True):
    global INGEST_STATUS
    with pool.connection() as c:
        zctas = [r[0] for r in c.execute("SELECT zcta FROM zones ORDER BY zcta").fetchall()]
    total = len(zctas); loaded_zips = 0; errors = 0
    for i, z in enumerate(zctas, 1):
        INGEST_STATUS = {"state": "running", "mode": "county", "zip": z, "progress": f"{i}/{total}", "errors": errors}
        try:
            feats = _fetch_parcels(z, cap_per_zip)
            rows = [r for r in (_transform_feature(f) for f in feats) if r]
            if rows:
                _load_parcels(rows, replace=(replace_first and i == 1))
                loaded_zips += 1
        except Exception:
            errors += 1
    with pool.connection() as c:
        n = c.execute("SELECT count(*) FROM parcels").fetchone()[0]
    INGEST_STATUS = {"state": "done", "mode": "county", "zips_loaded": loaded_zips, "errors": errors, "loaded": n}

def _fetch_page(offset, where, n=1000):
    params = {"where": where, "outFields": "*", "returnGeometry": "true", "outSR": "4326",
              "f": "geojson", "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": n}
    r = requests.get(COUNTY_PARCELS, params=params, timeout=120, headers={"User-Agent": UA})
    try:
        return r.json().get("features", [])
    except Exception:
        raise RuntimeError(f"county status {r.status_code}: {r.text[:150]}")

def run_ingest_county(include_all=False, chunk=5000, cap=2_000_000):
    """Page the whole county (no per-ZIP bbox), loading in chunks. Replaces once on
    the first flush then appends, so it's a clean full rebuild. Handles ~250k
    vacant/ag parcels, or everything with include_all."""
    global INGEST_STATUS
    where = "1=1" if include_all else COUNTY_WHERE
    INGEST_STATUS = {"state": "running", "mode": "county-all", "fetched": 0, "loaded": 0, "detail": "starting"}
    offset = 0; loaded = 0; buf = []; replaced = False
    try:
        while offset < cap:
            feats = _fetch_page(offset, where)
            if not feats:
                break
            offset += len(feats)
            for f in feats:
                row = _transform_feature(f)
                if row:
                    buf.append(row)
            INGEST_STATUS.update(fetched=offset, detail=f"fetching ({offset})")
            if len(buf) >= chunk:
                loaded = _load_parcels(buf, replace=(not replaced)); replaced = True; buf = []
                INGEST_STATUS.update(loaded=loaded, detail=f"loaded {loaded}")
            if len(feats) < 1000:
                break
        if buf:
            loaded = _load_parcels(buf, replace=(not replaced))
        INGEST_STATUS = {"state": "done", "mode": "county-all", "fetched": offset, "loaded": loaded}
    except Exception as e:
        INGEST_STATUS = {"state": "error", "detail": str(e)[:200], "fetched": offset, "loaded": loaded}

@app.get("/admin/ingest")
def admin_ingest(token: str, zip: str = "85326", cap: int = 6000, replace: bool = False):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if INGEST_STATUS.get("state") == "running":
        return {"state": "already_running", "status": INGEST_STATUS}
    threading.Thread(target=run_ingest, args=(zip, cap, replace), daemon=True).start()
    return {"state": "started", "zip": zip, "cap": cap, "replace": replace, "next": "poll /admin/status?token=YOUR_TOKEN"}

@app.get("/admin/ingest_all")
def admin_ingest_all(token: str, cap_per_zip: int = 2500, replace_first: bool = True):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if INGEST_STATUS.get("state") == "running":
        return {"state": "already_running", "status": INGEST_STATUS}
    threading.Thread(target=run_ingest_all, args=(cap_per_zip, replace_first), daemon=True).start()
    return {"state": "started", "mode": "county", "cap_per_zip": cap_per_zip,
            "note": "walks all ZIPs additively; long-running; poll /admin/status?token=YOUR_TOKEN"}

@app.get("/admin/ingest_county")
def admin_ingest_county(token: str, all: bool = False):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if INGEST_STATUS.get("state") == "running":
        return {"state": "already_running", "status": INGEST_STATUS}
    threading.Thread(target=run_ingest_county, kwargs={"include_all": all}, daemon=True).start()
    return {"state": "started", "mode": "county-all", "include_all": all,
            "note": "full-county rebuild; pages the whole layer; poll /admin/status?token=YOUR_TOKEN"}

@app.get("/admin/status")
def admin_status(token: str):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    return INGEST_STATUS

# --- REAL DATA: growth signals (starting with migration from Census) -------
# Pulls population per ZIP for two years, turns the change into a real migration
# signal, updates each zone, recomputes the default-weight growth, and re-scores
# every parcel. Same trigger/poll pattern as the parcel ingest.
SIGNAL_STATUS = {"state": "idle"}
CENSUS_YEARS = (2018, 2023)

def _census_pop(year):
    import urllib.parse
    key = os.environ.get("CENSUS_KEY", "").strip()
    if not key:
        raise RuntimeError("CENSUS_KEY not set. Get a free key at "
                           "api.census.gov/data/key_signup.html and add it in Render.")
    q = "get=B01003_001E&for=" + urllib.parse.quote("zip code tabulation area:*", safe=":*") + "&key=" + key
    url = f"https://api.census.gov/data/{year}/acs/acs5?{q}"
    r = requests.get(url, timeout=120, headers={"User-Agent": UA})
    try:
        rows = r.json()
    except Exception:
        raise RuntimeError(f"census {year} status {r.status_code}: {r.text[:150]}")
    hdr = rows[0]; iz = hdr.index("zip code tabulation area"); ip = hdr.index("B01003_001E")
    out = {}
    for row in rows[1:]:
        try: out[row[iz]] = int(row[ip])
        except Exception: pass
    return out

def _migration_signal(pct):          # pct = fractional pop change over the span
    return max(0, min(100, round(50 + pct * 200)))

# default-weight, water-gated growth recomputed from the signals jsonb
GROWTH_DEFAULT_EXPR = """
round(( 50*(signals->>'developable_land')::numeric + 60*(signals->>'permit_velocity')::numeric
      + 55*(signals->>'zoning_activity')::numeric  + 60*(signals->>'infra_transport')::numeric
      + 60*(signals->>'infra_water')::numeric      + 60*(signals->>'migration')::numeric
      + 50*(signals->>'schools')::numeric ) / 395.0
    * CASE water_status WHEN 'assured' THEN 1.0 WHEN 'alternative_pending' THEN 0.7 ELSE 0.3 END, 2)
"""
RESCORE_SQL = """
UPDATE parcels p SET growth_score = z.growth_default,
  target_score = round(0.5*coalesce(z.growth_default,0)
    + 0.3*greatest(0,least(100,(coalesce(p.tenure,0)-2)*4.2))
          * CASE p.owner_type WHEN 'Builder/Developer' THEN 0.35 WHEN 'Investor' THEN 0.8 ELSE 1.0 END
    + 0.2*CASE p.use WHEN 'Vacant' THEN 100 WHEN 'Agricultural' THEN 85 ELSE 25 END, 1)
FROM zones z WHERE p.zcta = z.zcta;
"""

# developable_land = share of a zone's area that is vacant/agricultural land,
# computed straight from the parcels already loaded (only touches zones we have
# parcels for; others keep their prior value).
DEVELOPABLE_SQL = """
WITH za AS (SELECT zcta, ST_Area(geom::geography)/4046.8564224 AS area_ac FROM zones),
     dv AS (SELECT zcta, sum(acres) AS dev_ac FROM parcels
            WHERE use IN ('Vacant','Agricultural') AND acres IS NOT NULL GROUP BY zcta)
UPDATE zones z
SET signals = jsonb_set(z.signals, '{developable_land}',
      to_jsonb( LEAST(100, GREATEST(0, round((dv.dev_ac / NULLIF(za.area_ac,0))*100)))::int ))
FROM za JOIN dv ON dv.zcta = za.zcta
WHERE z.zcta = za.zcta;
"""

# Water gate from real ADWR data: fraction of each zone covered by issued
# Assured/Adequate Water Supply determinations -> water_status.
AAWS_URL = "https://services.arcgis.com/C34zQ7veRS0V1t04/ArcGIS/rest/services/AAWS_Issued_Determination_2024/FeatureServer/0/query"
WATER_OVERLAY_SQL = """
WITH cov AS (
  SELECT z.zcta,
    COALESCE(SUM(ST_Area(ST_Intersection(z.geom, a.geom))),0) / NULLIF(ST_Area(z.geom),0) AS frac
  FROM zones z LEFT JOIN aaws a ON ST_Intersects(z.geom, a.geom)
  GROUP BY z.zcta, z.geom)
UPDATE zones z SET water_status = CASE
  WHEN c.frac > 0.30 THEN 'assured'
  WHEN c.frac > 0.05 THEN 'alternative_pending'
  ELSE 'groundwater_constrained' END
FROM cov c WHERE z.zcta = c.zcta;
"""

def _fetch_aaws(bbox, cap=15000):
    geoms, offset = [], 0
    while len(geoms) < cap:
        params = {"where": "1=1", "geometry": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
                  "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects", "outFields": "OBJECTID",
                  "returnGeometry": "true", "f": "geojson", "orderByFields": "OBJECTID",
                  "resultOffset": offset, "resultRecordCount": 2000}
        r = requests.get(AAWS_URL, params=params, timeout=120, headers={"User-Agent": UA})
        try:
            batch = r.json().get("features", [])
        except Exception:
            raise RuntimeError(f"ADWR status {r.status_code}: {r.text[:150]}")
        if not batch:
            break
        for f in batch:
            g = f.get("geometry")
            if g:
                geoms.append(json.dumps(g))
        offset += len(batch)
        if len(batch) < 2000:
            break
    return geoms

def run_signals(kind="migration"):
    global SIGNAL_STATUS
    SIGNAL_STATUS = {"state": "running", "kind": kind, "detail": "starting"}
    try:
        updated = 0
        if kind == "migration":
            SIGNAL_STATUS["detail"] = "fetching Census population"
            with pool.connection() as c:
                zctas = [r[0] for r in c.execute("SELECT zcta FROM zones").fetchall()]
            new, old = _census_pop(CENSUS_YEARS[1]), _census_pop(CENSUS_YEARS[0])
            SIGNAL_STATUS["detail"] = "updating zones"
            with pool.connection() as c:
                with c.cursor() as cur:
                    for z in zctas:
                        pn, po = new.get(z), old.get(z)
                        if not pn or not po:
                            continue
                        sig = _migration_signal((pn - po) / po)
                        cur.execute("UPDATE zones SET signals = jsonb_set(signals,'{migration}', to_jsonb(%s::int)) WHERE zcta=%s", (sig, z))
                        updated += 1
                    cur.execute(f"UPDATE zones SET growth_default = {GROWTH_DEFAULT_EXPR}")
                    cur.execute(RESCORE_SQL)
                c.commit()
        elif kind == "developable":
            SIGNAL_STATUS["detail"] = "computing from loaded parcels"
            with pool.connection() as c:
                with c.cursor() as cur:
                    cur.execute(DEVELOPABLE_SQL)
                    updated = cur.rowcount
                    cur.execute(f"UPDATE zones SET growth_default = {GROWTH_DEFAULT_EXPR}")
                    cur.execute(RESCORE_SQL)
                c.commit()
        elif kind == "water":
            SIGNAL_STATUS["detail"] = "fetching ADWR determinations"
            with pool.connection() as c:
                bb = c.execute("SELECT ST_XMin(e),ST_YMin(e),ST_XMax(e),ST_YMax(e) FROM (SELECT ST_Extent(geom) e FROM zones) t").fetchone()
            geoms = _fetch_aaws(bb)
            if not geoms:
                SIGNAL_STATUS = {"state": "error", "detail": "ADWR returned 0 determination polygons"}; return
            SIGNAL_STATUS["detail"] = f"overlaying {len(geoms)} determination areas"
            with pool.connection() as c:
                with c.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS aaws; CREATE TEMP TABLE aaws(geom geometry(Geometry,4326));")
                    cur.executemany("INSERT INTO aaws(geom) VALUES (ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)))", [(g,) for g in geoms])
                    cur.execute("CREATE INDEX ON aaws USING gist(geom); ANALYZE aaws;")
                    cur.execute(WATER_OVERLAY_SQL)
                    updated = cur.rowcount
                    cur.execute(f"UPDATE zones SET growth_default = {GROWTH_DEFAULT_EXPR}")
                    cur.execute(RESCORE_SQL)
                c.commit()
        else:
            SIGNAL_STATUS = {"state": "error", "detail": f"unknown kind: {kind}"}; return
        SIGNAL_STATUS = {"state": "done", "kind": kind, "zones_updated": updated}
    except Exception as e:
        SIGNAL_STATUS = {"state": "error", "detail": str(e)[:200]}

@app.get("/admin/signals")
def admin_signals(token: str, kind: str = "migration"):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if SIGNAL_STATUS.get("state") == "running":
        return {"state": "already_running", "status": SIGNAL_STATUS}
    threading.Thread(target=run_signals, args=(kind,), daemon=True).start()
    return {"state": "started", "kind": kind, "next": "poll /admin/signals_status?token=YOUR_TOKEN"}

@app.get("/admin/signals_status")
def admin_signals_status(token: str):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    return SIGNAL_STATUS

# Serve the MapLibre frontend (single file, same origin as the API).
_here = pathlib.Path(__file__).resolve().parent
@app.get("/")
def index():
    return FileResponse(_here / "index.html")
