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
.parcel{page-break-inside:avoid;margin-top:26px}
@media print{body{background:#fff}.page{padding:0;max-width:none}@page{margin:16mm}}
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


def _investor_body(rows, tot):
    out = []
    out.append(f"""
    <h2>The case</h2>
    <p>This brief covers {tot['n']} parcel{'s' if tot['n']!=1 else ''} totalling
    {_num(tot['acres'],1)} acres in Maricopa County, currently carried at
    {money(tot['ask'])}, or {money(tot['per_acre'])} per acre. Against a modelled
    value of {money(tot['value'])}, the position screens at an average value score of
    {tot['score']} out of 100, where 50 means an asset is worth what it costs.</p>
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
    directly. The fit spans 219,509 parcel-periods and independently reproduces the
    2008 collapse, which is a check that it is reading real history rather than
    noise.</div>""")

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
          <div class="kpi"><div class="k">Value score</div><div class="v">{r.get('value_score')}</div><div class="n">50 = fairly priced</div></div>
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
    <div class="risk"><b>What would make this wrong.</b> Value rests on the assessor's
    full cash value as the price basis, and on raw land that figure commonly lags the
    market, so the discount to model may be overstated. Conversion odds are fitted on
    construction dates, which trail the speculator's actual payoff by one to four
    years. The probability that Arizona groundwater policy shifts over thirty years is
    a judgment input, not an estimate, and it moves the value of any parcel that is not
    already water-served. Nothing here has been validated against realised
    transactions.</div>""")
    return "".join(out)


def _developer_body(rows, tot):
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


def build(rows, audience="investor"):
    tot = _portfolio(rows)
    investor = audience != "developer"
    title = "Investment Brief" if investor else "Development Opportunity Brief"
    body = _investor_body(rows, tot) if investor else _developer_body(rows, tot)
    aud = ("Prepared for prospective investors" if investor
           else "Prepared for builders and developers")
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
