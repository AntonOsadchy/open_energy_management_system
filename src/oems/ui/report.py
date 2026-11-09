"""Simulation report: one self-contained HTML file (opens offline, prints to PDF) with

* the system as drawn on the canvas (an SVG redrawn from the project's block positions and wires, with the
  energy of every wire after the run),
* key inputs: time settings, every block with its parameters and the input series it used,
* key outputs: profit and its parts, costs by category, KPIs, energy per wire,
* result charts (SVG): energy balance of each bus, marginal prices, storage state of charge, cumulative profit.

Everything comes from what a run already stores (summary, per-step table, config, canvas), so a saved simulation
gives the same report as the live run.
"""

from __future__ import annotations

import html
import math
import time

import numpy as np
import pandas as pd

KIND_COLOR = {"electricity": "#d4a106", "heat": "#e4572e", "cold": "#2d9cdb", "fuel": "#7a7f87", "other": "#8e6bbf"}
SERIES_COLORS = ["#2563eb", "#e4572e", "#15803d", "#7c3aed", "#c2410c", "#0891b2", "#be185d", "#4d7c0f", "#6b7280"]
CAT_LABEL = {"ENERGY": "energy", "NETWORK": "network fees", "TAX": "taxes", "PEAK": "peak charges",
             "REVENUE": "revenue", "OPERATION": "operation", "DEGRADATION": "degradation",
             "FLEXIBILITY": "flexibility", "EMISSIONS": "CO₂", "FIXED": "fixed charges",
             "INVESTMENT": "investment (CAPEX)", "FIXED_OM": "OPEX of new assets"}


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def fmt(v, digits: int = 0) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    return f"{v:,.{digits}f}".replace(",", " ")


def fmt_num(v) -> str:
    """Compact number for parameter tables."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return str(v)


# ---------------------------------------------------------------------------------------------- system diagram
def _nodes_edges(project: dict) -> tuple[dict, list]:
    data = ((project or {}).get("drawflow") or {}).get("drawflow", {}).get("Home", {}).get("data", {})
    nodes, edges = {}, []
    for nid, n in data.items():
        nodes[str(nid)] = n
    for nid, n in data.items():
        outs = n.get("data", {}).get("outs", [])
        for oc, o in (n.get("outputs") or {}).items():
            oi = int(oc.split("_")[1]) - 1
            for c in o.get("connections", []):
                tgt = data.get(str(c["node"]), {})
                ii = int(c["output"].split("_")[1]) - 1
                edges.append((str(nid), oi, str(c["node"]), ii, (outs[oi] if oi < len(outs) else {}),
                              (tgt.get("data", {}).get("ins") or [{}] * (ii + 1))[ii] if tgt else {}))
    return nodes, edges


def _box(n: dict) -> tuple[float, float, float, float]:
    d = n.get("data", {})
    kind = d.get("kind")
    w = {"bus": 150, "data": 150, "db": 150, "profile": 150, "dbconn": 170}.get(kind, 170)
    title, sub = _title(d)
    w = max(w, 8.2 * len(title) + 28, 6.2 * len(sub) + 28)        # wide enough for its name
    rows = max(len(d.get("ins", [])), len(d.get("outs", [])), 1)
    h = 44 + (0 if kind in ("bus", "data", "db", "profile", "dbconn") else 14 * rows)
    return float(n.get("pos_x", 0)), float(n.get("pos_y", 0)), w, max(h, 52)


def _title(d: dict) -> tuple[str, str]:
    kind = d.get("kind")
    if kind == "bus":
        return d.get("name", "bus"), f"bus · {d.get('carrier_kind', '')}"
    if kind == "data":
        return d.get("column", "input"), f"input · {d.get('label', '')}"
    if kind == "db":
        if d.get("source") == "solar":
            return f"PV {d.get('scenario', '')}", "DB input · capacity factor"
        if d.get("source") == "tariff":
            return f"{d.get('connection_type', '')} {d.get('series', '')}", f"DB tariff · {d.get('zone', '')}"
        return d.get("series", "series"), f"DB input · {d.get('zone', '')}"
    if kind == "dbconn":
        return d.get("name", "db"), "DB connection"
    if kind == "profile":
        return d.get("name", "profile"), f"daily profile · {d.get('unit', '')}"
    return d.get("name", "block"), (d.get("preset") or d.get("type") or "").replace("_", " ")


def _outline_point(box: tuple, px: float, py: float, rad: float = 26.0) -> tuple[float, float, float, float]:
    """Point of a rounded box outline nearest to (px, py) and the outward normal there."""
    x, y, w, h = box
    rad = min(rad, w / 2, h / 2)
    qx, qy = min(max(px, x + rad), x + w - rad), min(max(py, y + rad), y + h - rad)
    d = math.hypot(px - qx, py - qy)
    if d > 1e-6:
        nx, ny = (px - qx) / d, (py - qy) / d
        return qx + nx * rad, qy + ny * rad, nx, ny
    _, nx, ny = min([(px - x, -1, 0), (x + w - px, 1, 0), (py - y, 0, -1), (y + h - py, 0, 1)])
    return (x if nx < 0 else x + w) if nx else px, (y if ny < 0 else y + h) if ny else py, nx, ny


def system_svg(project: dict, summary: dict) -> str:
    """The canvas redrawn as SVG: blocks at their positions, energy wires coloured by carrier with MWh after the run,
    data wires dashed grey."""
    nodes, edges = _nodes_edges(project)
    if not nodes:
        return '<p class="muted">No canvas stored with this run.</p>'
    boxes = {nid: _box(n) for nid, n in nodes.items()}
    x0 = min(b[0] for b in boxes.values()) - 30
    y0 = min(b[1] for b in boxes.values()) - 30
    x1 = max(b[0] + b[2] for b in boxes.values()) + 30
    y1 = max(b[1] + b[3] for b in boxes.values()) + 40
    # MWh per canvas wire (component node <-> bus node), both directions of a link wire together
    mwh: dict[tuple[str, str], dict[str, float]] = {}
    for e in summary.get("edges", []):
        if e.get("node") and e.get("bus_node"):
            key = (str(e["node"]), str(e["bus_node"]))
            mwh.setdefault(key, {})
            mwh[key][e["port"]] = mwh[key].get(e["port"], 0.0) + (e.get("MWh") or 0.0)
    parts, labels = [], []          # labels go on top of wires and blocks
    for src, oi, dst, ii, oc, _ic in edges:
        if src not in boxes or dst not in boxes:
            continue
        sx, sy, sw, _ = boxes[src]
        dx, dy, _, _ = boxes[dst]
        ay, by = sy + 30 + 14 * oi, dy + 30 + 14 * ii
        ax, bx = sx + sw, dx
        sd, dd = nodes[src].get("data", {}), nodes[dst].get("data", {})
        nx, ny = -1.0, 0.0
        if dd.get("kind") == "bus":     # wires meet a bus where its outline is nearest, as on the canvas
            bx, by, nx, ny = _outline_point(boxes[dst], ax, ay)
        c = max(40.0, abs(bx - ax) / 2) if dd.get("kind") != "bus" else max(30.0, 0.4 * math.hypot(bx - ax, by - ay))
        path = f"M{ax:.0f},{ay:.0f} C{ax + c:.0f},{ay:.0f} {bx + nx * c:.0f},{by + ny * c:.0f} {bx:.0f},{by:.0f}"
        is_data = (oc or {}).get("type") == "data"
        if is_data:
            parts.append(f'<path d="{path}" class="w-data"/>')
            continue
        bus = sd if sd.get("kind") == "bus" else dd
        color = KIND_COLOR.get(bus.get("carrier_kind"), "#888")
        comp_id, bus_id = (dst, src) if sd.get("kind") == "bus" else (src, dst)
        flows = mwh.get((comp_id, bus_id), {})
        label = " · ".join(f"{p} {fmt(v, 1)}" for p, v in sorted(flows.items()) if abs(v) > 1e-9)
        parts.append(f'<path d="{path}" stroke="{color}" class="w-energy"/>')
        if label:
            mx, my = (ax + bx) / 2, (ay + by) / 2 - 6
            labels.append(f'<text x="{mx:.0f}" y="{my:.0f}" class="w-label">{esc(label)} MWh</text>')
    for nid, n in nodes.items():
        x, y, w, h = boxes[nid]
        d = n.get("data", {})
        title, sub = _title(d)
        kind = d.get("kind")
        stroke = KIND_COLOR.get(d.get("carrier_kind"), "#9aa1ab") if kind == "bus" else (
            "#9aa1ab" if kind in ("data", "db", "profile") else "#6b5bd2" if kind == "dbconn" else "#cbd2da")
        rx = 26 if kind == "bus" else 8
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w}" height="{h:.0f}" rx="{rx}" class="blk" '
                     f'stroke="{stroke}" stroke-width="{3 if kind == "bus" else 1.5}"/>')
        parts.append(f'<text x="{x + 12:.0f}" y="{y + 22:.0f}" class="b-title">{esc(title)}</text>')
        parts.append(f'<text x="{x + 12:.0f}" y="{y + 38:.0f}" class="b-sub">{esc(sub)}</text>')
        badge = (summary.get("badges") or {}).get(nid) or {}
        if badge.get("cost") is not None:
            parts.append(f'<text x="{x + 12:.0f}" y="{y + h + 14:.0f}" class="b-badge">{fmt(badge["cost"])} €</text>')
    return (f'<svg class="diagram" viewBox="{x0:.0f} {y0:.0f} {x1 - x0:.0f} {y1 - y0:.0f}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts + labels)}</svg>')


# ---------------------------------------------------------------------------------------------- charts
def _thin(idx: pd.DatetimeIndex, df: pd.DataFrame, max_points: int = 400) -> tuple[list, pd.DataFrame, str]:
    """Bin means for long horizons: hourly up to 2 weeks, else daily, else weekly."""
    n = len(df)
    if n <= max_points:
        return list(idx), df, ""
    rule = "D" if n / max(1, (idx[-1] - idx[0]).days or 1) <= 96 and (idx[-1] - idx[0]).days <= max_points else "W"
    out = df.groupby(idx.floor(rule) if rule == "D" else idx.to_period("W").start_time).mean()
    return list(out.index), out, {"D": "daily mean", "W": "weekly mean"}[rule]


def line_chart(title: str, idx, series: dict[str, np.ndarray], unit: str, *, step: bool = False,
               fill_first: bool = False, y_range: tuple | None = None) -> str:
    if not series:
        return ""
    df = pd.DataFrame(series, index=idx)
    xs, df, note = _thin(df.index, df)
    W, H, L, R, T, B = 900, 260, 62, 16, 14, 42
    vals = df.to_numpy(dtype=float)
    lo, hi = (y_range if y_range else (np.nanmin(vals), np.nanmax(vals)))
    lo, hi = min(lo, 0.0) if not y_range else lo, hi
    if hi - lo < 1e-9:
        hi = lo + 1
    pad = (hi - lo) * 0.06 if not y_range else 0
    lo, hi = lo - (pad if lo < 0 else 0), hi + pad
    n = len(xs)
    X = lambda i: L + (W - L - R) * (i / max(1, n - 1))  # noqa: E731
    Y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))  # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" xmlns="http://www.w3.org/2000/svg">']
    for k in range(6):
        v = lo + (hi - lo) * k / 5
        y = Y(v)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" class="ytick">{_short(v)}</text>')
    if lo < 0 < hi:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" class="zero"/>')
    for k in range(7):
        i = round(k * (n - 1) / 6)
        t = pd.Timestamp(xs[i])
        out.append(f'<text x="{X(i):.1f}" y="{H - B + 16}" class="xtick">{t:%d %b}</text>'
                   + (f'<text x="{X(i):.1f}" y="{H - B + 29}" class="xtick">{t:%H:%M}</text>' if note == "" else ""))
    for j, (_name, col) in enumerate(df.items()):
        color = SERIES_COLORS[j % len(SERIES_COLORS)]
        v = col.to_numpy(dtype=float)
        pts = []
        for i in range(n):
            if not np.isfinite(v[i]):
                continue
            if step and i > 0 and pts:
                pts.append(f"{X(i):.1f},{Y(v[i - 1]):.1f}")
            pts.append(f"{X(i):.1f},{Y(v[i]):.1f}")
        if fill_first and j == 0 and pts:
            out.append(f'<polygon points="{X(0):.1f},{Y(max(lo, 0)):.1f} {" ".join(pts)} {X(n - 1):.1f},'
                       f'{Y(max(lo, 0)):.1f}" fill="{color}" opacity=".12"/>')
        out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="1.6"/>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:{SERIES_COLORS[j % len(SERIES_COLORS)]}"></i>{esc(c)}</span>'
                     for j, c in enumerate(df.columns))
    return (f'<figure><figcaption>{esc(title)} <span class="muted">[{esc(unit)}]{" · " + note if note else ""}'
            f'</span></figcaption>{"".join(out)}<div class="legend">{legend}</div></figure>')


def _short(v: float) -> str:
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 100:
        return f"{v:.0f}"
    return f"{v:.2g}" if a < 1 else f"{v:.1f}"


# ---------------------------------------------------------------------------------------------- report
def _component_rows(config: dict, ts: pd.DataFrame) -> str:
    rows = []
    for c in config.get("components", []):
        params = []
        for k, v in c.items():
            if k in ("type", "name") or v in (None, "", [], {}):
                continue
            if isinstance(v, dict) and set(v) == {"series"}:
                col = next((x for x in ts.columns if x.startswith(f"input {c['name']}.{k}")), None)
                src = col.split("(", 1)[1].rstrip(")") if col and "(" in col else v["series"]
                params.append(f"<b>{esc(k)}</b> ← {esc(src)}")
            elif isinstance(v, dict) and set(v) == {"daily"}:
                params.append(f"<b>{esc(k)}</b> ← daily profile {esc(fmt_num(min(v['daily'])))}–"
                              f"{esc(fmt_num(max(v['daily'])))}")
            elif isinstance(v, dict):
                params.append(f"<b>{esc(k)}</b> " + ", ".join(
                    f"{esc(b)}: {('← series' if isinstance(x, dict) else fmt_num(x))}" for b, x in v.items()))
            elif isinstance(v, list):
                params.append(f"<b>{esc(k)}</b> {len(v)} item(s)")
            elif isinstance(v, float) and math.isinf(v):
                continue
            else:
                params.append(f"<b>{esc(k)}</b> {esc(fmt_num(v))}")
        rows.append(f"<tr><td>{esc(c['name'])}</td><td>{esc(c['type'])}</td><td>{' · '.join(params)}</td></tr>")
    return "".join(rows)


def _lifetime_html(summary: dict) -> str:
    """Multi-year run: headline figures and the per-year table (SoH, capacity, cycles, profit)."""
    life = summary.get("lifetime")
    if not life:
        return ""
    rows = [dict(zip(life["columns"], r, strict=True)) for r in life["rows"]]
    cols = [c for c in ("year", "storage", "soh_start", "soh_end", "capacity_start_mwh", "cycles", "cumulative_cycles",
                        "avg_cycles_per_day", "profit", "profit_cumulative", "profit_discounted") if c in life["columns"]
            and (c != "storage" or any(r.get("storage") for r in rows))
            and (c != "profit_discounted" or life.get("discount_rate"))]
    label = {"soh_start": "SoH start", "soh_end": "SoH end", "capacity_start_mwh": "capacity MWh",
             "cumulative_cycles": "cum. cycles", "avg_cycles_per_day": "avg cycles/day", "profit": "profit €",
             "profit_cumulative": "cum. profit €", "profit_discounted": "discounted €"}

    def cell(c, v):
        if v is None:
            return ""
        if c.startswith("soh"):
            return f"{100 * v:.1f} %"
        return str(v) if c in ("year", "storage") else fmt(v, 2 if c in ("cycles", "avg_cycles_per_day") else 0)

    head = "".join(f"<th>{esc(label.get(c, c))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td class='num'>{esc(cell(c, r.get(c)))}</td>" for c in cols) + "</tr>"
                   for r in rows)
    retired = " ".join(f"{esc(n)} retires after year {y} (end of life {100 * life['eol'][n]:.1f} % SoH)."
                       for n, y in life["retired"].items() if y)
    npv = f" · NPV at {100 * life['discount_rate']:g} %: <b>{fmt(life['npv'])} €</b>" if life.get("discount_rate") else ""
    if life.get("inflation"):
        npv += f" · prices +{100 * life['inflation']:g} %/yr (nominal profits)"
    return (f"<h2>Lifetime <span class='muted'>({life['years']} years, base period repeated)</span></h2>"
            f"<p>Total profit <b>{fmt(life['profit'])} €</b>{npv}. {retired}</p>"
            f"<table><tr>{head}</tr>{body}</table>"
            "<p class='muted'>Each year is optimised on its own (no degradation cost in the objective); after the year "
            "the battery's state of health is read from its SoH curves at its age and average cycles/day since "
            "commissioning, and the next year runs with that capacity.</p>")


def build_report(*, name: str, summary: dict, ts: pd.DataFrame, config: dict, project: dict | None,
                 notes: str = "") -> str:
    """The report as one HTML page. ``ts`` is the per-step table (``runs.timeseries``) with a time index."""
    ts = ts.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(ts.index, utc=True))
    tz = (config.get("time") or {}).get("tz")
    if tz:
        idx = idx.tz_convert(tz)
    ts.index = idx
    time_cfg = config.get("time") or {}
    profit = summary.get("profit") or {}
    kpis = summary.get("kpis") or []

    def kpi(name, scope=None):
        return next((k["value"] for k in kpis if k["kpi"] == name and (scope is None or k["scope"] == scope)), None)

    # ---- headline tiles
    imported = sum(e["MWh"] or 0 for e in summary.get("edges", []) if e["port"] == "import")
    exported = sum(e["MWh"] or 0 for e in summary.get("edges", []) if e["port"] == "export")
    tiles = [("Profit", f"{fmt(profit.get('total', -summary.get('total_cost', 0)))} €", "revenue − costs"),
             ("Revenue", f"{fmt(profit.get('revenue'))} €", "exports and other income"),
             ("Total cost", f"{fmt(summary.get('total_cost'))} €", "objective without tie-break / slack"),
             ("Imported", f"{fmt(imported, 1)} MWh", "all grid connections"),
             ("Exported", f"{fmt(exported, 1)} MWh", "all grid connections")]
    for k in kpis:
        if k["kpi"] == "cycles_throughput":
            tiles.append((f"{k['scope']} cycles", fmt(k["value"], 1), "stored in + out / 2 / capacity"))
    tiles_html = "".join(f'<div class="tile"><div class="t-label">{esc(a)}</div><div class="t-val">{esc(b)}</div>'
                         f'<div class="t-sub">{esc(c)}</div></div>' for a, b, c in tiles)

    # ---- inputs
    start, end = ts.index[0], ts.index[-1]
    setting_rows = [("Period", f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} ({len(ts)} steps)"),
                    ("Resolution", time_cfg.get("freq", summary.get("resolution", ""))),
                    ("Time zone", tz or "naive"),
                    ("CO₂ cap", f"{((config.get('emission_policy') or {}).get('co2') or {}).get('cap') or '–'} t"),
                    ("Solver", f"status {summary.get('status')}, build {summary.get('build_time', 0):.1f} s, "
                               f"solve {summary.get('solve_time', 0):.1f} s")]
    inputs = [c for c in ts.columns if c.startswith("input ")]
    input_rows = "".join(
        f"<tr><td>{esc(c[6:])}</td><td class='num'>{ts[c].min():.4g}</td><td class='num'>{ts[c].mean():.4g}</td>"
        f"<td class='num'>{ts[c].max():.4g}</td></tr>" for c in inputs)

    # ---- outputs
    cats = summary.get("by_category") or {}
    cat_rows = "".join(f"<tr><td>{esc(CAT_LABEL.get(k, k.lower()))}</td><td class='num'>{fmt(v)} €</td></tr>"
                       for k, v in cats.items() if k not in ("TIEBREAK", "SLACK") and v)
    period_rows = "".join(f"<tr><td>{esc(CAT_LABEL.get(p['category'], p['category']))} ({esc(p['component'])})</td>"
                          f"<td class='num'>{fmt(p['EUR'])} €</td></tr>" for p in profit.get("period_charges", []))
    kpi_rows = "".join(f"<tr><td>{esc(k['kpi'].replace('_', ' '))}</td><td>{esc(k['scope'])}</td>"
                       f"<td class='num'>{fmt(k['value'], 2 if abs(k['value'] or 0) < 100 else 0)}</td>"
                       f"<td>{esc(k['unit'])}</td></tr>" for k in kpis if k["value"] is not None)
    edge_rows = "".join(f"<tr><td>{esc(e['component'])}.{esc(e['port'])}</td><td>{esc(e['bus'])}</td>"
                        f"<td>{'→ bus' if e['direction'] == 'out' else '← bus'}</td>"
                        f"<td class='num'>{fmt(e['MWh'], 1)}</td></tr>"
                        for e in summary.get("edges", []) if abs(e.get("MWh") or 0) > 1e-9)

    # ---- charts
    charts = []
    for bus in summary.get("buses", []):
        flows = {c[5:].replace(" [MW]", ""): ts[c].to_numpy() for c in ts.columns
                 if c.startswith("flow ") and (c.endswith(f"→ {bus} [MW]") or c.startswith(f"flow {bus} →"))}
        flows = {k: v for k, v in flows.items() if np.abs(v).max() > 1e-9}
        charts.append(line_chart(f"Energy flows on bus {bus}", ts.index, flows, "MW", step=True))
    prices = {c[6:].replace(" [EUR/MWh]", ""): ts[c].to_numpy() for c in ts.columns if c.startswith("price ")}
    charts.append(line_chart("Marginal price per bus", ts.index, prices, "€/MWh", step=True))
    soc = {c[6:].replace(" [0-1]", ""): ts[c].to_numpy() * 100 for c in ts.columns
           if c.startswith("state ") and c.endswith("soc [0-1]")}
    if soc:
        charts.append(line_chart("Storage state of charge", ts.index, soc, "%", step=True, fill_first=True,
                                 y_range=(0, 100)))
    if "econ profit cumulative [EUR]" in ts:
        charts.append(line_chart("Cumulative profit (per-step part)", ts.index,
                                 {"cumulative profit": ts["econ profit cumulative [EUR]"].to_numpy()}, "€",
                                 fill_first=True))

    life_html = _lifetime_html(summary)

    css = """
body{font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937;margin:0;background:#f6f7f9}
main{max-width:1060px;margin:0 auto;padding:28px 28px 60px;background:#fff}
h1{font-size:24px;margin:0 0 4px} h2{font-size:17px;margin:30px 0 10px;padding-bottom:4px;border-bottom:2px solid #e5e7eb}
.muted{color:#6b7280;font-weight:400} .notes{white-space:pre-wrap;background:#f9fafb;padding:8px 12px;border-radius:6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:16px 0}
.tile{border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px} .t-label{font-size:12px;color:#6b7280}
.t-val{font-size:20px;font-weight:650} .t-sub{font-size:11px;color:#9ca3af}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px}
td,th{padding:4px 8px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
th{font-size:12px;color:#6b7280;font-weight:600} td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}
svg.diagram{width:100%;height:auto;border:1px solid #e5e7eb;border-radius:8px;background:#fbfbfc}
.blk{fill:#fff} .b-title{font-size:14px;font-weight:650;fill:#111827} .b-sub{font-size:11px;fill:#6b7280}
.b-badge{font-size:11px;fill:#2563eb} .w-energy{fill:none;stroke-width:3;opacity:.8}
.w-data{fill:none;stroke:#9aa1ab;stroke-width:1.4;stroke-dasharray:5 4}
.w-label{font-size:11px;fill:#111827;text-anchor:middle;paint-order:stroke;stroke:#fff;stroke-width:4}
figure{margin:14px 0 22px} figcaption{font-weight:600;margin-bottom:4px} svg.chart{width:100%;height:auto}
.grid{stroke:#eef0f3} .zero{stroke:#9ca3af} .ytick{font-size:11px;fill:#6b7280;text-anchor:end}
.xtick{font-size:11px;fill:#6b7280;text-anchor:middle}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:#374151}
.legend i{display:inline-block;width:14px;height:3px;margin-right:5px;vertical-align:middle}
footer{margin-top:36px;font-size:12px;color:#6b7280}
@media print{body{background:#fff} main{padding:0} h2{break-after:avoid} figure,table,svg.diagram{break-inside:avoid}}
"""
    created = time.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(name)} – Microgrid Sizing Tool report</title>
<style>{css}</style></head><body><main>
<h1>{esc(name)}</h1>
<div class="muted">Microgrid Sizing Tool simulation report · {esc(created)} · {start:%d %b %Y} – {end:%d %b %Y}, {len(ts)} steps ({esc(time_cfg.get('freq', ''))})</div>
{f'<p class="notes">{esc(notes)}</p>' if notes else ''}
<div class="tiles">{tiles_html}</div>

{life_html}
<h2>System{f' <span class="muted">(year {summary.get("year", 1)} in detail)</span>' if life_html else ''}</h2>
{system_svg(project or {}, summary)}
<p class="muted">Energy wires show the energy moved over the whole period (MWh); dashed grey wires carry input data.</p>

<h2>Key inputs</h2>
<div class="cols"><div><table><tr><th colspan="2">Settings</th></tr>
{''.join(f'<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>' for a, b in setting_rows)}</table></div>
<div><table><tr><th>Input series used</th><th class="num">min</th><th class="num">mean</th><th class="num">max</th></tr>
{input_rows or '<tr><td colspan="4" class="muted">no time series inputs</td></tr>'}</table></div></div>
<table><tr><th>Block</th><th>Type</th><th>Parameters</th></tr>{_component_rows(config, ts)}</table>

<h2>Key outputs</h2>
<div class="cols"><div><table><tr><th>Costs by category</th><th class="num">EUR</th></tr>{cat_rows}
<tr><td><b>Total cost</b></td><td class="num"><b>{fmt(summary.get('total_cost'))} €</b></td></tr></table>
<table><tr><th>Profit</th><th class="num">EUR</th></tr>
<tr><td>per time step (sum)</td><td class="num">{fmt(profit.get('steps'))} €</td></tr>
{period_rows.replace('<tr><td>', '<tr><td>− ')}
<tr><td><b>Profit</b></td><td class="num"><b>{fmt(profit.get('total'))} €</b></td></tr>
<tr><td>best step {esc(str(profit.get('best_step', ''))[:16])}</td><td class="num">{fmt(profit.get('best'), 2)} €</td></tr>
<tr><td>worst step {esc(str(profit.get('worst_step', ''))[:16])}</td><td class="num">{fmt(profit.get('worst'), 2)} €</td></tr></table></div>
<div><table><tr><th>Energy per wire</th><th>Bus</th><th></th><th class="num">MWh</th></tr>{edge_rows}</table></div></div>
<table><tr><th>KPI</th><th>Scope</th><th class="num">Value</th><th>Unit</th></tr>{kpi_rows}</table>
{''.join(f'<p class="muted">⚠ {esc(w)}</p>' for w in [*summary.get('notices', []), *summary.get('warnings', [])])}

<h2>Results</h2>
{''.join(charts)}

<footer>Linear dispatch optimisation (Pyomo + HiGHS) with perfect foresight over the whole period. Profit per step is
split from the optimiser's own cost terms; peak and fixed charges apply per billing period. Generated by Microgrid Sizing Tool.</footer>
</main></body></html>"""
