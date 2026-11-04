"""Thin adapter around the HiGHS interfaces of Pyomo.

Pyomo is migrating from the ``appsi`` interfaces to ``pyomo.contrib.solver``. This module hides
the difference: it returns a :class:`SolveInfo` with a normalised status and the duals of all
constraints. Dual sign convention (verified by tests): the dual is d(objective)/d(rhs).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

import pyomo.environ as pyo

from .exceptions import SolverError


class SolveStatus(StrEnum):
    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    UNBOUNDED = "unbounded"
    TIME_LIMIT = "time_limit"
    ERROR = "error"


@dataclass
class SolveInfo:
    status: SolveStatus
    objective: float | None
    solve_time: float
    backend: str
    duals: dict = field(default_factory=dict)
    message: str = ""

    @property
    def has_solution(self) -> bool:
        return self.status in (SolveStatus.OPTIMAL, SolveStatus.TIME_LIMIT) and self.objective is not None


def solve_model(m: pyo.ConcreteModel, *, time_limit: float | None = None, threads: int | None = None,
                options: dict | None = None, load_duals: bool = True) -> SolveInfo:
    """Solve ``m`` with HiGHS and load the primal solution into the model."""
    try:
        return _solve_contrib(m, time_limit, threads, options, load_duals)
    except ImportError:
        return _solve_appsi(m, time_limit, threads, options, load_duals)


def _solve_contrib(m, time_limit, threads, options, load_duals) -> SolveInfo:
    from pyomo.contrib.solver.common.factory import SolverFactory
    from pyomo.contrib.solver.common.results import SolutionStatus, TerminationCondition

    solver = SolverFactory("highs")
    if not solver.available():
        raise SolverError("HiGHS is not available - install it with `pip install highspy`")
    solver_options = dict(options or {})
    if threads:
        solver_options["threads"] = threads
    t0 = time.perf_counter()
    res = solver.solve(
        m,
        load_solutions=False,
        raise_exception_on_nonoptimal_result=False,
        time_limit=time_limit,
        solver_options=solver_options,
    )
    elapsed = time.perf_counter() - t0
    tc = res.termination_condition
    if tc == TerminationCondition.convergenceCriteriaSatisfied:
        status = SolveStatus.OPTIMAL
    elif tc in (TerminationCondition.provenInfeasible, TerminationCondition.locallyInfeasible):
        status = SolveStatus.INFEASIBLE
    elif tc in (TerminationCondition.unbounded, TerminationCondition.infeasibleOrUnbounded):
        status = SolveStatus.UNBOUNDED if tc == TerminationCondition.unbounded else SolveStatus.INFEASIBLE
    elif tc == TerminationCondition.maxTimeLimit:
        status = SolveStatus.TIME_LIMIT
    else:
        status = SolveStatus.ERROR

    objective = None
    duals = {}
    if status in (SolveStatus.OPTIMAL, SolveStatus.TIME_LIMIT) and res.solution_status in (
        SolutionStatus.optimal, SolutionStatus.feasible
    ):
        res.solution_loader.load_vars()
        objective = pyo.value(m.obj)
        if load_duals and status == SolveStatus.OPTIMAL:
            duals = res.solution_loader.get_duals()
    return SolveInfo(status, objective, elapsed, "contrib.highs", duals, str(tc))


def _solve_appsi(m, time_limit, threads, options, load_duals) -> SolveInfo:  # pragma: no cover - fallback
    opt = pyo.SolverFactory("appsi_highs")
    if not opt.available():
        raise SolverError("HiGHS is not available - install it with `pip install highspy`")
    if time_limit:
        opt.config.time_limit = time_limit
    for k, v in (options or {}).items():
        opt.highs_options[k] = v
    if threads:
        opt.highs_options["threads"] = threads
    if not hasattr(m, "dual"):
        m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    t0 = time.perf_counter()
    res = opt.solve(m, load_solutions=False)
    elapsed = time.perf_counter() - t0
    tc = res.solver.termination_condition
    mapping = {
        pyo.TerminationCondition.optimal: SolveStatus.OPTIMAL,
        pyo.TerminationCondition.infeasible: SolveStatus.INFEASIBLE,
        pyo.TerminationCondition.unbounded: SolveStatus.UNBOUNDED,
        pyo.TerminationCondition.infeasibleOrUnbounded: SolveStatus.INFEASIBLE,
        pyo.TerminationCondition.maxTimeLimit: SolveStatus.TIME_LIMIT,
    }
    status = mapping.get(tc, SolveStatus.ERROR)
    objective, duals = None, {}
    if status == SolveStatus.OPTIMAL:
        m.solutions.load_from(res)
        objective = pyo.value(m.obj)
        duals = dict(m.dual.items()) if load_duals else {}
    return SolveInfo(status, objective, elapsed, "appsi_highs", duals, str(tc))
