"""Test 06: battery charges when energy is cheap and discharges when expensive.
Data: test_4_full_system.xlsx only. Usage: python3 test_06_battery_logic.py <validation_dir>"""
import sys
import numpy as np
import pandas as pd

d = sys.argv[1]
TOL = 1e-6
ETA_RT = 0.95 * 0.95
ts = pd.read_excel(f"{d}/test_4_full_system.xlsx", sheet_name="Time series")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
t = pd.to_datetime(ts[col("time (Europe")])
imp, exp_ = ts[col("flow grid.import")], ts[col("flow el → grid.export")]
ch, dis = ts[col("flow el → store.charge")], ts[col("flow store.discharge")]
pv, dem, e = ts[col("flow prod.out")], ts[col("flow el → demand.in")], ts[col("state store.e")]
da, fi, fe = ts[col("input grid.import_price")], ts[col("input grid.import_fee")], ts[col("input grid.export_fee")]
lam = ts[col("price el")]
ref = ts[col("ref cost without battery")]
prof = ts[col("econ profit [")]
site = da + fi  # what one imported MWh costs the site
day = t.dt.date
C, D = ch > TOL, dis > TOL
o = {}

# 1. annual weighted prices
w = lambda p, f: float((p * f).sum() / f.sum())
for name, p in (("DA", da), ("DA+fee", site), ("bus", lam)):
    o[f"avg_{name}_charge"] = w(p, ch); o[f"avg_{name}_discharge"] = w(p, dis)
    o[f"avg_{name}_all_hours"] = float(p.mean())
o["breakeven_discharge_price_bus"] = o["avg_bus_charge"] / ETA_RT
o["hours_charge"], o["hours_discharge"], o["hours_both"] = int(C.sum()), int(D.sum()), int((C & D).sum())
o["hours_idle"] = int((~C & ~D).sum())

# 2. daily: rank of price within the local day (0 = cheapest, 1 = most expensive)
pct = site.groupby(day).rank(pct=True, method="average")
pct_bus = lam.groupby(day).rank(pct=True, method="average")
o["charge_energy_in_cheapest_half_of_day_DA+fee"] = float(ch[pct <= 0.5].sum() / ch.sum())
o["discharge_energy_in_dearest_half_of_day_DA+fee"] = float(dis[pct > 0.5].sum() / dis.sum())
o["charge_energy_in_cheapest_half_of_day_bus"] = float(ch[pct_bus <= 0.5].sum() / ch.sum())
o["discharge_energy_in_dearest_half_of_day_bus"] = float(dis[pct_bus > 0.5].sum() / dis.sum())
o["avg_rank_charge_DA+fee"] = float((pct * ch).sum() / ch.sum())
o["avg_rank_discharge_DA+fee"] = float((pct * dis).sum() / dis.sum())
g = pd.DataFrame({"day": day, "ch": ch, "dis": dis, "pc": site * ch, "pd": site * dis,
                  "bc": lam * ch, "bd": lam * dis})
dd = g.groupby("day").sum()
both = dd[(dd.ch > TOL) & (dd.dis > TOL)]
both = both.assign(p_ch=both.pc / both.ch, p_dis=both.pd / both.dis, b_ch=both.bc / both.ch, b_dis=both.bd / both.dis)
o["days_with_charge_and_discharge"] = len(both)
o["days_discharge_price_gt_charge_price_DA+fee"] = int((both.p_dis > both.p_ch).sum())
o["days_discharge_price_le_charge_price_DA+fee"] = int((both.p_dis <= both.p_ch).sum())
o["days_bus_spread_covers_losses"] = int((both.b_dis * ETA_RT >= both.b_ch - 1e-6).sum())
o["days_bus_spread_not_cover_losses"] = int((both.b_dis * ETA_RT < both.b_ch - 1e-6).sum())

# 3. LP-consistency per cycle: charge in hours where bus price is low relative to discharge hours.
#    Split year into cycles = maximal runs between moments the battery is empty (e ~ 0); within each
#    cycle, max bus price while charging should be <= eta_rt^-1 * min bus price while discharging?  Not
#    required (energy can be held over). Instead use the necessary condition: for charge hour i and the
#    *next* discharge hour j, eta_ch*eta_dis*lam_j >= lam_i  -> otherwise moving energy i->j loses money.
nxt = pd.Series(np.where(D, lam, np.nan)).bfill()      # bus price at next (or same) discharge hour
viol = C & ~D & (ETA_RT * nxt < lam - 1e-6)
o["charge_hours_where_next_discharge_does_not_cover_losses"] = int(viol.sum())

# 4. exceptions w.r.t. simple 'cheap/expensive' view: charge in dearest quartile / discharge in cheapest quartile of day
CQ = C & (pct > 0.75); DQ = D & (pct <= 0.25)
o["charge_hours_in_dearest_quarter_of_day"] = int(CQ.sum()); o["charge_MWh_there"] = float(ch[CQ].sum())
o["discharge_hours_in_cheapest_quarter_of_day"] = int(DQ.sum()); o["discharge_MWh_there"] = float(dis[DQ].sum())
# explanation of charges in the dearest quarter: from PV surplus (no import)?
o["  of which charge from PV surplus (no import)"] = int((CQ & (imp <= TOL)).sum())
o["  of which charge with import"] = int((CQ & (imp > TOL)).sum())
o["  of which simultaneous with discharge"] = int((CQ & D).sum())

# 5. charging vs negative prices
o["hours_DA_neg"] = int((da < 0).sum()); o["hours_DA_neg_charging"] = int(((da < 0) & C).sum())
o["hours_DA_neg_discharging"] = int(((da < 0) & D & ~C).sum())
o["hours_site_cost_neg"] = int((site < 0).sum()); o["hours_site_cost_neg_charging_at_max"] = int(((site < 0) & (ch >= 1 - TOL)).sum())

# 6. value of the battery
o["total_cost"] = float(-prof.sum()); o["ref_cost_no_battery"] = float(ref.sum())
o["battery_value_EUR"] = o["ref_cost_no_battery"] - o["total_cost"]
o["battery_value_per_MWh_discharged"] = o["battery_value_EUR"] / float(dis.sum())
o["hours_ref_lt_cost"] = int((ref < -prof - 1e-6).sum())   # hours where battery made this hour more expensive (charging)

for k, v in o.items():
    print(f"{k}\t{v!r}")
show = pd.DataFrame({"time": t, "ch": ch, "dis": dis, "e": e, "imp": imp, "exp": exp_, "pv": pv, "DA": da,
                     "DA+fee": site, "bus": lam, "rank": pct}).round(3)
print("\nDAYS WITH DISCHARGE PRICE <= CHARGE PRICE (DA+fee)")
print(both[both.p_dis <= both.p_ch][["ch", "dis", "p_ch", "p_dis", "b_ch", "b_dis"]].round(2).to_string())
print("\nCHARGE IN DEAREST QUARTER, sample"); print(show[CQ].head(15).to_string(index=False))
print("\nDISCHARGE IN CHEAPEST QUARTER, sample"); print(show[DQ].head(15).to_string(index=False))
# monthly view
m = pd.DataFrame({"m": t.dt.month, "ch": ch, "dis": dis, "pc": site * ch, "pd": site * dis}).groupby("m").sum()
m["p_ch"], m["p_dis"] = m.pc / m.ch, m.pd / m.dis
print("\nMONTHLY"); print(m[["ch", "dis", "p_ch", "p_dis"]].round(2).to_string())

# 7. reverse condition: energy discharged at j was charged at the previous charge hour i -> eta_rt*lam_j >= lam_i
prv = pd.Series(np.where(C, lam, np.nan)).ffill()
rv = D & ~C & (ETA_RT * lam < prv - 1e-6)
print("\ndischarge_hours_where_previous_charge_not_recovered", int(rv.sum()))
print("discharge_hours_before_first_charge (initial 1 MWh)", int((D & prv.isna()).sum()))
# charges in dearest quarter: next discharge time & bus price
nxt_t = pd.Series(np.where(D, t, pd.NaT)).bfill()
print("\nCHARGE IN DEAREST QUARTER -> next discharge")
for i in ts.index[CQ]:
    print(str(t[i]), "bus", round(lam[i], 2), "-> next discharge", str(pd.Timestamp(nxt_t[i])), "bus", round(nxt[i], 2),
          "| covers losses:", bool(ETA_RT * nxt[i] >= lam[i] - 1e-6))
# discharges in cheapest quarter that are NOT simultaneous charge/discharge hours
print("\nDISCHARGE IN CHEAPEST QUARTER (not simultaneous) -> previous charge")
prv_t = pd.Series(np.where(C, t, pd.NaT)).ffill()
for i in ts.index[DQ & ~C]:
    print(str(t[i]), "bus", round(lam[i], 2), "day max DA+fee", round(site[day == day[i]].max(), 2),
          "| prev charge", str(pd.Timestamp(prv_t[i])) if not pd.isna(prv_t[i]) else "none", "bus", None if pd.isna(prv[i]) else round(prv[i], 2))
