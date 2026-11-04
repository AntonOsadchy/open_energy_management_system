"""Generate synthetic example time series (one year) in examples/data/.

The data are illustrative only: day-ahead prices with daily/seasonal shape and noise (15-min),
network fees and grid CO2 intensity (hourly), PV capacity factor, heat and electricity loads and
ambient temperature (hourly). Run: python examples/make_data.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).parent / "data"
TZ = "Europe/Copenhagen"


def main(year: int = 2025, seed: int = 42) -> None:
    rng = np.random.default_rng(seed)
    OUT.mkdir(exist_ok=True)
    h = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="1h", tz=TZ, inclusive="left")
    q = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="15min", tz=TZ, inclusive="left")

    def shape(idx):
        hour = np.asarray(idx.hour + idx.minute / 60)
        doy = np.asarray(idx.day_of_year)
        return hour, doy

    # 15-min day-ahead price EUR/MWh
    hour, doy = shape(q)
    season = 15 * np.cos(2 * np.pi * (doy - 15) / 365)
    daily = 25 * np.sin(np.pi * (hour - 6) / 12) * (hour > 6) * (hour < 22) - 20 * np.exp(-((hour - 13) ** 2) / 6)
    da = 80 + season + daily + rng.normal(0, 12, len(q))
    pd.DataFrame({"da_price": da.round(2)}, index=q).rename_axis("time").to_csv(OUT / "prices_15min.csv")

    # hourly data
    hour, doy = shape(h)
    t_amb = (8 - 9 * np.cos(2 * np.pi * (doy - 15) / 365) + 4 * np.sin(np.pi * (hour - 9) / 12)
             + rng.normal(0, 1.5, len(h)))
    sun = np.clip(np.sin(np.pi * (hour - 5) / 15), 0, None) * (0.55 + 0.45 * np.sin(2 * np.pi * (doy - 80) / 365))
    pv_cf = np.clip(sun * rng.uniform(0.4, 1.0, len(h)), 0, 1)
    net_fee = np.where((hour >= 17) & (hour < 21), 60.0, np.where((hour >= 6) & (hour < 17), 25.0, 10.0))
    grid_co2 = np.clip(0.18 - 0.08 * sun + rng.normal(0, 0.02, len(h)), 0.02, None)
    workday = np.asarray(h.day_of_week) < 5
    shift = (hour >= 6) & (hour < 22)
    heat_load = 1.6 + 0.8 * workday * shift + 0.03 * np.clip(15 - t_amb, 0, None) + rng.normal(0, 0.05, len(h))
    space_heat = 0.04 * np.clip(16 - t_amb, 0, None)
    el_load = 0.6 + 0.5 * workday * shift + rng.normal(0, 0.03, len(h))
    cop90 = np.clip(2.4 + 0.02 * t_amb, 2.0, 3.0)
    df = pd.DataFrame({
        "t_amb": t_amb.round(2), "pv_cf": pv_cf.round(4), "net_fee": net_fee, "grid_co2": grid_co2.round(4),
        "heat_load": heat_load.round(4), "space_heat": space_heat.round(4), "el_load": el_load.round(4),
        "cop90": cop90.round(3), "gas_price": 40.0,
    }, index=h).rename_axis("time")
    df.to_csv(OUT / "site_hourly.csv")
    print(f"wrote {OUT}/prices_15min.csv ({len(q)} rows) and site_hourly.csv ({len(h)} rows)")


if __name__ == "__main__":
    main()
