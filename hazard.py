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
DIST_BINS = [0.25, 0.5, 1.0, 2.0, 5.0, 10.0]   # miles; 7 bins including the tail
PERIODS = [1990, 1995, 2000, 2005, 2010, 2015, 2020]


def bin_index(miles):
    if miles is None:
        return len(DIST_BINS)
    for i, edge in enumerate(DIST_BINS):
        if miles < edge:
            return i
    return len(DIST_BINS)


def design_row(period, miles, acres):
    """Intercept, period dummies (first period is the reference), distance-bin
    dummies (first bin is the reference), log acres."""
    row = [1.0]
    for p in PERIODS[1:]:
        row.append(1.0 if period == p else 0.0)
    b = bin_index(miles)
    for i in range(1, len(DIST_BINS) + 1):
        row.append(1.0 if b == i else 0.0)
    row.append(math.log(max(0.1, acres or 1.0)))
    return row


N_FEATURES = 1 + (len(PERIODS) - 1) + len(DIST_BINS) + 1


def fit_logit(X, y, l2=1e-3, iters=60, tol=1e-9):
    """Newton/IRLS logistic regression. Small ridge term keeps the Hessian well
    conditioned when a distance bin is thin."""
    import numpy as np
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, k = X.shape
    b = np.zeros(k)
    for _ in range(iters):
        z = np.clip(X @ b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        w = p * (1 - p) + 1e-9
        g = X.T @ (y - p) - l2 * b
        H = (X.T * w) @ X + l2 * np.eye(k)
        try:
            step = np.linalg.solve(H, g)
        except Exception:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        b = b + step
        if float(np.max(np.abs(step))) < tol:
            break
    return b.tolist()


def predict_p5(coefs, period, miles, acres):
    z = sum(c * x for c, x in zip(coefs, design_row(period, miles, acres)))
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def annual_hazard(p5):
    """5-year probability -> annual hazard, shifted earlier by the construction
    lag so the payoff is dated when the speculator actually gets paid."""
    p5 = max(1e-6, min(0.999, p5))
    h = 1.0 - (1.0 - p5) ** (1.0 / 5.0)
    # bring the event forward: same cumulative odds reached sooner
    return min(0.5, h * (5.0 / max(1.0, 5.0 - LAG_YEARS)))


def summarize(coefs):
    """Readable coefficient report, so the fit can be sanity-checked rather than
    trusted. Distance effects are relative to the nearest bin."""
    out = {"intercept": round(coefs[0], 4), "periods": {}, "distance_bins": {}}
    i = 1
    for p in PERIODS[1:]:
        out["periods"][p] = round(coefs[i], 4); i += 1
    labels = [f"<{DIST_BINS[0]}mi (reference)"]
    for j in range(len(DIST_BINS)):
        hi = DIST_BINS[j + 1] if j + 1 < len(DIST_BINS) else None
        labels.append(f"{DIST_BINS[j]}-{hi}mi" if hi else f">{DIST_BINS[-1]}mi")
    out["distance_bins"][labels[0]] = 0.0
    for j in range(1, len(labels)):
        out["distance_bins"][labels[j]] = round(coefs[i], 4); i += 1
    out["log_acres"] = round(coefs[i], 4)
    return out
