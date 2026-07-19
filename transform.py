"""
transform.py
Pure functions that turn raw county rows into parcel attributes.
Identical logic to the standalone build_parcels.py, kept dependency-free so the
ingest job and any test harness share one source of truth.
"""

import re

CUR_YEAR = 2026

# ---- owner classification --------------------------------------------------
ENTITY_KEYS  = ("LLC","L.L.C","INC","CORP","LP ","LLP","LTD","HOLDINGS","PROPERTIES",
                "PROPERTY","INVESTMENTS","CAPITAL","GROUP","PARTNERS","VENTURES",
                "ENTERPRISES","REALTY","ASSET","COMPANY","LAND CO","RANCH")
TRUST_KEYS   = ("TRUST","LIVING TR","FAMILY TR","REVOCABLE","IRREVOCABLE"," TR","TR ")
BUILDER_KEYS = ("HOMES","HOMEBUILD","DR HORTON","D R HORTON","LENNAR","PULTE","MERITAGE",
                "TAYLOR MORRISON","KB HOME","RICHMOND AMERICAN","TRI POINTE","SHEA",
                "MATTAMY","DEVELOPMENT","DEVELOPERS","COMMUNITIES","BUILDERS")

def classify_owner_type(owner_name, mailing_state=None):
    n = (owner_name or "").upper()
    if any(k in n for k in BUILDER_KEYS):   owner_type = "Builder/Developer"
    elif any(k in n for k in TRUST_KEYS):   owner_type = "Trust/LLC"
    elif any(k in n for k in ENTITY_KEYS):  owner_type = "Investor"
    else:                                   owner_type = "Individual"
    absentee = bool(mailing_state) and mailing_state.upper() != "AZ"
    return owner_type, absentee

# ---- land-use bucketing (replace with the authoritative PUC list) ----------
def bucket_use(puc):
    p = str(puc)
    if p.startswith("02"):   return "Agricultural"
    if p.startswith("00"):   return "Vacant"
    return "Improved"

# ---- sale history ----------------------------------------------------------
def latest_qualified_sale(sales):
    good = [s for s in sales if s.get("price") and not s.get("exempt")]
    return max(good, key=lambda s: s["year"]) if good else None

# ---- scoring (mirrors the app + build_parcels.py) --------------------------
SIGS = ["developable_land","permit_velocity","zoning_activity","infra_transport",
        "infra_water","migration","schools"]
DEFAULT_WEIGHTS = {"developable_land":50,"permit_velocity":60,"zoning_activity":55,
                   "infra_transport":60,"infra_water":60,"migration":60,"schools":50}
GATE = {"assured":1.0,"alternative_pending":0.7,"groundwater_constrained":0.3}

def zone_growth(sig, weights=DEFAULT_WEIGHTS, gate_on=True):
    num = sum(weights[k]*sig[k] for k in SIGS); den = sum(weights.values())
    s = num/den
    return s*GATE[sig["water_status"]] if gate_on else s

def tenure_score(tenure, owner_type):
    b = max(0, min(100, ((tenure or 0)-2)*4.2))
    f = 0.35 if owner_type=="Builder/Developer" else (0.8 if owner_type=="Investor" else 1.0)
    return b*f

def use_score(use):
    return 100 if use=="Vacant" else (85 if use=="Agricultural" else 25)

def target_score(growth, tenure, owner_type, use):
    return 0.5*growth + 0.3*tenure_score(tenure, owner_type) + 0.2*use_score(use)
