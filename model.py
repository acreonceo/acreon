"""
model.py
Land valuation for Acreon, rebuilt on the architecture from the economist review.

The prior model treated raw land as a growth asset: it mapped a 0-100 zone index
to an appreciation rate and compounded it. That is wrong for this asset. Fringe
land pays nothing, sits flat for years, then steps up when it becomes developable.
Value is the discounted expectation of one lumpy payoff.

So value is now:

    V = SUM_t  S(t-1) * h(t) * (1-ts) * D(t) * e^(-rho t)     conversion payoff
      + S(T) * V_ag * e^(-rho T)                              never-converts floor
      - SUM_t  S(t) * carry * e^(-rho t)                      cost of waiting

    h(t) = w(t) * h_base(zone)      annual hazard of conversion
    S(t) = prod (1 - h(s))          probability still unconverted
    w(t) = water feasibility, TIME-DEPENDENT and per-parcel (see WATER STATES)
    D(t) = D0 * e^(g_D t)           what a developer pays per acre, drifting

PERFORMANCE NOTE. V is linear in D0 and in current value once the hazard path is
fixed, and the hazard path depends only on (zone growth, water state). So we
precompute three coefficients per (zone, state) pair and each parcel costs two
multiplications. 150 zones x 3 states = 450 paths instead of 152,000.

WATER STATES (the binding constraint on this asset class)
  A  inside a designated provider service area / holds an AWS certificate.
     w(t) = 1. Developable now.
  B  irrigated agricultural land with grandfathered irrigation rights.
     Post-SB1611 (2025) these can be relinquished for groundwater savings credits
     that satisfy assured-supply physical availability within one mile. So this
     land has a real conversion path. w(t) starts high and rises.
  C  raw groundwater-dependent land. Blocked for subdivision since ADWR's 2023
     halt. Resolves only if absorbed into a provider (lambda_s) or if policy
     changes (lambda_p). w(t) = 1 - exp(-(lambda_s + lambda_p) t).

WHAT IS ESTIMATED VS ASSUMED. Right now h_base, lambda_s, lambda_p and g_D are
judgment, exposed as user assumptions rather than buried. D0 is measured from
observed prices of assured-supply land. The next build replaces h_base with a
hazard fit on construction-year data and D0 with a hedonic surface.
"""

from math import exp

# ---------------------------------------------------------------- assumptions
DEFAULTS = {
    "h_max":     0.060,   # annual conversion hazard in the hottest corridor  (judgment)
    "h_min":     0.001,   # ...and in dead desert                             (judgment)
    "lambda_s":  0.050,   # per-yr hazard a State C parcel joins a provider   (judgment)
    "lambda_p":  0.015,   # per-yr hazard of statewide policy resolution      (judgment)
    "g_D":       0.030,   # drift of developable-land prices                  (estimate later)
    "rho":       0.058,   # discount = long treasury ~4.3% + 1.5% illiquidity
    "sell_cost": 0.080,   # brokerage + closing on land
    "wB0":       0.60,    # State B feasibility today                         (judgment)
    "wB1":       0.90,    # State B feasibility once the program matures      (judgment)
    "lambda_B":  0.15,    # speed State B approaches wB1                      (judgment)
    "horizon":   30,      # years of simulation
}

HORIZON_MARKS = (5, 10, 20, 30)


def water_w(state, t, p, zone_heat=1.0):
    """Feasibility of ever being allowed to subdivide, as a function of time.

    zone_heat (0-1) scales the provider-absorption hazard for State C. A raw
    parcel two miles from an expanding service boundary gets absorbed at a very
    different rate than one twenty miles out, and applying a single lambda_s to
    both massively overvalues deep desert. Until distance-to-boundary is
    computed directly, the cleaned zone index stands in for frontier proximity.
    """
    if state == "A":
        return 1.0
    if state == "B":
        return p["wB0"] + (p["wB1"] - p["wB0"]) * (1.0 - exp(-p["lambda_B"] * t))
    lam = p["lambda_s"] * max(0.0, min(1.0, zone_heat)) + p["lambda_p"]
    return 1.0 - exp(-lam * t)


def hazard_base(growth_index, p):
    """Annual conversion hazard before the water gate. Linear in the cleaned zone
    index: the index measures how fast the frontier is moving, which is about
    TIMING, not about the rate land drifts upward."""
    gi = max(0.0, min(100.0, growth_index or 0.0))
    return p["h_min"] + (p["h_max"] - p["h_min"]) * gi / 100.0


# The frontage metric is computed from the parcel fabric, but that fabric only
# contains vacant and agricultural land. Rural areas are almost entirely vacant/
# ag, so a rural parcel sees every neighbour and reads as enclosed; in town the
# neighbours are built parcels absent from the table, so nothing reads as
# enclosed. The measure therefore flags rural land for being rural: it marked
# 46,106 of 152,359 parcels, roughly 30%, where the true rate is a few percent.
# It stays recorded but MUST NOT move value until it is validated against a road
# centreline layer. Flip this on only after that check.
APPLY_LANDLOCK_DISCOUNT = False


def site_factor(landlocked, flood_zone):
    """Multiplier on what a developer will pay. Floodway land is not developable
    at any water status; the 1-percent-annual-chance zones are buildable with
    mitigation."""
    f = 1.0
    if landlocked and APPLY_LANDLOCK_DISCOUNT:
        f *= 0.35                      # no legal access: a 50-90% discount
    z = (flood_zone or "").upper()
    if z == "FLOODWAY":
        f *= 0.10                      # not developable at any water status
    elif z:
        f *= 0.75                      # 100-yr floodplain: buildable with mitigation
    return f


def path(growth_index, state, p=None, hazard_override=None):
    """Precompute the (zone, state) hazard path. Returns the three coefficients
    that make per-parcel valuation two multiplications, plus timing outputs.

      payoff_coef : multiply by D0 (developer $/acre today)
      carry_coef  : multiply by annual carry $/acre
      floor_coef  : multiply by holding-value $/acre
    """
    p = {**DEFAULTS, **(p or {})}
    # A fitted hazard (estimated from construction-year history) takes precedence
    # over the judgment mapping from the zone index.
    h0 = hazard_override if hazard_override is not None else hazard_base(growth_index, p)
    T = int(p["horizon"])
    ts = p["sell_cost"]

    heat = max(0.0, min(1.0, (growth_index or 0.0) / 100.0))
    S = 1.0
    payoff = 0.0
    carry = 0.0
    p50 = None
    marks = {}
    for t in range(1, T + 1):
        h = h0 * water_w(state, t, p, heat)
        h = max(0.0, min(0.9, h))
        disc = exp(-p["rho"] * t)
        # payoff if it converts exactly in year t
        payoff += S * h * (1.0 - ts) * exp(p["g_D"] * t) * disc
        # carry is paid while still holding
        carry += S * disc
        S *= (1.0 - h)
        converted = 1.0 - S
        if p50 is None and converted >= 0.5:
            p50 = t
        if t in HORIZON_MARKS:
            marks[t] = converted
    return {
        "payoff_coef": payoff,
        "carry_coef": carry,
        "floor_coef": S * exp(-p["rho"] * T),
        "p50_years": p50,                 # None => never reaches 50% within horizon
        "p_convert": marks,               # {5: .., 10: .., 20: .., 30: ..}
        "hazard0": h0,
    }


def value_per_acre(coefs, d0_per_acre, hold_value_per_acre, carry_rate, site=1.0):
    """V per acre. Linear in the precomputed path coefficients.

    carry_rate is a fraction of holding value per year (property tax + minimal
    upkeep), computed per parcel from the tax roll rather than assumed flat.
    """
    d0 = max(0.0, d0_per_acre or 0.0)
    hv = max(0.0, hold_value_per_acre or 0.0)
    return (coefs["payoff_coef"] * d0 * site
            + coefs["floor_coef"] * hv
            - coefs["carry_coef"] * carry_rate * hv)


# ------------------------------------------------------------------ water state
def water_state(use, in_aws_area):
    """A: already served. B: irrigated ag with a conversion path under SB1611.
    C: raw groundwater-dependent land.

    NOTE: State B is approximated by the assessor's agricultural classification.
    True IGFR status requires ADWR's grandfathered-rights registry, which is a
    later data pull. This proxy will include some dry-farmed or grazing land that
    holds no irrigation right, which overstates its feasibility.
    """
    if in_aws_area:
        return "A"
    if use == "Agricultural":
        return "B"
    return "C"


# ------------------------------------------------------------ carrying cost
# Arizona legal class 2 (vacant/agricultural) is assessed at 15% of limited
# property value. Ag-classified land is assessed on use value, so effective carry
# against market value is far below the 0.7% flat rate the old model assumed.
ASSESS_RATIO = 0.15
DEFAULT_TAX_RATE = 0.011      # ~$11 per $100 of assessed value, Maricopa composite
UPKEEP = 0.001                # insurance, weed abatement, minimal maintenance


def carry_rate(fcv, lpv, tax_rate=DEFAULT_TAX_RATE):
    """Annual carry as a fraction of market value."""
    if not fcv or fcv <= 0:
        return UPKEEP + 0.002
    lpv = lpv if (lpv and lpv > 0) else fcv * 0.8
    return (tax_rate * ASSESS_RATIO * lpv) / fcv + UPKEEP


# --------------------------------------------------------- acquisition score
# Kept deliberately separate from value. A parcel is not worth more because its
# owner has held it 40 years; it is more ACQUIRABLE, possibly at a discount.
# Mixing the two, as the old 50/30/20 blend did, produced a number that was
# neither a value nor a probability.
ACQ = {
    "intercept": -1.20,
    "per_year_held": 0.045,     # capped at 40 years
    "absentee": 0.50,
    "trust_estate": 0.40,
    "no_recorded_sale": 0.60,   # inherited/exempt transfer: often the most motivated
    "builder": -0.80,
    "investor": -0.20,
}


def acquisition_score(tenure, owner_type, absentee, has_recorded_sale, coef=None):
    """0-100 likelihood this owner is approachable. Coefficients are judgment
    until fit on observed listings/sales."""
    c = {**ACQ, **(coef or {})}
    z = c["intercept"]
    z += c["per_year_held"] * min(40, tenure or 0)
    if absentee:
        z += c["absentee"]
    if owner_type == "Trust/LLC":
        z += c["trust_estate"]
    if not has_recorded_sale:
        z += c["no_recorded_sale"]
    if owner_type == "Builder/Developer":
        z += c["builder"]
    elif owner_type == "Investor":
        z += c["investor"]
    return 100.0 / (1.0 + exp(-z))


# ------------------------------------------------------------- cleaned index
# Removed from the old seven-signal index:
#   zoning_activity  - was an exact duplicate of permit_velocity (same source)
#   infra_water      - double-counted water, which belongs only in the gate
#   schools          - never wired to data, constant 50 for every zone
# Those three were ~57% of the old weighted index: one signal counted twice plus
# two constants that only compressed everything toward the middle.
SIG_KEYS = ["developable_land", "permit_velocity", "infra_transport", "migration"]
# NOTE: "permit_velocity" is the DB column name; it holds the sales-velocity measure.
DEFAULT_WEIGHTS = {"developable_land": 55, "permit_velocity": 60,
                   "infra_transport": 60, "migration": 60}


def growth_index(signals, weights=None):
    """0-100 index of how fast the development frontier is moving toward a zone.
    Feeds the hazard only. It no longer touches the appreciation rate, and it is
    no longer multiplied by the water gate: water is a separate, time-dependent
    feasibility term applied inside the hazard."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    num = den = 0.0
    for k in SIG_KEYS:
        v = signals.get(k)
        if v is None:
            continue
        num += w[k] * float(v)
        den += w[k]
    return num / den if den else 0.0
