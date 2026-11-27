# -*- coding: utf-8 -*-
"""低电回充 → 低电关机续航测试（电量 ≤20% 起，保持回充直到 <5% 关机）。

示例:
  python tools/endurance_shutdown.py 192.168.10.21
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.endurance import (  # noqa: E402
    _acquire_lock, _release_lock, run_shutdown_endurance)


def main():
    p = argparse.ArgumentParser(description="低电至关机续航测试")
    p.add_argument("ip")
    p.add_argument("--low-battery", type=int, default=20)
    p.add_argument("--shutdown-battery", type=int, default=5)
    p.add_argument("--sample-interval", type=float, default=15.0)
    p.add_argument("--wait-low-timeout", type=int, default=1800)
    p.add_argument("--max-duration", type=int, default=8 * 3600)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    lock_fd = _acquire_lock()
    try:
        result, base = run_shutdown_endurance(
            args.ip, low_battery=args.low_battery,
            shutdown_battery=args.shutdown_battery,
            sample_interval=args.sample_interval,
            wait_low_timeout=args.wait_low_timeout,
            max_duration=args.max_duration, out_dir=args.out)
    finally:
        _release_lock(lock_fd)
    print("RESULT_DIR", base)
    print("completed", result.get("completed"),
          "shutdown_seconds", int(result.get("shutdown_seconds", 0)),
          "reason", result.get("end_reason"))
    return 0 if result.get("completed") else 1


if __name__ == "__main__":
    sys.exit(main())
