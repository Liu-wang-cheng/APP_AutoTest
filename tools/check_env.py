# -*- coding: utf-8 -*-
"""核对 venv 里的依赖与 requirements.txt 是否一致。

    python tools/check_env.py

分步安装时很容易漏掉一两个包(尤其是大包被中断的情况),而缺包往往到真机
跑起来才报 ImportError。这里一次列全。
"""
import importlib.metadata as md
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.driver import BASE_DIR

REQ = os.path.join(BASE_DIR, "requirements.txt")


def parse_requirements(path):
    """→ {包名: 期望版本}; 忽略注释与空行"""
    want = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*(\S+)$", line)
            if m:
                want[m.group(1)] = m.group(2)
    return want


def main():
    if not os.path.exists(REQ):
        print(f"找不到 {REQ}")
        return 1
    want = parse_requirements(REQ)
    ok, mismatched, missing = 0, [], []

    for pkg, ver in want.items():
        try:
            have = md.version(pkg)
        except md.PackageNotFoundError:
            missing.append((pkg, ver))
            continue
        if have == ver:
            ok += 1
        else:
            mismatched.append((pkg, ver, have))

    print(f"requirements.txt 共 {len(want)} 项 —— 一致 {ok}, "
          f"版本不符 {len(mismatched)}, 缺失 {len(missing)}\n")
    for pkg, ver, have in mismatched:
        print(f"  [版本不符] {pkg:<16} 已装 {have:<12} 期望 {ver}")
    for pkg, ver in missing:
        print(f"  [缺失]     {pkg:<16} 期望 {ver}")
    if not mismatched and not missing:
        print("依赖环境一致")
    return 1 if (mismatched or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
