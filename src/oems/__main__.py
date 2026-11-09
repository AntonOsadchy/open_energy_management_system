"""Command line: ``python -m oems run system.yaml [--out results/] [--freq 15min] [--years 15]``.

With ``--years N`` (or ``lifetime: {years: N, discount_rate: r, inflation: i}`` in the config) the horizon is
repeated for N years with battery degradation (see :mod:`oems.degradation`); the results folder then has
``lifetime.csv`` and the full results of each distinct year in ``year_<k>/``.

With ``lifetime: {mode: optimise, years: N, discount_rate: r, inflation: i}`` the sizes of components with
``optimise_size`` are optimised over the N years in one model (see :mod:`oems.sizing`); the results folder then
has ``cash_flows.csv``, ``sizes.csv`` and ``investment.csv`` next to the results of the sized run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import from_config, grid_from_config
from .degradation import simulate_lifetime
from .exceptions import OEMSError
from .sizing import optimise_lifetime


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="oems", description="Microgrid Sizing Tool dispatch model")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="solve a system config and write CSV results")
    run.add_argument("config", type=Path)
    run.add_argument("--out", type=Path, default=None, help="results folder (default: <config dir>/results)")
    run.add_argument("--freq", choices=["15min", "30min", "1h"], help="override the model resolution")
    run.add_argument("--time-limit", type=float, default=None)
    run.add_argument("--years", type=int, default=None, help="repeat the horizon for this many years with "
                     "battery degradation (overrides lifetime.years in the config)")
    sub.add_parser("app", help="start the drag-and-drop UI")
    args = ap.parse_args(argv)

    if args.cmd == "app":
        from .ui.server import main as app_main
        app_main()
        return 0
    try:
        import yaml
        cfg = yaml.safe_load(args.config.read_text())
        if args.freq:
            time = cfg.setdefault("time", {})
            if "periods" in time and not time.get("end"):  # keep the horizon length
                old = grid_from_config(time)
                hours = {"15min": 0.25, "30min": 0.5, "1h": 1.0}
                time["periods"] = int(round(old.n * old.step_hours / hours[args.freq]))
            time["freq"] = args.freq
        if args.years:
            cfg["lifetime"] = {**(cfg.get("lifetime") or {}), "years": args.years}
        life = cfg.get("lifetime")

        def solve(c):
            es, opts = from_config(c, base_dir=args.config.parent)
            return es.solve(time_limit=args.time_limit or opts.get("time_limit"),
                            tie_break=opts.get("tie_break", 0.01))

        optimise = bool(life) and life.get("mode") == "optimise"
        if optimise:
            sr = optimise_lifetime(cfg, solve=solve, on_stage=lambda k, n, label: print(f"{label} ({k} of {n})"))
        elif life and int(life.get("years", 1)) > 1:
            lt = simulate_lifetime(cfg, int(life["years"]), discount_rate=float(life.get("discount_rate") or 0),
                                   inflation=float(life.get("inflation") or 0),
                                   solve=solve, on_year=lambda k, n, soh: print(f"year {k} of {n}", soh or ""))
        else:
            res = solve(cfg)
    except OEMSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = args.out or args.config.parent / "results"
    if optimise:
        import pandas as pd

        sr.results.to_csv(out)
        sr.table.to_csv(out / "cash_flows.csv", index=False)
        pd.DataFrame(sr.sizes).to_csv(out / "sizes.csv", index=False)
        kpis = {k: v for k, v in sr.kpis.items() if k != "hurdles"}
        pd.Series(kpis, name="value").rename_axis("item").to_csv(out / "investment.csv")
        print(pd.DataFrame(sr.sizes).to_string(index=False))
        print(", ".join(f"{k} {v:,.4g}" if isinstance(v, float) else f"{k} {v}" for k, v in kpis.items()))
        for h in sr.kpis.get("hurdles", []):
            print(f"hurdle {h['name']}: {'binding' if h['binding'] else 'met' if h['met'] else 'not met'}")
        for w in sr.warnings:
            print("note:", w)
        print(f"results written to {out}")
        return 0
    if life and int(life.get("years", 1)) > 1:
        out.mkdir(parents=True, exist_ok=True)
        lt.table.to_csv(out / "lifetime.csv", index=False)
        for y in lt.years:
            if not y.reused:
                y.results.to_csv(out / f"year_{y.year}")
        print(lt.table.to_string(index=False))
        print(f"results written to {out}")
        return 0
    res.to_csv(out)
    print(res.summary())
    print(f"results written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
