"""Exception hierarchy for oems."""

from __future__ import annotations


class OEMSError(Exception):
    """Base class for all oems errors."""


class ValidationError(OEMSError):
    """Raised when the system definition or its inputs are invalid.

    ``errors`` holds one human-readable message per problem found.
    """

    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("\n".join(f"- {e}" for e in self.errors))


class SolverError(OEMSError):
    """Raised when the solver is missing or fails unexpectedly."""


class InfeasibleModelError(OEMSError):
    """Raised when the model is infeasible.

    When the system was solved with ``diagnose=True`` the model is re-solved with slack
    on every bus and ``report`` lists where the energy balance could not be met
    (columns: bus, time, shortfall_MWh, excess_MWh).
    """

    def __init__(self, message: str, report=None):
        self.report = report
        super().__init__(message)


class UnboundedModelError(OEMSError):
    """Raised when the model is unbounded (e.g. a profitable arbitrage loop without limits)."""
