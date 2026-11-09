"""Microgrid Sizing Tool - linear multi-carrier dispatch model for industrial-site electrification studies.

Units: MW, MWh, EUR, EUR/MWh; emissions in t, t/MWh and EUR/t.
"""

from . import presets
from .carrier import Bus, Carrier
from .components import (
    CapacityLimit,
    Component,
    Converter,
    Demand,
    EnergyCharge,
    EnergyCredit,
    FixedCharge,
    FlexibleDemand,
    GridConnection,
    PeakCharge,
    Producer,
    Sink,
    Storage,
    ThermalZone,
    register_component,
)
from .costs import CostCategory, FlowCost
from .emissions import EmissionPolicy
from .exceptions import InfeasibleModelError, OEMSError, SolverError, UnboundedModelError, ValidationError
from .results import Results
from .system import EnergySystem
from .timeseries import DailyProfile, TimeGrid, as_param, energy_to_power, load_csv

__version__ = "0.1.0"

__all__ = [
    "presets", "Bus", "Carrier", "CapacityLimit", "Component", "Converter", "Demand", "EnergyCharge",
    "EnergyCredit", "FixedCharge", "FlexibleDemand", "GridConnection", "PeakCharge", "Producer", "Sink",
    "Storage", "ThermalZone", "register_component", "CostCategory", "FlowCost", "EmissionPolicy",
    "InfeasibleModelError", "OEMSError", "SolverError", "UnboundedModelError", "ValidationError", "Results",
    "EnergySystem", "DailyProfile", "TimeGrid", "as_param", "energy_to_power", "load_csv",
]
