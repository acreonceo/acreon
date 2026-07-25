"""
report.py
Printable market reports for one or more parcels, written for two audiences.

INVESTOR brief answers: why is this worth capital, what is the wait, what is the
downside. It leads with price against modelled value, the conversion timing
distribution, and the water path, because those are what determine the return.

DEVELOPER brief answers a different question: can I build here, when, and what do
I have to solve first. It leads with acreage, water feasibility, frontier
proximity and site constraints, and deliberately omits the owner's cost basis and
the acquisition score, which are the buyer's private leverage.

Output is self-contained HTML with print styling, so it opens in a browser and
prints to PDF without any server-side PDF dependency.
"""

from datetime import date

WS_LABEL = {
    "A": "Served / assured supply",
    "B": "Irrigated agriculture, SB1611 conversion path",
    "C": "Raw, groundwater dependent",
}
WS_INVESTOR = {
    "A": ("Water is already resolved. Land here can be subdivided today, which is "
          "why it trades at a premium: the option has largely been paid for."),
    "B": ("Irrigated farmland carrying a grandfathered irrigation right. Under "
          "SB1611 (2025) that right can be permanently relinquished for "
          "groundwater savings credits usable within one mile, giving this land a "
          "legal route to subdivision that raw desert does not have. The market "
          "has not fully repriced this."),
    "C": ("Groundwater dependent. Since ADWR stopped approving groundwater-based "
          "assured supply applications in 2023, subdivision here depends on being "
          "absorbed into a designated provider's service area or on a change in "
          "state policy. That is the source of both the discount and the risk."),
}
WS_DEVELOPER = {
    "A": "Inside a designated provider or holding an assured supply certificate. No water entitlement work required.",
    "B": "Irrigation grandfathered right available for relinquishment under SB1611; credits apply within one mile of the retired land.",
    "C": "No current path to an assured water supply determination. Requires provider annexation or a policy change before subdivision.",
}

CSS = """
:root{--ink:#1b1915;--soft:#5b564d;--faint:#8b8579;--line:#e0d9c9;--teal:#2f6f6a;
      --good:#1a6b3a;--warn:#8a6d3b;--bad:#8c2d1a;--paper:#fbfaf6}
*{box-sizing:border-box}
body{font-family:Georgia,'Times New Roman',serif;color:var(--ink);margin:0;background:#f2efe8}
.page{max-width:820px;margin:0 auto;background:#fff;padding:44px 54px 60px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;margin:30px 0 8px;padding-bottom:5px;border-bottom:2px solid var(--ink)}
h3{font-size:14px;margin:20px 0 6px;color:var(--teal)}
.sub{color:var(--soft);font-size:12.5px}
.meta{display:flex;justify-content:space-between;align-items:baseline;
      border-bottom:3px solid var(--ink);padding-bottom:12px;margin-bottom:8px}
.brand{font-weight:700;font-size:20px;letter-spacing:-.02em}
p{line-height:1.62;font-size:13.5px;margin:9px 0}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}
th{text-align:left;font-size:10px;letter-spacing:.09em;text-transform:uppercase;
   color:var(--faint);border-bottom:1px solid var(--line);padding:5px 6px}
td{padding:7px 6px;border-bottom:1px solid var(--line);
   font-family:ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
td.t{font-family:Georgia,serif}
.kpis{display:flex;gap:12px;margin:14px 0}
.kpi{flex:1;border:1px solid var(--line);border-radius:7px;padding:10px 12px;background:var(--paper)}
.kpi .k{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--faint)}
.kpi .v{font-size:21px;font-weight:700;font-family:ui-monospace,Menlo,monospace;margin-top:2px}
.kpi .n{font-size:11px;color:var(--soft)}
.tag{display:inline-block;font-size:10.5px;padding:2px 8px;border-radius:999px;
     border:1px solid var(--line);color:var(--soft);margin-right:5px}
.note{background:var(--paper);border-left:3px solid var(--teal);padding:10px 14px;margin:14px 0;font-size:12.5px}
.risk{background:#fdf6f3;border-left:3px solid var(--bad);padding:10px 14px;margin:14px 0;font-size:12.5px}
.foot{margin-top:34px;padding-top:12px;border-top:1px solid var(--line);
      font-size:10.5px;color:var(--faint);line-height:1.5}
.parcel{page-break-inside:avoid;margin-top:26px;page-break-before:auto}
h4{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);
   margin:16px 0 4px;font-family:Georgia,serif}
.aerial{margin:12px 0 6px}
.shot{position:relative;overflow:hidden;border-radius:6px;border:1px solid var(--line);max-width:100%}
.shot img,.shot svg{position:absolute;top:0;left:0;max-width:100%}
.imgcap{font-size:10.5px;color:var(--faint);margin-top:5px}
@media print{
  body{background:#fff}
  .page{padding:0;max-width:none}
  @page{margin:16mm}
  h2{page-break-after:avoid}
  h3,h4{page-break-after:avoid}
  table,.kpis,.note,.risk,.aerial{page-break-inside:avoid}
  .shot img{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  a{text-decoration:none;color:inherit}
}
"""


def money(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "n/a"
    s = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e6:
        return f"{s}${v/1e6:.2f}M"
    if v >= 1e3:
        return f"{s}${v/1e3:,.0f}k"
    return f"{s}${v:,.0f}"


def _num(v, d=0):
    try:
        return f"{float(v):,.{d}f}"
    except (TypeError, ValueError):
        return "n/a"


# --- AERIAL -----------------------------------------------------------------
# Esri's World Imagery export endpoint returns a PNG for a bounding box, so the
# browser fetches it directly at print time and no server-side image pipeline or
# rendering dependency is needed. The parcel is drawn over it as inline SVG,
# which makes the shape self-evident: a reader seeing a two-mile ribbon
# understands the problem without being told what an inscribed radius is.
ESRI_IMG = ("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery"
            "/MapServer/export")
IMG_W, IMG_H = 760, 470


def _rings(gj):
    """Every coordinate ring in a GeoJSON polygon or multipolygon. Returns an
    empty list for anything unusable, so a parcel with no stored geometry simply
    prints without an aerial rather than failing the whole document."""
    import json as _json
    if not gj:
        return []
    try:
        g = _json.loads(gj) if isinstance(gj, (str, bytes)) else gj
    except Exception:
        return []
    if not isinstance(g, dict):
        return []
    t, c = g.get("type"), g.get("coordinates") or []
    if t == "Polygon":
        return [r for r in c if len(r) > 2]
    if t == "MultiPolygon":
        return [r for poly in c for r in poly if len(r) > 2]
    return []


def _aerial(r, pad=0.55, caption=None):
    rings = _rings(r.get("geom_json"))
    if not rings:
        return ""
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # pad, then force the box to the image aspect so nothing is stretched
    w = max((x1 - x0) * (1 + pad), 0.0016)
    h = max((y1 - y0) * (1 + pad), 0.0016)
    import math as _m
    aspect = IMG_W / IMG_H
    lat_scale = max(0.2, _m.cos(_m.radians(cy)))     # degrees of lon per degree of lat
    if (w * lat_scale) / h < aspect:
        w = (h * aspect) / lat_scale
    else:
        h = (w * lat_scale) / aspect
    bx0, bx1, by0, by1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    url = (f"{ESRI_IMG}?bbox={bx0:.6f},{by0:.6f},{bx1:.6f},{by1:.6f}"
           f"&bboxSR=4326&imageSR=3857&size={IMG_W},{IMG_H}&format=png&f=image")
    paths = []
    for ring in rings:
        pts = " ".join(
            f"{(p[0]-bx0)/(bx1-bx0)*IMG_W:.1f},{(1-(p[1]-by0)/(by1-by0))*IMG_H:.1f}"
            for p in ring)
        paths.append(f'<polygon points="{pts}" fill="rgba(255,214,0,.16)" '
                     f'stroke="#ffd600" stroke-width="2.5" />')
    cap = f'<div class="imgcap">{caption}</div>' if caption else ""
    return (f'<div class="aerial"><div class="shot" style="width:{IMG_W}px;height:{IMG_H}px">'
            f'<img src="{url}" width="{IMG_W}" height="{IMG_H}" alt="aerial view">'
            f'<svg viewBox="0 0 {IMG_W} {IMG_H}" width="{IMG_W}" height="{IMG_H}">'
            f'{"".join(paths)}</svg></div>{cap}</div>')


def _ground(r):
    """Plain-language read of what the geometry will physically hold."""
    rad = r.get("usable_radius_ft")
    lp = r.get("largest_part_acres")
    ac = float(r.get("acres") or 0)
    parts = r.get("parts")
    road = r.get("road_ft")
    out = []
    if rad is not None:
        rad = float(rad)
        if rad < 100:
            out.append(f"The widest point of this parcel fits a circle of only "
                       f"{rad:,.0f} feet radius. That is a corridor, not a site: it "
                       f"supports signage, utilities and drainage, and cannot hold a "
                       f"development pad regardless of total acreage.")
        elif rad < 200:
            out.append(f"Largest circle that fits inside is {rad:,.0f} feet radius, "
                       f"below the depth a commercial pad needs.")
        elif rad < 300:
            out.append(f"Largest circle that fits inside is {rad:,.0f} feet radius, "
                       f"workable for commercial but tight for two-sided residential "
                       f"blocks.")
        else:
            out.append(f"Largest circle that fits inside is {rad:,.0f} feet radius, "
                       f"enough depth for conventional subdivision blocks.")
    if lp is not None and ac > 0 and float(lp) < ac * 0.9:
        out.append(f"The ground is in {parts or 'several'} separate pieces and the "
                   f"largest is {float(lp):,.1f} acres, so it should be judged at that "
                   f"scale rather than at {ac:,.1f}.")
    if road is not None:
        road = float(road)
        out.append(f"Nearest road centreline is {road:,.0f} feet away."
                   + ("" if road <= 150 else
                      " Access would have to be established before anything is built."))
    return " ".join(out) or "Parcel geometry has not been measured."


def _comps_table(r):
    cs = r.get("comps") or []
    if not cs:
        return ('<p class="sub">No recorded arm\'s-length sales of vacant or '
                'agricultural land within three miles in the last ten years. On fringe '
                'ground that is common, and it is the reason a comparable-sales '
                'valuation cannot be produced here.</p>')
    rowsh = "".join(
        f"<tr><td class='t'>{c.get('situs_address') or c.get('apn')}</td>"
        f"<td style='text-align:right'>{_num(c.get('acres'),1)}</td>"
        f"<td style='text-align:right'>{money(c.get('paid'))}</td>"
        f"<td style='text-align:right'>{money(c.get('price_per_acre'))}</td>"
        f"<td style='text-align:right'>{c.get('acquired') or ''}</td>"
        f"<td style='text-align:right'>{_num(c.get('miles_away'),1)}</td></tr>"
        for c in cs)
    return (f"<table><tr><th>Recorded sale</th><th style='text-align:right'>Acres</th>"
            f"<th style='text-align:right'>Paid</th><th style='text-align:right'>$/acre</th>"
            f"<th style='text-align:right'>Year</th><th style='text-align:right'>Miles</th></tr>"
            f"{rowsh}</table>"
            f"<p class='sub'>Recorded transactions, not estimates. These are the only "
            f"observed dollar figures in this document.</p>")


def _portfolio(rows):
    ac = sum(float(r.get("acres") or 0) for r in rows)
    ask = sum(float(r.get("est") or 0) for r in rows)
    val = sum(float(r.get("value_total") or 0) for r in rows)
    p50s = [r["p50_years"] for r in rows if r.get("p50_years")]
    return {
        "n": len(rows), "acres": ac, "ask": ask, "value": val,
        "per_acre": (ask / ac) if ac else 0,
        "score": round(sum(r.get("value_score") or 0 for r in rows) / len(rows)) if rows else 0,
        "p50": (sorted(p50s)[len(p50s)//2] if p50s else None),
    }


def _investor_body(rows, tot, meta=None):
    out = []
    out.append(f"""
    <h2>The case</h2>
    <p>This brief covers {tot['n']} parcel{'s' if tot['n']!=1 else ''} totalling
    {_num(tot['acres'],1)} acres in Maricopa County, currently carried at
    {money(tot['ask'])}, or {money(tot['per_acre'])} per acre. Against a modelled
    value of {money(tot['value'])}, the position screens at an average value score of
    {tot['score']} out of 100, where 50 means an asset is worth what it costs.</p>
    <p>Figures are stated for a {tot['horizon']}-year holding period. That matters:
    the same multiple on capital is a very different investment over five years
    than over thirty, so every parcel here is scored on return per year over that
    period, not on value accumulated across an arbitrary window.</p>
    <p>The thesis is not that raw land appreciates steadily. It does not. Fringe land
    sits close to its holding value for years and then steps up sharply when it
    becomes developable. What is being bought is the probability of that conversion,
    discounted for the wait and net of the cost of carrying the land meanwhile. On
    this position the model puts even odds of conversion at
    {str(tot['p50']) + ' years' if tot['p50'] else 'beyond the 30-year horizon'}.</p>
    <div class="note"><b>How the value is derived.</b> Conversion odds are estimated
    from Maricopa County's own construction history: every improved parcel carries a
    build year, so the development frontier can be reconstructed for any past year and
    the relationship between distance-to-frontier and subsequent conversion measured
    directly.{tot['fit_line']}</div>""")

    for r in rows:
        ws = r.get("water_state") or "C"
        out.append(f"""
        <div class="parcel">
        <h3>{r.get('situs_address') or r.get('apn')} &middot; {_num(r.get('acres'),2)} acres</h3>
        <div class="sub">APN {r.get('apn')} &middot; {r.get('city') or ''} {r.get('zcta') or ''}
          &middot; {r.get('use')}</div>
        <div class="kpis">
          <div class="kpi"><div class="k">Asking / assessed</div><div class="v">{money(r.get('price_per_acre'))}</div><div class="n">per acre</div></div>
          <div class="kpi"><div class="k">Modelled value</div><div class="v">{money(r.get('value_per_acre'))}</div><div class="n">per acre</div></div>
          <div class="kpi"><div class="k">Return</div><div class="v">{_num(r.get('annual_return_pct'),1)}%</div><div class="n">per year, {tot['horizon']}-yr hold</div></div>
          <div class="kpi"><div class="k">Even odds by</div><div class="v">{r.get('p50_years') or '30+'}</div><div class="n">years to convert</div></div>
        </div>
        <p><b>Water.</b> {WS_INVESTOR.get(ws, '')}</p>
        <p><b>Position.</b> Held by {r.get('owner')} ({r.get('owner_type')}) for
        {r.get('tenure') if r.get('tenure') is not None else 'an unrecorded period'}
        {'years' if r.get('tenure') is not None else ''}
        {', mailing out of state' if r.get('absentee') else ''}. Whole-parcel modelled
        value {money(r.get('value_total'))} against {money(r.get('est'))} carried.
        Annual carry runs {r.get('carry_pct')}% of value, computed from the tax roll
        rather than assumed.</p>
        </div>""")

    out.append("""
    <div class="risk"><b>What would make this wrong.</b> Both sides of the value
    comparison come from the assessor. The price basis is the parcel's full cash
    value, and the developer price it is measured against is the median full cash
    value per acre of assured-supply land in the same ZIP. Neither is a transaction.
    A ratio between them therefore measures how this parcel is assessed relative to
    its neighbours, which is informative but is not a market valuation, and a large
    ratio is as likely to indicate an assessment anomaly as an opportunity. Raw
    fringe land trades too rarely to build comparable sales, and without a full deed
    chain a repeat-sales index cannot be constructed either, so no valuation here has
    been validated against realised transactions. Conversion odds are fitted on
    construction dates, which trail the speculator's actual payoff by one to four
    years. The probability that Arizona groundwater policy shifts over thirty years is
    a judgment input, not an estimate.</div>""")
    return "".join(out)


def _partner_body(rows, tot, meta=None):
    """A partner is not buying a parcel, they are buying a method. So the method
    leads, what it cannot do is stated before what it can, and the parcels appear
    as worked examples rather than as the ask."""
    meta = meta or {}
    bt = meta.get("backtest") or []
    out = []
    out.append(f"""
    <h2>The thesis</h2>
    <p>Raw fringe land is not a growth asset. It sits near its holding value for
    years and then steps up sharply when it becomes developable. What is being
    bought is the probability and the timing of that single event, discounted for
    the wait and net of the cost of carrying the land meanwhile. Everything in this
    brief follows from treating conversion as the event to be predicted, rather
    than treating price as a trend to be extrapolated.</p>

    <h2>What this method can and cannot do</h2>
    <p><b>It can date conversion.</b> Every improved parcel in Maricopa County
    carries a construction year, which is a census of development events going back
    decades. The frontier can be reconstructed for any past year, each parcel's
    distance to that frontier measured, and the relationship between distance and
    subsequent conversion estimated from what actually happened.{tot['fit_line']}</p>
    <p><b>It cannot appraise land.</b> Fringe parcels trade too rarely to build
    comparable sales, and county records carry only the most recent transaction, so
    a repeat-sales index cannot be constructed either. Any per-acre value in this
    document is a model output resting on assessor opinion, not a market
    observation. We treat that as a screen, not as a valuation, and we do not
    represent it as one. Recorded sales, shown per parcel below, are the only
    observed dollar figures here.</p>
    <div class="note"><b>Why that distinction is the edge.</b> Timing is the part
    of this asset class that is genuinely knowable and almost never measured.
    Valuation is the part everyone claims and no one can evidence. Being explicit
    about which is which is what makes the timing work credible.</div>""")

    if bt:
        trs = "".join(
            f"<tr><td>{b.get('vintage')}</td>"
            f"<td style='text-align:right'>{b.get('outcome_window_years')}y</td>"
            f"<td style='text-align:right'>{_pct(_top_q(b,'conversion_rate'))}</td>"
            f"<td style='text-align:right'>{_pct(b.get('bottom_quintile_rate'))}</td>"
            f"<td style='text-align:right'>{_num(b.get('spread_above_floor'),1)} pp</td></tr>"
            for b in bt if not b.get("error"))
        out.append(f"""
        <h2>The test</h2>
        <p>The ranking was run forward from four past start years using only what
        was knowable then, and scored against what was subsequently built. It was
        then run again against a permutation floor: which cells converted was
        reshuffled while population, parcel density and totals were held fixed, so
        location could no longer carry information. Whatever spread survives that
        shuffle is skill rather than geometry.</p>
        <table>
          <tr><th>Ranked from</th><th style="text-align:right">Watched</th>
              <th style="text-align:right">Top fifth converted</th>
              <th style="text-align:right">Bottom fifth</th>
              <th style="text-align:right">Above the shuffled floor</th></tr>
          {trs}
        </table>
        <p class="sub">Measured on half-mile cells of fixed ground, not parcels, so
        subdivision cannot inflate the converted side. Cells already developed at
        the start year are excluded, so these figures describe frontier expansion
        and say nothing about infill.</p>""")

    out.append(f"""
    <h2>How the universe narrows</h2>
    <p>Every vacant and agricultural parcel in the county enters. Public bodies,
    homebuilders who already assembled their sites, and parcels whose assessed
    figure is not a credible price are removed, because none of them are sellers.
    What survives is screened on water status, tenure, owner type, parcel geometry
    and road access, then ranked on modelled conversion timing. The {tot['n']}
    parcel{'s' if tot['n']!=1 else ''} below came through that process and total
    {_num(tot['acres'],1)} acres carried at {money(tot['ask'])}.</p>""")

    for r in rows:
        ws = r.get("water_state") or "C"
        out.append(f"""
        <div class="parcel">
        <h3>{r.get('situs_address') or r.get('apn')} &middot; {_num(r.get('acres'),2)} acres</h3>
        <div class="sub">APN {r.get('apn')} &middot; {r.get('city') or ''} {r.get('zcta') or ''}
          &middot; {r.get('use')} &middot; {WS_LABEL.get(ws, ws)}</div>
        {_aerial(r, caption='Parcel boundary in yellow. Imagery: Esri World Imagery.')}
        <h4>On record</h4>
        <table>
          <tr><td class="t">Owner</td><td style="text-align:right">{r.get('owner') or 'n/a'} ({r.get('owner_type') or 'n/a'})</td></tr>
          <tr><td class="t">Held</td><td style="text-align:right">{str(r.get('tenure')) + ' years' if r.get('tenure') is not None else 'no recorded sale'}</td></tr>
          <tr><td class="t">Last recorded sale</td><td style="text-align:right">{(money(r.get('paid')) + ' in ' + str(r.get('acquired'))) if r.get('paid') and r.get('acquired') else 'none on record'}</td></tr>
          <tr><td class="t">Assessed full cash value</td><td style="text-align:right">{money(r.get('est'))} &middot; {money(r.get('price_per_acre'))}/ac</td></tr>
          <tr><td class="t">Annual carry, from the tax roll</td><td style="text-align:right">{r.get('carry_pct')}%</td></tr>
        </table>
        <h4>What the ground will hold</h4>
        <p>{_ground(r)}</p>
        <h4>Water</h4>
        <p>{WS_INVESTOR.get(ws, '')}</p>
        <h4>Recorded sales within three miles</h4>
        {_comps_table(r)}
        <h4>Modelled timing</h4>
        <p>Even odds of conversion at
        {str(r.get('p50_years')) + ' years' if r.get('p50_years') else 'beyond the 30-year horizon'},
        on a {tot['horizon']}-year view. This is the model output we stand behind;
        the per-acre value figures elsewhere in the system are a screen and are not
        reproduced here.</p>
        </div>""")

    out.append("""
    <div class="risk"><b>What would make this wrong.</b> Conversion odds are fitted
    on construction dates, which trail the speculator's payoff by one to four years,
    so the timing shown is late rather than early. Land platted before 2008 and never
    built reads as unconverted even though the owner was paid. The probability that
    Arizona groundwater policy shifts over a thirty-year hold is a judgment input,
    not an estimate, and it moves every parcel that is not already water-served. No
    figure in this document has been validated against realised transactions,
    because on this asset class there are not enough of them to validate against.
    Legal access, easements, mineral and grazing rights, topography, utility
    distances, zoning and any Luke AFB overlay have not been verified.</div>""")
    return "".join(out)


def _top_q(b, key):
    qs = b.get("quintiles") or []
    top = next((q for q in qs if q.get("quintile") == 5), None)
    return (top or {}).get(key)


def _pct(v):
    try:
        return f"{float(v)*100:.1f}%" if float(v) <= 1 else f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _developer_body(rows, tot, meta=None):
    out = []
    out.append(f"""
    <h2>Summary</h2>
    <p>{tot['n']} parcel{'s' if tot['n']!=1 else ''} totalling {_num(tot['acres'],1)}
    acres in Maricopa County. This brief sets out what each parcel is, the state of
    its water entitlement, how close development has already reached it, and what
    would have to be resolved before a subdivision could proceed.</p>""")

    for r in rows:
        ws = r.get("water_state") or "C"
        edge = r.get("edge_miles")
        edge_txt = (f"{_num(edge,1)} miles from the nearest built parcel"
                    if edge is not None else "distance to existing development not computed")
        out.append(f"""
        <div class="parcel">
        <h3>{r.get('situs_address') or r.get('apn')} &middot; {_num(r.get('acres'),2)} acres</h3>
        <div class="sub">APN {r.get('apn')} &middot; {r.get('city') or ''} {r.get('zcta') or ''}</div>
        <div>
          <span class="tag">{r.get('use')}</span>
          <span class="tag">{WS_LABEL.get(ws, ws)}</span>
          {'<span class="tag">In a mapped flood zone</span>' if r.get('flood_zone') else ''}
          {'<span class="tag">Frontage unverified</span>' if r.get('landlocked') else ''}
        </div>
        <table>
          <tr><th>Attribute</th><th style="text-align:right">Detail</th></tr>
          <tr><td class="t">Gross acreage</td><td style="text-align:right">{_num(r.get('acres'),2)} ac</td></tr>
          <tr><td class="t">Assessed full cash value</td><td style="text-align:right">{money(r.get('est'))}</td></tr>
          <tr><td class="t">Implied land basis</td><td style="text-align:right">{money(r.get('price_per_acre'))}/ac</td></tr>
          <tr><td class="t">Comparable developable land</td><td style="text-align:right">{money(r.get('dev_price_per_acre'))}/ac</td></tr>
          <tr><td class="t">Development frontier</td><td style="text-align:right">{edge_txt}</td></tr>
          <tr><td class="t">Modelled conversion, even odds</td><td style="text-align:right">{str(r.get('p50_years')) + ' years' if r.get('p50_years') else 'beyond 30 years'}</td></tr>
        </table>
        <p><b>Water entitlement.</b> {WS_DEVELOPER.get(ws, '')}</p>
        <p><b>Readiness.</b> {_readiness(r)}</p>
        </div>""")

    out.append("""
    <div class="risk"><b>Diligence still required.</b> Legal access, easements, mineral
    and grazing rights, topography and washes, utility stub distances, jurisdictional
    zoning and any Luke AFB overlay have not been verified here. Acreage and value are
    taken from assessor records and should be confirmed against survey and title. The
    water designation reflects mapped determinations, not an application on this
    parcel.</div>""")
    return "".join(out)


def _readiness(r):
    ws = r.get("water_state") or "C"
    p50 = r.get("p50_years")
    edge = r.get("edge_miles")
    bits = []
    if ws == "A":
        bits.append("Water is resolved, so entitlement work can begin without a supply strategy.")
    elif ws == "B":
        bits.append("The irrigation right is the asset: retiring it under the Ag-to-Urban "
                    "program supplies the physical availability finding for a certificate "
                    "within a mile of the land.")
    else:
        bits.append("A water strategy is the gating item and must be solved before "
                    "any subdivision plat can be approved.")
    if edge is not None:
        if edge < 1:
            bits.append("Development already abuts the parcel, so services are close and "
                        "absorption is demonstrated.")
        elif edge < 3:
            bits.append("Existing development is within a few miles, which usually means "
                        "utility extension rather than new trunk infrastructure.")
        else:
            bits.append("The parcel sits well beyond the current build-out edge, so "
                        "infrastructure extension is a material cost line.")
    if p50 and p50 <= 10:
        bits.append("On the model's timing this is a near-term rather than a land-bank position.")
    return " ".join(bits)


def build(rows, audience="investor", horizon=10, meta=None):
    meta = meta or {}
    tot = _portfolio(rows)
    tot["horizon"] = horizon
    n = meta.get("hazard_rows")
    ev = meta.get("hazard_events")
    tot["fit_line"] = (f" The current fit spans {n:,} parcel-periods carrying "
                       f"{ev:,} recorded conversions." if n and ev else "")
    title = {"partner": "Partnership Brief",
             "developer": "Development Opportunity Brief"}.get(audience, "Investment Brief")
    body = ({"partner": _partner_body,
             "developer": _developer_body}.get(audience, _investor_body))(rows, tot, meta)
    aud = {"partner": "Prepared for prospective partners",
           "developer": "Prepared for builders and developers"}.get(
               audience, "Prepared for prospective investors")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Acreon {title}</title><style>{CSS}</style></head><body><div class="page">
<div class="meta"><div><div class="brand">Acreon</div>
  <div class="sub">{aud}</div></div>
  <div class="sub" style="text-align:right">{date.today():%d %B %Y}<br>
  Maricopa County, Arizona</div></div>
<h1>{title}</h1>
<div class="sub">{tot['n']} parcel{'s' if tot['n']!=1 else ''} &middot;
  {_num(tot['acres'],1)} acres &middot; {money(tot['ask'])} carried</div>
{body}
<div class="foot">Prepared by Acreon from Maricopa County Assessor records, US Census
population data, Arizona Department of Water Resources assured water supply
determinations and ADOT programmed projects. Conversion timing is modelled from county
construction history. Figures are estimates for discussion and are not an appraisal,
an offer, or investment advice. Verify all facts independently before transacting.
</div></div></body></html>"""
