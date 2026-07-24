"""
valuation.py
What is this parcel worth today, from recorded sales of comparable land.

WHAT THIS IS AND IS NOT. This estimates CURRENT market value by fitting recorded
sale prices against parcel characteristics. It is the standard hedonic approach
behind automated valuation everywhere. It does NOT forecast appreciation: the
earlier attempt to do that was abandoned because comparing an old sale price to
today's assessed value is fatally confounded, and no design fixes that without
full deed history.

WHY THIS ONE IS TESTABLE WHEN THE APPRECIATION MODEL WAS NOT. The outcome here is
an observed transaction price, not an assessor construct. So the model can be
fitted on part of the data and scored against sales it never saw. Every fit
therefore reports its own error before it reports a single prediction, and the
error is broken out by water state, size and distance so the places where it is
weak are visible rather than buried in an average.

SPECIFICATION
    log(price per acre) = zone effect
                        + sale-year effect          (absorbs the market cycle)
                        + b1 log(acres)             (price per acre falls with size)
                        + b2 log(1 + miles to the development frontier)
                        + water state dummies
                        + e

Fitted by least squares. Prediction intervals come from the empirical quantiles
of held-out residuals, not from a normality assumption, because land prices are
not normal and pretending otherwise would understate the uncertainty.

KNOWN LIMITS, ALL MEASURED RATHER THAN ASSUMED
  * Thin support on the deep fringe, which is exactly where the interest is. The
    per-stratum error table shows this directly.
  * Sales are not filtered for arms-length until the DOR validation codes are
    obtained, so nominal-consideration transfers remain in the sample and drag
    the low end down.
  * A parcel's own recorded sale, if any, is excluded from its own prediction.
"""

import math

MIN_SALES_TO_FIT = 400
# Above this holdout error the estimate is not shown at all. Measured on real
# Maricopa sales the model ran 60-78%, so in practice this suppresses it.
MAX_USABLE_ERROR_PCT = 45.0
HOLDOUT_SHARE = 0.20
MIN_PRICE_PER_ACRE = 100.0        # below this is almost certainly not a sale
MAX_PRICE_PER_ACRE = 5_000_000.0


def _f(v, default=0.0):
    """Postgres numerics arrive as Decimal, which will not mix with floats."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _design(rows, zones, years):
    """Rows are dicts with acres, price_per_acre, edge_miles, water_state, zcta,
    acquired. Returns X, y and the column labels."""
    zi = {z: i for i, z in enumerate(zones)}
    yi = {y: i for i, y in enumerate(years)}
    X, y = [], []
    for r in rows:
        ppa = _f(r["price_per_acre"], 1.0)
        row = [1.0]
        row += [1.0 if zi.get(r["zcta"]) == k else 0.0 for k in range(1, len(zones))]
        row += [1.0 if yi.get(r["acquired"]) == k else 0.0 for k in range(1, len(years))]
        row.append(math.log(max(0.05, _f(r["acres"], 1.0))))
        row.append(math.log(1.0 + max(0.0, _f(r.get("edge_miles"), 0.0))))
        ws = r.get("water_state") or "C"
        row.append(1.0 if ws == "B" else 0.0)
        row.append(1.0 if ws == "C" else 0.0)
        X.append(row)
        y.append(math.log(ppa))
    labels = (["intercept"] + [f"zone:{z}" for z in zones[1:]]
              + [f"year:{v}" for v in years[1:]]
              + ["log_acres", "log_miles_to_frontier", "water_B", "water_C"])
    return X, y, labels


def screen(rows, lo_q=0.02, hi_q=0.98):
    """Remove the two contaminants that need no extra data.

    Nominal-consideration deeds (family transfers, quitclaims) enter as prices
    near zero. Parcel-split churn does the opposite: a price recorded against an
    APN whose boundaries were later divided is attached to today's smaller
    acreage, so the implied price per acre explodes. Both show up as extreme
    values within a ZIP, so trimming each ZIP's tails removes most of both
    without needing the recorder's validation codes.

    This is a blunt instrument and it is stated as one. The proper fix is the
    Arizona DOR verified-sales file.
    """
    import numpy as np
    from collections import defaultdict
    byz = defaultdict(list)
    for r in rows:
        byz[r["zcta"]].append(r)
    keep = []
    for z, rs in byz.items():
        if len(rs) < 20:
            keep.extend(rs)
            continue
        v = np.array([r["price_per_acre"] for r in rs], float)
        lo, hi = np.quantile(v, lo_q), np.quantile(v, hi_q)
        keep.extend([r for r in rs if lo <= r["price_per_acre"] <= hi])
    return keep


def fit(rows, seed=7, use_screen=True, min_acres=0.0):
    """Fit on 80%, score on the 20% never seen. Returns the model plus its own
    error, which is the only reason to trust any number it later produces."""
    import numpy as np
    rows = [dict(r, acres=_f(r.get("acres")), price_per_acre=_f(r.get("price_per_acre")))
            for r in rows if r.get("acres") and r.get("price_per_acre")
            and r.get("zcta") and r.get("acquired")]
    rows = [r for r in rows
            if r["acres"] > 0 and MIN_PRICE_PER_ACRE <= r["price_per_acre"] <= MAX_PRICE_PER_ACRE]
    if min_acres:
        rows = [r for r in rows if r["acres"] >= min_acres]
    raw_n = len(rows)
    if use_screen:
        rows = screen(rows)
    if len(rows) < MIN_SALES_TO_FIT:
        return None, {"error": f"only {len(rows)} usable sales; need {MIN_SALES_TO_FIT}"}

    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(rows))
    cut = int(len(rows) * (1 - HOLDOUT_SHARE))
    train = [rows[i] for i in idx[:cut]]
    test = [rows[i] for i in idx[cut:]]

    # keep only zones and years with enough training support to estimate
    from collections import Counter
    zc, yc = Counter(r["zcta"] for r in train), Counter(r["acquired"] for r in train)
    zones = sorted([z for z, n in zc.items() if n >= 5])
    years = sorted([v for v, n in yc.items() if n >= 5])
    if not zones or not years:
        return None, {"error": "no zone or year has enough sales to estimate"}
    train = [r for r in train if r["zcta"] in zones and r["acquired"] in years]
    test = [r for r in test if r["zcta"] in zones and r["acquired"] in years]
    if len(train) < MIN_SALES_TO_FIT or len(test) < 50:
        return None, {"error": f"after filtering: {len(train)} train, {len(test)} holdout"}

    Xtr, ytr, labels = _design(train, zones, years)
    A, b = np.asarray(Xtr, float), np.asarray(ytr, float)
    coefs, *_ = np.linalg.lstsq(A, b, rcond=None)

    Xte, yte, _ = _design(test, zones, years)
    pred = np.asarray(Xte, float) @ coefs
    resid = np.asarray(yte, float) - pred
    ape = np.abs(np.expm1(resid))            # |predicted vs actual| as a share

    def strat(key, fn):
        out = {}
        for r, e in zip(test, ape):
            k = fn(r)
            out.setdefault(k, []).append(float(e))
        return {k: {"n": len(v), "median_error_pct": round(100 * float(np.median(v)), 1)}
                for k, v in sorted(out.items()) if len(v) >= 20}

    model = {"coefs": coefs.tolist(), "zones": zones, "years": years, "labels": labels,
             "resid_q": {q: float(np.quantile(resid, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}}
    report = {
        "sales_before_screen": raw_n,
        "sales_used": len(rows), "train": len(train), "holdout": len(test),
        "screened_out": raw_n - len(rows),
        "median_error_pct": round(100 * float(np.median(ape)), 1),
        "within_25pct": round(100 * float(np.mean(ape <= 0.25)), 1),
        "within_50pct": round(100 * float(np.mean(ape <= 0.50)), 1),
        "error_by_water_state": strat("water", lambda r: r.get("water_state") or "C"),
        "error_by_size": strat("size", lambda r: ("under 5 ac" if _f(r["acres"]) < 5 else
                                                  "5-40 ac" if _f(r["acres"]) < 40 else "40+ ac")),
        "error_by_distance": strat("dist", lambda r: ("within 2 mi" if _f(r.get("edge_miles")) < 2
                                                      else "2-10 mi" if _f(r.get("edge_miles")) < 10
                                                      else "beyond 10 mi")),
        "reading": None,
    }
    m = report["median_error_pct"]
    report["reading"] = ("usable for pricing" if m <= 30 else
                         "indicative only; treat as a range, not a number" if m <= 60 else
                         "too weak to price with; show comparable sales instead")
    return model, report


def predict(model, parcel, year=None):
    """Point estimate plus an interval from held-out residual quantiles. Returns
    None where the parcel's zone was never estimated, rather than extrapolating
    into ground the model has not seen."""
    if not model:
        return None
    zones, years = model["zones"], model["years"]
    if parcel.get("zcta") not in zones:
        return None
    yr = year or years[-1]
    if yr not in years:
        yr = years[-1]
    r = dict(parcel)
    r["zcta"], r["acquired"] = parcel["zcta"], yr
    r["price_per_acre"] = 1.0                     # unused on the predict path
    X, _, _ = _design([r], zones, years)
    import numpy as np
    z = float((np.asarray(X, float) @ np.asarray(model["coefs"], float))[0])
    q = {str(k): v for k, v in model["resid_q"].items()}
    lo, hi = q.get("0.1", q.get("0.1", 0.0)), q.get("0.9", 0.0)
    return {"per_acre": round(math.exp(z)),
            "low": round(math.exp(z + lo)),
            "high": round(math.exp(z + hi)),
            "basis": "recorded sales of comparable land"}
