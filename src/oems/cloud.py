"""Solve a run on a cloud machine (Modal): the app stays local, only the solve goes up.

A run is self-contained: the config plus the input series it references go up, the results come back (pickled, the
Pyomo model released). The worker runs the same ``oems`` code (its source is shipped with the image; numpy, pandas,
Pyomo and HiGHS are pinned to the local versions so pickles load on both sides). Supported: single-period runs and
lifetime optimisations (``lifetime.mode == "optimise"``), whose two models are solved on two machines at once (one
after the other with investment hurdles).

Setup once: ``pip install modal`` and ``modal token new``. Then :func:`solve_remote` (the UI's *Compute: Cloud*)
starts a machine with ``MEMORY_MB`` of RAM per model; it stops when its solve ends, billing per second.

:func:`execute` is the worker's job itself, usable locally (tests use it as the stand-in for the cloud).
"""

from __future__ import annotations

import json
import pickle
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .exceptions import InfeasibleModelError, OEMSError, RunCancelled, SolverError, UnboundedModelError, ValidationError

#: RAM and CPU reserved on the cloud machine (billed as reserved): 20 hourly years peaked well below 16 GB, and HiGHS
#: uses mostly one core. Each result reports the worker's peak (``cloud_stats``) to tune these.
MEMORY_MB = 16384
CPUS = 2.0
TIMEOUT_S = 6 * 3600
APP_NAME = "oems-solver"

ERRORS = {c.__name__: c for c in (ValidationError, InfeasibleModelError, UnboundedModelError, SolverError, OEMSError)}


# ------------------------------------------------------------------ the job (runs on the worker)
def execute(cfg: dict, frames: dict):
    """Solve one model ``cfg`` with the input ``frames`` ({dataset: DataFrame}); its Pyomo model released. A lifetime
    optimisation is two such models (sized, reference), solved as two jobs - in parallel in the cloud."""
    from .config import from_config

    if (cfg.get("lifetime") or {}).get("mode") not in (None, "optimise"):
        raise ValidationError("multi-year runs (fixed sizes, degradation) run on this computer, not in the cloud")
    es, opts = from_config(cfg, frames=frames)
    res = es.solve(time_limit=opts.get("time_limit"), tie_break=opts.get("tie_break", 0.01))
    res.release_model()
    return res


def _worker(payload: bytes) -> tuple:
    """The cloud function: ``("result", pickled Results, stats)`` or ``("error", class name, errors or message,
    stats)``; stats = peak memory and CPU time of the machine."""
    import resource
    import time

    cfg, frames = pickle.loads(payload)
    try:
        out = ("result", pickle.dumps(execute(cfg, frames)))
    except ValidationError as e:
        out = ("error", "ValidationError", e.errors)
    except OEMSError as e:
        out = ("error", type(e).__name__, str(e))
    except Exception as e:  # pragma: no cover - unexpected, reported as is
        out = ("error", "OEMSError", f"{type(e).__name__}: {e}")
    stats = {"peak_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,     # Linux: kB
             "cpu_s": time.process_time(), "memory_mb": MEMORY_MB, "cpus": CPUS}
    return (*out, stats)


# ------------------------------------------------------------------ the app side
def available() -> tuple[bool, str]:
    """Whether cloud runs can start here: Modal installed and a token configured; else why not."""
    try:
        import modal  # noqa: F401
    except ImportError:
        return False, "Modal is not installed in this Python environment (pip install modal)"
    try:
        from modal.config import config
        if not (config.get("token_id") and config.get("token_secret")):
            return False, "Modal has no login token yet: run `modal token new` in a terminal"
    except Exception as e:  # pragma: no cover - depends on the Modal version
        return False, f"Modal is not configured: {e}"
    return True, ""


def series_refs(cfg) -> set[str]:
    """Datasets referenced by ``{"series": "dataset:column"}`` anywhere in a config."""
    found = set()

    def walk(v):
        if isinstance(v, dict):
            if set(v) == {"series"} and isinstance(v["series"], str) and ":" in v["series"]:
                found.add(v["series"].split(":", 1)[0])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(cfg.get("components", []))
    walk(cfg.get("emission_policy", {}))
    return found


def pack(cfg: dict, base_dir: str | Path = ".") -> bytes:
    """The job: the config and the input series it uses, loaded here as the local run would load them."""
    from .timeseries import load_csv

    tz = (cfg.get("time") or {}).get("tz")
    frames = {}
    for ds in series_refs(cfg):
        path = Path((cfg.get("datasets") or {}).get(ds, ""))
        if not str(path):
            raise ValidationError(f"unknown dataset {ds!r}")
        frames[ds] = load_csv(path if path.is_absolute() else Path(base_dir) / path, tz=tz)
    cfg = {**json.loads(json.dumps(cfg, default=str)), "datasets": {}}
    return pickle.dumps((cfg, frames))


def _pinned() -> list[str]:
    from importlib.metadata import version

    import numpy
    import pandas
    import pyomo
    import yaml

    return [f"numpy=={numpy.__version__}", f"pandas=={pandas.__version__}", f"pyomo=={pyomo.__version__}",
            f"highspy=={version('highspy')}", f"pyyaml=={yaml.__version__}"]


def _app():
    import sys

    import modal

    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    image = modal.Image.debian_slim(python_version=py).pip_install(*_pinned()).add_local_python_source("oems")
    app = modal.App(APP_NAME)
    # retries: a cloud machine can be pre-empted (stopped by the provider); the solve then starts again on another
    fn = app.function(image=image, memory=MEMORY_MB, cpu=CPUS, timeout=TIMEOUT_S, serialized=True,
                      retries=modal.Retries(max_retries=2, initial_delay=1.0))(_worker)
    return app, fn


def _unpack(outcome: tuple):
    kind, *rest = outcome
    if kind == "error":
        cls_name, detail, _stats = rest
        raise ERRORS.get(cls_name, OEMSError)(detail)
    data, stats = rest
    res = pickle.loads(data)
    res.cloud_stats = stats             # peak memory and CPU time of the cloud machine
    return res


def _jobs(run, cancel=None):
    """``call(payloads) -> outcomes``: every payload solved at the same time (one cloud machine each). Setting the
    ``cancel`` event stops the machines and raises RunCancelled."""
    if run is not None:     # stand-in (tests): one after the other - Pyomo's solver output capture is not thread-safe
        def call_here(payloads):
            outs = []
            for p in payloads:
                if cancel is not None and cancel.is_set():
                    raise RunCancelled("stopped by user")
                outs.append(run(p))
            return outs
        return nullcontext(call_here)
    ok, why = available()
    if not ok:
        raise SolverError(f"cloud run not possible: {why}")
    app, fn = _app()

    def call(payloads):
        calls = [fn.spawn(p) for p in payloads]
        outs: list = [None] * len(calls)
        while any(o is None for o in outs):
            for i, c in enumerate(calls):
                if cancel is not None and cancel.is_set():
                    for x in calls:
                        x.cancel()          # stops the machines (and their billing)
                    raise RunCancelled("stopped by user")
                if outs[i] is None:
                    try:
                        outs[i] = c.get(timeout=1)
                    except Exception as e:
                        if "Timeout" not in type(e).__name__:
                            raise
        return outs

    @contextmanager
    def session():
        with app.run():
            yield call
    return session()


def solve_remote(cfg: dict, *, base_dir: str | Path = ".", on_stage=None, run=None, cancel=None):
    """Run ``cfg`` in the cloud; same result as locally. A lifetime optimisation sends its two models (optimal sizes,
    reference case) to two machines at once, so it takes as long as the slower one; with investment hurdles the
    reference goes first (its result bounds the sized model). ``on_stage(k, n, label)``
    reports progress; ``run(payload) -> outcome`` replaces the cloud call (tests: :func:`run_locally`); setting the
    ``cancel`` event (a ``threading.Event``) stops the machines and raises RunCancelled."""
    from .sizing import hurdles, optimise_lifetime

    life = cfg.get("lifetime") or {}
    if life and life.get("mode") != "optimise":
        raise ValidationError("multi-year runs (fixed sizes, degradation) run on this computer, not in the cloud")
    with _jobs(run, cancel) as call:
        if life:
            def both(sized, reference):
                return [_unpack(o) for o in call([pack(sized, base_dir), pack(reference, base_dir)])]

            def one(c):
                return _unpack(call([pack(c, base_dir)])[0])
            if hurdles(cfg):    # the sized model is bounded by the reference case's result: one after the other
                sr = optimise_lifetime(cfg, solve=one, on_stage=on_stage)
            else:
                sr = optimise_lifetime(cfg, solve_both=both, on_stage=on_stage)
            sr.cloud_stats = [sr.results.cloud_stats, sr.reference.cloud_stats]
            return sr
        if on_stage:
            on_stage(1, 1, "solving")
        return _unpack(call([pack(cfg, base_dir)])[0])


def run_locally(payload: bytes) -> tuple:
    """Stand-in for the cloud call: the worker in this process (tests, and checking a payload)."""
    return _worker(payload)
