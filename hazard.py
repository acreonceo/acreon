"""
hazard.py
Estimates the development conversion hazard from Maricopa's own history.

THE IDEA. Every improved parcel carries a construction year. That is a census of
development events going back decades, with no survivorship problem: we observe
every parcel that converted and every parcel that did not. From it we can
reconstruct the development frontier at any past year (the built set at year t is
simply every parcel whose construction year is <= t), measure how far each
still-vacant parcel was from that frontier, and then estimate how strongly
distance-to-frontier predicted conversion over the following five years.

That replaces the judgment hazard (h_max sliding from 0.1% to 6%) with a fitted
one, which was the highest-value recommendation in the economist review.

SPECIFICATION
    Panel:      parcel x 5-year period, 1990 through 2020
    At risk:    parcel not yet built at the start of the period
    Event:      construction year falls inside the period
    Model:      discrete-time logit
                logit P(convert in period) = period FE + f(distance to frontier)
                                             + b*log(acres)
    f() is piecewise constant over distance bins rather than linear, because
    leapfrog development makes the effect non-monotonic: builders skip over held
    land, so hazard does not fall smoothly with distance.

    The fitted 5-year probability converts to an annual hazard:
        h_annual = 1 - (1 - p5)^(1/5)

BIASES WE KNOW ABOUT (from the review, worth restating where the code lives)
  * Construction lags the speculator's payoff. The owner sells to a developer
    one to four years before anything is built, so a hazard fit on construction
    dates the payoff late. LAG_YEARS shifts for this.
  * Zombie subdivisions: land platted before 2008 and never built reads as
    unconverted even though the landowner was paid. Period fixed effects absorb
    most of this.
  * Teardowns misdate original conversion. Rare on fringe land.
"""

import math

LAG_YEARS = 2          # payoff precedes construction by roughly this much
DIST_BINS = [0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0]   # miles; 8 bins including the tail
# The 20-mile edge matters in Maricopa: without it the far west desert around
# Tonopah shares a bin with land a few miles outside Surprise, and the model
# cannot tell them apart.
PERIODS = [1990, 1995, 2000, 2005, 2010, 2015, 2020]


MIN_EVENTS_PER_BIN = 40


def bin_index(miles, bins=None):
    """Index of the distance bin. A null distance returns None rather than the
    tail bin: silently filing unknown distance as ">20 miles" made a missing
    join look like the most remote land in the county."""
    bins = DIST_BINS if bins is None else bins
    if miles is None:
        return None
    for i, edge in enumerate(bins):
        if miles < edge:
            return i
    return len(bins)


def pool_bins(rows, bins=None, min_events=MIN_EVENTS_PER_BIN):
    """Merge distance bins that carry too few conversion events to estimate.

    A bin with almost no events drives its coefficient to an extreme value held
    finite only by the ridge penalty, which then reverses against the next bin
    and makes remote land look more developable than mid-distance land. Pooling
    sparse cells is the honest fix: you cannot estimate a cell with no events.

    rows: (period, event, acres, miles)
    """
    bins = list(DIST_BINS if bins is None else bins)
    while True:
        counts = [0] * (len(bins) + 1)
        exposure = [0] * (len(bins) + 1)
        for _, e, _, d in rows:
            i = bin_index(d, bins)
            if i is None:
                continue
            exposure[i] += 1
            counts[i] += int(e)
        thin = [i for i, c in enumerate(counts) if c < min_events]
        if not thin or len(bins) == 1:
            return bins, counts, exposure
        i = thin[0]
        # drop the edge that merges the sparse cell into its neighbour
        bins.pop(min(i, len(bins) - 1))


def design_row(period, miles, acres=None, bins=None):
    """Intercept, period dummies (first period is the reference), distance-bin
    dummies (first bin is the reference).

    `acres` is accepted and ignored. It used to enter as log(acres) with a
    strongly negative coefficient, which read as "large parcels do not convert"
    but was an artefact of how the panel is built. When raw ground converts it is
    subdivided, so the converted records are post-subdivision lots of a fifth of
    an acre while the unconverted records are whole tracts. Size was therefore
    perfectly confounded with the outcome: a parcel is small BECAUSE it converted.
    Ranking on it put every large parcel in the bottom quintile regardless of
    where it sat, including infill ground a few hundred feet from built houses.
    Scale now enters as an observation weight instead (see fit_logit), which is
    subdivision-invariant. This also matches the backtest, whose own scorer takes
    distance alone.
    """
    bins = DIST_BINS if bins is None else bins
    if miles is None:
        raise ValueError("design_row needs a distance; callers must drop null edge_miles")
    row = [1.0]
    for p in PERIODS[1:]:
        row.append(1.0 if period == p else 0.0)
    b = bin_index(miles, bins)
    for i in range(1, len(bins) + 1):
        row.append(1.0 if b == i else 0.0)
    return row


def n_features(bins=None):
    bins = DIST_BINS if bins is None else bins
    return 1 + (len(PERIODS) - 1) + len(bins)


# Kept for callers that import it, but it is only correct for unpooled bins.
# pool_bins can shrink the bin list, so prefer n_features(bins).
N_FEATURES = n_features()


def fit_logit(X, y, w=None, l2=1e-3, iters=60, tol=1e-9):
    """Newton/IRLS logistic regression, optionally weighted.

    w is an exposure weight per observation. Passing acres makes the fit measure
    the share of LAND that converts rather than the share of parcel records,
    which is the quantity that does not change when a tract is cut into lots.
    Unweighted, one 25-acre conversion enters as roughly a hundred events while
    one 25-acre non-conversion enters as a single non-event, which inflates the
    baseline hazard by about the average lots-per-tract.
    """
    import numpy as np
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, k = X.shape
    ow = np.ones(n) if w is None else np.clip(np.asarray(w, dtype=float), 1e-6, None)
    b = np.zeros(k)
    for _ in range(iters):
        z = np.clip(X @ b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        mu = ow * (p * (1 - p)) + 1e-9
        g = X.T @ (ow * (y - p)) - l2 * b
        H = (X.T * mu) @ X + l2 * np.eye(k)
        try:
            step = np.linalg.solve(H, g)
        except Exception:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        b = b + step
        if float(np.max(np.abs(step))) < tol:
            break
    return b.tolist()


def predict_p5(coefs, period, miles, acres=None, bins=None):
    z = sum(c * x for c, x in zip(coefs, design_row(period, miles, acres, bins)))
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def annual_hazard(p5):
    """5-year probability -> annual hazard, shifted earlier by the construction
    lag so the payoff is dated when the speculator actually gets paid.

    The shift compresses the same cumulative probability into the shorter window
    the owner actually waits. The old form multiplied the annual rate by
    5/(5-LAG), which is a good approximation at low hazard but overstates it
    badly once p5 is large, and then collided with the 0.5 cap.
    """
    p5 = max(1e-6, min(0.999, p5))
    yrs = max(1.0, 5.0 - LAG_YEARS)
    return min(0.5, 1.0 - (1.0 - p5) ** (1.0 / yrs))


def summarize(coefs, bins=None, counts=None, exposure=None):
    """Readable coefficient report, so the fit can be sanity-checked rather than
    trusted. Distance effects are relative to the nearest bin."""
    out = {"intercept": round(coefs[0], 4), "periods": {}, "distance_bins": {}}
    i = 1
    for p in PERIODS[1:]:
        out["periods"][p] = round(coefs[i], 4); i += 1
    b = DIST_BINS if bins is None else bins
    labels = [f"<{b[0]}mi (reference)"]
    for j in range(len(b)):
        hi = b[j + 1] if j + 1 < len(b) else None
        labels.append(f"{b[j]}-{hi}mi" if hi else f">{b[-1]}mi")
    out["distance_bins"][labels[0]] = 0.0
    for j in range(1, len(labels)):
        out["distance_bins"][labels[j]] = round(coefs[i], 4); i += 1
    if counts is not None:
        out["events_per_bin"] = {labels[k]: counts[k] for k in range(len(labels))}
        out["rows_per_bin"] = {labels[k]: exposure[k] for k in range(len(labels))}
        out["bins_pooled"] = (bins is not None and list(bins) != list(DIST_BINS))
    return out
