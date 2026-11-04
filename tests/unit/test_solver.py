"""Unit tests for the HiGHS solver adapter (oems.solver)."""

import pyomo.environ as pyo

from oems.solver import SolveStatus, solve_model


def _model(lower=1.0, unbounded=False):
    m = pyo.ConcreteModel()
    m.x = pyo.Var(bounds=(0, 3))
    m.y = pyo.Var(within=pyo.Reals)
    m.c = pyo.Constraint(expr=m.x - lower == 0)
    m.obj = pyo.Objective(expr=100 * m.x + (m.y if unbounded else 0))
    return m


def test_dual_sign_convention():
    """For `x - demand = 0` with cost 100/x the dual is +100 (= d objective / d rhs).

    The bus-balance shadow prices rely on this sign: a grid-fed bus must show the import price.
    """
    m = _model()
    info = solve_model(m)
    assert info.status == SolveStatus.OPTIMAL
    assert abs(info.duals[m.c] - 100.0) < 1e-9
    assert abs(info.objective - 100.0) < 1e-9


def test_infeasible_status():
    """x in [0, 3] cannot equal 5: status INFEASIBLE, no solution loaded."""
    info = solve_model(_model(lower=5.0))
    assert info.status == SolveStatus.INFEASIBLE and not info.has_solution


def test_unbounded_status():
    """An objective with a free variable and no bounds is reported as UNBOUNDED."""
    info = solve_model(_model(unbounded=True))
    assert info.status == SolveStatus.UNBOUNDED
