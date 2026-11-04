"""Integration: config round-trip, command line and the shipped examples."""

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

import oems
from oems import presets as P
from oems.__main__ import main as cli
from oems.config import from_config, save_config, to_config

ROOT = Path(__file__).resolve().parents[2]


def _system():
    g = oems.TimeGrid(start="2025-01-01", periods=48, freq="1h", tz="Europe/Copenhagen")
    idx = g.index
    price = pd.Series([40.0 + (i % 24) * 3 for i in range(48)], idx)
    cop = pd.Series([2.5 + 0.01 * i for i in range(48)], idx)
    es = oems.EnergySystem(g, name="rt")
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity", emission_factor={"co2": 0.1}))
    heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
    es.add(oems.GridConnection("dso", el, import_price=price, export_price=price, max_export=1,
                               tariffs=[oems.PeakCharge(100, period="D", measurement="1h")]),
           P.heat_pump("hp", el, heat, cop=cop, p_th_max=2, flow_costs={"in_el": oems.FlowCost(-5, "TAX")}),
           P.hot_water_tank("tes", heat, e_max=4, p_max=2, loss_per_h=0.01),
           P.battery("bess", el, e_max=1, p_max=0.5),
           oems.FlexibleDemand("proc", heat, baseline=1.0, p_max=2, window="D", max_deferred=2),
           oems.Demand("base", el, profile=0.3))
    es.set_emission_policy("co2", price=80)
    return es


def test_config_roundtrip_in_memory():
    """to_config -> from_config gives the same optimum (series passed as in-memory frames)."""
    es = _system()
    ref = es.solve().objective
    cfg, series = to_config(es)
    es2, _ = from_config(cfg, frames={"inputs": pd.DataFrame(series)})
    assert es2.solve().objective == pytest.approx(ref, rel=1e-9)


def test_config_roundtrip_via_files(tmp_path):
    """save_config writes system.yaml + inputs.csv; loading the folder reproduces the optimum."""
    es = _system()
    ref = es.solve().objective
    path = save_config(es, tmp_path)
    assert (tmp_path / "inputs.csv").exists()
    es2, _ = from_config(path)
    assert es2.solve().objective == pytest.approx(ref, rel=1e-9)


def test_config_errors_are_reported(tmp_path):
    """An unknown component type and an unknown parameter are both reported in one ValidationError."""
    cfg = {"time": {"start": "2025-01-01", "periods": 2, "freq": "1h"},
           "carriers": [{"name": "el", "kind": "electricity"}], "buses": [{"name": "el", "carrier": "el"}],
           "components": [{"type": "Nope", "name": "x"}, {"type": "Demand", "name": "d", "bus": "el", "foo": 1}]}
    with pytest.raises(oems.ValidationError) as exc:
        from_config(cfg)
    msg = str(exc.value)
    assert "unknown component type 'Nope'" in msg and "unknown parameter 'foo'" in msg


def test_cli_run(tmp_path, capsys):
    """`python -m oems run system.yaml` solves the config and writes the CSV results folder."""
    path = save_config(_system(), tmp_path)
    assert cli(["run", str(path), "--out", str(tmp_path / "res")]) == 0
    assert (tmp_path / "res" / "flows.csv").exists()
    assert "status=optimal" in capsys.readouterr().out


def test_cli_resolution_override(tmp_path):
    """--freq 15min solves the same 48 h horizon at 15-min resolution (192 steps)."""
    path = save_config(_system(), tmp_path)
    assert cli(["run", str(path), "--freq", "15min", "--out", str(tmp_path / "r15")]) == 0
    meta = yaml.safe_load((tmp_path / "r15" / "metadata.json").read_text())
    assert meta["steps"] == 192 and meta["resolution"] == "15min"


def test_electrification_example_runs():
    """The shipped example solves for 7 days and the electrified variant has higher heat electrification."""
    r = subprocess.run([sys.executable, "02_site_electrification.py", "--days", "7", "--out", "../out/test"],
                       cwd=ROOT / "examples", capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert "=== electrified ===" in r.stdout


def test_tutorial_costs_match_readme():
    """The README tutorial quotes total costs after each step (3240, 645, 516, 1481 EUR); the script reproduces
    them. Step 2 is checkable by hand: 14 h x 2 d x 0.5 MW x 160 + 10 h x 2 d x 0.5 MW x 100 = 3240 EUR."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[2] / "examples" / "00_tutorial.py"
    spec = importlib.util.spec_from_file_location("tutorial", path)
    tutorial = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tutorial)
    expected = {2: 3240, 3: 645, 4: 516, 5: 1481}
    for step, cost in expected.items():
        assert tutorial.site(step).solve().total_cost == pytest.approx(cost, abs=0.5)
