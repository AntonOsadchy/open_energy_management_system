"""Command line: ``python -m oems run system.yaml [--out results/] [--freq 15min]``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import from_config, grid_from_config
from .exceptions import OEMSError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="oems", description="Open EMS dispatch model")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="solve a system config and write CSV results")
    run.add_argument("config", type=Path)
    run.add_argument("--out", type=Path, default=None, help="results folder (default: <config dir>/results)")
    run.add_argument("--freq", choices=["15min", "30min", "1h"], help="override the model resolution")
    run.add_argument("--time-limit", type=float, default=None)
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
        es, opts = from_config(cfg, base_dir=args.config.parent)
        res = es.solve(time_limit=args.time_limit or opts.get("time_limit"), tie_break=opts.get("tie_break", 0.01))
    except OEMSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = args.out or args.config.parent / "results"
    res.to_csv(out)
    print(res.summary())
    print(f"results written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
