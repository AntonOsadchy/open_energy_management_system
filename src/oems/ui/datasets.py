"""Uploaded CSV datasets of the UI workspace."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from ..timeseries import infer_resolution, load_csv


class DatasetStore:
    """CSV files in ``<workspace>/data`` plus an index file with their metadata."""

    def __init__(self, workspace: Path):
        self.dir = Path(workspace) / "data"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.index: dict[str, dict] = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        self._cache: dict[str, pd.DataFrame] = {}

    def _save(self):
        self.index_path.write_text(json.dumps(self.index, indent=2))

    def add(self, filename: str, content: bytes, tz: str | None = None) -> dict:
        ds_id = "ds_" + uuid.uuid4().hex[:8]
        path = self.dir / f"{ds_id}.csv"
        path.write_bytes(content)
        try:
            df = load_csv(path, tz=tz)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        df.columns = [re.sub(r"[^\w\-]", "_", str(c)) for c in df.columns]
        df.to_csv(path, index_label="time")  # normalised column names
        meta = {"id": ds_id, "name": Path(filename).stem, "file": path.name, "columns": list(df.columns),
                "resolution": infer_resolution(df.index), "start": str(df.index[0]), "end": str(df.index[-1]),
                "rows": len(df), "tz": tz}
        self.index[ds_id] = meta
        self._save()
        self._cache[ds_id] = df
        return {**meta, "preview": self.preview(ds_id)}

    def frame(self, ds_id: str) -> pd.DataFrame:
        if ds_id not in self._cache:
            meta = self.index[ds_id]
            self._cache[ds_id] = load_csv(self.dir / meta["file"], tz=meta.get("tz"))
        return self._cache[ds_id]

    def preview(self, ds_id: str, points: int = 60) -> dict:
        """Downsampled values per column for sparklines."""
        df = self.frame(ds_id)
        out = {}
        for c in df.columns:
            v = df[c].to_numpy(dtype=float)
            bins = np.array_split(v[~np.isnan(v)], min(points, max(1, len(v))))
            out[c] = [float(np.mean(b)) if len(b) else None for b in bins]
        return out

    def series(self, ds_id: str, col: str, max_points: int = 5000) -> dict:
        s = self.frame(ds_id)[col].dropna()
        step = max(1, len(s) // max_points)
        s = s.iloc[::step]
        return {"time": [str(t) for t in s.index], "values": s.round(6).tolist()}

    def paths(self) -> dict[str, str]:
        return {k: str(self.dir / v["file"]) for k, v in self.index.items()}

    def list(self) -> list[dict]:
        return list(self.index.values())

    def delete(self, ds_id: str) -> None:
        meta = self.index.pop(ds_id, None)
        if meta:
            (self.dir / meta["file"]).unlink(missing_ok=True)
            self._cache.pop(ds_id, None)
            self._save()
