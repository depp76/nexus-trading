"""DART collection CLI (docs/dart_db_plan_2026-11-26.md 6, 12).

Only parses arguments and calls ``data.dart.sync.run_sync`` (collection) or
``data.dart.query`` (read-only reports); all logic lives there.

    python tools/dart_sync.py --init                 # company + report lists
    python tools/dart_sync.py --backfill [--tickers 005930 000660]
    python tools/dart_sync.py --daily
    python tools/dart_sync.py --auto                 # scheduler entry (plan 12)
    python tools/dart_sync.py --show 005930 [--as-of 2025-11-10]   # quarterly growth table
    python tools/dart_sync.py --issues               # unmapped accounts, fs_div changes, ...
    python tools/dart_sync.py --segments [--tickers 005930 ...]   # revenue by segment (stage 4)
    python tools/dart_sync.py --segment-report [--tickers ...]    # check results, passed-then-failed, names to map
"""
import argparse
import logging
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from data.dart.sync import run_sync  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Collect OpenDART filings and financials into dart.db.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_const", const="init", dest="mode")
    g.add_argument("--backfill", action="store_const", const="backfill", dest="mode")
    g.add_argument("--daily", action="store_const", const="daily", dest="mode")
    g.add_argument("--auto", action="store_const", const="auto", dest="mode")
    g.add_argument("--segments", action="store_const", const="segments", dest="mode")
    g.add_argument("--segment-report", action="store_true", help="print the segment check per stock")
    g.add_argument("--show", metavar="CODE", help="print the quarterly growth table of a stock")
    g.add_argument("--issues", action="store_true", help="print what collection could not do cleanly")
    ap.add_argument("--as-of", help="with --show: only filings received by this date (YYYY-MM-DD)")
    ap.add_argument("--quarters", type=int, default=12, help="with --show: quarters to print")
    ap.add_argument("--limit", type=int, default=50, help="with --issues: rows per list")
    ap.add_argument("--tickers", nargs="+", help="limit to these 6-digit stock codes")
    ap.add_argument("--budget", type=int, help="stop after this many API calls")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.show or args.issues or args.segment_report:
        from data.dart import query
        if args.show:
            print(query.format_growth_table(args.show, args.as_of, quarters=args.quarters))
        elif args.issues:
            print(query.format_issues(args.limit))
        else:
            print(query.format_segment_report(args.tickers))
        return 0

    last = {"msg": None}

    def progress(done, total, msg):
        head = msg.split(" ")[0]
        if args.verbose or head != last["msg"] or done % 200 == 0:
            print(f"[{done}/{total}] {msg}", flush=True)
            last["msg"] = head

    # Ctrl+C asks the run to stop at the next report boundary: what is done is
    # committed and the run ends as "partial" (review_agy L2). A second Ctrl+C
    # falls back to the default handler and interrupts at once.
    cancel = threading.Event()

    def on_sigint(_signum, _frame):
        cancel.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)
        print("stopping after the current report... (Ctrl+C again to abort now)", flush=True)

    previous = signal.signal(signal.SIGINT, on_sigint)
    trigger = "scheduler" if args.mode == "auto" else "cli"
    try:
        res = run_sync(args.mode, trigger, budget=args.budget, cancel=cancel, on_progress=progress,
                       tickers=args.tickers)
    finally:
        signal.signal(signal.SIGINT, previous)
    print(f"status={res.status} run={res.run_id} calls={res.calls} new_filings={res.filings_added} "
          f"periods={res.periods_fetched} segments={res.segments_done} pending={res.pending_left}"
          + (f" failed={res.periods_failed}+{res.segments_failed}" if res.periods_failed or res.segments_failed else "")
          + (f" error={res.error}" if res.error else ""))
    if res.unknown_tickers:
        print("no DART company for: " + ", ".join(res.unknown_tickers) + " (not in the company list)")
    return 0 if res.status in ("done", "partial", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
