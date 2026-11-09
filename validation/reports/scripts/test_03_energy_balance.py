"""Test 03: energy balance. import + PV used = demand + export + battery losses + change in stored energy.
Losses computed from the efficiencies, independently of the reported state of energy.
Data: test_4_full_system.xlsx only. Usage: python3 test_03_energy_balance.py <validation_dir>"""
import sys
import openpyxl
import pandas as pd

d = sys.argv[1]
f = f"{d}/test_4_full_system.xlsx"
P = {(r[0], r[1]): r[2] for r in openpyxl.load_workbook(f, read_only=True)["Parameters"].iter_rows(min_row=2, values_only=True)}
s = "Storage store"
eta_ch, eta_dis, e_max, soc_init, sigma = (P[(s, k)] for k in ("eta_ch", "eta_dis", "e_max", "soc_init", "self_discharge"))

ts = pd.read_excel(f, sheet_name="Time series")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
t = pd.to_datetime(ts[col("time (Europe")]); dt = ts[col("step_h")]
imp, exp_ = ts[col("flow grid.import")] * dt, ts[col("flow el → grid.export")] * dt
dem, ch = ts[col("flow el → demand.in")] * dt, ts[col("flow el → store.charge")] * dt
dis, pv = ts[col("flow store.discharge")] * dt, ts[col("flow prod.out")] * dt
curt, pv_av = ts[col("state prod.curtailment")] * dt, ts[col("input prod.profile")] * dt
e = ts[col("state store.e")]
e0 = soc_init * e_max
de = e - pd.concat([pd.Series([e0]), e.iloc[:-1]], ignore_index=True)  # change in stored energy per hour

loss_ch = (1 - eta_ch) * ch            # lost while charging
loss_dis = (1 / eta_dis - 1) * dis     # lost while discharging (extra energy drawn from the cell)
loss = loss_ch + loss_dis              # sigma = 0 -> no self-discharge loss

# hourly balance: supply - (consumption + losses + storage build-up)
resid = (imp + pv) - (dem + exp_ + loss + de)
# storage-side balance: charge - discharge = losses + delta e
resid_store = (ch - dis) - (loss + de)

S = lambda x: float(x.sum())
o = {
    "sigma": sigma, "eta_ch": eta_ch, "eta_dis": eta_dis, "e_start": e0, "e_end": float(e.iloc[-1]),
    "import": S(imp), "pv_available": S(pv_av), "pv_curtailed": S(curt), "pv_used": S(pv),
    "demand": S(dem), "export": S(exp_), "charge": S(ch), "discharge": S(dis),
    "loss_charge": S(loss_ch), "loss_discharge": S(loss_dis), "loss_total": S(loss),
    "loss_as_charge_minus_discharge_minus_de": S(ch) - S(dis) - (float(e.iloc[-1]) - e0),
    "delta_e_year": float(e.iloc[-1]) - e0,
    "supply": S(imp) + S(pv), "consumption_plus_losses_plus_de": S(dem) + S(exp_) + S(loss) + (float(e.iloc[-1]) - e0),
    "annual_residual": S(resid),
    "pv_check_used_plus_curt_minus_avail": S(pv) + S(curt) - S(pv_av),
    "hourly_resid_max_abs": float(resid.abs().max()), "hourly_resid_worst_at": str(t.iloc[resid.abs().idxmax()]),
    "hours_resid_gt_1e-6": int((resid.abs() > 1e-6).sum()),
    "store_resid_max_abs": float(resid_store.abs().max()),
    "loss_share_of_charge": S(loss) / S(ch), "roundtrip_eff_implied": S(dis) / (S(ch) - (float(e.iloc[-1]) - e0)),
    "roundtrip_eff_theory": eta_ch * eta_dis,
}
for k, v in o.items():
    print(f"{k}\t{v!r}")
