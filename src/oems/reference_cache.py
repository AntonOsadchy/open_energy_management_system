"""Stored reference cases of lifetime optimisations, so a baseline is solved once and reused.

A lifetime optimisation compares its optimal sizes with a reference case (``oems.sizing``): the same model without
the investment, or a baseline system. Its solve is only needed for a handful of numbers
(:class:`oems.sizing.ReferenceSummary`), which are kept here under a key of everything they depend on
(:func:`reference_key`): the reference config (system, time, lifetime years / discount rate / inflation / model,
solver settings, emission policy), the content of the data files it reads and the model code itself. Any change of
those gives another key, so a stored case is never stale; variants compared with the same baseline under the same run
settings share one entry.

:class:`ReferenceCache` keeps one small JSON file per case in a folder (the UI: ``<workspace>/reference_cases``).
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
from pathlib import Path

#: Bump when the stored summary changes its meaning.
VERSION = 1

#: Parts of ``lifetime`` the reference case's solution depends on (hurdles and targets belong to the sized model).
LIFETIME_KEYS = ("mode", "years", "discount_rate", "inflation", "model")


@functools.lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """Hash of the model code (the oems package without the UI): results of older code are not reused."""
    root = Path(__file__).parent
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "ui" in p.relative_to(root).parts:
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _series_refs(node, out: set) -> None:
    if isinstance(node, dict):
        if set(node) == {"series"} and isinstance(node["series"], str):
            out.add(node["series"].split(":", 1)[0])
        for v in node.values():
            _series_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _series_refs(v, out)


def reference_key(reference: dict, base_dir: str | Path | None = None) -> str | None:
    """Key of a reference config (as built by ``sizing.reference_config``); None when it cannot be keyed (a data
    file is missing or the config is not JSON), in which case nothing is stored or reused."""
    try:
        used: set = set()
        _series_refs(reference, used)
        paths = reference.get("datasets") or {}
        data = {}
        for ds in sorted(used):
            path = Path(paths[ds])
            path = path if path.is_absolute() else Path(base_dir or ".") / path
            data[ds] = hashlib.sha256(path.read_bytes()).hexdigest()
        body = {k: v for k, v in reference.items() if k not in ("name", "datasets", "compute", "lifetime")}
        body["lifetime"] = {k: (reference.get("lifetime") or {}).get(k) for k in LIFETIME_KEYS}
        text = json.dumps({"v": VERSION, "code": code_fingerprint(), "data": data, "cfg": body}, sort_keys=True)
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class ReferenceCache:
    """Reference summaries as ``<folder>/<key>.json`` (the newest ``keep`` are kept)."""

    def __init__(self, folder: str | Path, keep: int = 500):
        self.dir = Path(folder)
        self.keep = keep

    def key(self, reference: dict, base_dir: str | Path | None = None) -> str | None:
        return reference_key(reference, base_dir)

    def get(self, key: str | None) -> dict | None:
        """The stored record ``{"summary": ..., "created": ..., "name": ...}`` or None."""
        if not key:
            return None
        try:
            rec = json.loads((self.dir / f"{key}.json").read_text())
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) and rec.get("v") == VERSION and "summary" in rec else None

    def put(self, key: str | None, summary: dict, name: str | None = None) -> None:
        if not key:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            rec = {"v": VERSION, "created": time.strftime("%Y-%m-%d %H:%M"), "name": name, "summary": summary}
            tmp = self.dir / f"{key}.tmp"
            tmp.write_text(json.dumps(rec, separators=(",", ":")))
            tmp.replace(self.dir / f"{key}.json")
            old = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[self.keep:]
            for p in old:
                p.unlink(missing_ok=True)
        except OSError:         # a cache that cannot be written is not an error of the run
            pass
