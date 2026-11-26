"""DART collection CLI (docs/dart_db_plan_2026-11-26.md 6, 12).

Only parses arguments and calls ``data.dart.sync.run_sync``; all collection
logic lives there.

    python tools/dart_sync.py --init                 # company + report lists
    python tools/dart_sync.py --backfill [--tickers 005930 000660]
    python tools/dart_sync.py --daily
    python tools/dart_sync.py --auto                 # scheduler entry (plan 12)
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from data.dart.sync import run_sync  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Collect OpenDART filings and financials into dart.db.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_const", const="init", dest="mode")
    g.add_argument("--backfill", action="store_const", const="backfill", dest="mode")
    g.add_argument("--daily", action="store_const", const="daily", dest="mode")
    g.add_argument("--auto", action="store_const", const="auto", dest="mode")
    ap.add_argument("--tickers", nargs="+", help="limit to these 6-digit stock codes")
    ap.add_argument("--budget", type=int, help="stop after this many API calls")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    last = {"msg": None}

    def progress(done, total, msg):
        head = msg.split(" ")[0]
        if args.verbose or head != last["msg"] or done % 200 == 0:
            print(f"[{done}/{total}] {msg}", flush=True)
            last["msg"] = head

    trigger = "scheduler" if args.mode == "auto" else "cli"
    res = run_sync(args.mode, trigger, budget=args.budget, on_progress=progress, tickers=args.tickers)
    print(f"status={res.status} run={res.run_id} calls={res.calls} new_filings={res.filings_added} "
          f"periods={res.periods_fetched} pending={res.pending_left}" + (f" error={res.error}" if res.error else ""))
    return 0 if res.status in ("done", "partial", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
