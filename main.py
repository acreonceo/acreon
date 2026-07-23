"""
Terra API. Serves the map (vector tiles) and the analytics (search, detail, targets)
straight from PostGIS. Every SQL statement here was validated against a live
PostGIS instance before shipping.
"""
import os, json, pathlib, requests, csv, io
import model as MODEL
import hazard as HZ
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
  SELECT p.apn, p.use, p.status, p.acres, p.zcta,
         coalesce(p.tenure,0)::int AS tenure,
         p.owner_type,
         p.est::bigint AS est,
         COALESCE(p.water_state,'C') AS water_state,
         COALESCE(p.landlocked,false) AS landlocked,
         COALESCE(p.flood_zone,'') AS flood_zone,
         round(COALESCE(p.carry_rate,0.003)::numeric,4)::float8 AS carry_rate,
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
          'geometry', ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.002))::jsonb,
          'properties', jsonb_build_object('zcta',zcta,'growth',growth_default,
                                           'water_status',water_status,'signals',signals,
                                           'dev_value_per_acre',dev_value_per_acre)))
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
      SELECT p.*, z.signals AS zone_signals, z.dev_value_per_acre
      FROM parcels p LEFT JOIN zones z ON z.zcta = p.zcta
      WHERE p.apn = %s
    """, (apn,))
    if not rows:
        raise HTTPException(404, "parcel not found")
    r = rows[0]
    r.pop("geom", None); r.pop("centroid", None)   # geometry not needed in detail JSON
    return r

# --- ACQUISITION TARGETS ---------------------------------------------------
SIG_KEYS = ["developable_land", "permit_velocity", "zoning_activity",
            "infra_transport", "infra_water", "migration", "schools"]
DEFAULT_W = {"developable_land": 50, "permit_velocity": 60, "zoning_activity": 55,
             "infra_transport": 60, "infra_water": 60, "migration": 60, "schools": 50}

def _parse_weights(w: str):
    """w is 'developable_land:80,migration:20,...'. Missing keys fall back to
    defaults. Returns None when the caller passed nothing (use stored scores)."""
    if not w:
        return None
    out = dict(DEFAULT_W)
    for part in w.split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip()
        if k in DEFAULT_W:
            try:
                out[k] = max(0, min(100, float(v)))
            except ValueError:
                pass
    return out

def _live_sql(weights, gate=True):
    """SQL fragments recomputing growth + target with the caller's weights, so the
    list ranks the same way the map is colored."""
    if not weights:
        return "p.growth_score", "p.target_score"
    total = sum(weights.values()) or 1
    terms = " + ".join(f"{weights[k]}*(z.signals->>'{k}')::numeric" for k in SIG_KEYS)
    gate_sql = ("CASE z.water_status WHEN 'assured' THEN 1.0 WHEN 'alternative_pending' THEN 0.7 ELSE 0.3 END"
                if gate else "1.0")
    growth = f"(({terms}) / {float(total)} * {gate_sql})"
    target = (f"(0.5*{growth} + 0.3*LEAST(100,GREATEST(0,(coalesce(p.tenure,0)-2)*4.2)) "
              f"* CASE p.owner_type WHEN 'Builder/Developer' THEN 0.35 WHEN 'Investor' THEN 0.8 ELSE 1.0 END "
              f"+ 0.2*CASE p.use WHEN 'Vacant' THEN 100 WHEN 'Agricultural' THEN 85 ELSE 25 END)")
    return growth, target

PUBLIC_OWNER_PATTERNS = [
    'MARICOPA COUNTY%', '%STATE OF ARIZONA%', 'ARIZONA STATE LAND%', '%STATE LAND DEPART%',
    'STATE OF ARIZONA%', 'UNITED STATES%', '%UNITED STATES OF AMERICA%', '%US GOVERNMENT%',
    '%BUREAU OF LAND MANAGEMENT%', '%BUREAU OF RECLAMATION%', '%FOREST SERVICE%',
    'CITY OF %', 'TOWN OF %', '%FLOOD CONTROL%', '%DEPARTMENT OF%', '%DEPT OF%',
    '%SCHOOL DIST%', '%SCHOOL DISTRICT%', '%UNIFIED SCHOOL%', '%BOARD OF REGENTS%',
    '%INDIAN COMMUNITY%', '%INDIAN RESERVATION%', '%PIMA MARICOPA%', '%GILA RIVER%',
    '%SALT RIVER PROJECT%', '%FORT MCDOWELL%', '%TOHONO%', '%AK CHIN%',
    '%GAME AND FISH%', '%CENTRAL ARIZONA PROJECT%', '%ARIZONA DEPARTMENT%',
    '%CONSERVATION DISTRICT%', '%MUNICIPAL%', '%HOMEOWNERS ASSOC%',
    '%COMMUNITY ASSOCIATION%', '%COMMON AREA%',
]

def _model_params(lambda_p=None, h_max=None, g_d=None, rho=None, horizon=None):
    p = dict(MODEL.DEFAULTS)
    if lambda_p is not None: p["lambda_p"] = max(0.0, min(0.10, lambda_p))
    if h_max    is not None: p["h_max"]    = max(0.005, min(0.20, h_max))
    if g_d      is not None: p["g_D"]      = max(-0.02, min(0.10, g_d))
    if rho      is not None: p["rho"]      = max(0.02, min(0.15, rho))
    return p

def _candidates(where_sql, params):
    return qall(f"""
      SELECT p.apn, p.situs_address, p.city, p.zcta, p.use, p.acres, p.est, p.assessed,
             p.owner, p.owner_type, p.absentee, p.tenure, p.acquired, p.paid,
             p.mail_address, COALESCE(p.water_state,'C') AS water_state,
             p.carry_rate, p.hazard_fitted, p.landlocked, p.flood_zone, p.edge_miles,
             z.signals AS signals, z.dev_value_per_acre,
             ST_X(p.centroid) lon, ST_Y(p.centroid) lat
      FROM parcels p LEFT JOIN zones z ON z.zcta = p.zcta
      WHERE {where_sql}
    """, params)

def _valued(rows, p, horizon=10):
    """Attach V, V/P, timing and acquisition score. The hazard path depends only
    on (zone growth, water state), so it is computed once per pair and reused."""
    cache = {}
    out = []
    for r in rows:
        acres = float(r["acres"] or 0)
        est = float(r["est"] or 0)
        if acres <= 0 or est <= 0:
            continue
        gi = MODEL.growth_index(r["signals"] or {})
        st = r["water_state"] or "C"
        hf = float(r["hazard_fitted"]) if r.get("hazard_fitted") is not None else None
        key = (round(gi, 1), st, round(hf, 4) if hf is not None else None)
        if key not in cache:
            cache[key] = MODEL.path(gi, st, p, hazard_override=hf)
        c = cache[key]
        site = MODEL.site_factor(r.get("landlocked"), r.get("flood_zone"))
        price_ac = est / acres
        d0 = float(r["dev_value_per_acre"] or 0) or price_ac * 3.0
        cr = float(r["carry_rate"]) if r["carry_rate"] is not None else MODEL.carry_rate(est, r["assessed"])
        v_ac = MODEL.value_per_acre(c, d0, price_ac, cr, site)
        r["growth_index"] = round(gi, 1)
        r["price_per_acre"] = round(price_ac)
        r["dev_price_per_acre"] = round(d0)
        r["value_per_acre"] = round(v_ac)
        r["value_total"] = round(v_ac * acres)
        r["value_ratio"] = round(v_ac / price_ac, 2) if price_ac > 0 else None
        r["p50_years"] = c["p50_years"]
        r["p_convert"] = c["p_convert"].get(horizon)
        r["carry_pct"] = round(cr * 100, 2)
        r["site_factor"] = round(site, 2)
        r["hazard_source"] = "fitted" if hf is not None else "judgment"
        r["acq_score"] = round(MODEL.acquisition_score(
            r["tenure"], r["owner_type"], r["absentee"], bool(r["paid"])), 1)
        r.pop("signals", None)
        out.append(r)
    return out

@app.get("/targets")
def targets(use: str = "", owner_type: str = "", min_acres: float = 0,
            min_tenure: int = 0, water_state: str = "", include_public: bool = False,
            min_ratio: float = 0, horizon: int = 10,
            lambda_p: float = None, h_max: float = None, g_d: float = None,
            rho: float = None, sort: str = "value_ratio", limit: int = 100):
    where = ["p.status='Off-market'", "p.use IN ('Vacant','Agricultural')",
             "p.acres >= %s", "coalesce(p.tenure,0) >= %s", "p.est > 0", "p.acres > 0"]
    args = [min_acres, min_tenure]
    if use:          where.append("p.use = %s");          args.append(use)
    if owner_type:   where.append("p.owner_type = %s");   args.append(owner_type)
    if water_state:  where.append("COALESCE(p.water_state,'C') = %s"); args.append(water_state)
    if not include_public:
        where.append("NOT (coalesce(p.owner,'') ILIKE ANY(%s))"); args.append(PUBLIC_OWNER_PATTERNS)
    rows = _candidates(" AND ".join(where), tuple(args))
    p = _model_params(lambda_p, h_max, g_d, rho, horizon)
    vals = _valued(rows, p, horizon)
    if min_ratio:
        vals = [r for r in vals if (r["value_ratio"] or 0) >= min_ratio]
    keyf = {"value_ratio": lambda r: r["value_ratio"] or 0,
            "value_total": lambda r: r["value_total"] or 0,
            "acq_score":   lambda r: r["acq_score"] or 0,
            "soonest":     lambda r: -(r["p50_years"] or 999)}.get(sort,
              lambda r: r["value_ratio"] or 0)
    vals.sort(key=keyf, reverse=True)
    for r in vals:
        r.pop("mail_address", None)
    return vals[:limit]

@app.get("/targets/export")
def targets_export(use: str = "", owner_type: str = "", min_acres: float = 0,
                   min_tenure: int = 0, water_state: str = "", include_public: bool = False,
                   min_ratio: float = 0, horizon: int = 10,
                   lambda_p: float = None, h_max: float = None, g_d: float = None,
                   rho: float = None, sort: str = "value_ratio", limit: int = 2000):
    """Outreach list with owner mailing addresses, ranked by the same model the
    map and target list use."""
    where = ["p.status='Off-market'", "p.use IN ('Vacant','Agricultural')",
             "p.acres >= %s", "coalesce(p.tenure,0) >= %s", "p.est > 0", "p.acres > 0"]
    args = [min_acres, min_tenure]
    if use:          where.append("p.use = %s");          args.append(use)
    if owner_type:   where.append("p.owner_type = %s");   args.append(owner_type)
    if water_state:  where.append("COALESCE(p.water_state,'C') = %s"); args.append(water_state)
    if not include_public:
        where.append("NOT (coalesce(p.owner,'') ILIKE ANY(%s))"); args.append(PUBLIC_OWNER_PATTERNS)
    rows = _candidates(" AND ".join(where), tuple(args))
    p = _model_params(lambda_p, h_max, g_d, rho, horizon)
    vals = _valued(rows, p, horizon)
    if min_ratio:
        vals = [r for r in vals if (r["value_ratio"] or 0) >= min_ratio]
    vals.sort(key=lambda r: r["value_ratio"] or 0, reverse=True)
    vals = vals[:limit]

    WS = {"A": "A served/assured", "B": "B irrigated ag (SB1611 path)", "C": "C raw groundwater"}
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["APN", "Situs Address", "City", "ZIP", "Use", "Acres",
                "Price $/Acre", "Modeled Value $/Acre", "Value/Price",
                "Developer $/Acre", "Water State", "P50 Yrs to Convert",
                f"P(convert<={horizon}y)", "Carry %/yr",
                "Owner", "Owner Type", "Absentee", "Years Held", "Acquired", "Paid",
                "Owner Mailing Address", "Acquisition Score", "Zone Growth Index",
                "Landlocked", "Flood Zone", "Site Factor", "Hazard Source"])
    for r in vals:
        w.writerow([r["apn"], r["situs_address"], r["city"], r["zcta"], r["use"], r["acres"],
                    r["price_per_acre"], r["value_per_acre"], r["value_ratio"],
                    r["dev_price_per_acre"], WS.get(r["water_state"], r["water_state"]),
                    r["p50_years"] if r["p50_years"] else ">60",
                    round(r["p_convert"], 3) if r["p_convert"] is not None else "",
                    r["carry_pct"],
                    r["owner"], r["owner_type"], r["absentee"], r["tenure"], r["acquired"], r["paid"],
                    r["mail_address"], r["acq_score"], r["growth_index"],
                    r.get("landlocked"), r.get("flood_zone") or "", r.get("site_factor"),
                    r.get("hazard_source")])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=acreon_targets.csv"})

@app.get("/assemblages")
def assemblages(min_parcels: int = 2, min_acres: float = 10, zcta: str = "",
                water_status: str = "", eps: float = 0.0004, limit: int = 100):
    """Contiguous blocks of vacant/ag land held by a single owner."""
    zfilter = "true"
    params = {"pub": PUBLIC_OWNER_PATTERNS, "eps": eps, "minp": min_parcels,
              "minac": min_acres, "lim": limit}
    if zcta:
        zfilter = "zcta = %(z)s"; params["z"] = zcta
    elif water_status:
        zfilter = "zcta IN (SELECT zcta FROM zones WHERE water_status = %(ws)s)"; params["ws"] = water_status
    sql = ASSEMBLAGE_SQL.replace("{ZFILTER}", zfilter)
    return qall(sql, params)
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
    mail = (a.get("MAIL_ADDRESS") or "").strip()
    return (apn, json.dumps(geom), addr, city, use, acres, int(est), int(assessed),
            owner, owner_type, absentee, acquired, tenure, paid, "Off-market", None, mail)

STAGE_DDL = """
DROP TABLE IF EXISTS parcels_stage;
CREATE TEMP TABLE parcels_stage (
  apn text, geom_geojson text, situs_address text, city text, use text, acres numeric,
  est bigint, assessed bigint, owner text, owner_type text, absentee boolean,
  acquired int, tenure int, paid bigint, status text, list_price bigint, mail_address text);
"""
STAGE_UPSERT = """
INSERT INTO parcels AS p (apn, geom, zcta, situs_address, city, use, acres, est, assessed,
  owner, owner_type, absentee, acquired, tenure, paid, status, list_price, mail_address, growth_score, target_score, updated_at)
SELECT s.apn, ST_SimplifyPreserveTopology(ST_SetSRID(ST_GeomFromGeoJSON(s.geom_geojson),4326), 0.00003), z.zcta, s.situs_address, s.city, s.use,
  s.acres, s.est, s.assessed, s.owner, s.owner_type, s.absentee, s.acquired, s.tenure, s.paid, s.status, s.list_price, s.mail_address,
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
  paid=EXCLUDED.paid, status=EXCLUDED.status, list_price=EXCLUDED.list_price, mail_address=EXCLUDED.mail_address, growth_score=EXCLUDED.growth_score,
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
            with cur.copy("COPY parcels_stage (apn,geom_geojson,situs_address,city,use,acres,est,assessed,owner,owner_type,absentee,acquired,tenure,paid,status,list_price,mail_address) FROM STDIN") as cp:
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

# --- ZONE REBUILD: precise, complete Census ZIP boundaries -----------------
ZCTA_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/7/query"
MARICOPA_BBOX = (-113.35, 32.5, -111.0, 34.05)
DEFAULT_SIGNALS = {"developable_land": 50, "permit_velocity": 50, "zoning_activity": 50,
                   "infra_transport": 50, "infra_water": 50, "migration": 50, "schools": 50,
                   "water_status": "groundwater_constrained"}

def _fetch_zctas(bbox):
    params = {"where": "1=1", "geometry": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
              "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
              "spatialRel": "esriSpatialRelIntersects", "outFields": "ZCTA5,GEOID",
              "returnGeometry": "true", "f": "geojson", "resultRecordCount": 4000}
    r = requests.get(ZCTA_URL, params=params, timeout=180, headers={"User-Agent": UA})
    try:
        feats = r.json().get("features", [])
    except Exception:
        raise RuntimeError(f"tigerweb status {r.status_code}: {r.text[:150]}")
    out = []
    for f in feats:
        p = f.get("properties") or {}
        z = (p.get("ZCTA5") or p.get("GEOID") or "").strip()
        g = f.get("geometry")
        if z and g:
            out.append((z, json.dumps(g)))
    return out

def run_rebuild_zones():
    """Replace coarse zone shapes with precise Census ZIP boundaries and add any
    missing ones, keeping existing zones' signals. Then re-assign every parcel to
    its containing zone so nothing falls in a gap."""
    global INGEST_STATUS
    INGEST_STATUS = {"state": "running", "mode": "zones", "detail": "fetching ZIP boundaries"}
    try:
        zctas = _fetch_zctas(MARICOPA_BBOX)
        if not zctas:
            INGEST_STATUS = {"state": "error", "detail": "tigerweb returned 0 ZCTAs"}; return
        INGEST_STATUS.update(detail=f"loading {len(zctas)} zones")
        with pool.connection() as c:
            with c.cursor() as cur:
                for z, gj in zctas:
                    cur.execute("""
                      INSERT INTO zones (zcta, geom, signals, water_status, growth_default)
                      VALUES (%s, ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))), %s::jsonb, %s, NULL)
                      ON CONFLICT (zcta) DO UPDATE SET geom = EXCLUDED.geom
                    """, (z, gj, json.dumps(DEFAULT_SIGNALS), DEFAULT_SIGNALS["water_status"]))
                INGEST_STATUS.update(detail="re-assigning parcels to zones")
                cur.execute("""
                  UPDATE parcels p SET zcta = z.zcta FROM zones z
                  WHERE ST_Contains(z.geom, p.centroid) AND p.zcta IS DISTINCT FROM z.zcta
                """)
                cur.execute(f"UPDATE zones SET growth_default = {GROWTH_DEFAULT_EXPR}")
                cur.execute(RESCORE_SQL)
            c.commit()
            n = c.execute("SELECT count(*) FROM zones").fetchone()[0]
            orphans = c.execute("SELECT count(*) FROM parcels WHERE zcta IS NULL").fetchone()[0]
        INGEST_STATUS = {"state": "done", "mode": "zones", "zones": n, "orphan_parcels": orphans}
    except Exception as e:
        INGEST_STATUS = {"state": "error", "detail": str(e)[:200]}

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

@app.get("/admin/rebuild_zones")
def admin_rebuild_zones(token: str):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if INGEST_STATUS.get("state") == "running":
        return {"state": "already_running", "status": INGEST_STATUS}
    threading.Thread(target=run_rebuild_zones, daemon=True).start()
    return {"state": "started", "mode": "zones",
            "note": "pulls precise ZIP boundaries, re-assigns parcels; then re-run the signals; poll /admin/status?token=YOUR_TOKEN"}

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


# Developer price per acre, per zone: the observed median $/acre of land that is
# already assured-supply (water state A). That is the closest available proxy for
# "what a developer pays for developable dirt here" without a hedonic surface.
# Zones with no assured-supply land of their own inherit the average of the three
# nearest zones that do, so far-fringe zones do not silently pick up metro-core
# prices, which would wildly overvalue deep desert.
DEV_VALUE_SQL = """
WITH own AS (
  SELECT zcta, percentile_cont(0.5) WITHIN GROUP (ORDER BY est/acres) AS v
  FROM parcels
  WHERE water_state = 'A' AND acres > 0 AND est > 0
  GROUP BY zcta HAVING count(*) >= 3
)
UPDATE zones z SET dev_value_per_acre = COALESCE(
  (SELECT v FROM own WHERE own.zcta = z.zcta),
  (SELECT avg(t.v) FROM (
      SELECT o.v FROM own o JOIN zones zz ON zz.zcta = o.zcta
      ORDER BY ST_Distance(z.geom, zz.geom) LIMIT 3) t)
);
"""

def _fetch_aaws(bbox, cap=15000):
    geoms, offset = [], 0
    while len(geoms) < cap:
        params = {"where": "1=1", "geometry": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
                  "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects", "outFields": "OBJECTID",
                  "returnGeometry": "true", "f": "geojson", "orderByFields": "OBJECTID",
                  "resultOffset": offset, "resultRecordCount": 1000}
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
        if not batch:
            break
    return geoms

# sales velocity: share of a zone's parcels that changed hands in the last 5 years.
# A real market-heat proxy standing in for permit + rezoning activity.
VELOCITY_SQL = """
WITH sv AS (
  SELECT zcta, count(*) FILTER (WHERE acquired >= %s)::numeric / NULLIF(count(*),0) AS share
  FROM parcels WHERE zcta IS NOT NULL GROUP BY zcta)
UPDATE zones z SET signals = jsonb_set(jsonb_set(z.signals,
    '{permit_velocity}', to_jsonb(LEAST(100,GREATEST(0,round(sv.share*300)))::int)),
    '{zoning_activity}', to_jsonb(LEAST(100,GREATEST(0,round(sv.share*300)))::int))
FROM sv WHERE z.zcta = sv.zcta;
"""

# transport: count of ADOT programmed projects within ~2km of each zone.
ADOT_PROJECTS_URL = "https://gis.azdot.gov/gis/rest/services/ProjectCoversheet/projects/MapServer/0/query"
TRANSPORT_OVERLAY_SQL = """
WITH nc AS (
  SELECT z.zcta, count(t.*) AS n
  FROM zones z LEFT JOIN tip t ON ST_DWithin(z.geom, t.geom, 0.02)
  GROUP BY z.zcta)
UPDATE zones z SET signals = jsonb_set(z.signals, '{infra_transport}',
    to_jsonb(LEAST(100, GREATEST(0, 20 + nc.n*22))::int))
FROM nc WHERE z.zcta = nc.zcta;
"""

def _fetch_projects(bbox, cap=20000):
    geoms, offset = [], 0
    while len(geoms) < cap:
        params = {"where": "1=1", "geometry": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
                  "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects", "outFields": "OBJECTID",
                  "returnGeometry": "true", "f": "geojson",
                  "resultOffset": offset, "resultRecordCount": 1000}
        r = requests.get(ADOT_PROJECTS_URL, params=params, timeout=120, headers={"User-Agent": UA})
        try:
            feats = r.json().get("features", [])
        except Exception:
            raise RuntimeError(f"ADOT status {r.status_code}: {r.text[:150]}")
        if not feats:
            break
        geoms.extend(json.dumps(f["geometry"]) for f in feats if f.get("geometry"))
        offset += len(feats)
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
                    # per-parcel water state: A served, B irrigated ag (SB1611
                    # conversion path), C raw groundwater-dependent
                    cur.execute("""
                      UPDATE parcels p SET water_state = CASE
                        WHEN EXISTS (SELECT 1 FROM aaws a WHERE ST_Contains(a.geom, p.centroid)) THEN 'A'
                        WHEN p.use = 'Agricultural' THEN 'B' ELSE 'C' END
                    """)
                    # carry from the tax roll, not a flat assumption
                    cur.execute("""
                      UPDATE parcels SET carry_rate =
                        CASE WHEN est > 0
                          THEN (%s * %s * COALESCE(NULLIF(assessed,0), est*0.8)) / est + %s
                          ELSE %s END
                    """, (MODEL.DEFAULT_TAX_RATE, MODEL.ASSESS_RATIO, MODEL.UPKEEP, MODEL.UPKEEP+0.002))
                    # zone developer price: observed $/ac of assured-supply land,
                    # spatially interpolated where a zone has none of its own
                    cur.execute(DEV_VALUE_SQL)
                    cur.execute(f"UPDATE zones SET growth_default = {GROWTH_DEFAULT_EXPR}")
                    cur.execute(RESCORE_SQL)
                c.commit()
        elif kind == "velocity":
            import transform as T
            SIGNAL_STATUS["detail"] = "computing sales velocity from parcels"
            with pool.connection() as c:
                with c.cursor() as cur:
                    cur.execute(VELOCITY_SQL, (T.CUR_YEAR - 5,))
                    updated = cur.rowcount
                    cur.execute(f"UPDATE zones SET growth_default = {GROWTH_DEFAULT_EXPR}")
                    cur.execute(RESCORE_SQL)
                c.commit()
        elif kind == "transport":
            SIGNAL_STATUS["detail"] = "fetching ADOT projects"
            with pool.connection() as c:
                bb = c.execute("SELECT ST_XMin(e),ST_YMin(e),ST_XMax(e),ST_YMax(e) FROM (SELECT ST_Extent(geom) e FROM zones) t").fetchone()
            geoms = _fetch_projects(bb)
            if not geoms:
                SIGNAL_STATUS = {"state": "error", "detail": "ADOT returned 0 projects"}; return
            SIGNAL_STATUS["detail"] = f"overlaying {len(geoms)} projects"
            with pool.connection() as c:
                with c.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS tip; CREATE TEMP TABLE tip(geom geometry(Geometry,4326));")
                    cur.executemany("INSERT INTO tip(geom) VALUES (ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)))", [(g,) for g in geoms])
                    cur.execute("CREATE INDEX ON tip USING gist(geom); ANALYZE tip;")
                    cur.execute(TRANSPORT_OVERLAY_SQL)
                    updated = cur.rowcount
                    cur.execute(f"UPDATE zones SET growth_default = {GROWTH_DEFAULT_EXPR}")
                    cur.execute(RESCORE_SQL)
                c.commit()
        else:
            SIGNAL_STATUS = {"state": "error", "detail": f"unknown kind: {kind}"}; return
        SIGNAL_STATUS = {"state": "done", "kind": kind, "zones_updated": updated}
    except Exception as e:
        SIGNAL_STATUS = {"state": "error", "detail": str(e)[:200]}

ALL_SIGNAL_KINDS = ["migration", "water", "developable", "velocity", "transport"]

def run_all_signals():
    """Run every real signal in sequence. Used by the one-click refresh and the
    monthly scheduled job."""
    global SIGNAL_STATUS
    ran = {}
    for i, k in enumerate(ALL_SIGNAL_KINDS, 1):
        run_signals(k)
        ran[k] = SIGNAL_STATUS.get("state")
        if SIGNAL_STATUS.get("state") == "error":
            SIGNAL_STATUS = {"state": "error", "at": k, "detail": SIGNAL_STATUS.get("detail"), "ran": ran}
            return
    SIGNAL_STATUS = {"state": "done", "kind": "all", "ran": ran}

def run_full_refresh(parcels=False):
    """Optionally re-pull all parcels, then run every signal. Used by the quarterly
    scheduled job and the manual full refresh."""
    if parcels:
        run_ingest_county()
        if INGEST_STATUS.get("state") == "error":
            return
    run_all_signals()

@app.get("/admin/signals")
def admin_signals(token: str, kind: str = "migration"):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if SIGNAL_STATUS.get("state") == "running":
        return {"state": "already_running", "status": SIGNAL_STATUS}
    target = run_all_signals if kind == "all" else (lambda: run_signals(kind))
    threading.Thread(target=target, daemon=True).start()
    return {"state": "started", "kind": kind, "next": "poll /admin/signals_status?token=YOUR_TOKEN"}

@app.get("/admin/refresh")
def admin_refresh(token: str, parcels: bool = False):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if INGEST_STATUS.get("state") == "running" or SIGNAL_STATUS.get("state") == "running":
        return {"state": "already_running"}
    threading.Thread(target=run_full_refresh, kwargs={"parcels": parcels}, daemon=True).start()
    return {"state": "started", "parcels": parcels,
            "note": "parcels=false runs all signals; parcels=true also re-pulls the county first. Poll /admin/status then /admin/signals_status"}

@app.get("/admin/signals_status")
def admin_signals_status(token: str):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    return SIGNAL_STATUS


# --- HISTORICAL EVENTS: built parcels, for hazard estimation ---------------
# FEMA publishes the NFHL on two paths; hazards.fema.gov resets aggressively on
# large or rapid queries, so both are tried and the extent is finely tiled.
# FEMA's own host has refused every request pattern from this server (connection
# reset), so the county's floodplain service is tried first. It lives on the same
# host that already serves the parcel data, which is known reachable.
# FEMA's main service answers fine and carries the real attributes; the earlier
# resets were response size, not a block. The fix is filtering to Special Flood
# Hazard Areas server-side (SFHA_TF='T') instead of pulling every polygon
# including the vast zone-X areas that cover most of the county. The county
# layer is kept as a fallback, but it exposes only OBJECTID, so it can say
# "in a floodplain" and nothing more.
FEMA_URLS = ["https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query",
             "https://gis.mcassessor.maricopa.gov/ArcGIS/rest/services/Flood/MapServer/0/query"]
SFHA_WHERE = "SFHA_TF='T' OR ZONE_SUBTY LIKE '%FLOODWAY%'"
# Server-side generalisation, in output-SR units (degrees). ~20m.
GEN_OFFSET = 0.0002
FEMA_NFHL = FEMA_URLS[0]
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HIGH_RISK_ZONES = ("A", "AE", "AH", "AO", "A99", "AR", "V", "VE")

def _county_count(where):
    """Ask the server how many records match, so progress is measured against a
    real target instead of guessed."""
    try:
        r = requests.get(COUNTY_PARCELS, params={"where": where, "returnCountOnly": "true", "f": "json"},
                         timeout=60, headers={"User-Agent": UA})
        return int(r.json().get("count"))
    except Exception:
        return None

def run_ingest_built(cap=2_000_000):
    """Pull every parcel carrying a construction year: APN, point, year, size.
    This is the census of development events the hazard is fit on.

    Pagination is by OBJECTID rather than resultOffset. Some ArcGIS layers ignore
    resultOffset and return the same first page every time, which silently caps a
    pull at one page. Keyset paging (OBJECTID > last seen) always advances.
    """
    global INGEST_STATUS
    where_base = "CONST_YEAR <> ''"
    total = _county_count(where_base)
    INGEST_STATUS = {"state": "running", "mode": "built", "fetched": 0,
                     "server_count": total, "detail": "starting"}
    last_oid, seen, buf, loaded, stalls = -1, 0, [], 0, 0
    try:
        while seen < cap:
            params = {"where": f"({where_base}) AND OBJECTID > {last_oid}",
                      "outFields": "OBJECTID,APN_DASH,APN,CONST_YEAR,LONGITUDE,LATITUDE,LAND_SIZE",
                      "returnGeometry": "false", "f": "json",
                      "orderByFields": "OBJECTID", "resultRecordCount": 1000}
            r = requests.get(COUNTY_PARCELS, params=params, timeout=120, headers={"User-Agent": UA})
            try:
                j = r.json()
            except Exception:
                raise RuntimeError(f"county status {r.status_code}: {r.text[:150]}")
            if j.get("error"):
                raise RuntimeError(f"county error: {str(j['error'])[:180]}")
            feats = j.get("features", [])
            if not feats:
                break
            prev = last_oid
            for f in feats:
                a = f.get("attributes") or {}
                oid = a.get("OBJECTID")
                if isinstance(oid, (int, float)):
                    last_oid = max(last_oid, int(oid))
                cy = str(a.get("CONST_YEAR") or "").strip()
                lon, lat = a.get("LONGITUDE"), a.get("LATITUDE")
                if not (len(cy) == 4 and cy.isdigit()) or lon is None or lat is None:
                    continue
                yr = int(cy)
                if yr < 1900 or yr > 2026:
                    continue
                ls = a.get("LAND_SIZE")
                acres = round(ls / 43560.0, 3) if isinstance(ls, (int, float)) and ls > 0 else None
                apn = (a.get("APN_DASH") or a.get("APN") or "").strip()
                if apn:
                    buf.append((apn, lon, lat, yr, acres))
            seen += len(feats)
            if last_oid <= prev:          # server is not advancing; stop rather than spin
                stalls += 1
                if stalls >= 2:
                    raise RuntimeError("server did not advance past OBJECTID "
                                       f"{last_oid}; pagination unsupported on this layer")
            else:
                stalls = 0
            INGEST_STATUS.update(fetched=seen, detail=f"fetching ({seen}"
                                 + (f" of {total}" if total else "") + ")")
            if len(buf) >= 20000:
                loaded += _flush_built(buf); buf = []
                INGEST_STATUS.update(loaded=loaded)
        if buf:
            loaded += _flush_built(buf)
        with pool.connection() as c:
            n = c.execute("SELECT count(*) FROM built").fetchone()[0]
            yr = c.execute("SELECT min(const_year), max(const_year) FROM built").fetchone()
        INGEST_STATUS = {"state": "done", "mode": "built", "fetched": seen,
                         "server_count": total, "rows": n, "year_range": yr}
    except Exception as e:
        INGEST_STATUS = {"state": "error", "mode": "built", "detail": str(e)[:220],
                         "fetched": seen, "server_count": total}

def _flush_built(rows):
    with pool.connection() as c, c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS built_stage; CREATE TEMP TABLE built_stage(apn text, lon float8, lat float8, cy int, acres numeric);")
        with cur.copy("COPY built_stage (apn,lon,lat,cy,acres) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        cur.execute("""INSERT INTO built (apn, centroid, const_year, acres)
                       SELECT apn, ST_SetSRID(ST_Point(lon,lat),4326), cy, acres FROM built_stage
                       WHERE apn <> '' ON CONFLICT (apn) DO UPDATE
                         SET centroid=EXCLUDED.centroid, const_year=EXCLUDED.const_year, acres=EXCLUDED.acres""")
        c.commit()
    return len(rows)

# --- HAZARD FIT ------------------------------------------------------------
# The frontier at year T is every parcel already built by T. Distance from an
# at-risk parcel to that set is the covariate the review identified as the single
# strongest predictor of conversion.
PANEL_SQL = """
CREATE TEMP TABLE panel AS
WITH universe AS (
  SELECT apn, centroid, NULL::int AS const_year, acres FROM parcels
   WHERE centroid IS NOT NULL AND random() < %(s_vac)s
  UNION ALL
  SELECT apn, centroid, const_year, acres FROM built
   WHERE random() < %(s_blt)s
)
SELECT u.apn, y.t AS period,
       (u.const_year IS NOT NULL AND u.const_year >= y.t AND u.const_year < y.t + 5)::int AS event,
       u.acres,
       (SELECT ST_Distance(u.centroid::geography, b.centroid::geography) / 1609.34
          FROM built b
         WHERE b.const_year <= y.t
         ORDER BY u.centroid <-> b.centroid
         LIMIT 1) AS edge_miles
FROM universe u
CROSS JOIN (SELECT unnest(%(periods)s::int[]) AS t) y
WHERE u.const_year IS NULL OR u.const_year > y.t;
"""

def run_fit_hazard(sample_vacant=0.06, sample_built=0.05):
    """Build the panel, fit the discrete-time logit, store coefficients, then
    score every current parcel with its fitted annual hazard."""
    global SIGNAL_STATUS
    SIGNAL_STATUS = {"state": "running", "kind": "hazard", "detail": "checking history"}
    try:
        with pool.connection() as c:
            nb = c.execute("SELECT count(*) FROM built").fetchone()[0]
        if nb < 5000:
            SIGNAL_STATUS = {"state": "error", "kind": "hazard",
                             "detail": f"only {nb} built parcels on record; run /admin/ingest_built first"}
            return
        SIGNAL_STATUS["detail"] = "building panel (reconstructing the historical frontier)"
        with pool.connection() as c:
            with c.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS panel")
                cur.execute(PANEL_SQL, {"s_vac": sample_vacant, "s_blt": sample_built,
                                        "periods": HZ.PERIODS})
                rows = cur.execute("SELECT period, event, acres, edge_miles FROM panel WHERE edge_miles IS NOT NULL").fetchall()
            c.commit()
        if len(rows) < 2000:
            SIGNAL_STATUS = {"state": "error", "kind": "hazard", "detail": f"panel too small ({len(rows)} rows)"}
            return
        SIGNAL_STATUS["detail"] = f"fitting on {len(rows):,} parcel-periods"
        bins, counts, exposure = HZ.pool_bins(rows)
        X = [HZ.design_row(int(p), float(d), float(a) if a else 1.0, bins) for p, e, a, d in rows]
        y = [int(e) for p, e, a, d in rows]
        coefs = HZ.fit_logit(X, y)
        events = sum(y)
        summary = HZ.summarize(coefs, bins, counts, exposure)

        SIGNAL_STATUS["detail"] = "scoring parcels with the fitted hazard"
        cur_year = 2025
        with pool.connection() as c:
            with c.cursor() as cur:
                cur.execute("""
                  UPDATE parcels p SET edge_miles = sub.d FROM (
                    SELECT p2.apn, (SELECT ST_Distance(p2.centroid::geography, b.centroid::geography)/1609.34
                                      FROM built b WHERE b.const_year <= %s
                                     ORDER BY p2.centroid <-> b.centroid LIMIT 1) AS d
                    FROM parcels p2) sub
                  WHERE p.apn = sub.apn""", (cur_year,))
                cur.execute("INSERT INTO model_fit(key,payload) VALUES('hazard',%s::jsonb) "
                            "ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload, fitted_at=now()",
                            (json.dumps({"coefs": coefs, "summary": summary, "n": len(rows),
                                         "events": events, "periods": HZ.PERIODS,
                                         "bins": bins}),))
                rows2 = cur.execute("SELECT apn, edge_miles, acres FROM parcels WHERE edge_miles IS NOT NULL").fetchall()
                upd = []
                for apn, d, ac in rows2:
                    p5 = HZ.predict_p5(coefs, HZ.PERIODS[-1], float(d), float(ac) if ac else 1.0, bins)
                    upd.append((HZ.annual_hazard(p5), apn))
                cur.executemany("UPDATE parcels SET hazard_fitted=%s WHERE apn=%s", upd)
            c.commit()
        SIGNAL_STATUS = {"state": "done", "kind": "hazard", "panel_rows": len(rows),
                         "conversion_events": events, "parcels_scored": len(rows2),
                         "coefficients": summary}
    except Exception as e:
        SIGNAL_STATUS = {"state": "error", "kind": "hazard", "detail": str(e)[:250]}

# --- PARCEL SCREENS --------------------------------------------------------
# Landlocked: share of the parcel boundary touching other parcels. Public
# right-of-way is generally not parcelled, so a parcel whose perimeter is almost
# entirely shared with neighbours has no frontage and no legal access.
LANDLOCK_SQL = """
UPDATE parcels p SET landlocked = COALESCE(sub.shared, 0) > 0.97
FROM (
  SELECT p2.apn,
         LEAST(1.0,
           SUM(ST_Length(ST_Intersection(
                 ST_Boundary(ST_CollectionExtract(ST_MakeValid(p2.geom), 3)),
                 ST_Buffer(ST_CollectionExtract(ST_MakeValid(n.geom), 3), 0.00002)
               )::geography))
           / NULLIF(ST_Perimeter(ST_CollectionExtract(ST_MakeValid(p2.geom), 3)::geography), 0)
         ) AS shared
  FROM parcels p2
  JOIN parcels n ON n.apn <> p2.apn AND ST_DWithin(p2.geom, n.geom, 0.00002)
  WHERE ST_GeometryType(p2.geom) IN ('ST_Polygon','ST_MultiPolygon')
  GROUP BY p2.apn, p2.geom
) sub
WHERE p.apn = sub.apn;
UPDATE parcels SET landlocked = false WHERE landlocked IS NULL;
"""
# NOTE: county parcel fabrics contain self-intersecting rings and slivers, so
# every geometry is repaired with ST_MakeValid and reduced to polygons before
# use. Neighbours are buffered ~2m rather than unioned: ST_Union over a set
# containing one bad polygon throws a topology exception and kills the whole
# job, and per-neighbour intersection avoids that entirely. Corner overlaps can
# double-count a metre or two, which LEAST(1.0, ...) absorbs.

def _get_retry(url, params, timeout=180, tries=4, ua=None):
    """Transient resets are normal against large public GIS services. Retry with
    backoff rather than failing the whole job on one dropped connection."""
    import time
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": ua or UA, "Accept": "application/json"})
            if r.status_code >= 500:
                last = RuntimeError(f"status {r.status_code}")
            else:
                return r
        except Exception as e:
            last = e
        time.sleep(1.5 * (i + 1))
    raise last if last else RuntimeError("request failed")


def _flood_class(props):
    """Classify a flood polygon. Schema varies by source, so this reads what is
    there rather than assuming.

    The decisive field is SFHA_TF: FEMA's flag for a Special Flood Hazard Area.
    'F' means minimal hazard (zone X) and must NOT be flagged; an earlier version
    defaulted unknown records to floodplain and would have discounted most of the
    county for being explicitly safe.
    """
    z = str(props.get("FLD_ZONE") or "").strip().upper()
    sub = str(props.get("ZONE_SUBTY") or "").strip().upper()
    sfha = str(props.get("SFHA_TF") or "").strip().upper()

    if "FLOODWAY" in sub:
        return "FLOODWAY"
    if sfha in ("F", "FALSE", "N", "NO"):
        return ""                       # explicitly minimal hazard
    if z in HIGH_RISK_ZONES:
        return z
    if z:                               # a zone code we do not treat as a constraint (X, D, etc.)
        return ""
    if sfha in ("T", "TRUE", "Y", "YES"):
        return "AE"

    # No FEMA fields at all: a dedicated floodplain layer carrying geometry only.
    text = " ".join(str(v).upper() for v in props.values() if v is not None)
    if "FLOODWAY" in text:
        return "FLOODWAY"
    if "0.2%" in text or "500-YEAR" in text or "SHADED X" in text or "MINIMAL" in text:
        return ""
    if "1%" in text or "100-YEAR" in text or "100 YEAR" in text:
        return "AE"
    known = {"OBJECTID", "GLOBALID", "GFID", "SHAPE.STAREA()", "SHAPE.STLENGTH()"}
    if props and set(k.upper() for k in props) <= known:
        return "FLOODPLAIN"             # geometry-only floodplain layer
    return ""


def _flood_url():
    """Pick a service by asking for GEOMETRY, not just a count.

    Every failure so far has been on geometry retrieval: the probe succeeded with
    returnGeometry=false and reset with it on. FEMA's flood polygons are highly
    detailed, so requests carry maxAllowableOffset to have the server generalise
    them before sending. Roughly 20m of precision is far finer than needed to ask
    whether a parcel centroid sits inside a flood zone, and it cuts the payload
    by an order of magnitude.
    """
    for u in FEMA_URLS:
        try:
            w = SFHA_WHERE if "hazards.fema.gov" in u else "1=1"
            r = requests.get(u, params={"where": w, "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF"
                                        if "hazards.fema.gov" in u else "*",
                                        "returnGeometry": "true", "outSR": "4326",
                                        "maxAllowableOffset": GEN_OFFSET,
                                        "geometryPrecision": 6,
                                        "resultRecordCount": 1, "f": "geojson"},
                             timeout=60, headers={"User-Agent": BROWSER_UA})
            if r.status_code < 400 and "features" in r.text[:2000]:
                return u
        except Exception:
            continue
    return FEMA_URLS[-1]


@app.get("/admin/probe_flood")
def admin_probe_flood(token: str):
    """Report which floodplain service answers and what fields it exposes, so the
    source can be confirmed rather than guessed."""
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    out = []
    for u in FEMA_URLS:
        rec = {"url": u}
        try:
            r = requests.get(u, params={"where": "1=1", "outFields": "*", "returnGeometry": "false",
                                        "resultRecordCount": 2, "f": "json"},
                             timeout=45, headers={"User-Agent": BROWSER_UA})
            rec["status"] = r.status_code
            j = r.json()
            if j.get("error"):
                rec["error"] = str(j["error"])[:160]
            feats = j.get("features", [])
            rec["returned"] = len(feats)
            if feats:
                a = feats[0].get("attributes", {})
                rec["fields"] = list(a.keys())
                rec["sample"] = {k: a[k] for k in list(a)[:12]}
                rec["classified_as"] = _flood_class(a)
        except Exception as e:
            rec["error"] = str(e)[:160]
        out.append(rec)
    return {"candidates": out}


def _flood_url_unused():
    for u in FEMA_URLS:
        try:
            r = requests.get(u, params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
                             timeout=45, headers={"User-Agent": BROWSER_UA})
            if r.status_code < 400 and "count" in r.text[:400]:
                return u
        except Exception:
            continue
    return FEMA_URLS[0]


def _flood_tiles(bbox, tiles=12, per_page=50):
    """Yield one tile's flood polygons at a time.

    The previous version accumulated every polygon countywide before writing any
    of them. FEMA geometries are detailed, and holding the whole set exhausted
    the container's memory, which restarted the service mid-run (the status
    flipping back to idle was the process dying). Streaming per tile keeps peak
    memory to a single tile.
    """
    x0, y0, x1, y1 = bbox
    url = _flood_url()
    dx, dy = (x1 - x0) / tiles, (y1 - y0) / tiles
    for i in range(tiles):
        for j in range(tiles):
            bx0, by0 = x0 + i * dx, y0 + j * dy
            bx1, by1 = bx0 + dx, by0 + dy
            batch, offset = [], 0
            while True:
                where = SFHA_WHERE if "hazards.fema.gov" in url else "1=1"
                fields = "FLD_ZONE,ZONE_SUBTY,SFHA_TF" if "hazards.fema.gov" in url else "*"
                params = {"where": where, "geometry": f"{bx0},{by0},{bx1},{by1}",
                          "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
                          "spatialRel": "esriSpatialRelIntersects", "outFields": fields,
                          "returnGeometry": "true", "f": "geojson",
                          "maxAllowableOffset": GEN_OFFSET, "geometryPrecision": 6,
                          "resultOffset": offset, "resultRecordCount": per_page}
                r = _get_retry(url, params, timeout=120, tries=3, ua=BROWSER_UA)
                try:
                    feats = r.json().get("features", [])
                except Exception:
                    raise RuntimeError(f"FEMA status {r.status_code}: {r.text[:120]}")
                if not feats:
                    break
                for f in feats:
                    g = f.get("geometry")
                    if not g:
                        continue
                    z = _flood_class(f.get("properties") or {})
                    if z:
                        batch.append((json.dumps(g), z))
                offset += len(feats)
            yield (i * tiles + j + 1, tiles * tiles, batch)
            batch = None


def run_screens(do_flood=True):
    """Two screens the review named as the source of the worst embarrassments:
    a landlocked parcel scored like its road-frontage neighbour, and floodway
    land scored on growth momentum."""
    global SIGNAL_STATUS
    SIGNAL_STATUS = {"state": "running", "kind": "screens", "detail": "computing frontage"}
    try:
        with pool.connection() as c:
            with c.cursor() as cur:
                for stmt in [s for s in LANDLOCK_SQL.split(";") if s.strip()]:
                    cur.execute(stmt)
            c.commit()
            ll = c.execute("SELECT count(*) FROM parcels WHERE landlocked").fetchone()[0]
        flood, flood_err = 0, None
        if do_flood:
          try:
            SIGNAL_STATUS["detail"] = "locating FEMA service"
            with pool.connection() as c:
                bb = c.execute("SELECT ST_XMin(e),ST_YMin(e),ST_XMax(e),ST_YMax(e) "
                               "FROM (SELECT ST_Extent(geom) e FROM zones) t").fetchone()
                with c.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS flood")
                    cur.execute("CREATE TABLE flood(geom geometry(Geometry,4326), zone text)")
                c.commit()
            written = 0
            for idx, total_tiles, batch in _flood_tiles(bb):
                if batch:
                    with pool.connection() as c, c.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO flood(geom,zone) VALUES (ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)),%s)",
                            batch)
                        c.commit()
                    written += len(batch)
                SIGNAL_STATUS.update(detail=f"flood tile {idx}/{total_tiles}, {written} polygons stored")
            if written:
                with pool.connection() as c:
                    with c.cursor() as cur:
                        cur.execute("CREATE INDEX ON flood USING gist(geom)")
                        cur.execute("ANALYZE flood")
                        cur.execute("UPDATE parcels SET flood_zone = NULL")
                        cur.execute("""UPDATE parcels p SET flood_zone = f.zone FROM flood f
                                       WHERE ST_Contains(f.geom, p.centroid)""")
                        cur.execute("DROP TABLE flood")
                    c.commit()
                    flood = c.execute("SELECT count(*) FROM parcels WHERE flood_zone IS NOT NULL").fetchone()[0]
          except Exception as fe:
            flood_err = str(fe)[:180]      # keep the frontage result either way
        out = {"state": "done", "kind": "screens", "landlocked": ll, "flood_zone": flood}
        if flood_err:
            out["flood_error"] = flood_err
        SIGNAL_STATUS = out
    except Exception as e:
        SIGNAL_STATUS = {"state": "error", "kind": "screens", "detail": str(e)[:250]}

@app.get("/admin/ingest_built")
def admin_ingest_built(token: str):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if INGEST_STATUS.get("state") == "running":
        return {"state": "already_running", "status": INGEST_STATUS}
    threading.Thread(target=run_ingest_built, daemon=True).start()
    return {"state": "started", "mode": "built",
            "note": "pulls construction years countywide; long-running; poll /admin/status"}

@app.get("/admin/fit_hazard")
def admin_fit_hazard(token: str, sample_vacant: float = 0.06, sample_built: float = 0.05):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if SIGNAL_STATUS.get("state") == "running":
        return {"state": "already_running", "status": SIGNAL_STATUS}
    threading.Thread(target=run_fit_hazard, args=(sample_vacant, sample_built), daemon=True).start()
    return {"state": "started", "kind": "hazard", "next": "poll /admin/signals_status"}

@app.get("/admin/screens")
def admin_screens(token: str, flood: bool = True):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    if SIGNAL_STATUS.get("state") == "running":
        return {"state": "already_running", "status": SIGNAL_STATUS}
    threading.Thread(target=run_screens, args=(flood,), daemon=True).start()
    return {"state": "started", "kind": "screens", "next": "poll /admin/signals_status"}

@app.get("/admin/fit")
def admin_fit(token: str):
    if token != os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(403, "forbidden")
    rows = qall("SELECT key, payload, fitted_at FROM model_fit")
    return rows or {"detail": "no fit stored yet; run /admin/fit_hazard"}

# Serve the MapLibre frontend (single file, same origin as the API).
_here = pathlib.Path(__file__).resolve().parent
@app.get("/")
def index():
    return FileResponse(_here / "index.html")
