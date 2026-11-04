"""Input data of the in-app tutorial (same site as examples/00_tutorial.py)."""

from __future__ import annotations

import numpy as np
import pandas as pd

NAME = "tutorial_data"
START, DAYS, FREQ, TZ = "2025-06-02", 2, "1h", "Europe/Copenhagen"


def tutorial_frame() -> pd.DataFrame:
    """Two days, hourly: spot price (EUR/MWh), PV capacity factor (0..1) and process-heat load (MW)."""
    idx = pd.date_range(START, periods=24 * DAYS, freq=FREQ, tz=TZ, name="time")
    hour = idx.hour
    return pd.DataFrame({
        "price": np.where((hour >= 7) & (hour < 21), 120.0, 60.0),
        "pv_cf": np.clip(np.sin((hour - 6) / 14 * np.pi), 0, None).round(4),
        "heat_load": np.where((hour >= 6) & (hour < 18), 0.8, 0.3),
    }, index=idx)


def tutorial_csv() -> bytes:
    return tutorial_frame().to_csv().encode()
