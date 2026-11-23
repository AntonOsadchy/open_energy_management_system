"""Validation: one-shot lifetime capacity optimisation (oems.sizing.optimise_lifetime) against hand-computed cases."""

import copy

import pytest

from oems.config import from_config
from oems.exceptions import InfeasibleModelError, ValidationError
from oems.sizing import hurdle_value, hurdle_weights, optimise_lifetime

#: A 4-hour base period is scaled to a year: 365 days / (4 h / 24) = 2190 base periods per year.
SCALE = 2190


def _cfg(years=2, rate=0.0, store=None, pv=None, inflation=0.0):
    """Prices alternate 10 / 100 EUR/MWh, 1 MW demand, no export. A battery of E <= 1 MWh (efficiency 1, starting
    empty) charges E twice at 10 and discharges it twice at 100: it saves 2 * 90 * E per base period, i.e.
    180 * 2190 = 394,200 EUR per MWh and year; more than 1 MWh saves nothing more (the demand is 1 MW)."""
    comps = [{"type": "GridConnection", "name": "grid", "bus": "el", "import_price": [10, 100, 10, 100],
              "max_export": 0},
             {"type": "Demand", "name": "load", "bus": "el", "profile": 1}]
    if store is not None:
        comps.append({"type": "Storage", "name": "bess", "bus": "el", "eta_ch": 1, "eta_dis": 1, "soc_init": 0,
                      "optimise_size": True, "e_size_max": 5, "capex_power": 0, "opex": 0, **store})
    if pv is not None:
        comps.append({"type": "Producer", "name": "pv", "bus": "el", "cf": [0, 0.5, 0, 0.5],
                      "optimise_size": True, "opex": 0, **pv})
    return {"time": {"start": "2025-01-01", "periods": 4, "freq": "1h"},
            "carriers": [{"name": "electricity", "kind": "electricity"}],
            "buses": [{"name": "el", "carrier": "electricity"}], "components": comps,
            "lifetime": {"mode": "optimise", "years": years, "discount_rate": rate, "inflation": inflation}}


def _size(sr, comp, quantity):
    return next(s["value"] for s in sr.sizes if s["component"] == comp and s["quantity"] == quantity)


def test_battery_is_built_when_lifetime_savings_beat_the_capex():
    """2 years undiscounted: savings 2 * 394,200 = 788,400 EUR per MWh (up to 1 MWh). CAPEX 700,000 -> 1 MWh,
    NPV 88,400."""
    sr = optimise_lifetime(_cfg(store={"capex_energy": 700_000}))
    assert _size(sr, "bess", "energy") == pytest.approx(1, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(788_400 - 700_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(sr.kpis["npv_check"], rel=1e-9)
    t = sr.table.set_index("year")
    assert list(t["savings"]) == pytest.approx([0, 394_200, 394_200], rel=1e-6)
    assert list(t["net_cash_flow"]) == pytest.approx([-700_000, 394_200, 394_200], rel=1e-6)
    assert sr.kpis["payback"] == pytest.approx(700_000 / 394_200, rel=1e-6)


def test_battery_is_not_built_when_capex_exceeds_lifetime_savings():
    sr = optimise_lifetime(_cfg(store={"capex_energy": 900_000}))
    assert _size(sr, "bess", "energy") == pytest.approx(0, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(0, abs=1e-3)
    assert sr.kpis["irr"] is None and sr.kpis["payback"] is None


def test_discounting_and_opex_move_the_break_even():
    """5 % over 2 years: annuity 1/1.05 + 1/1.05^2 = 1.8594. Savings - OPEX per MWh and year = 394,200 - 50,000;
    break-even CAPEX = 344,200 * 1.8594 = 640,016 EUR/MWh."""
    annuity = 1 / 1.05 + 1 / 1.05**2
    breakeven = (394_200 - 50_000) * annuity
    built = optimise_lifetime(_cfg(rate=0.05, store={"capex_energy": breakeven - 1000, "opex": 50_000}))
    idle = optimise_lifetime(_cfg(rate=0.05, store={"capex_energy": breakeven + 1000, "opex": 50_000}))
    assert _size(built, "bess", "energy") == pytest.approx(1, abs=1e-6)
    assert _size(idle, "bess", "energy") == pytest.approx(0, abs=1e-6)
    assert built.kpis["npv"] == pytest.approx(1000, rel=1e-3)
    assert built.kpis["irr"] == pytest.approx(0.05, abs=2e-3)       # NPV ~ 0 at the discount rate
    t = built.table.set_index("year")
    assert t.loc[2, "discount_factor"] == pytest.approx(1 / 1.05**2)
    assert list(t["opex"]) == pytest.approx([0, 50_000, 50_000], rel=1e-6)


def test_inflation_raises_savings_and_opex_every_year_but_not_the_capex():
    """10 % inflation, undiscounted: savings 394,200 in year 1 and 433,620 in year 2, OPEX 50,000 then 55,000."""
    sr = optimise_lifetime(_cfg(store={"capex_energy": 700_000, "opex": 50_000}, inflation=0.1))
    t = sr.table.set_index("year")
    assert list(t["price_index"]) == pytest.approx([1, 1, 1.1])
    assert list(t["savings"]) == pytest.approx([0, 394_200, 433_620], rel=1e-6)
    assert list(t["opex"]) == pytest.approx([0, 50_000, 55_000], rel=1e-6)
    assert t.loc[0, "capex"] == pytest.approx(700_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(344_200 + 378_620 - 700_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(sr.kpis["npv_check"], rel=1e-9)
    assert sr.kpis["inflation"] == 0.1


def test_inflation_moves_the_break_even_in_the_optimisation():
    """5 % discount, 3 % inflation over 2 years: present value of a base-year EUR per year 1/1.05 + 1.03/1.05^2;
    a CAPEX just under 344,200 * that is built, just over it not (without inflation it would not be built)."""
    annuity = 1 / 1.05 + 1.03 / 1.05**2
    breakeven = (394_200 - 50_000) * annuity
    built = optimise_lifetime(_cfg(rate=0.05, store={"capex_energy": breakeven - 1000, "opex": 50_000}, inflation=0.03))
    idle = optimise_lifetime(_cfg(rate=0.05, store={"capex_energy": breakeven + 1000, "opex": 50_000}, inflation=0.03))
    flat = optimise_lifetime(_cfg(rate=0.05, store={"capex_energy": breakeven - 1000, "opex": 50_000}))
    assert _size(built, "bess", "energy") == pytest.approx(1, abs=1e-6)
    assert _size(idle, "bess", "energy") == pytest.approx(0, abs=1e-6)
    assert _size(flat, "bess", "energy") == pytest.approx(0, abs=1e-6)
    assert built.kpis["npv"] == pytest.approx(1000, rel=1e-3)


def test_inflation_input_error():
    with pytest.raises(ValidationError):
        optimise_lifetime(_cfg(store={"capex_energy": 1}, inflation=2))


def test_power_capex_and_fixed_duration():
    """Power costs 100,000 EUR/MW and E = 2 h x P: 1 MWh needs 0.5 MW; the battery then charges 0.5 MW for an hour
    and can only fill half of it, so a 1 MWh / 0.5 MW battery saves 2 * 90 * 0.5 per base period."""
    sr = optimise_lifetime(_cfg(store={"capex_energy": 10_000, "capex_power": 100_000, "duration": 2}))
    e, p = _size(sr, "bess", "energy"), _size(sr, "bess", "power")
    assert e == pytest.approx(2 * p, rel=1e-6)
    assert p >= 0.5 - 1e-6 and sr.kpis["npv"] > 0


def test_pv_is_sized_to_cover_the_expensive_hours_and_stops_at_its_maximum():
    """PV cf 0.5 in the two 100-EUR hours: 2 MW covers the 1 MW demand there (saving 2 * 100 per MW of demand and
    base period); a limit of 1 MW binds and is reported."""
    sr = optimise_lifetime(_cfg(pv={"capex": 100_000, "capacity_max": 3}))
    assert _size(sr, "pv", "capacity") == pytest.approx(2, abs=1e-6)
    capped = optimise_lifetime(_cfg(pv={"capex": 100_000, "capacity_max": 1}))
    assert _size(capped, "pv", "capacity") == pytest.approx(1, abs=1e-6)
    assert any("maximum" in w for w in capped.warnings)
    dear = optimise_lifetime(_cfg(pv={"capex": 10_000_000, "capacity_max": 3}))
    assert _size(dear, "pv", "capacity") == pytest.approx(0, abs=1e-6)


def test_reference_case_keeps_a_minimum_size_and_counts_only_the_increment():
    """A minimum of 0.5 MWh / 0.5 MW (what exists already) is in both cases: the investment is the extra 0.5 MWh."""
    sr = optimise_lifetime(_cfg(store={"capex_energy": 700_000, "e_size_min": 0.5, "p_size_min": 0.5}))
    assert _size(sr, "bess", "energy") == pytest.approx(1, abs=1e-6)
    assert sr.kpis["capex"] == pytest.approx(0.5 * 700_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(0.5 * (788_400 - 700_000), rel=1e-6)


def test_a_baseline_system_is_the_reference_of_the_cash_flows():
    """lifetime.reference: another system (here the site with a 0.5 MWh battery it already has) is the case without
    the investment, solved on the sized model's horizon (its own time section is replaced). The sizes do not change
    (1 MWh, 700,000 EUR); the savings are measured against the baseline: 394,200 - 0.5 * 394,200 = 197,100 EUR a
    year, so NPV = 2 * 197,100 - 700,000."""
    base = _cfg()
    base.pop("lifetime")
    base["time"] = {"start": "2024-06-01", "periods": 8, "freq": "1h"}
    base["components"].append({"type": "Storage", "name": "old", "bus": "el", "e_max": 0.5, "p_ch_max": 0.5,
                               "p_dis_max": 0.5, "eta_ch": 1, "eta_dis": 1, "soc_init": 0})
    cfg = _cfg(store={"capex_energy": 700_000})
    cfg["lifetime"].update(reference=base, reference_name="site today")
    sr = optimise_lifetime(cfg)
    assert _size(sr, "bess", "energy") == pytest.approx(1, abs=1e-6)
    t = sr.table.set_index("year")
    assert list(t["savings"]) == pytest.approx([0, 197_100, 197_100], rel=1e-6)
    assert sr.kpis["capex"] == pytest.approx(700_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(2 * 197_100 - 700_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(sr.kpis["npv_check"], rel=1e-9)
    assert sr.kpis["reference"] == "site today"
    assert len(sr.reference.grid) == len(sr.results.grid)          # on the sized model's horizon
    # the baseline of the same site without the battery gives the default reference case
    plain = _cfg(store={"capex_energy": 700_000})
    base0 = copy.deepcopy(plain)
    base0["components"] = base0["components"][:2]
    plain_sr = optimise_lifetime(plain)
    plain["lifetime"]["reference"] = base0
    assert optimise_lifetime(plain).kpis["npv"] == pytest.approx(plain_sr.kpis["npv"], rel=1e-9)


def test_capex_and_opex_are_required_when_the_size_is_optimised():
    cfg = _cfg(store={"capex_energy": 700_000})
    del cfg["components"][2]["opex"]
    with pytest.raises(ValidationError, match="OPEX required"):
        optimise_lifetime(cfg)
    cfg = _cfg(pv={"capacity_max": 1})
    with pytest.raises(ValidationError, match="CAPEX"):
        optimise_lifetime(cfg)


def test_an_optimised_size_needs_a_lifetime_run():
    cfg = _cfg(store={"capex_energy": 700_000})
    del cfg["lifetime"]
    es, _ = from_config(cfg)
    with pytest.raises(ValidationError, match="lifetime optimisation"):
        es.solve()


def test_a_fixed_size_storage_still_requires_its_size():
    cfg = copy.deepcopy(_cfg(store={"capex_energy": 1}))
    store = cfg["components"][2]
    store["optimise_size"] = False
    del cfg["lifetime"]
    es, _ = from_config(cfg)
    with pytest.raises(ValidationError, match="e_max: is required"):
        es.solve()


def test_peak_charges_are_discounted_per_year():
    """A 1-day base period stands for 365 days. A daily peak charge of 10 EUR/MW on a flat 1 MW import: every
    modelled day stands for 365 billed days of its year, so the present value is 10 * 365 * (1/1.1 + 1/1.1^2)."""
    cfg = _cfg(years=2, rate=0.1, pv={"capex": 1e9, "capacity_max": 0})
    cfg["time"]["periods"] = 24
    cfg["components"][0]["import_price"] = 50
    cfg["components"][0]["tariffs"] = [{"type": "PeakCharge", "rate": 10, "period": "D"}]
    cfg["components"][2]["cf"] = 0.5
    sr = optimise_lifetime(cfg)
    assert sr.results.costs.loc["grid", "PEAK"] == pytest.approx(10 * 365 * (1 / 1.1 + 1 / 1.1**2), rel=1e-6)
    assert sr.results.costs.loc["grid", "ENERGY"] == pytest.approx(50 * 24 * 365 * (1 / 1.1 + 1 / 1.1**2), rel=1e-6)


def test_the_blocks_own_size_fields_are_ignored_when_the_size_is_optimised():
    """A battery block keeps e_max / p_ch_max / p_dis_max of 0.25 from before it was optimised: they must not cap the
    optimised size (only e_size_* / p_size_* do), so it still reaches 1 MWh / 1 MW as without them."""
    sr = optimise_lifetime(_cfg(store={"capex_energy": 700_000, "e_max": 0.25, "p_ch_max": 0.25, "p_dis_max": 0.25}))
    assert _size(sr, "bess", "energy") == pytest.approx(1, abs=1e-6)
    assert _size(sr, "bess", "power") >= 1 - 1e-6


# ------------------------------------------------------------------ investment hurdles
def _portfolio(inflation=0.0, **hurdles):
    """A 0.5 MWh battery (very profitable) and PV of up to 5 MW (less so) over 4 years at 5 %: unconstrained both
    are built, IRR 46.6 %, payback 1.68 years. A hurdle above that shrinks the PV, the asset with the lower return."""
    cfg = _cfg(years=4, rate=0.05, store={"capex_energy": 400_000, "e_size_max": 0.5},
               pv={"capex": 500_000, "capacity_max": 5}, inflation=inflation)
    if hurdles:
        cfg["lifetime"]["hurdles"] = hurdles
    return optimise_lifetime(cfg)


def test_hurdle_weights():
    sr = _portfolio()
    hz = sr.horizon
    assert hurdle_weights("irr", 0.1, hz) == pytest.approx([1.1**-y for y in (1, 2, 3, 4)])
    assert hurdle_weights("payback", 2.25, hz) == pytest.approx([1, 1, 0.25, 0])
    assert hurdle_weights("payback_discounted", 2.25, hz) == pytest.approx([1.05**-1, 1.05**-2, 0.25 * 1.05**-3, 0])
    assert hurdle_weights("payback", 9, hz) == pytest.approx([1, 1, 1, 1])


def test_hurdle_value_is_the_weighted_cash_flow_of_the_investment():
    """reference - sized, weighted by W, is -CAPEX + sum(W * net cash flow) of the year table, e.g. the NPV at 10 %
    for the IRR weights: for the 1 MWh battery -700,000 + 394,200 * (1/1.1 + 1/1.1^2)."""
    sr = optimise_lifetime(_cfg(store={"capex_energy": 700_000}))
    w = hurdle_weights("irr", 0.1, sr.horizon)
    weighted = hurdle_value(sr.reference, w) - hurdle_value(sr.results, w)
    assert weighted == pytest.approx(-700_000 + 394_200 * (1 / 1.1 + 1 / 1.1**2), rel=1e-6)
    net = sr.table["net_cash_flow"].to_numpy()
    assert weighted == pytest.approx(net[0] + w @ net[1:], rel=1e-6)


def test_a_minimum_irr_above_the_optimum_shrinks_the_investment_to_meet_it():
    free = _portfolio()
    sr = _portfolio(min_irr=0.6)
    assert free.kpis["irr"] == pytest.approx(0.4656, abs=1e-3)
    assert sr.kpis["irr"] == pytest.approx(0.6, abs=1e-4)
    assert _size(sr, "bess", "energy") == pytest.approx(0.5, abs=1e-6)          # the better asset stays
    assert 0 < _size(sr, "pv", "capacity") < _size(free, "pv", "capacity")
    assert 0 < sr.kpis["npv"] < free.kpis["npv"]
    (h,) = sr.kpis["hurdles"]
    assert h["kind"] == "irr" and h["met"] and h["binding"] and h["achieved"] == sr.kpis["irr"]


@pytest.mark.parametrize("hurdles,key,target", [({"min_irr": 0.6}, "irr", 0.6),
                                                ({"max_payback": 1.5, "payback": "discounted"}, "payback_discounted", 1.5)])
def test_hurdles_are_met_on_the_nominal_cash_flows_with_inflation(hurdles, key, target):
    """With 4 % inflation the cash flows grow every year; the hurdle constraint weighs them as the year table does."""
    sr = _portfolio(inflation=0.04, **hurdles)
    assert sr.kpis[key] == pytest.approx(target, abs=1e-4)
    assert sr.kpis["hurdles"][0]["binding"]


def test_a_hurdle_already_met_changes_nothing():
    free = _portfolio()
    sr = _portfolio(min_irr=0.4, max_payback=3)
    assert sr.kpis["npv"] == pytest.approx(free.kpis["npv"], rel=1e-6)
    assert [s["value"] for s in sr.sizes] == pytest.approx([s["value"] for s in free.sizes], abs=1e-6)
    assert all(h["met"] and not h["binding"] for h in sr.kpis["hurdles"])


@pytest.mark.parametrize("kind,target", [("simple", 1.2), ("discounted", 1.5)])
def test_a_maximum_payback_is_met_exactly_when_binding(kind, target):
    free = _portfolio()
    sr = _portfolio(max_payback=target, payback=kind)
    key = "payback" if kind == "simple" else "payback_discounted"
    assert free.kpis[key] > target
    assert sr.kpis[key] == pytest.approx(target, abs=1e-4)
    assert sr.kpis["npv"] < free.kpis["npv"] and sr.kpis["hurdles"][0]["binding"]


def test_an_impossible_hurdle_means_no_investment_not_an_infeasible_model():
    sr = _portfolio(min_irr=5)
    assert all(s["value"] == pytest.approx(0, abs=1e-6) for s in sr.sizes)
    assert sr.kpis["npv"] == pytest.approx(0, abs=1)                 # EUR: solver noise on a site cost of millions
    assert sr.kpis["irr"] is None and sr.kpis["payback"] is None
    assert any("no investment meets the hurdles" in w for w in sr.warnings)


def test_hurdle_input_errors():
    for bad in ({"min_irr": -2}, {"max_payback": 0}, {"max_payback": 3, "payback": "monthly"}):
        with pytest.raises(ValidationError):
            _portfolio(**bad)


# ------------------------------------------------------------------ one weighted year = the full chronology
def _week(model, hurdles=None, cap=None, inflation=0.0):
    """A week of hourly prices with a daily spread and noise, a PV and a battery to size, a daily peak charge and
    a CO2 price: everything the lifetime model weighs per year. (A monthly peak would differ: stacked weeks share a
    month in the full model - an artefact of stacking a short base period, not of the weighting.)"""
    import numpy as np

    rng = np.random.default_rng(3)
    h = np.arange(168)
    price = (60 + 40 * np.sin(2 * np.pi * (h - 6) / 24) + rng.normal(0, 8, 168)).round(2).tolist()
    load = (1 + 0.4 * np.sin(2 * np.pi * (h - 14) / 24)).round(3).tolist()
    cf = np.clip(np.sin(2 * np.pi * (h % 24 - 6) / 24), 0, None).round(3).tolist()
    cfg = {"time": {"start": "2025-03-03", "periods": 168, "freq": "1h"},
           "carriers": [{"name": "electricity", "kind": "electricity", "emission_factor": {"co2": 0.3}}],
           "buses": [{"name": "el", "carrier": "electricity"}],
           "components": [
               {"type": "GridConnection", "name": "grid", "bus": "el", "import_price": price, "export_price": 0,
                "max_export": 1, "tariffs": [{"type": "PeakCharge", "rate": 150, "period": "D"}]},
               {"type": "Demand", "name": "load", "bus": "el", "profile": load},
               {"type": "Producer", "name": "pv", "bus": "el", "cf": cf, "optimise_size": True, "capacity_max": 3,
                "capex": 450_000, "opex": 8_000},
               {"type": "Storage", "name": "bess", "bus": "el", "soc_init": 0.5, "optimise_size": True,
                "e_size_max": 4, "p_size_max": 2, "capex_energy": 160_000, "capex_power": 60_000, "opex": 3_000}],
           "emission_policy": {"co2": {"price": 50, **({"cap": cap} if cap else {})}},
           "lifetime": {"mode": "optimise", "years": 6, "discount_rate": 0.07, "model": model,
                        "inflation": inflation}}
    if hurdles:
        cfg["lifetime"]["hurdles"] = hurdles
    return optimise_lifetime(cfg)


@pytest.mark.parametrize("hurdles,inflation", [(None, 0.0), ({"min_irr": 0.15}, 0.0), (None, 0.03)])
def test_one_weighted_year_gives_the_same_investment_as_the_full_chronology(hurdles, inflation):
    """6 years stacked (1008 steps) vs the week once, weighted (168 steps): same sizes, and NPV, IRR, payback and
    yearly cash flows within 1 % (also with inflation). They differ only by the storage level at the year boundaries (stacked: carried
    over; weighted: each year ends at its start level) - here 0.3 % of the NPV with a week as the base period, ~50x
    less with a calendar year."""
    full, fast = _week("full", hurdles, inflation=inflation), _week("weighted", hurdles, inflation=inflation)
    assert fast.horizon.n * 6 == full.horizon.n
    # with inflation the years weigh differently, so the year-boundary difference moves the (flat) optimum ~1 %
    assert [s["value"] for s in fast.sizes] == pytest.approx([s["value"] for s in full.sizes],
                                                             rel=2e-2 if inflation else 1e-4, abs=1e-5)
    assert any(s["value"] > 0.05 for s in fast.sizes)
    for k in ("npv", "irr", "payback", "payback_discounted", "capex", "savings_year1"):
        assert fast.kpis[k] == pytest.approx(full.kpis[k], rel=1e-2), k
    for col in ("savings", "net_cash_flow", "discounted_cumulative"):
        assert fast.table[col].to_numpy()[1:] == pytest.approx(full.table[col].to_numpy()[1:],
                                                               abs=1e-2 * full.kpis["capex"]), col
    assert fast.table["capex"].iloc[0] == pytest.approx(full.table["capex"].iloc[0], rel=1e-2 if inflation else 1e-4)
    assert fast.results.total_cost == pytest.approx(full.results.total_cost, rel=1e-3)
    if hurdles:
        assert fast.kpis["hurdles"][0]["binding"] == full.kpis["hurdles"][0]["binding"]


def test_emission_cap_counts_every_year_in_the_weighted_model():
    """The CO2 cap is on the whole run: the weighted week stands for 6 years of emissions (~208 t with the
    investment, ~302 t without), as the stacked one does - counted once it would be a sixth."""
    full, fast = _week("full", cap=305), _week("weighted", cap=305)
    for a, b in ((full.results, fast.results), (full.reference, fast.reference)):
        e_full, e_fast = a.emissions.sum().sum(), b.emissions.sum().sum()
        assert e_fast == pytest.approx(e_full, rel=1e-3) and 200 < e_fast <= 305 + 1e-6


# ------------------------------------------------------------------ fixed state of health
def test_fixed_soh_shrinks_the_usable_energy_not_the_power():
    """A 1 MWh battery at 80 % SoH stores 0.8 MWh: it moves 2 x 0.8 MWh from 10 to 100 EUR/MWh per base period
    (savings 2 x 90 x 0.8 = 144 instead of 180), and its SoC still reaches 100 % (of the usable energy)."""
    def run(soh):
        cfg = _cfg(store={"capex_energy": 1})
        del cfg["lifetime"]
        cfg["components"][2].update(optimise_size=False, e_max=1, p_ch_max=1, p_dis_max=1, soh=soh)
        es, _ = from_config(cfg)
        return es.solve()

    new, aged = run(None), run(0.8)
    assert aged.total_cost - new.total_cost == pytest.approx(2 * 90 * 0.2, abs=1e-3)
    soc = aged.states.xs(("bess", "soc"), level=("component", "state"), axis=1).iloc[:, 0]
    assert soc.max() == pytest.approx(1.0, abs=1e-6)


def test_fixed_soh_in_the_sizing_pays_capex_on_the_nominal_energy():
    """At 50 % SoH a nominal MWh stores half a MWh: the useful battery (1 MWh stored) is 2 MWh nominal and saves
    394,200 EUR per year and usable MWh; CAPEX 300,000 per nominal MWh -> NPV 2 x 394,200 - 2 x 300,000."""
    sr = optimise_lifetime(_cfg(store={"capex_energy": 300_000, "soh": 0.5}))
    assert _size(sr, "bess", "energy") == pytest.approx(2, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(2 * 394_200 - 2 * 300_000, rel=1e-6)


def test_soh_curves_or_a_fixed_soh_not_both():
    cfg = _cfg(store={"capex_energy": 1, "soh": 0.9, "degradation": {"curves": {"1": [0.95, 0.9]}}})
    with pytest.raises(ValidationError, match="either SoH curves or a fixed SoH"):
        optimise_lifetime(cfg)
    with pytest.raises(ValidationError, match="above 0"):
        optimise_lifetime(_cfg(store={"capex_energy": 1, "soh": 0}))


# ------------------------------------------------------------------ minimum self-sufficiency
def _self_sufficient(target, cap=5):
    """Unprofitable PV (CAPEX 10 MEUR/MW), 1 MW demand, no export. PV of size x covers min(0.5 x, 1) MW in 2 of 4
    hours: self-sufficiency min(0.5 x, 1) / 2, so 25 % needs exactly 1 MW of PV."""
    cfg = _cfg(pv={"capex": 1e7, "capacity_max": cap})
    cfg["lifetime"]["self_sufficiency"] = target
    return optimise_lifetime(cfg)


def test_a_self_sufficiency_target_builds_what_it_takes_and_no_more():
    sr = _self_sufficient(0.25)
    assert _size(sr, "pv", "capacity") == pytest.approx(1.0, abs=1e-6)
    k = sr.kpis
    assert k["self_sufficiency"] == pytest.approx(0.25, abs=1e-6) and k["self_sufficiency_reference"] == pytest.approx(0)
    assert k["self_sufficiency_target"] == 0.25 and k["self_sufficiency_binding"]
    assert k["npv"] < 0                                     # the target costs money: the NPV shows how much


def test_without_a_target_the_unprofitable_pv_is_not_built():
    sr = optimise_lifetime(_cfg(pv={"capex": 1e7, "capacity_max": 5}))
    assert _size(sr, "pv", "capacity") == pytest.approx(0, abs=1e-6)
    assert sr.kpis["self_sufficiency"] == pytest.approx(0, abs=1e-9) and "self_sufficiency_target" not in sr.kpis


def test_an_unreachable_self_sufficiency_is_explained():
    with pytest.raises(ValidationError, match="self-sufficiency of 25 % cannot be reached"):
        _self_sufficient(0.25, cap=0.5)
    with pytest.raises(ValidationError, match="not a fraction"):
        _self_sufficient(40)


# ------------------------------------------------------------------ grid connection expansion
def _grid_cfg(years=2, demand=2, pv=None, **grid):
    """Existing consumption capacity of 1 MW (max_import) importing at 10 EUR/MWh, no export (max_export 0, not
    expanded), a demand of ``demand`` MW and a genset at 100 EUR/MWh for what the grid cannot bring. Each MW of import
    expansion replaces 1 MW of genset in every hour: it saves 90 EUR/MWh * 8760 h = 788,400 EUR per year, 1,576,800
    over 2 undiscounted years."""
    comps = [{"type": "GridConnection", "name": "grid", "bus": "el", "import_price": 10, "max_import": 1,
              "max_export": 0, "optimise_size": True, "expansion_max_export": 0, "opex_import": 0, **grid},
             {"type": "Demand", "name": "load", "bus": "el", "profile": demand},
             {"type": "Producer", "name": "genset", "bus": "el", "profile": 5, "marginal_cost": 100}]
    if pv is not None:
        comps.append({"type": "Producer", "name": "pv", "bus": "el", "profile": pv})
    return {"time": {"start": "2025-01-01", "periods": 4, "freq": "1h"},
            "carriers": [{"name": "electricity", "kind": "electricity"}],
            "buses": [{"name": "el", "carrier": "electricity"}], "components": comps,
            "lifetime": {"mode": "optimise", "years": years, "discount_rate": 0.0}}


def test_grid_is_expanded_when_the_savings_beat_the_capex():
    """CAPEX 1,000,000 EUR/MW < 1,576,800 of savings: the import capacity grows from 1 to the 2 MW of demand (more
    saves nothing), NPV 576,800. CAPEX is paid on the MW added only; the reported size is the import capacity after
    it. Export (max expansion 0) is not sized and needs no CAPEX or OPEX."""
    sr = optimise_lifetime(_grid_cfg(capex_import=1_000_000, expansion_max_import=3))
    assert sr.sizes == [{"component": "grid", "quantity": "import", "value": pytest.approx(2, abs=1e-6),
                         "unit": "MW", "min": 1.0, "max": 4.0}]
    assert sr.kpis["npv"] == pytest.approx(2 * 788_400 - 1_000_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(sr.kpis["npv_check"], rel=1e-9)
    t = sr.table.set_index("year")
    assert list(t["capex"]) == pytest.approx([1_000_000, 0, 0], rel=1e-6)
    assert list(t["savings"]) == pytest.approx([0, 788_400, 788_400], rel=1e-6)
    assert list(t["grid import MWh"]) == pytest.approx([0, 2 * 8760, 2 * 8760], rel=1e-6)
    ref_import = sr.reference.flows.xs(("grid", "import"), level=("component", "port"), axis=1).to_numpy()
    assert ref_import.max() == pytest.approx(1)         # the reference: the existing connection


def test_grid_is_not_expanded_when_the_capex_exceeds_the_savings():
    sr = optimise_lifetime(_grid_cfg(capex_import=2_000_000))
    assert _size(sr, "grid", "import") == pytest.approx(1, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(0, abs=1e-3) and sr.kpis["irr"] is None


@pytest.mark.parametrize("opex, added", [(300_000, 0.0), (280_000, 1.0)])
def test_grid_expansion_opex_moves_the_break_even(opex, added):
    """CAPEX 1,000,000 + 2 years of OPEX per MW added against 1,576,800 of savings: 300,000/yr -> -23,200 per MW,
    not built; 280,000/yr -> +16,800 per MW, built."""
    sr = optimise_lifetime(_grid_cfg(capex_import=1_000_000, opex_import=opex))
    assert _size(sr, "grid", "import") == pytest.approx(1 + added, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(added * (1_576_800 - 2 * opex - 1_000_000), abs=1e-3)


def test_grid_expansion_at_its_maximum_is_flagged():
    sr = optimise_lifetime(_grid_cfg(capex_import=1_000_000, expansion_max_import=0.5))
    assert _size(sr, "grid", "import") == pytest.approx(1.5, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(0.5 * (1_576_800 - 1_000_000), rel=1e-6)
    assert any("grid: the optimal import is at its maximum (1.5 MW)" in w for w in sr.warnings)


@pytest.mark.parametrize("capex_import, capex_export, imp, exp, npv", [
    (300_000, 500_000, 2.0, 1.0, 350_400 - 300_000),
    (400_000, 400_000, 1.0, 3.0, 2 * (438_000 - 400_000)),
])
def test_import_and_export_are_expanded_each_by_its_own_capex(capex_import, capex_export, imp, exp, npv):
    """1 MW each way today. Hours 1 and 3: 2 MW demand, grid at 60 EUR/MWh, a genset at 100 for the rest; hours 2 and
    4: 3 MW of PV, export paid 50. Over 2 years each MW of import capacity added saves 40 * 4380 * 2 = 350,400 EUR, each
    MW of export capacity earns 50 * 4380 * 2 = 438,000 EUR. Each direction is built when its own CAPEX is lower: import
    +1 MW at 300,000 (export at 500,000 is not worth it), or export +2 MW at 400,000 (import at 400,000 is not)."""
    cfg = _grid_cfg(demand=[2, 0, 2, 0], pv=[0, 3, 0, 3], import_price=60, export_price=50, max_export=1,
                    expansion_max_import=3, expansion_max_export=3, capex_import=capex_import,
                    capex_export=capex_export, opex_export=0)
    sr = optimise_lifetime(cfg)
    assert _size(sr, "grid", "import") == pytest.approx(imp, abs=1e-6)
    assert _size(sr, "grid", "export") == pytest.approx(exp, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(npv, rel=1e-6)
    assert list(sr.table["capex"]) == pytest.approx([(imp - 1) * capex_import + (exp - 1) * capex_export, 0, 0])


@pytest.mark.parametrize("change, msg", [
    ({"max_import": None}, "an expansion of import needs the existing max_import"),
    ({"capex_import": None}, "CAPEX import required"),
    ({"lifetime": None}, "needs a lifetime optimisation run"),
])
def test_grid_expansion_errors(change, msg):
    cfg = _grid_cfg(capex_import=1_000_000)
    grid = cfg["components"][0]
    for k, v in change.items():
        if k == "lifetime":
            cfg.pop("lifetime")
        elif v is None:
            grid.pop(k, None)
    with pytest.raises(ValidationError, match=msg):
        es, _ = from_config(cfg)
        es.validate()


def _grid_expansion(fee, years=2, rate=0.1):
    """2 MW demand, a connection of 1 MW each way at 10 EUR/MWh whose import and export can be expanded (a nominal
    CAPEX of 1 EUR/MW each) and a backup supply at 110 EUR/MWh. Each MW of import expansion saves 100 EUR/MWh * 8760 h
    = 876,000 EUR per year, but the import capacity fee is charged on the import capacity every year. An export
    capacity fee of 50,000 EUR/MW/yr is charged on the export capacity, which nothing needs beyond the existing 1 MW."""
    cfg = _cfg(years=years, rate=rate)
    cfg["components"] = [
        {"type": "GridConnection", "name": "grid", "bus": "el", "import_price": 10, "max_import": 1, "max_export": 1,
         "import_capacity_fee": fee, "export_capacity_fee": 50_000, "optimise_size": True,
         "expansion_max_import": 3, "expansion_max_export": 3, "capex_import": 1, "opex_import": 0,
         "capex_export": 1, "opex_export": 0},
        {"type": "GridConnection", "name": "backup", "bus": "el", "import_price": 110, "max_export": 0},
        {"type": "Demand", "name": "load", "bus": "el", "profile": 2}]
    return optimise_lifetime(cfg)


@pytest.mark.parametrize("fee, expansion", [(800_000, 1.0), (900_000, 0.0)])
def test_capacity_fees_decide_the_grid_expansion(fee, expansion):
    """Import fee 800,000 < 876,000 saved per MW and year: the import capacity is expanded by the 1 MW still bought
    from the backup, saving 76,000 EUR every year; fee 900,000 > 876,000: no expansion. Both fees are NETWORK costs,
    every year discounted at 10 % (annuity 1/1.1 + 1/1.1^2), each on its direction's capacity after the expansion:
    import 1 MW + the expansion, export the existing 1 MW (not expanded: nothing to export)."""
    sr = _grid_expansion(fee)
    assert _size(sr, "grid", "import") == pytest.approx(1 + expansion, abs=1e-6)
    assert _size(sr, "grid", "export") == pytest.approx(1, abs=1e-6)
    annuity = 1 / 1.1 + 1 / 1.1**2
    network = (fee * (1 + expansion) + 50_000 * 1) * annuity
    assert sr.results.costs.loc["grid", "NETWORK"] == pytest.approx(network, rel=1e-6)
    t = sr.table.set_index("year")
    assert list(t["savings"]) == pytest.approx([0] + [(876_000 - fee) * expansion] * 2, abs=1e-3)


def test_reference_case_that_cannot_run_is_explained():
    """2 MW demand, 1 MW of import and no genset: the sized model expands the import by 1 MW, but the reference case
    (the same model with no expansion) cannot supply the demand. The error says so and how to fix it; with a baseline
    variant that can run (2 MW of import today) the run solves."""
    cfg = _grid_cfg(capex_import=1_000_000)
    cfg["components"] = [c for c in cfg["components"] if c["name"] != "genset"]
    with pytest.raises(InfeasibleModelError, match="the reference case is infeasible: this model without the new "
                                                   "assets .* cannot meet the energy balance on bus el.*baseline"):
        optimise_lifetime(cfg)
    baseline = copy.deepcopy(cfg)
    baseline.pop("lifetime")
    baseline["components"][0] = {"type": "GridConnection", "name": "grid", "bus": "el", "import_price": 10,
                                 "max_import": 2, "max_export": 0}
    cfg["lifetime"].update(reference=baseline, reference_name="today")
    sr = optimise_lifetime(cfg)
    assert _size(sr, "grid", "import") == pytest.approx(2, abs=1e-6)
    assert sr.kpis["npv"] == pytest.approx(-1_000_000, rel=1e-6)    # the same supply as today, for the CAPEX


def test_lifetime_optimisation_without_a_reference_case():
    """lifetime.compare false: one solve, no reference case. Here the site cannot run without the expansion (no
    genset), which fails when compared with "this model without the new assets". The expansion to the 2 MW of demand
    is reported with the present value of the site's lifetime cost instead of NPV / IRR / payback: undiscounted, 2 MW
    * 8760 h * 10 EUR/MWh = 175,200 per year of import, + 10,000 OPEX per year, + 1,000,000 CAPEX; the yearly cost
    table adds up to it (= the run's total cost)."""
    cfg = _grid_cfg(capex_import=1_000_000, opex_import=10_000)
    cfg["components"] = [c for c in cfg["components"] if c["name"] != "genset"]
    cfg["lifetime"]["compare"] = False
    stages = []
    sr = optimise_lifetime(cfg, on_stage=lambda k, n, label: stages.append((k, n)))
    assert stages == [(1, 1)] and sr.reference is None
    assert _size(sr, "grid", "import") == pytest.approx(2, abs=1e-6)
    k = sr.kpis
    assert k["compared"] is False and k["npv"] is None and k["irr"] is None and k["payback"] is None
    assert k["lifetime_cost"] == pytest.approx(1_000_000 + 2 * (175_200 + 10_000), rel=1e-6)
    assert k["lifetime_cost"] == pytest.approx(sr.results.total_cost, rel=1e-9)
    t = sr.table.set_index("year")
    assert list(t["total_cost"]) == pytest.approx([1_000_000, 185_200, 185_200], rel=1e-6)
    assert "savings" not in t and "net_cash_flow" not in t


@pytest.mark.parametrize("rate", [0.0, 0.07])
def test_the_reference_case_does_not_change_the_optimal_sizes(rate):
    """The same battery and grid sizing with and without a reference case: minimising the lifetime cost is the same as
    maximising the NPV against any fixed case, so only the reported figures differ (also discounted, with inflation:
    the table's discounted total cost still adds up to the lifetime cost)."""
    cfg = _cfg(years=3, rate=rate, store={"capex_energy": 300_000}, inflation=0.02)
    cfg["components"][0].update(max_import=2, optimise_size=True, expansion_max_import=3, capex_import=50_000,
                                opex_import=1_000, expansion_max_export=0)
    compared = optimise_lifetime(copy.deepcopy(cfg))
    cfg["lifetime"]["compare"] = False
    alone = optimise_lifetime(cfg)
    assert [(s["component"], s["quantity"]) for s in alone.sizes] == [(s["component"], s["quantity"])
                                                                     for s in compared.sizes]
    for a, b in zip(alone.sizes, compared.sizes, strict=True):
        assert a["value"] == pytest.approx(b["value"], abs=1e-5)
    assert alone.kpis["lifetime_cost"] == pytest.approx(compared.results.total_cost, rel=1e-7)
    assert alone.table["discounted"].sum() == pytest.approx(alone.results.total_cost, rel=1e-7)


def test_hurdles_need_a_reference_case():
    cfg = _grid_cfg(capex_import=1_000_000)
    cfg["lifetime"].update(compare=False, hurdles={"min_irr": 0.1})
    with pytest.raises(ValidationError, match="hurdles .* need a case to compare with"):
        optimise_lifetime(cfg)


def test_a_baseline_with_variable_sizes_is_compared_at_its_optimum():
    """Two investment options for a 2 MW demand every other hour ([0, 2, 0, 2]) behind a 1 MW connection: the
    baseline variant expands the grid (CAPEX 1,000,000 EUR/MW: +1 MW), this variant adds a lossless battery instead
    (300,000 EUR/MWh: 1 MWh, charged in the empty hours). The baseline's own variable size is optimised, not pinned to
    its minimum (that would leave it unable to supply the demand): both import the same energy, so the NPV of the
    battery over the grid expansion is the CAPEX difference, 1,000,000 - 300,000."""
    def system(extra):
        return {"time": {"start": "2025-01-01", "periods": 4, "freq": "1h"},
                "carriers": [{"name": "electricity", "kind": "electricity"}],
                "buses": [{"name": "el", "carrier": "electricity"}],
                "components": [{"type": "GridConnection", "name": "grid", "bus": "el", "import_price": 10,
                                "max_import": 1, "max_export": 0},
                               {"type": "Demand", "name": "load", "bus": "el", "profile": [0, 2, 0, 2]}] + extra}
    baseline = system([])
    baseline["components"][0].update(optimise_size=True, expansion_max_export=0, capex_import=1_000_000,
                                     opex_import=0)
    cfg = system([{"type": "Storage", "name": "bess", "bus": "el", "eta_ch": 1, "eta_dis": 1, "soc_init": 0,
                   "optimise_size": True, "e_size_max": 5, "capex_energy": 300_000, "capex_power": 0, "opex": 0}])
    cfg["lifetime"] = {"mode": "optimise", "years": 2, "discount_rate": 0.0, "reference": baseline,
                       "reference_name": "grid only"}
    stages = []
    sr = optimise_lifetime(cfg, on_stage=lambda k, n, label: stages.append(label))
    assert stages[-1] == "baseline 'grid only'"
    assert _size(sr, "bess", "energy") == pytest.approx(1, abs=1e-6)
    assert sr.kpis["reference_sizes"] == [{"component": "grid", "quantity": "import",
                                           "value": pytest.approx(2, abs=1e-6), "unit": "MW"}]
    assert sr.kpis["capex"] == pytest.approx(300_000 - 1_000_000, rel=1e-6)    # this variant's minus the baseline's
    assert sr.kpis["npv"] == pytest.approx(700_000, rel=1e-6)
    assert sr.kpis["npv"] == pytest.approx(sr.kpis["npv_check"], rel=1e-9)


# ------------------------------------------------------------------ stored reference cases
def _counting_solve():
    solved = []

    def solve(c):
        solved.append(c)
        es, opts = from_config(c)
        return es.solve(time_limit=opts.get("time_limit"), tie_break=opts.get("tie_break", 0.01))
    return solve, solved


@pytest.mark.parametrize("hurdles", [None, {"min_irr": 0.3}])
def test_a_stored_reference_case_is_not_solved_again(tmp_path, hurdles):
    """With a ReferenceCache the reference case is solved once: the second run solves only the sized model and gives
    the same sizes, cash flows and KPIs (also with a hurdle, whose bound comes from the stored case). Another variant
    with the same baseline reuses it too; a change of the baseline (its price) or of the run settings (the discount
    rate) is another case and is solved."""
    from oems.reference_cache import ReferenceCache
    from oems.sizing import ReferenceSummary

    def cfg(capex=400_000, rate=0.05, price=100):
        c = _cfg(years=4, rate=rate, store={"capex_energy": capex, "e_size_max": 0.5},
                 pv={"capex": 500_000, "capacity_max": 5})
        base = _cfg()
        base.pop("lifetime")
        base["components"][0]["import_price"] = [10, price, 10, price]
        c["lifetime"].update(reference=base, reference_name="today")
        if hurdles:
            c["lifetime"]["hurdles"] = hurdles
        return c

    cache = ReferenceCache(tmp_path / "cases")
    solve, solved = _counting_solve()
    first = optimise_lifetime(cfg(), solve=solve, cache=cache)
    assert len(solved) == 2 and first.kpis["reference_reused"] is None
    again = optimise_lifetime(cfg(), solve=solve, cache=cache)
    assert len(solved) == 3 and isinstance(again.reference, ReferenceSummary) and again.kpis["reference_reused"]
    assert [s["value"] for s in again.sizes] == pytest.approx([s["value"] for s in first.sizes], abs=1e-9)
    for k in ("npv", "npv_check", "irr", "payback", "payback_discounted", "capex", "savings_year1"):
        assert again.kpis[k] == pytest.approx(first.kpis[k], rel=1e-9), k
    assert again.kpis.get("hurdles") == first.kpis.get("hurdles")
    assert again.table.to_numpy() == pytest.approx(first.table.to_numpy(), rel=1e-9, abs=1e-6)
    optimise_lifetime(cfg(capex=350_000), solve=solve, cache=cache)     # another variant, the same baseline
    assert len(solved) == 4
    optimise_lifetime(cfg(price=90), solve=solve, cache=cache)          # the baseline changed
    assert len(solved) == 6
    optimise_lifetime(cfg(rate=0.06), solve=solve, cache=cache)         # the run settings changed
    assert len(solved) == 8
    assert len(list((tmp_path / "cases").glob("*.json"))) == 3


def test_a_changed_data_file_is_another_reference_case(tmp_path):
    """The stored case is keyed by the content of the data files the reference reads, not by their names."""
    from oems.reference_cache import ReferenceCache

    def write(price):
        (tmp_path / "p.csv").write_text("time,price\n" + "".join(
            f"2025-01-01 0{h}:00,{p}\n" for h, p in enumerate([10, price, 10, price])))

    cfg = _cfg(store={"capex_energy": 700_000})
    cfg["datasets"] = {"p": str(tmp_path / "p.csv")}
    cfg["components"][0]["import_price"] = {"series": "p:price"}
    cache = ReferenceCache(tmp_path / "cases")
    solve, solved = _counting_solve()
    write(100)
    optimise_lifetime(copy.deepcopy(cfg), solve=solve, cache=cache)
    optimise_lifetime(copy.deepcopy(cfg), solve=solve, cache=cache)
    assert len(solved) == 3
    write(90)
    optimise_lifetime(copy.deepcopy(cfg), solve=solve, cache=cache)
    assert len(solved) == 5


def test_hurdles_out_of_reach_for_a_site_that_needs_the_investment_are_named():
    """The battery variant of the two-option case above cannot run without its battery. At 2,000,000 EUR/MWh it costs
    1,000,000 more than the baseline's grid expansion and saves nothing, so a minimum IRR cannot be met while the
    demand still has to be covered: the error names the hurdle instead of an unexplained infeasible model."""
    def system(extra):
        return {"time": {"start": "2025-01-01", "periods": 4, "freq": "1h"},
                "carriers": [{"name": "electricity", "kind": "electricity"}],
                "buses": [{"name": "el", "carrier": "electricity"}],
                "components": [{"type": "GridConnection", "name": "grid", "bus": "el", "import_price": 10,
                                "max_import": 1, "max_export": 0},
                               {"type": "Demand", "name": "load", "bus": "el", "profile": [0, 2, 0, 2]}] + extra}
    baseline = system([])
    baseline["components"][0].update(optimise_size=True, expansion_max_export=0, capex_import=1_000_000,
                                     opex_import=0)
    cfg = system([{"type": "Storage", "name": "bess", "bus": "el", "eta_ch": 1, "eta_dis": 1, "soc_init": 0,
                   "optimise_size": True, "e_size_max": 5, "capex_energy": 2_000_000, "capex_power": 0, "opex": 0}])
    cfg["lifetime"] = {"mode": "optimise", "years": 2, "discount_rate": 0.0, "reference": baseline,
                       "hurdles": {"min_irr": 0.09}}
    with pytest.raises(InfeasibleModelError, match="meet the investment hurdles \\(IRR >= 9 %\\)"):
        optimise_lifetime(cfg)
