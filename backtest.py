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

# Calibration from a null test: on synthetic data where conversion was decided by
# a coin flip, with position and acreage held identical between the converted and
# never-converted groups, this harness reported a 5 to 8 point spread. On data
# with a real planted frontier it reported 98 points. So a spread in single
# digits is indistinguishable from noise, and anything above roughly 20 points is
# separation the harness would not produce by accident.
NULL_BASELINE_PP = 8.0


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


def _row(miles, acres, bins):
    import hazard as HZ
    r = [1.0]
    b = HZ.bin_index(miles, bins)
    for i in range(1, len(bins) + 1):
        r.append(1.0 if b == i else 0.0)
    r.append(math.log(max(0.1, acres or 1.0)))
    return r


def score(coefs, bins, miles, acres):
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
            "median_acres": round(sorted(r[3] for r in block)[len(block)//2], 2),
            "median_edge_miles": round(sorted(r[4] for r in block)[len(block)//2], 2),
        })
    return out


def summarise(vintage, quints, n_train, n_events):
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
    """A deliberately conservative reading. This is the model's own report card,
    so it states what the numbers show and leaves the judgement open."""
    good = [r for r in results if r.get("spread_percentage_points") is not None]
    if not good:
        return "No vintage produced a usable result."
    spreads = [r["spread_percentage_points"] for r in good]
    mono = sum(1 for r in good if r["monotone_across_quintiles"])
    weak = [s for s in spreads if abs(s) <= NULL_BASELINE_PP]
    return (f"{len(good)} vintages tested. The gap in conversion rate between the "
            f"top and bottom fifth ranged {min(spreads)} to {max(spreads)} percentage "
            f"points, rising across quintiles in {mono} of {len(good)}. This shows only "
            f"whether the ranking separated land that later got developed from land "
            f"that did not. It does not show that the dollar values or the returns are "
            f"right, and it was produced by the same system it is testing, so it should "
            f"be read by someone who did not build the model. For calibration, this "
            f"harness returns {NULL_BASELINE_PP} points or less on data with no real "
            f"signal, so a spread in single digits means nothing was detected. "
            f"Check the calibration block separately: ranking well and predicting the "
            f"right LEVEL are different things, and the dollar values depend on the "
            f"level."
            + (f" {len(weak)} of {len(good)} vintages fall in that range."
               if weak else ""))
