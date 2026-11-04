"""Unit tests for input validation, topology checks and warnings (EnergySystem.validate)."""

import pytest
from helpers import el_system, grid_h, heat_el_system

import oems
from oems.components import REGISTRY


def test_required_parameter_missing():
    """A Storage without e_max is rejected with a message naming the field."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el), oems.Storage("bess", el, p_ch_max=1, p_dis_max=1, e_max=None))
    with pytest.raises(oems.ValidationError, match="bess.e_max"):
        es.validate()


def test_range_check():
    """Efficiencies above 1 are rejected."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el), oems.Storage("bess", el, e_max=1, p_ch_max=1, p_dis_max=1, eta_ch=1.2))
    with pytest.raises(oems.ValidationError, match="eta_ch"):
        es.validate()


def test_unknown_bus():
    """Referencing a bus that does not exist is an error."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el), oems.Demand("d", "nope", profile=1))
    with pytest.raises(oems.ValidationError, match="unknown bus"):
        es.validate()


def test_carrier_kind_mismatch():
    """A ThermalZone cold port must connect to a bus of carrier kind 'cold'."""
    es, el, heat = heat_el_system(grid_h(2))
    es.add(oems.GridConnection("g", heat),
           oems.ThermalZone("z", cold_bus=heat, C=1, R=10, theta_amb=0, theta_min=0, theta_max=1, theta_init=0))
    with pytest.raises(oems.ValidationError, match="carrier kind 'cold'"):
        es.validate()


def test_duplicate_and_invalid_names():
    """Duplicate component names and names containing '.' are rejected."""
    es, el = el_system(grid_h(2))
    es.add(oems.Demand("d", el, profile=1))
    with pytest.raises(oems.ValidationError):
        es.add(oems.Demand("d", el, profile=1))
    with pytest.raises(oems.ValidationError):
        es.add(oems.Demand("a.b", el, profile=1))


def test_converter_same_bus_in_and_out():
    """A converter cannot use the same bus as input and output."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el), oems.Converter("c", {el: 1}, {el: 1}))
    with pytest.raises(oems.ValidationError, match="both input and output"):
        es.validate()


def test_converter_port_names():
    """Converter ports are named in_<bus> / out_<bus>."""
    es, el, heat = heat_el_system(grid_h(2))
    hp = oems.presets.heat_pump("hp", el, heat, cop=3, p_th_max=1)
    es.add(oems.GridConnection("g", el), hp, oems.Demand("d", heat, profile=0.5))
    es.validate()
    assert set(hp.ports()) == {"in_el", "out_heat"}


def test_bus_without_supply_is_an_error():
    """A bus with demand but no supplying component cannot balance."""
    es, el = el_system(grid_h(2))
    es.add(oems.Demand("d", el, profile=1))
    with pytest.raises(oems.ValidationError, match="no supply"):
        es.validate()


def test_flow_cost_on_unknown_port():
    """flow_costs on a port the component does not have is an error listing the valid ports."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el, flow_costs={"bogus": 1}), oems.Demand("d", el, profile=1))
    with pytest.raises(oems.ValidationError, match="unknown port"):
        es.validate()


def test_exclusive_requires_finite_limits():
    """exclusive=True on a grid connection needs finite import/export limits."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el, exclusive=True), oems.Demand("d", el, profile=1))
    with pytest.raises(oems.ValidationError, match="finite"):
        es.validate()


def test_soc_init_outside_bounds():
    """soc_init below soc_min is rejected."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el),
           oems.Storage("s", el, e_max=1, p_ch_max=1, p_dis_max=1, soc_min=0.2, soc_init=0.1))
    with pytest.raises(oems.ValidationError, match="soc_init"):
        es.validate()


def test_thermal_zone_stability_check():
    """dt >= R*C is an error (explicit Euler unstable); dt > 0.2*R*C is a warning."""
    es, el, heat = heat_el_system(grid_h(2))
    kw = dict(theta_amb=0, theta_min=0, theta_max=30, theta_init=20)
    es.add(oems.GridConnection("g", heat), oems.ThermalZone("z", heat_bus=heat, C=0.5, R=1, **kw))
    with pytest.raises(oems.ValidationError, match="unstable"):
        es.validate()
    es2, el2, heat2 = heat_el_system(grid_h(2))
    es2.add(oems.GridConnection("g", heat2), oems.ThermalZone("z", heat_bus=heat2, C=1, R=3, **kw))
    assert any("20%" in w for w in es2.validate().warnings)


def test_peak_charge_requires_unit_weights():
    """Peak charges with non-unit time weights (representative periods) are rejected."""
    es, el = el_system(grid_h(2, weights=[1, 2]))
    es.add(oems.GridConnection("g", el, peak_charge=10), oems.Demand("d", el, profile=1))
    with pytest.raises(oems.ValidationError, match="unit time weights"):
        es.validate()


def test_arbitrage_warning():
    """Export credit above import cost without exclusive=True triggers a warning."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("g", el, import_price=10, export_price=50, max_import=1, max_export=1),
           oems.Demand("d", el, profile=0.5))
    assert any("arbitrage" in w for w in es.validate().warnings)


def test_emission_double_count_warning():
    """A converter factor on a carrier that already has a boundary factor is flagged."""
    g = grid_h(2)
    es = oems.EnergySystem(g)
    gas = es.add_bus("gas", oems.Carrier("gas", kind="fuel", emission_factor={"co2": 0.2}))
    heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
    es.add(oems.GridConnection("gas_grid", gas, import_price=30),
           oems.presets.gas_boiler("gb", gas, heat, p_th_max=2, emission_factors={"in_gas": {"co2": 0.2}}),
           oems.Demand("d", heat, profile=1))
    assert any("counted twice" in w for w in es.validate().warnings)


def test_registry_contains_all_components():
    """All component types are registered (available to configs and the UI palette)."""
    assert {"GridConnection", "Producer", "Demand", "FlexibleDemand", "Sink", "Storage", "Converter",
            "ThermalZone"} <= set(REGISTRY)


def test_no_grid_given():
    """Validating a system without a TimeGrid raises a clear error."""
    es = oems.EnergySystem()
    with pytest.raises(oems.ValidationError, match="TimeGrid"):
        es.validate()
