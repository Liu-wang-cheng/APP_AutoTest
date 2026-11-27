# -*- coding: utf-8 -*-
"""连续续航测试：多个模式/挡位按顺序跑，每个组合等满电后开始。

示例:
  python tools/endurance_suite.py 192.168.10.21
  python tools/endurance_suite.py 192.168.10.21 \\
      --combos "sweepOnly:4:0,sweepMop:4:3"
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.endurance import (  # noqa: E402
    DEFAULT_COMBOS, _acquire_lock, _release_lock, parse_combos,
    run_endurance_suite)


def main():
    p = argparse.ArgumentParser(description="连续续航测试")
    p.add_argument("ip")
    p.add_argument("--combos", default="",
                   help="mode:fan[:water] 逗号分隔；留空用默认组合")
    p.add_argument("--low-battery", type=int, default=20)
    p.add_argument("--sample-interval", type=float, default=15.0)
    p.add_argument("--stop-on-fail", action="store_true")
    p.add_argument("--max-duration", type=int, default=8 * 3600,
                   help="每个组合的超时秒数(默认 8 小时)；总超时 = 它 × 组合数")
    p.add_argument("--shutdown-after", action="store_true",
                   help="所有组合跑完后，从低电回充持续监测到低电关机")
    p.add_argument("--no-report", action="store_true")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    combos = parse_combos(args.combos) or list(DEFAULT_COMBOS)
    print("组合:", combos)
    lock_fd = _acquire_lock()
    try:
        entries, base, report = run_endurance_suite(
            args.ip, combos=combos, out_dir=args.out,
            stop_on_fail=args.stop_on_fail, report=not args.no_report,
            shutdown_after=args.shutdown_after,
            shutdown_kwargs={"low_battery": args.low_battery},
            low_battery=args.low_battery,
            sample_interval=args.sample_interval,
            max_duration=args.max_duration)
    finally:
        _release_lock(lock_fd)
    print("SUITE_DIR", base)
    for e in entries:
        print("%s fan=%s water=%s %s %s"
              % (e["mode"], e["fan"], e["water"],
                 "PASS" if e["completed"] else "FAIL", e["reason"]))
    print("REPORT", report or "-")
    return 0 if all(e["completed"] for e in entries) else 1


if __name__ == "__main__":
    sys.exit(main())
