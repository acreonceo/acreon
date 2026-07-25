"""
transform.py
Pure functions that turn raw county rows into parcel attributes.
Identical logic to the standalone build_parcels.py, kept dependency-free so the
ingest job and any test harness share one source of truth.
"""

import re

CUR_YEAR = 2026

# ---- owner classification --------------------------------------------------
# Matching is on whole words against a punctuation-normalised name. Substring
# matching was silently wrong: " TR" fired on TRAN, TRACY and TREVOR, so a large
# block of individually-owned parcels was being filed as Trust/LLC, and the
# tenure discount for entities was applied to them.
ENTITY_KEYS  = ("LLC","L L C","INC","INCORPORATED","CORP","CORPORATION","LP","LLP","LTD",
                "HOLDINGS","PROPERTIES","PROPERTY","INVESTMENTS","INVESTMENT","CAPITAL",
                "GROUP","PARTNERS","PARTNERSHIP","VENTURES","ENTERPRISES","REALTY",
                "ASSET","ASSETS","COMPANY","LAND CO","RANCH","RANCHES")
TRUST_KEYS   = ("TRUST","TRUSTEE","TRUSTEES","TR","LIVING TRUST","FAMILY TRUST",
                "REVOCABLE","IRREVOCABLE","ESTATE OF")
# A firm that builds is not a seller of raw ground, however it is incorporated.
# CONSTRUCTION and CONTRACTING were both missing, which let builder-owned land
# through the acquisition screen as ordinary investor stock.
BUILDER_KEYS = ("HOMES","HOME","HOMEBUILDER","HOMEBUILDERS","HOMEBUILDING","BUILDER",
                "BUILDERS","BUILDING","CONSTRUCTION","CONTRACTING","CONTRACTOR",
                "CONTRACTORS","DEVELOPMENT","DEVELOPMENTS","DEVELOPERS","DEVELOPER",
                "COMMUNITIES","DR HORTON","D R HORTON","LENNAR","PULTE","MERITAGE",
                "TAYLOR MORRISON","KB HOME","RICHMOND AMERICAN","TRI POINTE","SHEA",
                "MATTAMY","ASHTON WOODS","TOLL BROTHERS","BROOKFIELD RESIDENTIAL")

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY",
    "LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
    "DC","PR","VI","GU","AS","MP",
}
# County records carry foreign owners with a blank or non-standard state field.
FOREIGN_HINTS = ("MEX","MEXICO","SONORA","JALISCO","SINALOA","CHIHUAHUA","BAJA","D F",
                 "CANADA","ONTARIO","QUEBEC","ALBERTA","BRITISH COLUMBIA","MANITOBA",
                 "SASKATCHEWAN","UK","UNITED KINGDOM","ENGLAND","GERMANY","FRANCE",
                 "SWITZERLAND","CHINA","HONG KONG","TAIWAN","JAPAN","KOREA","SINGAPORE",
                 "INDIA","ISRAEL","AUSTRALIA","BRAZIL","ARGENTINA","SPAIN","ITALY")

def _norm(s):
    """Uppercase, punctuation to spaces, whitespace collapsed. L.L.C. -> L L C."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", (s or "").upper())).strip()

def _has(text, keys):
    return any(re.search(r"(?<![A-Z0-9])" + re.escape(k) + r"(?![A-Z0-9])", text) for k in keys)

def classify_absentee(mailing_state=None, mailing_address=None):
    """True out of state, False in Arizona, None when the record cannot say.

    The old rule was `bool(state) and state != "AZ"`, which resolved a missing
    state to False. Every foreign owner therefore read as a local owner, and an
    owner in Hermosillo looked like an owner in Glendale. Unknown is now None so
    it is never counted as local; `absentee IS TRUE` treats it correctly.
    """
    st = _norm(mailing_state)
    if st in ("AZ", "ARIZONA"):
        return False
    if st in US_STATES or st.replace(" ", "") in US_STATES:
        return True
    addr = _norm(mailing_address)
    if addr:
        if _has(addr, FOREIGN_HINTS):
            return True
        if re.search(r"(?<![A-Z0-9])(AZ|ARIZONA)(?![A-Z0-9])", addr):
            return False
        m = re.findall(r"(?<![A-Z0-9])([A-Z]{2})(?![A-Z0-9])", addr)
        hits = [x for x in m if x in US_STATES]
        if hits:
            return hits[-1] != "AZ"
    return None

def classify_owner_type(owner_name, mailing_state=None, mailing_address=None):
    n = _norm(owner_name)
    if _has(n, BUILDER_KEYS):    owner_type = "Builder/Developer"
    elif _has(n, TRUST_KEYS):    owner_type = "Trust/LLC"
    elif _has(n, ENTITY_KEYS):   owner_type = "Investor"
    else:                        owner_type = "Individual"
    return owner_type, classify_absentee(mailing_state, mailing_address)

# ---- land-use bucketing (replace with the authoritative PUC list) ----------
def bucket_use(puc):
    """PUC arrives as a string from the ArcGIS layer but as an int from CSV and
    JSON exports, where 200 loses its leading zero and read as Improved."""
    p = str(puc if puc is not None else "").strip()
    if not p:
        return "Improved"
    if p.isdigit():
        p = p.zfill(4)
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
    num = sum(weights.get(k, 0) * float(sig.get(k) or 0) for k in SIGS)
    den = sum(weights.get(k, 0) for k in SIGS) or 1
    s = num / den
    # An unrecognised or missing water status is the constrained case, not a
    # free pass. KeyError here used to abort a whole zone load.
    return s * GATE.get(sig.get("water_status"), GATE["groundwater_constrained"]) if gate_on else s

def tenure_score(tenure, owner_type):
    b = max(0, min(100, ((tenure or 0)-2)*4.2))
    f = 0.35 if owner_type=="Builder/Developer" else (0.8 if owner_type=="Investor" else 1.0)
    return b*f

def use_score(use):
    return 100 if use=="Vacant" else (85 if use=="Agricultural" else 25)

def target_score(growth, tenure, owner_type, use):
    return 0.5*growth + 0.3*tenure_score(tenure, owner_type) + 0.2*use_score(use)
