"""strategy/trend_following/run.py — Headless entry point (NEX-12, no UI).

  python -m strategy.trend_following.run --build-snapshot dev_2021_2024   # network, saves the dataset
  python -m strategy.trend_following.run --snapshot <folder or name>       # reruns the matrix offline

The run reads a snapshot (cache/snapshots/<name>), calls ``run_research`` and
writes the markdown report to ``cache/dev_reports/`` (override with --out). The
report starts with a provenance block: commit, spec version, data window,
snapshot hash, the survivorship-bias warning (checklist B1) and the holdout
isolation caveat. The CLI has no holdout switch; the development window guard
(config.DEV_END) stays on.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from paths import BASE_DIR, CACHE_DIR
from strategy.trend_following.config import DEV_END, DEV_START, SPEC_VERSION, VARIANTS
from strategy.trend_following.dataset import load_dataset
from strategy.trend_following.research import ResearchRequest, run_research
from strategy.trend_following.snapshot import (
    SNAPSHOT_ROOT, default_snapshot_dir, load_snapshot, read_manifest, save_snapshot,
)

DEFAULT_REPORT_DIR = Path(CACHE_DIR) / "dev_reports"
HOLDOUT_NOTICE = "홀드아웃 OS 격리 없음(사용자 수용, 2026-10-14)"


def git_commit(repo: str = BASE_DIR) -> str:
    """HEAD hash of the repo, suffixed '+dirty' when tracked files differ; 'unknown' without git."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                              check=True, timeout=15).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                               capture_output=True, text=True, check=True, timeout=15).stdout.strip()
        return head + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def report_header(manifest: dict, commit: str) -> list[str]:
    per = manifest["period"]
    rows = manifest["rows"]
    return [
        "> **개발 구간 백테스트 리포트** — 홀드아웃(2025-01-01 이후) 데이터는 포함되지 않았다.",
        "",
        f"- 대상 커밋: `{commit}`",
        f"- 스펙 버전: {manifest.get('spec_version', SPEC_VERSION)}",
        f"- 데이터 기간: 평가 {per['eval_start']} ~ {per['last_date']} "
        f"(워밍업 시작 {per['calendar_start']}, 종료일 가드 {per['dev_end_guard']})",
        f"- 스냅샷 SHA-256: `{manifest['sha256']}` (종목 {rows['stock_tickers']}개, 일수 {rows['calendar_days']}, "
        f"종가 봉 {rows['stock_close_bars']}, 수급 셀 {rows['flow_cells']})",
        f"- ⚠ 유니버스 생존편향(체크리스트 B1): {manifest['universe']}. "
        "과거 시점의 구성종목이 아니라 현재 시가총액 상위 종목을 소급 적용했으므로 기간 중 상장폐지·탈락 종목은 "
        "유니버스에 없다(편향 방향: 성과 과대, 크기 추정 불가).",
        f"- ⚠ {HOLDOUT_NOTICE}: 코드 가드(기본 차단)로만 막고 있으며 OS 수준 분리는 없다.",
    ]


def resolve_snapshot(arg: str) -> Path:
    p = Path(arg)
    return p if (p / "manifest.json").exists() else default_snapshot_dir(arg)


def run_from_snapshot(snapshot, out_dir=None, variants: tuple[str, ...] | None = None,
                      run_event_study: bool = True, progress=None, commit: str | None = None) -> Path:
    """Run the research matrix on a snapshot and return the saved report path."""
    folder = resolve_snapshot(str(snapshot))
    manifest = read_manifest(folder)
    ds = load_snapshot(folder)                       # verifies the content hash
    req = ResearchRequest(
        start=ds.eval_dates[0], variant_ids=tuple(variants or VARIANTS),
        include_flows=ds.flow_fi is not None, run_event_study=run_event_study,
    )
    result = run_research(ds, req, progress=progress)
    md = result.to_markdown(report_header(manifest, commit if commit is not None else git_commit()))
    out = Path(out_dir) if out_dir else DEFAULT_REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"trend_following_{SPEC_VERSION}_{manifest['sha256'][:12]}_{datetime.now():%Y%m%d_%H%M%S}.md"
    path.write_text(md, encoding="utf-8")
    return path


def build_snapshot(name: str, start: date = DEV_START, end: date = DEV_END, universe_size: int = 200,
                   include_flows: bool = True, progress=None) -> dict:
    ds = load_dataset(start, end, universe_size=universe_size, include_flows=include_flows, progress=progress)
    return save_snapshot(ds, default_snapshot_dir(name))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m strategy.trend_following.run", description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", help="snapshot folder, or a name under cache/snapshots/")
    g.add_argument("--build-snapshot", metavar="NAME", help=f"load the development window and save it as {SNAPSHOT_ROOT}/NAME")
    ap.add_argument("--out", help=f"report folder (default {DEFAULT_REPORT_DIR})")
    ap.add_argument("--variants", help="comma separated variant ids (default: all)")
    ap.add_argument("--no-event-study", action="store_true")
    ap.add_argument("--universe-size", type=int, default=200)
    ap.add_argument("--no-flows", action="store_true", help="--build-snapshot: skip investor flows")
    args = ap.parse_args(argv)

    def say(msg: str) -> None:
        print(msg, flush=True)

    if args.build_snapshot:
        man = build_snapshot(args.build_snapshot, universe_size=args.universe_size,
                             include_flows=not args.no_flows, progress=say)
        say(f"snapshot saved: {default_snapshot_dir(args.build_snapshot)} sha256={man['sha256']}")
        return 0
    variants = tuple(v.strip() for v in args.variants.split(",")) if args.variants else None
    path = run_from_snapshot(args.snapshot, args.out, variants, not args.no_event_study, progress=say)
    say(f"report saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
