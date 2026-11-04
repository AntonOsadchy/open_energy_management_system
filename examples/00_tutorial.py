"""Tutorial: build a small site step by step (see README, section "Tutorial")."""

import numpy as np
import pandas as pd

import oems
from oems import presets as P

# 1. Time grid and inputs: two days, hourly
grid = oems.TimeGrid(start="2025-06-02", periods=48, freq="1h", tz="Europe/Copenhagen")
hour = grid.index.hour
price = pd.Series(np.where((hour >= 7) & (hour < 21), 120.0, 60.0), index=grid.index)   # EUR/MWh
pv_cf = pd.Series(np.clip(np.sin((hour - 6) / 14 * np.pi), 0, None), index=grid.index)  # 0..1
el_load = 0.5                                                                            # MW, constant
heat_load = pd.Series(np.where((hour >= 6) & (hour < 18), 0.8, 0.3), index=grid.index)   # MW
cop = 3.0


def site(step):
    es = oems.EnergySystem(grid)

    # 2. Buses and the grid connection
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"))
    es.add(oems.GridConnection("dso", el, import_price=price, import_fee=40, export_price=price,
                               max_import=2, max_export=1),
           oems.Demand("base_el", el, profile=el_load))

    # 3. Rooftop PV
    if step >= 3:
        es.add(P.pv("pv", el, capacity=1.0, cf=pv_cf))

    # 4. Battery
    if step >= 4:
        es.add(P.battery("bess", el, e_max=2, p_max=1, eta_rt=0.9))

    # 5. Heat: heat pump, tank and demand on a second bus
    if step >= 5:
        heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
        es.add(P.heat_pump("hp", el, heat, cop=cop, p_th_max=1.5),
               P.hot_water_tank("tank", heat, e_max=4, p_max=1, loss_per_h=0.005),
               oems.Demand("process_heat", heat, profile=heat_load))
    return es


if __name__ == "__main__":
    for step in (2, 3, 4, 5):
        res = site(step).solve()
        print(f"step {step}: total cost {res.total_cost:8.0f} EUR")

    # 6. Results
    print(res.summary())
    print(res.costs.round(1))
    print(res.flow_matrix("el").round(2))
    print(res.kpis().round(2))
    print(res.shadow_prices.head(8).round(1))
    res.to_csv("out/tutorial")
