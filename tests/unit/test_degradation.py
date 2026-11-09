"""Unit: SoH curves and their interpolation by age and cumulative cycles (oems.degradation)."""

import pytest

from oems.degradation import DegradationCurves
from oems.exceptions import ValidationError

# BESS IQ profile "standard" (bess_degradation_curves): SoH after year 1, 2, ... per cycles/day; all end at 0.645
R1 = [0.952, 0.925, 0.902, 0.883, 0.865, 0.85, 0.834, 0.82, 0.807, 0.794, 0.782, 0.771, 0.759, 0.749, 0.738, 0.728,
      0.718, 0.708, 0.698, 0.689, 0.68, 0.671, 0.662, 0.654, 0.645]
R15 = [0.944, 0.911, 0.884, 0.86, 0.839, 0.819, 0.8, 0.783, 0.767, 0.751, 0.736, 0.722, 0.708, 0.694, 0.681, 0.668,
       0.656, 0.645]
STANDARD = {1.0: R1, 1.5: R15}


def test_exact_at_curve_points():
    """At 1 cycle/day for 5 years (1825 cycles) SoH is the 1/day curve's year-5 value 0.865; at 1.5/day 0.839."""
    c = DegradationCurves(STANDARD, "bess")
    assert c.eol == pytest.approx(0.645)
    assert c.at(5, 1825)[0] == pytest.approx(0.865)
    assert c.at(5, 1.5 * 1825)[0] == pytest.approx(0.839)
    assert c.at(0, 0) == (1.0, None)


def test_interpolates_between_rates_at_the_average_rate():
    """1.25 cycles/day on average after 5 years lies halfway: (0.865 + 0.839) / 2 = 0.852."""
    c = DegradationCurves(STANDARD, "bess")
    soh, note = c.at(5, 1.25 * 365 * 5)
    assert soh == pytest.approx(0.852) and note is None


def test_days_per_year_sets_the_average_rate():
    """With a 366-day base year, 1.25 * 366 * 5 cycles is still 1.25 cycles/day."""
    assert DegradationCurves(STANDARD).at(5, 1.25 * 366 * 5, 366)[0] == pytest.approx(0.852)


@pytest.mark.parametrize("rate, expected", [(0.5, 0.865), (3.0, 0.839)])
def test_rates_outside_the_curves_use_the_nearest_curve_with_a_note(rate, expected):
    soh, note = DegradationCurves(STANDARD, "bess").at(5, rate * 365 * 5)
    assert soh == pytest.approx(expected) and "outside the curves" in note


def test_single_curve_ignores_cycling():
    c = DegradationCurves({1.5: R15}, "bess")
    assert c.single and c.at(3, 0)[0] == pytest.approx(0.884) and c.at(3, 1e6)[0] == pytest.approx(0.884)


def test_ended_curve_continues_along_its_last_slope():
    """The 1.5/day curve ends at year 18 (0.645, slope -0.011/yr): at year 19 it reads 0.634, below end of life, so
    interpolating near it retires the battery; at 1.25/day in year 19: (0.698 + 0.634) / 2 = 0.666."""
    c = DegradationCurves(STANDARD)
    assert c.curve_at(1, 19) == pytest.approx(0.634)
    assert c.at(19, 1.25 * 365 * 19)[0] == pytest.approx(0.666)


def test_fractional_age_interpolates_linearly():
    assert DegradationCurves({1.0: R1}).curve_at(0, 0.5) == pytest.approx((1 + 0.952) / 2)


@pytest.mark.parametrize("curves, msg", [({1.0: [0.9, 0.95]}, "increases"), ({1.0: [1.2]}, "between 0 and 1"),
                                         ({}, "no SoH curves"), ({0: [0.9]}, "must be > 0")])
def test_bad_curves_are_rejected(curves, msg):
    with pytest.raises(ValidationError, match=msg):
        DegradationCurves(curves, "bess")


def test_different_end_values_warn_and_use_the_highest_as_end_of_life():
    c = DegradationCurves({1.0: [0.9, 0.7], 2.0: [0.8, 0.6]}, "bess")
    assert c.eol == pytest.approx(0.7) and "different SoH" in c.warnings[0]


def test_config_form_with_string_rates():
    c = DegradationCurves.from_config({"curves": {"1": R1, "1.5": R15}, "source": "standard SoH curves"}, "bess")
    assert c.rates == [1.0, 1.5]
