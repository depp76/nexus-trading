"""NEX-12: dataset snapshot save / load / hash."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

from strategy.trend_following.dataset import HoldoutGuardError
from strategy.trend_following.snapshot import (
    SnapshotIntegrityError, dataset_sha256, load_snapshot, read_manifest, save_snapshot,
)
from tests.strategy.trend_following.helpers import synthetic_dataset


def _same(a, b):
    if a is None or b is None:
        return a is b
    return a.shape == b.shape and np.array_equal(a, b, equal_nan=True)


class TestSnapshotRoundtrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "snap"

    def test_roundtrip_is_array_identical(self):
        man = save_snapshot(self.ds, self.dir)
        back = load_snapshot(self.dir)
        a, b = self.ds, back
        self.assertEqual(a.dates, b.dates)
        self.assertEqual(a.stocks.tickers, b.stocks.tickers)
        self.assertEqual((a.stocks.markets, a.stocks.names), (b.stocks.markets, b.stocks.names))
        for f in ("open", "high", "low", "close", "volume"):
            self.assertTrue(_same(getattr(a.stocks, f), getattr(b.stocks, f)), f)
            self.assertTrue(_same(getattr(a.bm, f), getattr(b.bm, f)), f"bm {f}")
        for f in ("index_close", "rf_annual", "rf_period", "flow_fi", "flow_retail"):
            self.assertTrue(_same(getattr(a, f), getattr(b, f)), f)
        self.assertEqual((a.start_idx, a.rf_source, a.bm_is_total_return, a.bm.tickers),
                         (b.start_idx, b.rf_source, b.bm_is_total_return, b.bm.tickers))
        self.assertEqual(dataset_sha256(a), dataset_sha256(b))
        self.assertEqual(man["sha256"], dataset_sha256(b))

    def test_manifest_fields(self):
        save_snapshot(self.ds, self.dir)
        man = read_manifest(self.dir)
        self.assertEqual(man["tickers"], self.ds.stocks.tickers)
        self.assertEqual(man["period"]["last_date"], self.ds.dates[-1].isoformat())
        self.assertEqual(man["rows"]["stock_tickers"], self.ds.stocks.N)
        self.assertGreater(man["rows"]["flow_cells"], 0)
        self.assertIn("survivorship", man["universe"])
        self.assertEqual(len(man["sha256"]), 64)
        json.dumps(man)

    def test_one_changed_cell_changes_the_hash(self):
        h0 = dataset_sha256(self.ds)
        for arr in (self.ds.stocks.close, self.ds.flow_fi, self.ds.bm.open):
            old = arr[100, 0]
            arr[100, 0] = old + 1.0 if np.isfinite(old) else 1.0
            try:
                self.assertNotEqual(h0, dataset_sha256(self.ds))
            finally:
                arr[100, 0] = old
        self.assertEqual(h0, dataset_sha256(self.ds))

    def test_tampered_snapshot_is_rejected(self):
        import polars as pl
        save_snapshot(self.ds, self.dir)
        f = self.dir / "stocks_close.parquet"
        df = pl.read_parquet(f)
        col = df.columns[0]
        vals = df.get_column(col).to_list()
        vals[200] = (vals[200] or 0.0) + 1.0
        df.with_columns(pl.Series(col, vals)).write_parquet(f)
        with self.assertRaises(SnapshotIntegrityError):
            load_snapshot(self.dir)
        load_snapshot(self.dir, verify=False)

    def test_no_flows_no_bm(self):
        ds = synthetic_dataset(with_flows=False, with_bm=False)
        save_snapshot(ds, self.dir)
        back = load_snapshot(self.dir)
        self.assertIsNone(back.flow_fi)
        self.assertIsNone(back.bm)
        self.assertEqual(dataset_sha256(ds), dataset_sha256(back))

    def test_dataset_past_the_guard_is_not_saved(self):
        ds = synthetic_dataset(n_days=900, first_day=date(2022, 1, 3), start=date(2023, 1, 2))
        self.assertGreater(ds.dates[-1], date(2024, 12, 31))
        with self.assertRaises(HoldoutGuardError):
            save_snapshot(ds, self.dir)


if __name__ == "__main__":
    unittest.main()
