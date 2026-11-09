"""Test 09: every KPI in KPIs.xlsx (Model KPIs, Summary) recomputed from test_4_full_system.xlsx hourly data.
KPIs.xlsx supplies only the reported figures under test. Usage: python3 test_09_kpis.py <validation_dir>"""
import sys
import openpyxl
import pandas as pd

d = sys.argv[1]
f = f"{d}/test_4_full_system.xlsx"
TOL = 1e-6
P = {(r[0], r[1]): r[2] for r in openpyxl.load_workbook(f, read_only=True)["Parameters"].iter_rows(min_row=2, values_only=True)}
E_MAX = P[("Storage store", "e_max")]; EC, ED = P[("Storage store", "eta_ch")], P[("Storage store", "eta_dis")]
TB = P[("settings", "tie_break")]

ts = pd.read_excel(f, sheet_name="Time series")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
t = pd.to_datetime(ts[col("time (Europe")]); dt = ts[col("step_h")]; day = t.dt.date
imp, exp_ = ts[col("flow grid.import")], ts[col("flow el → grid.export")]
ch, dis = ts[col("flow el → store.charge")], ts[col("flow store.discharge")]
pv, curt, av = ts[col("flow prod.out")], ts[col("state prod.curtailment")], ts[col("input prod.profile")]
dem, soc = ts[col("flow el → demand.in")], ts[col("state store.soc")]
da, fi, fe = ts[col("input grid.import_price")], ts[col("input grid.import_fee")], ts[col("input grid.export_fee")]
pe = ts[col("input grid.export_price")]; lam = ts[col("price el")]
ref, prof = ts[col("ref cost without battery")], ts[col("econ profit [")]
S = lambda x: float((x * dt).sum())
wavg = lambda p, w: float((p * w).sum() / w.sum())

# ---- recomputed values
energy, network, revenue = S(da * imp), S(fi * imp + fe * exp_), S(pe * exp_)
total = energy + network - revenue
tiebreak = S(TB * (imp + exp_ + ch + dis))
thr_eff = (EC * ch + dis / ED) * dt / 2 / E_MAX           # README definition (after efficiency losses)
thr_raw = (ch + dis) * dt / 2 / E_MAX                     # Summary-note definition (at the bus)
C, D = ch > TOL, dis > TOL
x = {}
# Model KPIs
x["import_energy"] = S(imp); x["export_energy"] = S(exp_); x["max_import"] = float(imp.max())
x["discharged"] = S(dis); x["equivalent_cycles"] = S(dis) / E_MAX
x["cycles_throughput"] = float(thr_eff.sum())
x["max_cycles_per_day"] = float(thr_eff.groupby(day).sum().max())
x["soc_min"], x["soc_mean"], x["soc_max"], x["soc_end"] = (100 * float(v) for v in (soc.min(), soc.mean(), soc.max(), soc.iloc[-1]))
x["production"] = S(pv); x["curtailment"] = S(curt)
x["self_consumption"] = (S(pv) - S(exp_)) / S(pv)
x["self_sufficiency"] = (S(pv) - S(exp_)) / ((S(pv) - S(exp_)) + S(imp))
x["delivered_energy"] = S(dem); x["total_cost"] = total
wd = dem + ch + exp_
x["avg_marginal_price"] = wavg(lam, wd)
# alternatives, for diagnosis
alt = {"cycles_throughput (raw, (charge+discharge)/2/e_max)": float(thr_raw.sum()),
       "max_cycles_per_day (raw)": float(thr_raw.groupby(day).sum().max())}
# Summary
y = {
    "Total cost": total, "Energy (day-ahead on import)": energy, "Network (import + export fees)": network,
    "Export revenue": revenue, "Tie-break (excluded)": tiebreak, "Solver objective": total + tiebreak,
    "Average cost of supplied demand": total / S(dem), "Reference cost without battery": float(ref.sum()),
    "Value of the battery": float(ref.sum()) - total, "Demand": S(dem), "PV available": S(av), "PV used": S(pv),
    "PV curtailed": S(curt), "Grid import": S(imp), "Grid export": S(exp_), "Battery charged": S(ch),
    "Battery discharged": S(dis), "Battery losses": S(ch) - S(dis), "Max import": float(imp.max()),
    "Max export": float(exp_.max()), "Hours importing and exporting at once": int(((imp > TOL) & (exp_ > TOL)).sum()),
    "Avg day-ahead price (time-weighted)": float(da.mean()), "Avg day-ahead price paid on import": wavg(da, imp),
    "Avg day-ahead price received on export": wavg(pe, exp_),
    "Equivalent full cycles (discharge / e_max)": S(dis) / E_MAX,
    "Cycles, throughput": float(thr_eff.sum()), "Max cycles in a day (throughput)": x["max_cycles_per_day"],
    "Avg price when charging": wavg(da, ch), "Avg price when discharging": wavg(da, dis),
    "Avg import fee when charging": wavg(fi, ch), "Avg import fee when discharging": wavg(fi, dis),
    "Hours charging / discharging / idle": f"{int(C.sum())} / {int(D.sum())} / {int((~C & ~D).sum())}",
    "Hours charging and discharging at once": int((C & D).sum()),
    "SOC min / mean / max / end": f"{100*soc.min():.0f}% / {100*soc.mean():.1f}% / {100*soc.max():.0f}% / {100*soc.iloc[-1]:.0f}%",
    "Hours with negative day-ahead price": int((da < 0).sum()),
    "Self-consumption of PV": x["self_consumption"], "Self-sufficiency": x["self_sufficiency"],
}

# ---- reported
mk = pd.read_excel(f"{d}/KPIs.xlsx", sheet_name="Model KPIs")
sm = pd.read_excel(f"{d}/KPIs.xlsx", sheet_name="Summary")
sm["item"] = sm["item"].astype(str).str.strip()


def cmp(rep, calc):
    if isinstance(calc, str):
        return str(rep).strip() == calc, ""
    rep = float(rep)
    diff = abs(rep - calc); rel = diff / max(abs(calc), 1e-12)
    # 6 significant decimals are shown in KPIs.xlsx: accept 1e-6 relative OR 5e-7 absolute rounding
    return (rel <= 1e-6 or diff <= 5e-7), f"{rel:.1e}"


rows = []
for _, r in mk.iterrows():
    k = r["kpi"]; ok, rel = cmp(r["value"], x[k])
    rows.append(("Model KPIs", k, r["value"], x[k], rel, ok))
for k, v in y.items():
    rep = sm.loc[sm["item"] == k, "value"]
    if rep.empty:
        rows.append(("Summary", k, "MISSING", v, "", False)); continue
    ok, rel = cmp(rep.iloc[0], v)
    rows.append(("Summary", k, rep.iloc[0], v, rel, ok))
listed = set(y) | {"Run", "Status", "Horizon", "Economics", "Energy balance (MWh)", "Grid", "nan"}
unchecked = [i for i in sm["item"] if i not in listed and not i.startswith("Battery (store")]
for s, k, rep, calc, rel, ok in rows:
    print(f"{s}\t{k}\t{rep}\t{calc}\t{rel}\t{'PASS' if ok else 'FAIL'}")
print("\nALTERNATIVE DEFINITIONS");
for k, v in alt.items(): print(f"{k}\t{v}")
print("Summary items not recomputed:", unchecked)
print("PASS", sum(r[5] for r in rows), "of", len(rows))
