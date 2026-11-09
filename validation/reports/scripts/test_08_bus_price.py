"""Test 08: bus marginal price `price el` is consistent with the grid, PV and battery operating state.
Data: test_4_full_system.xlsx only. Usage: python3 test_08_bus_price.py <validation_dir>"""
import sys
import openpyxl
import pandas as pd

d = sys.argv[1]
f = f"{d}/test_4_full_system.xlsx"
TOL, PTOL = 1e-6, 1e-6   # MW, EUR/MWh
P = {(r[0], r[1]): r[2] for r in openpyxl.load_workbook(f, read_only=True)["Parameters"].iter_rows(min_row=2, values_only=True)}
TB = P[("settings", "tie_break")]
MAXI, MAXE = P[("GridConnection grid", "max_import")], P[("GridConnection grid", "max_export")]
P_CH, P_DIS = P[("Storage store", "p_ch_max")], P[("Storage store", "p_dis_max")]
ETA_C, ETA_D = P[("Storage store", "eta_ch")], P[("Storage store", "eta_dis")]

ts = pd.read_excel(f, sheet_name="Time series")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
t = pd.to_datetime(ts[col("time (Europe")])
imp, exp_ = ts[col("flow grid.import")], ts[col("flow el → grid.export")]
ch, dis = ts[col("flow el → store.charge")], ts[col("flow store.discharge")]
pv, curt, av = ts[col("flow prod.out")], ts[col("state prod.curtailment")], ts[col("input prod.profile")]
e = ts[col("state store.e")]
da, fi = ts[col("input grid.import_price")], ts[col("input grid.import_fee")]
pe, fe = ts[col("input grid.export_price")], ts[col("input grid.export_fee")]
lam = ts[col("price el")]

buy = da + fi + TB          # marginal cost of importing one more MWh (incl. tie-break)
sell = pe - fe - TB         # marginal value of exporting one more MWh (incl. tie-break)
I_int = (imp > TOL) & (imp < MAXI - TOL)
I_max = imp >= MAXI - TOL
E_int = (exp_ > TOL) & (exp_ < MAXE - TOL)
G0 = (imp <= TOL) & (exp_ <= TOL)
R = []


def chk(name, mask, viol, extra=""):
    n, v = int(mask.sum()), int((mask & viol).sum())
    R.append((name, n, v, extra))


# grid
dI = (lam - buy)[I_int]
chk("Import between 0 and 2 MW: price el = DA + import fee + tie-break", I_int, (lam - buy).abs() > PTOL,
    f"max |diff| {dI.abs().max():.1e}")
dE = (lam - sell)[E_int]
chk("Export between 0 and 2 MW: price el = DA − export fee − tie-break", E_int, (lam - sell).abs() > PTOL,
    f"max |diff| {dE.abs().max():.1e}")
prem = (lam - buy)[I_max]
chk("Import at 2 MW limit: price el ≥ DA + import fee + tie-break", I_max, lam < buy - PTOL,
    f"premium over import cost: min {prem.min():.2f}, mean {prem.mean():.2f}, max {prem.max():.2f} EUR/MWh; "
    f"hours with premium > 0: {int((prem > PTOL).sum())}")
inside = G0
chk("No grid flow: DA − export fee − TB ≤ price el ≤ DA + import fee + TB", inside,
    (lam > buy + PTOL) | (lam < sell - PTOL),
    f"hours at lower bound {int((inside & ((lam - sell).abs() <= PTOL)).sum())}, at upper {int((inside & ((lam - buy).abs() <= PTOL)).sum())}")
# PV (marginal cost 0, no tie-break on PV)
Pp = (pv > TOL) & (curt > TOL)
chk("PV partly curtailed: price el = 0", Pp, lam.abs() > PTOL, f"max |price el| {lam[Pp].abs().max():.1e}")
Pf = (av > TOL) & (curt <= TOL)
chk("PV fully used: price el ≥ 0", Pf, lam < -PTOL)
Pc = (av > TOL) & (pv <= TOL)
chk("PV fully curtailed: price el ≤ 0", Pc, lam > PTOL)
# battery, same-hour conditions (charging costs lam + TB per MWh, discharging earns lam − TB)
both = (ch > TOL) & (dis > TOL)
chk("Charging and discharging at once: price el < 0", both, lam >= 0, f"price el range {lam[both].min():.2f} … {lam[both].max():.2f}")
# simultaneous at full charge power only pays if the round-trip waste is worth it -> price el < -TB*(…) ; report only
# inter-temporal: between consecutive hours with the battery charging at an interior level and not at SOC bounds,
# the shadow value of stored energy is constant -> price el / eta_ch (charging) equals price el*eta_dis (discharging).
# We check the necessary condition on all consecutive interior charge->discharge pairs (no SOC bound hit in between).
ci = (ch > TOL) & (ch < P_CH - TOL) & (dis <= TOL)
di = (dis > TOL) & (dis < P_DIS - TOL) & (ch <= TOL)
mu_c = (lam + TB) / ETA_C       # value of 1 MWh stored, implied by interior charging
mu_d = (lam - TB) * ETA_D       # value of 1 MWh stored, implied by interior discharging
mu = pd.Series(float("nan"), index=ts.index); mu[ci] = mu_c[ci]; mu[di] = mu_d[di]
bound = (e <= 1e-6) | (e >= 2 - 1e-6)
pairs, viol, worst = 0, 0, 0.0
last = None
for i in ts.index:
    if bound[i]:
        last = None
    if pd.notna(mu[i]):
        if last is not None:
            pairs += 1; dv = abs(mu[i] - mu[last]); worst = max(worst, dv); viol += dv > 1e-4
        last = i if not bound[i] else None
R.append(("Battery interior operation: implied value of stored energy constant between SOC bounds", pairs, viol,
          f"max |Δ stored-energy value| {worst:.1e} EUR/MWh over {pairs} consecutive pairs"))

# summary statistics
stats = {
    "price el min / mean / max": (float(lam.min()), float(lam.mean()), float(lam.max())),
    "hours price el < 0": int((lam < -PTOL).sum()), "hours price el = 0": int((lam.abs() <= PTOL).sum()),
    "hours importing (interior / at limit)": (int(I_int.sum()), int(I_max.sum())),
    "hours exporting (interior)": int(E_int.sum()), "hours no grid flow": int(G0.sum()),
    "covered hours (some state classified)": int((I_int | I_max | E_int | G0).sum()),
    "avg price el weighted by withdrawals (demand+charge+export)":
        float((lam * (ts[col('flow el → demand.in')] + ch + exp_)).sum() / (ts[col('flow el → demand.in')] + ch + exp_).sum()),
}
for r in R:
    print(f"{r[0]}\thours {r[1]}\tviolations {r[2]}\t{r[3]}")
for k, v in stats.items():
    print(f"{k}\t{v}")
show = pd.DataFrame({"time": t, "imp": imp, "ch": ch, "dis": dis, "e": e, "DA+fee+TB": buy, "price_el": lam,
                     "premium": lam - buy}).round(3)
print("\nTOP 10 PREMIUM AT IMPORT LIMIT"); print(show[I_max].nlargest(10, "premium").to_string(index=False))
print("\nPREMIUM AT LIMIT, by battery state")
print("  charging 1 MW:", int((I_max & (ch >= P_CH - TOL)).sum()), "| other:", int((I_max & (ch < P_CH - TOL)).sum()))
