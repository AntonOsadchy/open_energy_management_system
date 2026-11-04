"""Industrial site electrification: baseline (gas boiler) vs electrified (HP + e-boiler + TES + BESS).

Inputs come at mixed resolutions (15-min prices, hourly site data); the model aligns them to the
chosen resolution. Run:  python examples/02_site_electrification.py [--freq 1h|15min] [--days N]
"""

import argparse
from pathlib import Path

import oems
from oems import presets as P

DATA = Path(__file__).parent / "data"
TZ = "Europe/Copenhagen"


def build(grid: oems.TimeGrid, electrified: bool) -> oems.EnergySystem:
    prices = oems.load_csv(DATA / "prices_15min.csv", tz=TZ)["da_price"]
    site = oems.load_csv(DATA / "site_hourly.csv", tz=TZ)

    es = oems.EnergySystem(grid, name="electrified" if electrified else "baseline")
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"))
    gas = es.add_bus("gas", oems.Carrier("gas", kind="fuel"))
    h90 = es.add_bus("heat_90C", oems.Carrier("heat", kind="heat"))
    h45 = es.add_bus("heat_45C", h90.carrier)
    cold = es.add_bus("cold", oems.Carrier("cold", kind="cold"))

    es.add(
        oems.GridConnection("dso", el, import_price=prices, import_fee=site["net_fee"], export_price=prices,
                            export_fee=2.0, max_import=6.0, max_export=1.0, peak_charge=4500,
                            peak_period="M", peak_measurement="15min",
                            emission_factors={"import": {"co2": site["grid_co2"]}}),
        oems.GridConnection("gas_grid", gas, import_price=site["gas_price"],
                            emission_factors={"import": {"co2": 0.202}}),
        P.pv("pv_roof", el, capacity=0.8, cf=site["pv_cf"]),
        P.gas_boiler("gb", gas, h90, eta=0.92, p_th_max=4.0),
        P.link("cascade", h90, h45),
        oems.FlexibleDemand("process_heat", h90, baseline=site["heat_load"], p_max=4.0, window="D",
                            max_deferred=2.0, max_advanced=1.0),
        oems.Demand("space_heat", h45, profile=site["space_heat"]),
        P.chiller("ch1", el, cold, eer=4.0, p_cold_max=1.0),
        oems.ThermalZone("cold_store", cold_bus=cold, C=0.9, R=60, theta_amb=site["t_amb"], theta_min=-22,
                         theta_max=-18, theta_init=-20, gains=0.05),
        oems.Demand("base_el", el, profile=site["el_load"]),
    )
    if electrified:
        es.add(
            P.heat_pump("hp1", el, h90, cop=site["cop90"], p_th_max=1.5),
            P.e_boiler("eb", el, h90, p_max=2.0),
            P.hot_water_tank("tes", h90, e_max=10, p_max=3, loss_per_h=0.003),
            P.battery("bess", el, e_max=2.0, p_max=1.0, eta_rt=0.9, cycle_cost=10),
        )
    es.set_emission_policy("co2", price=85)
    return es


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="1h", choices=["1h", "15min"])
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    if not (DATA / "site_hourly.csv").exists():
        from make_data import main as make
        make()
    grid = oems.TimeGrid(start="2025-01-01", periods=args.days * (24 if args.freq == "1h" else 96),
                         freq=args.freq, tz=TZ)
    results = {}
    for electrified in (False, True):
        es = build(grid, electrified)
        res = es.solve()
        results[es.name] = res
        print(f"\n=== {es.name} ===")
        print(res.summary())
        res.to_csv(Path(args.out) / es.name)
    k0 = results["baseline"].kpis().set_index(["kpi", "scope"])["value"]
    k1 = results["electrified"].kpis().set_index(["kpi", "scope"])["value"]
    print("\nKPI comparison (baseline -> electrified)")
    for key in [("total_cost", "system"), ("emissions_co2", "system"), ("heat_electrification_share", "heat")]:
        print(f"  {key[0]:<28} {k0.get(key, 0):>14,.2f} -> {k1.get(key, 0):>14,.2f}")


if __name__ == "__main__":
    main()
