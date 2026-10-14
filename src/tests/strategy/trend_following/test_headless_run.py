"""NEX-12: headless entry point (python -m strategy.trend_following.run)."""
import tempfile
import unittest
from pathlib import Path

from strategy.trend_following import run as run_mod
from strategy.trend_following.config import SPEC_VERSION
from strategy.trend_following.snapshot import read_manifest, save_snapshot
from tests.strategy.trend_following.helpers import synthetic_dataset


class TestHeadlessRun(unittest.TestCase):
    def test_run_writes_report_with_provenance_header(self):
        ds = synthetic_dataset(with_flows=True)
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap"
            man = save_snapshot(ds, snap)
            out = Path(tmp) / "out"
            code = run_mod.main(["--snapshot", str(snap), "--out", str(out), "--variants", "A0,A1",
                                 "--no-event-study"])
            self.assertEqual(code, 0)
            reports = list(out.glob("*.md"))
            self.assertEqual(len(reports), 1)
            md = reports[0].read_text(encoding="utf-8")
        head = md.split("## Warnings")[0]
        self.assertIn(man["sha256"], head)
        self.assertIn(f"스펙 버전: {SPEC_VERSION}", head)
        self.assertIn(man["period"]["eval_start"], head)
        self.assertIn(man["period"]["last_date"], head)
        self.assertIn("생존편향", head)
        self.assertIn("홀드아웃 OS 격리 없음(사용자 수용, 2026-10-14)", head)
        self.assertRegex(head, r"대상 커밋: `[0-9a-f]{40}(\+dirty)?`")
        self.assertIn("| A0 |", md)

    def test_header_separates_code_spec_from_snapshot_spec(self):
        ds = synthetic_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            man = save_snapshot(ds, Path(tmp) / "s")
        man = dict(man, spec_version="v05")
        head = "\n".join(run_mod.report_header(man, "abc"))
        self.assertIn(f"- 스펙 버전: {SPEC_VERSION} (대상 코드 기준", head)
        self.assertIn("- 스냅샷 생성 스펙 버전: v05", head)
        self.assertIn("전략 규칙 버전이 아니다", head)
        same = "\n".join(run_mod.report_header(dict(man, spec_version=SPEC_VERSION), "abc"))
        self.assertIn("대상 코드와 동일", same)

    def test_commit_can_be_injected(self):
        ds = synthetic_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            save_snapshot(ds, Path(tmp) / "s")
            path = run_mod.run_from_snapshot(Path(tmp) / "s", Path(tmp) / "o", ("A0",), False, commit="abc123")
            self.assertIn("`abc123`", path.read_text(encoding="utf-8"))

    def test_header_without_git(self):
        ds = synthetic_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            save_snapshot(ds, Path(tmp) / "s")
            lines = run_mod.report_header(read_manifest(Path(tmp) / "s"), "unknown")
        self.assertTrue(any("unknown" in x for x in lines))


if __name__ == "__main__":
    unittest.main()
