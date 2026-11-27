# -*- coding: utf-8 -*-
"""命令行跑一次续航测试（满电 → 低电回充）。

示例:
  python tools/endurance_run.py 192.168.10.21 --mode sweepOnly --fan 4
"""
import argparse
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.endurance import _acquire_lock, _release_lock, run_endurance  # noqa: E402


def main():
    p = argparse.ArgumentParser(description="续航测试")
    p.add_argument("ip")
    p.add_argument("--mode", default="sweepOnly",
                   choices=["sweepOnly", "sweepMop", "mopOnly"])
    p.add_argument("--fan", type=int, default=4)
    p.add_argument("--water", type=int, default=0)
    p.add_argument("--low-battery", type=int, default=20)
    p.add_argument("--sample-interval", type=float, default=15.0)
    p.add_argument("--out", default=None)
    p.add_argument("--excel", action="store_true",
                   help="跑完自动生成 Excel 报告（与 result.json 同源）")
    args = p.parse_args()
    # 单实例：后台启动器可能同时拉起两份，两个 APOS 客户端会互相干扰
    lock_fd = _acquire_lock()
    try:
        result, base = run_endurance(
            args.ip, mode=args.mode, fan_level=args.fan,
            water_level=args.water, low_battery=args.low_battery,
            sample_interval=args.sample_interval, out_dir=args.out)
    finally:
        _release_lock(lock_fd)
    print("RESULT_DIR", base)
    print("completed", result.get("completed"),
          "clean_seconds", int(result.get("clean_seconds", 0)),
          "reason", result.get("end_reason"))
    if args.excel:
        report_tool = os.path.join(BASE, "tools", "endurance_report.py")
        xlsx = os.path.join(
            os.path.dirname(os.path.abspath(base)),
            "续航测试_%s.xlsx" % time.strftime("%Y%m%d_%H%M%S"))
        proc = subprocess.run([sys.executable, report_tool, base,
                               "--out", xlsx], capture_output=True, text=True)
        if proc.returncode == 0:
            print("EXCEL", proc.stdout.strip() or xlsx)
        else:
            print("EXCEL_FAILED", (proc.stderr or proc.stdout).strip())
    return 0 if result.get("completed") else 1


if __name__ == "__main__":
    sys.exit(main())
