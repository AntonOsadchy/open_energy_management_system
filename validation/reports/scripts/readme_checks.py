"""All 12 checks of README §5 for run test_4_full_system.
Data: test_4_full_system.xlsx. KPIs.xlsx is read ONLY for status / slack / objective (checks 1 and 12),
which exist nowhere else; they are the reported figures under test, not inputs.
Usage: python3 readme_checks.py <validation_dir>"""
import sys
import numpy as np
import openpyxl
import pandas as pd

d = sys.argv[1]
f = f"{d}/test_4_full_system.xlsx"
TOL_MW = 1e-6
TOL_REL = 1e-6
R = []  # (id, name, pass, detail)


def add(i, name, ok, detail):
    R.append((i, name, bool(ok), detail))


P = {}
for r in openpyxl.load_workbook(f, read_only=True)["Parameters"].iter_rows(min_row=2, values_only=True):
    P[(r[0], r[1])] = r[2]
g, s = "GridConnection grid", "Storage store"
max_imp, max_exp = P[(g, "max_import")], P[(g, "max_export")]
p_ch, p_dis, e_max = P[(s, "p_ch_max")], P[(s, "p_dis_max")], P[(s, "e_max")]
eta_ch, eta_dis, sigma = P[(s, "eta_ch")], P[(s, "eta_dis")], P[(s, "self_discharge")]
soc_init, soc_lo, soc_hi = P[(s, "soc_init")], P[(s, "soc_min")], P[(s, "soc_max")]
dem_prof = P[("Demand demand", "profile")]
tb_rate = P[("settings", "tie_break")]

ts = pd.read_excel(f, sheet_name="Time series")
costs = pd.read_excel(f, sheet_name="Costs")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
t = pd.to_datetime(ts[col("time (Europe")])
dt = ts[col("step_h")]
imp, exp_ = ts[col("flow grid.import")], ts[col("flow el → grid.export")]
dem, ch = ts[col("flow el → demand.in")], ts[col("flow el → store.charge")]
dis, pv = ts[col("flow store.discharge")], ts[col("flow prod.out")]
e, soc = ts[col("state store.e")], ts[col("state store.soc")]
curt, pv_av = ts[col("state prod.curtailment")], ts[col("input prod.profile")]
p_imp, p_exp = ts[col("input grid.import_price")], ts[col("input grid.export_price")]
f_imp, f_exp = ts[col("input grid.import_fee")], ts[col("input grid.export_fee")]
rev, c_en, c_net = ts[col("econ revenue")], ts[col("econ cost ENERGY")], ts[col("econ cost NETWORK")]
prof, prof_cum = ts[col("econ profit [")], ts[col("econ profit cumulative")]
worst = lambda ser: f"{ser.abs().max():.2e} at {t.iloc[ser.abs().idxmax()]}"

# 1 status & slack (KPIs.xlsx Run, reported values)
run = pd.read_excel(f"{d}/KPIs.xlsx", sheet_name="Run")
rv = dict(zip(run["item"].astype(str).str.strip(), run["value"]))
slack_cols = [c for c in ts.columns if "slack" in c.lower()]
slack_cost = costs.loc[costs.category.str.upper() == "SLACK", "EUR"].sum()
add(1, "Status optimal, no slack",
    str(rv["status"]).strip() == "optimal" and str(rv["slack"]).startswith("none") and not slack_cols and slack_cost == 0,
    f"status = '{rv['status']}'; slack = '{rv['slack']}'; solver warnings = '{rv.get('solver warnings')}'; "
    f"no slack column in Time series; SLACK cost in Costs = {slack_cost}")

# 2 flows >= 0 and limits
flows = {"import": (imp, max_imp), "export": (exp_, max_exp), "charge": (ch, p_ch), "discharge": (dis, p_dis),
         "pv": (pv, None), "demand": (dem, None)}
neg = {k: int((v < -TOL_MW).sum()) for k, (v, _) in flows.items()}
over = {k: int((v > lim + TOL_MW).sum()) for k, (v, lim) in flows.items() if lim is not None}
mx = {k: round(float(v.max()), 9) for k, (v, lim) in flows.items() if lim is not None}
mn = min(float(v.min()) for v, _ in flows.values())
add(2, "Flows ≥ 0 and within limits", sum(neg.values()) == 0 and sum(over.values()) == 0,
    f"min flow = {mn:.3g}; max: import {mx['import']}/{max_imp}, export {mx['export']}/{max_exp}, "
    f"charge {mx['charge']}/{p_ch}, discharge {mx['discharge']}/{p_dis} MW; violations: negative {sum(neg.values())}, over limit {sum(over.values())}")

# 3 bus balance
bal = (imp + dis + pv) - (exp_ + ch + dem)
add(3, "Bus balance every hour", (bal.abs() > TOL_MW).sum() == 0,
    f"max |in − out| = {worst(bal)} MW; hours > 1e-6: {(bal.abs() > TOL_MW).sum()}")

# 4 demand
dd = dem - dem_prof
add(4, "demand.in = 1 MW every hour", (dd.abs() > TOL_MW).sum() == 0,
    f"min {dem.min()}, max {dem.max()} MW; hours off profile: {(dd.abs() > TOL_MW).sum()}")

# 5 PV
dpv = pv + curt - pv_av
add(5, "PV used + curtailed = available", (dpv.abs() > TOL_MW).sum() == 0 and (curt < -TOL_MW).sum() == 0,
    f"max |diff| = {worst(dpv)} MW; negative curtailment hours: {(curt < -TOL_MW).sum()}; "
    f"curtailed {float((curt*dt).sum()):.3f} MWh")

# 6 grid exclusive
ex = imp / max_imp + exp_ / max_exp
both = int(((imp > TOL_MW) & (exp_ > TOL_MW)).sum())
add(6, "Grid exclusive: import/2 + export/2 ≤ 1", (ex > 1 + TOL_MW).sum() == 0,
    f"max value = {ex.max():.6f}; hours > 1: {(ex > 1 + TOL_MW).sum()}; hours importing and exporting at once: {both}")

# 7 storage recursion
e_prev = pd.concat([pd.Series([soc_init * e_max]), e.iloc[:-1]], ignore_index=True)
e_step = (1 - sigma) ** dt * e_prev + (eta_ch * ch - dis / eta_dis) * dt  # one-step, from reported e[t-1]
de_step = e - e_step
e_rec = np.empty(len(ts)); x = soc_init * e_max  # full recursion from e[-1] only
for i in range(len(ts)):
    x = (1 - sigma) ** dt.iat[i] * x + (eta_ch * ch.iat[i] - dis.iat[i] / eta_dis) * dt.iat[i]
    e_rec[i] = x
de_full = e - e_rec
add(7, "Storage energy recursion", (de_step.abs() > TOL_MW).sum() == 0 and np.abs(de_full).max() <= 1e-6,
    f"one-step max |diff| = {worst(de_step)} MWh; full recursion from e[−1] = {soc_init*e_max} MWh: "
    f"max |drift| = {np.abs(de_full).max():.2e} MWh")

# 8 SOC bounds & end condition
dsoc = soc - e / e_max
add(8, "0 ≤ SOC ≤ 1, end e ≥ 1 MWh",
    (soc < soc_lo - TOL_MW).sum() == 0 and (soc > soc_hi + TOL_MW).sum() == 0 and e.iloc[-1] >= soc_init * e_max - TOL_MW
    and (dsoc.abs() > TOL_MW).sum() == 0,
    f"SOC min {soc.min():.6f}, max {soc.max():.6f}; e min {e.min():.6f}, max {e.max():.6f} MWh; "
    f"end e = {e.iloc[-1]:.6f} MWh (≥ {soc_init*e_max}); max |soc − e/e_max| = {dsoc.abs().max():.1e}")

# 9 simultaneous charge & discharge
sim = (ch > TOL_MW) & (dis > TOL_MW)
eff = p_imp + f_imp
bad = sim & (eff >= 0)
add(9, "Simultaneous charge/discharge only when DA + import fee < 0", bad.sum() == 0,
    f"{sim.sum()} hours with both; of these, DA + fee < 0 in {(sim & (eff < 0)).sum()}, ≥ 0 in {bad.sum()}; "
    f"DA + fee in those hours ranges {eff[sim].min():.2f} … {eff[sim].max():.2f} EUR/MWh")
sim_rows = ts.loc[sim, [col("time (Europe"), col("flow el → store.charge"), col("flow store.discharge"),
                        col("input grid.import_price"), col("input grid.import_fee")]].copy()
sim_rows["DA+fee"] = eff[sim]

# 10 per-step economics
d_en = c_en - p_imp * imp * dt
d_rev = rev - p_exp * exp_ * dt
d_net = c_net - (f_imp * imp + f_exp * exp_) * dt
m10 = max(d_en.abs().max(), d_rev.abs().max(), d_net.abs().max())
add(10, "Per-step ENERGY, REVENUE, NETWORK", m10 <= 1e-6,
    f"max |diff|: ENERGY {d_en.abs().max():.1e}, REVENUE {d_rev.abs().max():.1e}, NETWORK {d_net.abs().max():.1e} EUR")

# 11 profit
d_p = prof - (rev - c_en - c_net)
total_cost_costs = costs.loc[costs.category != "TIEBREAK", "EUR"].sum()
rel11 = abs(prof.sum() + total_cost_costs) / total_cost_costs
add(11, "Profit = revenue − costs; Σ = −total cost",
    d_p.abs().max() <= 1e-6 and rel11 <= TOL_REL and abs(prof_cum.iloc[-1] - prof.sum()) <= 1e-6 * abs(prof.sum()),
    f"per-step max |diff| = {d_p.abs().max():.1e} EUR; Σ profit = {prof.sum():,.6f}; −total cost (Costs) = {-total_cost_costs:,.6f}; "
    f"rel. diff {rel11:.1e}; last cumulative = {prof_cum.iloc[-1]:,.6f}")

# 12 Costs sheet vs recomputed, objective
cat = costs.groupby("category")["EUR"].sum()
re_ = {"ENERGY": float((p_imp * imp * dt).sum()),
       "NETWORK": float(((f_imp * imp + f_exp * exp_) * dt).sum()),
       "REVENUE": -float((p_exp * exp_ * dt).sum()),
       "TIEBREAK": float((tb_rate * (imp + exp_ + ch + dis) * dt).sum())}
rel12 = {k: abs(cat.get(k, 0) - v) / abs(v) for k, v in re_.items()}
other = {k: v for k, v in cat.items() if k not in re_ and v != 0}
obj = float(rv["objective"]); tc_run = float(rv["total_cost"])
obj_rel = abs(obj - (total_cost_costs + cat["TIEBREAK"])) / obj
add(12, "Costs sheet = recomputed; objective = total + TIEBREAK",
    max(rel12.values()) <= TOL_REL and not other and obj_rel <= TOL_REL and abs(tc_run - total_cost_costs) / tc_run <= TOL_REL,
    "; ".join(f"{k}: Costs {cat.get(k,0):,.6f} vs recomputed {v:,.6f} (rel {rel12[k]:.1e})" for k, v in re_.items())
    + f"; other non-zero categories: {other or 'none'}; objective {obj:,.6f} vs total+TB {total_cost_costs + cat['TIEBREAK']:,.6f} (rel {obj_rel:.1e})")

for i, n, ok, det in R:
    print(f"{i}\t{'PASS' if ok else 'FAIL'}\t{n}\t{det}")
print("\nSIMULTANEOUS CHARGE/DISCHARGE HOURS")
print(sim_rows.to_string(index=False))
