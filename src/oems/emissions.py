"""Emission accounting policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EmissionPolicy:
    """System-level treatment of one emission type (e.g. "co2").

    price:        EUR/t (scalar or series); adds an EMISSIONS cost to every emitting component
    cap:          t over the horizon (weighted); adds a constraint whose dual is the implied price
    report_only:  account emissions but never price them
    """

    gas: str = "co2"
    price: object = None
    cap: float | None = None
    report_only: bool = False
