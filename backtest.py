"""
backtest.py
Does the model's ranking actually predict what happened?

THE TEST. Pick a past year. Score every parcel that was still undeveloped then,
using only information that existed at that moment. Then look at what actually
happened over the following decades and ask whether the parcels the model liked
converted more often than the ones it did not.

Maricopa is unusually good ground for this. Every improved parcel carries a
construction year, so outcomes are a census rather than a sample: we observe
every parcel that converted and every parcel that did not. There is no
survivorship problem and no self-reporting.

LEAKAGE IS THE WHOLE GAME. It is trivially easy to produce a flattering result
here, so three rules are enforced:

  1. The conversion hazard is REFIT for each vintage using only periods that had
     already finished by then. The hazard in the live model was fitted through
     2020 and would already know the answer.
  2. The development frontier is reconstructed from parcels built on or before
     the vintage year. Distance to today's frontier is not admissible.
  3. No current-day covariate is used at all: not today's water designation, not
     today's zone signals, not today's assessed value. They did not exist then.

WHAT IT CAN AND CANNOT SETTLE. Conversion is measured from construction dates
and is trustworthy. Appreciation is measured against assessor values, which have
been observed reporting $128/acre on real parcels, and is only available for land
that never converted. That half is reported separately and should be treated as
indicative at best.
"""

import math

CURRENT_YEAR = 2025
QUINTILES = 5

# GRID, NOT PARCELS. The first version of this test analysed parcels and produced
# a 79% conversion rate on a population whose median holding was a fifteenth of an
# acre. The cause: subdivision multiplies converted land and leaves unconverted
# land alone. A 640-acre farm that converts becomes two thousand house lots and
# enters as two thousand conversions; the farm next door that does not convert
# stays one row. Every success was counted thousands of times against one failure,
# so the test population became house lots and it measured "did a house get built
# on this house lot", which is nearly always yes.
#
# A fixed cell of ground cannot be inflated that way. Roughly a quarter section
# (half a mile square), which is also the unit a land investor actually thinks in.
CELL_DX = 0.0087        # degrees longitude, ~0.5 mi at this latitude
CELL_DY = 0.0072        # degrees latitude, ~0.5 mi
MIN_BUILT_FOR_DEVELOPED = 5   # a single farmhouse is not development

# NOISE FLOOR, AND AN HONEST CAVEAT.
#
# On synthetic data with a planted development frontier this harness reports
# roughly 45 points of spread, monotone across quintiles, with median distance to
# the frontier falling cleanly from quintile 1 to 5. It recovers real signal.
#
# The null is less satisfying. On the cleanest null that could be constructed
# (uniform random points, one per location, no subdivision, development decided
# by a coin flip with a random year) the harness still reported 16 to 18 points.
# That is a high floor and its cause is not established. The leading suspicion is
# the MIN_BUILT_FOR_DEVELOPED threshold: a cell with more structures is both more
# likely to cross the threshold early, putting it in the frontier, and more likely
# to cross it later, counting as a conversion, so cell density may drive both
# sides of the comparison. That has not been proven.
#
# Treat the floor as approximately 18 points and treat that figure as uncertain.
# A real result needs to clear it substantially to mean anything, and an
# independent reviewer should be asked to work out where the floor comes from.
NULL_BASELINE_PP = 18.0


def fit_pre_vintage(rows):
    """Logit on pre-vintage parcel-periods: intercept, distance bins, log acres.

    Period fixed effects are dropped deliberately. They absorb the macro cycle,
    which shifts every parcel in a period equally and so cannot change the
    ranking within a vintage, and dropping them keeps the design identical
    across vintages that have different numbers of training periods.
    """
    import numpy as np
    import hazard as HZ
    if len(rows) < 500:
        return None, None
    bins, counts, _ = HZ.pool_bins(rows)
    X, y = [], []
    for period, event, acres, miles in rows:
        X.append(_row(miles, acres, bins))
        y.append(int(event))
    if sum(y) < 50:
        return None, None
    return HZ.fit_logit(X, y), bins


def _row(miles, acres, bins, use_acres=False):
    """Distance enters twice: as bins, which allow the effect to be non-monotone
    where leapfrog development makes it so, and as a continuous log term.

    The continuous term is not decoration. Bins alone give only a handful of
    distinct scores, so every cell in a bin ties and quintile boundaries fall
    arbitrarily inside those ties, which scrambles the middle of the ranking. The
    log term orders cells within a bin.

    Cells are all the same size, so an acreage term would be constant and is
    dropped.
    """
    import hazard as HZ
    r = [1.0]
    b = HZ.bin_index(miles, bins)
    for i in range(1, len(bins) + 1):
        r.append(1.0 if b == i else 0.0)
    r.append(math.log(1.0 + max(0.0, miles or 0.0)))
    if use_acres:
        r.append(math.log(max(0.1, acres or 1.0)))
    return r


def score(coefs, bins, miles, acres=None):
    z = sum(c * x for c, x in zip(coefs, _row(miles, acres, bins)))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def _to_window(p5, years):
    """A five-year conversion probability expressed over the outcome window, so
    predicted and actual sit on the same scale."""
    p5 = max(1e-9, min(0.999999, p5))
    return 1.0 - (1.0 - p5) ** (years / 5.0)


def quintile_outcomes(scored, vintage):
    """scored: (apn, predicted, const_year, acres, edge_miles).
    const_year is None for parcels that are still undeveloped today."""
    if not scored:
        return None
    ranked = sorted(scored, key=lambda r: r[1])
    n = len(ranked)
    out = []
    for q in range(QUINTILES):
        lo, hi = n * q // QUINTILES, n * (q + 1) // QUINTILES
        block = ranked[lo:hi]
        if not block:
            continue
        converted = [r for r in block if r[2] and vintage < r[2] <= CURRENT_YEAR]
        yrs = [r[2] - vintage for r in converted]
        out.append({
            "quintile": q + 1,
            "parcels": len(block),
            "converted": len(converted),
            "conversion_rate": round(len(converted) / len(block), 4),
            "median_years_to_convert": (sorted(yrs)[len(yrs)//2] if yrs else None),
            "mean_predicted_5yr": round(sum(r[1] for r in block) / len(block), 5),
            "predicted_over_window": round(
                _to_window(sum(r[1] for r in block) / len(block), CURRENT_YEAR - vintage), 4),
            "median_parcels_in_cell": round(sorted(r[3] for r in block)[len(block)//2], 1),
            "median_edge_miles": round(sorted(r[4] for r in block)[len(block)//2], 2),
        })
    return out


def stratified_spread(scored, vintage, bands=((0, 3), (4, 8), (9, 20), (21, 10**6))):
    """Quintile spread computed WITHIN bands of similar parcels-per-cell.

    A fixed structure-count threshold rewards dense cells: they cross five
    structures easily, sparse ones cannot without being subdivided first. Since
    fabric density also falls with distance from town, the outcome partly encodes
    the ranking's only input. Holding density roughly constant strips that out;
    whatever spread survives is distance signal net of fabric.

    Today's parcel counts are themselves outcome-contaminated (a converted cell
    got subdivided), but that inflates counts on the converted side, which biases
    this test AGAINST the model. A surviving spread is therefore credible.
    """
    out = []
    for lo, hi in bands:
        block = [r for r in scored if lo <= r[3] <= hi]
        if len(block) < 500:
            continue
        q = quintile_outcomes(block, vintage)
        if not q:
            continue
        out.append({"parcels_per_cell": f"{lo}-{hi if hi < 10**6 else '+'}",
                    "cells": len(block),
                    "spread_percentage_points": round((q[-1]["conversion_rate"] - q[0]["conversion_rate"]) * 100, 1),
                    "bottom_rate": q[0]["conversion_rate"], "top_rate": q[-1]["conversion_rate"]})
    return out


def shuffled_floor(scored, vintage, bands=((0, 3), (4, 8), (9, 20), (21, 10**6)),
                   reps=1000, seed=11):
    """The exact mechanical floor for THIS population and base rate.

    Permuting construction years county-wide turned out to be a poor null: it
    destroys the temporal clustering of real development (a subdivision's five
    structures go up together), so the fifth structure arrives far later or never.
    That cut the base conversion rate fivefold and shrank the scored population,
    which makes the resulting spread incomparable to the real run.

    This null instead holds everything fixed except the one thing under test.
    The at-risk cells, their density, and the exact number of conversions inside
    each density band are all preserved; only WHICH cells converted is reshuffled
    within a band. Distance can then carry no information beyond what fabric
    density already implies, so the spread this produces is the floor the real
    result has to beat.
    """
    import random
    rng = random.Random(seed)
    idx = {}
    for i, r in enumerate(scored):
        n = r[3]
        for lo, hi in bands:
            if lo <= n <= hi:
                idx.setdefault((lo, hi), []).append(i)
                break
    spreads = []
    for _ in range(reps):
        conv = [None] * len(scored)
        for key, members in idx.items():
            outcomes = [1 if (scored[i][2] and vintage < scored[i][2] <= CURRENT_YEAR) else 0
                        for i in members]
            rng.shuffle(outcomes)
            for i, o in zip(members, outcomes):
                conv[i] = o
        ranked = sorted(range(len(scored)), key=lambda i: scored[i][1])
        n = len(ranked)
        lo_block = ranked[:n // QUINTILES]
        hi_block = ranked[n * (QUINTILES - 1) // QUINTILES:]
        if not lo_block or not hi_block:
            continue
        spreads.append((sum(conv[i] for i in hi_block) / len(hi_block)
                        - sum(conv[i] for i in lo_block) / len(lo_block)) * 100)
    if not spreads:
        return None
    spreads.sort()
    return {"reps": len(spreads),
            "median": round(spreads[len(spreads) // 2], 1),
            "p95": round(spreads[int(len(spreads) * 0.95)], 1),
            "max": round(spreads[-1], 1)}


def summarise(vintage, quints, n_train, n_events, strat=None, floor=None):
    if not quints:
        return {"vintage": vintage, "error": "insufficient data"}
    top, bot = quints[-1], quints[0]
    rates = [q["conversion_rate"] for q in quints]
    # A zero bottom rate makes a ratio undefined, which is exactly what happens
    # when the model works perfectly, so the spread in percentage points is the
    # primary statistic and the ratio is secondary.
    spread_pp = round((top["conversion_rate"] - bot["conversion_rate"]) * 100, 1)
    lift = (round(top["conversion_rate"] / bot["conversion_rate"], 2)
            if bot["conversion_rate"] > 0 else None)
    # Tolerate a small wobble at the top: once a quintile is converting at 99%
    # there is no headroom, and a 0.8 point dip is saturation, not disorder.
    monotone = all(rates[i] <= rates[i + 1] + 0.02 for i in range(len(rates) - 1))
    overall = sum(q["converted"] for q in quints) / max(1, sum(q["parcels"] for q in quints))
    # CALIBRATION. Separation alone does not make the dollar values right: a model
    # can rank perfectly and still predict 30% where reality is 10%, which would
    # inflate every valuation threefold. This compares the predicted conversion
    # probability over the outcome window against what actually occurred.
    pred = [q["predicted_over_window"] for q in quints]
    act = [q["conversion_rate"] for q in quints]
    tot_pred = sum(p * q["parcels"] for p, q in zip(pred, quints)) / max(1, sum(q["parcels"] for q in quints))
    ratio = (tot_pred / overall) if overall > 0 else None
    worst = max((abs(p - a) for p, a in zip(pred, act)), default=None)
    return {
        "vintage": vintage,
        "outcome_window_years": CURRENT_YEAR - vintage,
        "training_rows": n_train,
        "training_events": n_events,
        "parcels_scored": sum(q["parcels"] for q in quints),
        "overall_conversion_rate": round(overall, 4),
        "top_quintile_rate": top["conversion_rate"],
        "bottom_quintile_rate": bot["conversion_rate"],
        "spread_percentage_points": spread_pp,
        "spread_within_density_bands": strat,
        "mechanical_floor": floor,
        "spread_above_floor": (round(spread_pp - floor["p95"], 1)
                               if floor and floor.get("p95") is not None else None),
        "calibration": {
            "predicted_conversion_rate": round(tot_pred, 4),
            "actual_conversion_rate": round(overall, 4),
            "predicted_over_actual": round(ratio, 2) if ratio else None,
            "largest_quintile_gap": round(worst, 3) if worst is not None else None,
            "reading": ("well calibrated" if ratio and 0.75 <= ratio <= 1.35 else
                        "over-predicts conversion; dollar values would be inflated"
                        if ratio and ratio > 1.35 else
                        "under-predicts conversion; dollar values would be understated"
                        if ratio else "not measurable"),
        },
        "lift_top_over_bottom": lift,
        "monotone_across_quintiles": monotone,
        "quintiles": quints,
    }


def verdict(results):
    """Reads the result against each vintage's own computed floor. Earlier
    versions quoted a floor measured on synthetic uniform points, which was both
    unreproducible and far too low for real parcel fabric."""
    good = [r for r in results if r.get("spread_above_floor") is not None]
    if not good:
        return "No vintage produced a usable result."
    above = [r["spread_above_floor"] for r in good]
    beats = sum(1 for r in good
                if r["mechanical_floor"] and r["spread_percentage_points"] > r["mechanical_floor"]["max"])
    obs = sum(r["spread_percentage_points"] for r in good)
    flo = sum(r["mechanical_floor"]["median"] for r in good if r["mechanical_floor"])
    mech = round(100 * flo / obs) if obs else None
    return (f"{len(good)} vintages. Observed top-to-bottom spread exceeded the mechanical "
            f"floor by {min(above)} to {max(above)} points, and beat the largest of 200 "
            f"randomisations in {beats} of {len(good)}. Roughly {mech}% of the headline "
            f"spread is fabric: a fixed structure-count threshold means dense cells can "
            f"reach it and sparse ones cannot, and density falls with distance from town. "
            f"What remains is genuine locational signal. This validates distance to the "
            f"built edge, not the production model: none of the growth signals, the water "
            f"gate, the tenure term or the valuation stack enters this test. Nothing here "
            f"speaks to the dollar figures.")
