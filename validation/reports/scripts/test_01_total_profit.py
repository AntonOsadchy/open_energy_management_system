"""Test 01: total profit in KPIs vs total profit from hourly results.
Hourly data: test_4_full_system.xlsx only. KPIs.xlsx read only for the reported totals under test."""
import sys
import pandas as pd
import numpy as np

d = sys.argv[1]
ts = pd.read_excel(f"{d}/test_4_full_system.xlsx", sheet_name="Time series")
costs = pd.read_excel(f"{d}/test_4_full_system.xlsx", sheet_name="Costs")
summ = pd.read_excel(f"{d}/KPIs.xlsx", sheet_name="Summary")
mk = pd.read_excel(f"{d}/KPIs.xlsx", sheet_name="Model KPIs")
run = pd.read_excel(f"{d}/KPIs.xlsx", sheet_name="Run")

c = {k: [x for x in ts.columns if x.startswith(k)][0] for k in [
    "step_h", "flow grid.import", "flow el → grid.export", "econ revenue", "econ cost ENERGY",
    "econ cost NETWORK", "econ profit [", "econ profit cumulative", "input grid.import_price",
    "input grid.export_price", "input grid.import_fee", "input grid.export_fee"]}
dt = ts[c["step_h"]]; imp = ts[c["flow grid.import"]]; exp_ = ts[c["flow el → grid.export"]]

out = {}
# A: sum of model's hourly profit column
out["A_sum_econ_profit"] = ts[c["econ profit ["]].sum()
out["A_last_cumulative"] = ts[c["econ profit cumulative"]].iloc[-1]
out["A_sum_rev_minus_costs"] = (ts[c["econ revenue"]] - ts[c["econ cost ENERGY"]] - ts[c["econ cost NETWORK"]]).sum()
# B: independent recompute from flows x input prices
rev = ts[c["input grid.export_price"]] * exp_ * dt
ene = ts[c["input grid.import_price"]] * imp * dt
net = (ts[c["input grid.import_fee"]] * imp + ts[c["input grid.export_fee"]] * exp_) * dt
prof_B = rev - ene - net
out["B_recomputed_profit"] = prof_B.sum()
out["B_revenue"], out["B_energy"], out["B_network"] = rev.sum(), ene.sum(), net.sum()
# per-hour consistency
out["max_abs_hour_diff_profit_col_vs_recompute"] = (ts[c["econ profit ["]] - prof_B).abs().max()
out["max_abs_hour_diff_profit_col_vs_rev_minus_costs"] = (ts[c["econ profit ["]] - (ts[c["econ revenue"]] - ts[c["econ cost ENERGY"]] - ts[c["econ cost NETWORK"]])).abs().max()
out["max_abs_cumsum_diff"] = (ts[c["econ profit ["]].cumsum() - ts[c["econ profit cumulative"]]).abs().max()

# reported
g = lambda df, col, key, vcol: float(df.loc[df[col].astype(str).str.strip() == key, vcol].iloc[0])
out["KPI_Summary_total_cost"] = g(summ, "item", "Total cost", "value")
out["KPI_Summary_energy"] = g(summ, "item", "Energy (day-ahead on import)", "value")
out["KPI_Summary_network"] = g(summ, "item", "Network (import + export fees)", "value")
out["KPI_Summary_export_rev"] = g(summ, "item", "Export revenue", "value")
out["KPI_Summary_tiebreak"] = g(summ, "item", "Tie-break (excluded)", "value")
out["KPI_Summary_objective"] = g(summ, "item", "Solver objective", "value")
out["KPI_Model_total_cost"] = g(mk, "kpi", "total_cost", "value")
out["KPI_Run_total_cost"] = g(run, "item", "total_cost", "value")
out["KPI_Run_objective"] = g(run, "item", "objective", "value")
cs = costs.groupby("category")["EUR"].sum()
out["Costs_sheet_total_excl_TB"] = costs.loc[costs.category != "TIEBREAK", "EUR"].sum()
out["Costs_sheet_TB"] = cs.get("TIEBREAK", 0)
for k, v in cs.items(): out[f"Costs_{k}"] = v
# tie-break recompute: 0.01 EUR/MWh on grid import+export, storage charge+discharge
ch = ts[[x for x in ts.columns if x.startswith("flow el → store.charge")][0]]
dis = ts[[x for x in ts.columns if x.startswith("flow store.discharge")][0]]
out["TB_grid_recomputed"] = (0.01 * (imp + exp_) * dt).sum()
out["TB_store_recomputed"] = (0.01 * (ch + dis) * dt).sum()
out["n_rows"] = len(ts); out["step_h_unique"] = sorted(dt.unique().tolist())
out["profit_sum_negative_hours"] = int((prof_B < 0).sum()); out["profit_sum_positive_hours"] = int((prof_B > 0).sum())
for k, v in out.items(): print(f"{k}\t{v!r}")
