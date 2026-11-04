"""Convenience factories that return configured generic components.

Nothing is added to a system unless you call ``EnergySystem.add`` with the returned object.
"""

from __future__ import annotations

import math

from .components.converter import Converter
from .components.producer import Producer
from .components.storage import Storage


def _bus_name(bus) -> str:
    return bus if isinstance(bus, str) else bus.name


def battery(name, bus, *, e_max, p_max, eta_rt=0.9, soc_min=0.0, soc_max=1.0, soc_init=0.5,
            cycle_cost=0.0, **kwargs) -> Storage:
    """Battery with symmetric power rating and round-trip efficiency ``eta_rt``."""
    eta = math.sqrt(eta_rt)
    return Storage(name, bus, e_max=e_max, p_ch_max=p_max, p_dis_max=p_max, eta_ch=eta, eta_dis=eta,
                   soc_min=soc_min, soc_max=soc_max, soc_init=soc_init, cycle_cost=cycle_cost, **kwargs)


def hot_water_tank(name, bus, *, e_max, p_max, loss_per_h=0.0, eta=1.0, soc_init=0.5, **kwargs) -> Storage:
    """Sensible heat (or cold) storage with standing losses per hour."""
    return Storage(name, bus, e_max=e_max, p_ch_max=p_max, p_dis_max=p_max, eta_ch=eta, eta_dis=eta,
                   self_discharge=loss_per_h, soc_init=soc_init, **kwargs)


def heat_pump(name, el_bus, heat_bus, *, cop, p_th_max, source_bus=None, **kwargs) -> Converter:
    """Electric heat pump: 1 MWh el -> COP MWh heat. With ``source_bus`` the heat source
    (COP - 1 MWh per MWh el) is drawn from that bus. ``p_th_max`` limits the heat output."""
    inputs = {el_bus: 1.0}
    if source_bus is not None:
        inputs[source_bus] = cop - 1.0
    return Converter(name, inputs, {heat_bus: cop}, capacity_port=_bus_name(heat_bus), p_max=p_th_max, **kwargs)


def e_boiler(name, el_bus, heat_bus, *, p_max, eta=0.99, **kwargs) -> Converter:
    """Electric boiler; ``p_max`` is the electric rating."""
    return Converter(name, {el_bus: 1.0}, {heat_bus: eta}, p_max=p_max, **kwargs)


def chiller(name, el_bus, cold_bus, *, eer, p_cold_max, heat_recovery_bus=None, **kwargs) -> Converter:
    """Compression chiller: 1 MWh el -> EER MWh cold (+ EER + 1 MWh recoverable heat)."""
    outputs = {cold_bus: eer}
    if heat_recovery_bus is not None:
        outputs[heat_recovery_bus] = eer + 1.0
    return Converter(name, {el_bus: 1.0}, outputs, capacity_port=_bus_name(cold_bus), p_max=p_cold_max, **kwargs)


def gas_boiler(name, fuel_bus, heat_bus, *, eta=0.92, p_th_max, **kwargs) -> Converter:
    """Fuel boiler; ``p_th_max`` is the thermal rating."""
    return Converter(name, {fuel_bus: 1.0}, {heat_bus: eta}, capacity_port=_bus_name(heat_bus),
                     p_max=p_th_max, **kwargs)


def chp(name, fuel_bus, el_bus, heat_bus, *, eta_el, eta_th, p_fuel_max, **kwargs) -> Converter:
    """Combined heat and power with fixed output ratios; ``p_fuel_max`` is the fuel input rating."""
    return Converter(name, {fuel_bus: 1.0}, {el_bus: eta_el, heat_bus: eta_th}, p_max=p_fuel_max, **kwargs)


def link(name, from_bus, to_bus, *, eta=1.0, p_max=float("inf"), **kwargs) -> Converter:
    """One-directional link between two buses (heat cascade, transformer)."""
    return Converter(name, {from_bus: 1.0}, {to_bus: eta}, p_max=p_max, **kwargs)


def pv(name, bus, *, capacity, cf, curtailable=True, **kwargs) -> Producer:
    """PV plant from capacity (MW) and a capacity-factor profile."""
    return Producer(name, bus, capacity=capacity, cf=cf, curtailable=curtailable, **kwargs)
