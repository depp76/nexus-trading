"""strategy/trend_following/snapshot.py — Save / restore a loaded Dataset so
Validation can rerun the backtest on byte-identical data (NEX-12; checklist
A-C, G).

A snapshot is a folder under ``cache/snapshots/`` (gitignored):
  manifest.json       tickers, period, rows per source, SHA-256, universe method
  series.parquet      Date, index_close, rf_annual, rf_period
  <array>.parquet     one date x ticker float64 table per price / flow array

The SHA-256 in the manifest is computed from the *array contents* (a canonical
header plus little-endian float64 bytes), not from the parquet files, so it is
independent of the parquet encoder and any changed cell changes it.
``load_snapshot`` recomputes it and refuses to return a mismatching Dataset.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from paths import CACHE_DIR
from strategy.trend_following.config import DEV_END, SPEC_VERSION
from strategy.trend_following.dataset import Dataset, PriceBook, check_dev_window

SNAPSHOT_FORMAT = 1
SNAPSHOT_ROOT = Path(CACHE_DIR) / "snapshots"
_PRICE_FIELDS = ("open", "high", "low", "close", "volume")


class SnapshotIntegrityError(RuntimeError):
    """The arrays read back from a snapshot do not match the manifest hash."""


def _arrays(ds: Dataset) -> dict[str, np.ndarray]:
    """Every 2-D array of the dataset in a fixed order, keyed by file stem."""
    out: dict[str, np.ndarray] = {f"stocks_{f}": getattr(ds.stocks, f) for f in _PRICE_FIELDS}
    if ds.flow_fi is not None:
        out["flow_fi"] = ds.flow_fi
        out["flow_retail"] = ds.flow_retail
    if ds.bm is not None:
        for f in _PRICE_FIELDS:
            out[f"bm_{f}"] = getattr(ds.bm, f)
    return out


def _header(ds: Dataset) -> dict:
    return {
        "format": SNAPSHOT_FORMAT,
        "dates": [d.isoformat() for d in ds.dates],
        "tickers": list(ds.stocks.tickers),
        "markets": {t: ds.stocks.markets.get(t, "") for t in ds.stocks.tickers},
        "names": {t: ds.stocks.names.get(t, "") for t in ds.stocks.tickers},
        "start_idx": int(ds.start_idx),
        "bm_ticker": ds.bm.tickers[0] if ds.bm is not None else "",
        "bm_is_total_return": bool(ds.bm_is_total_return),
        "rf_source": ds.rf_source,
        "has_flows": ds.flow_fi is not None,
        "universe": str(ds.info.get("universe", "")),
    }


def _canonical(a: np.ndarray) -> bytes:
    arr = np.ascontiguousarray(np.asarray(a, dtype="<f8"))
    return np.where(np.isnan(arr), np.nan, arr).tobytes()      # one NaN bit pattern


def dataset_sha256(ds: Dataset) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(_header(ds), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for name, a in _arrays(ds).items():
        h.update(name.encode("utf-8"))
        h.update(str(np.shape(a)).encode("ascii"))
        h.update(_canonical(a))
    for name, a in (("index_close", ds.index_close), ("rf_annual", ds.rf_annual), ("rf_period", ds.rf_period)):
        h.update(name.encode("utf-8"))
        h.update(_canonical(a))
    return h.hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows_per_source(ds: Dataset) -> dict:
    rows = {
        "calendar_days": int(ds.stocks.T),
        "index_closes": int(np.isfinite(ds.index_close).sum()),
        "stock_close_bars": int(np.isfinite(ds.stocks.close).sum()),
        "stock_tickers": int(ds.stocks.N),
        "rf_rows": int(np.isfinite(ds.rf_annual).sum()),
        "flow_cells": int(np.isfinite(ds.flow_fi).sum()) if ds.flow_fi is not None else 0,
        "bm_close_bars": int(np.isfinite(ds.bm.close).sum()) if ds.bm is not None else 0,
    }
    return rows


def default_snapshot_dir(name: str) -> Path:
    return SNAPSHOT_ROOT / name


def save_snapshot(ds: Dataset, path, allow_holdout: bool = False) -> dict:
    """Write `ds` into folder `path` and return the manifest. A dataset with
    dates after DEV_END is refused unless `allow_holdout`."""
    if ds.dates:
        check_dev_window(ds.dates[-1], allow_holdout, "dataset last date")
    folder = Path(path)
    folder.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    for name, a in _arrays(ds).items():
        cols = ds.bm.tickers if name.startswith("bm_") else ds.stocks.tickers
        f = folder / f"{name}.parquet"
        pl.DataFrame({c: np.asarray(a, dtype=float)[:, j] for j, c in enumerate(cols)}).write_parquet(f)
        files[f.name] = _file_sha256(f)
    series = folder / "series.parquet"
    pl.DataFrame({
        "Date": ds.dates, "index_close": np.asarray(ds.index_close, dtype=float),
        "rf_annual": np.asarray(ds.rf_annual, dtype=float), "rf_period": np.asarray(ds.rf_period, dtype=float),
    }).write_parquet(series)
    files[series.name] = _file_sha256(series)

    head = _header(ds)
    manifest = {
        **{k: head[k] for k in ("format", "tickers", "markets", "names", "start_idx", "bm_ticker",
                                "bm_is_total_return", "rf_source", "has_flows", "universe")},
        "spec_version": SPEC_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "period": {
            "calendar_start": ds.dates[0].isoformat(), "eval_start": ds.dates[ds.start_idx].isoformat(),
            "last_date": ds.dates[-1].isoformat(), "dev_end_guard": DEV_END.isoformat(),
        },
        "rows": _rows_per_source(ds),
        "sha256": dataset_sha256(ds),
        "files": files,
        "info": ds.info,
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return manifest


def read_manifest(path) -> dict:
    return json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))


def _read_array(folder: Path, name: str) -> np.ndarray:
    return pl.read_parquet(folder / f"{name}.parquet").to_numpy().astype(float, copy=False)


def load_snapshot(path, allow_holdout: bool = False, verify: bool = True) -> Dataset:
    """Rebuild the Dataset saved by `save_snapshot`; the content hash must match
    the manifest (SnapshotIntegrityError otherwise)."""
    folder = Path(path)
    man = read_manifest(folder)
    series = pl.read_parquet(folder / "series.parquet")
    dates = [d if isinstance(d, date) else d.date() for d in series.get_column("Date").to_list()]
    if dates:
        check_dev_window(dates[-1], allow_holdout, "snapshot last date")
    tickers = list(man["tickers"])

    def book(prefix: str, cols: list[str], markets: dict, names: dict) -> PriceBook:
        arrs = {f: _read_array(folder, f"{prefix}_{f}") for f in _PRICE_FIELDS}
        return PriceBook(dates=dates, tickers=cols, open=arrs["open"], high=arrs["high"], low=arrs["low"],
                         close=arrs["close"], volume=arrs["volume"], markets=markets, names=names)

    stocks = book("stocks", tickers, dict(man["markets"]), dict(man["names"]))
    bm = book("bm", [man["bm_ticker"]], {man["bm_ticker"]: "ETF"}, {man["bm_ticker"]: man["bm_ticker"]}) \
        if man["bm_ticker"] else None
    flow_fi = flow_retail = None
    if man["has_flows"]:
        flow_fi, flow_retail = _read_array(folder, "flow_fi"), _read_array(folder, "flow_retail")
    ds = Dataset(
        stocks=stocks, index_close=series.get_column("index_close").to_numpy().astype(float),
        bm=bm, bm_is_total_return=bool(man["bm_is_total_return"]),
        rf_annual=series.get_column("rf_annual").to_numpy().astype(float),
        rf_period=series.get_column("rf_period").to_numpy().astype(float), rf_source=man["rf_source"],
        flow_fi=flow_fi, flow_retail=flow_retail, start_idx=int(man["start_idx"]), info=dict(man.get("info", {})),
    )
    if verify:
        got = dataset_sha256(ds)
        if got != man["sha256"]:
            raise SnapshotIntegrityError(f"snapshot hash mismatch in {folder}: manifest {man['sha256']}, data {got}")
    return ds
