# -*- coding: utf-8 -*-
"""P1 运行时页面文本快照 + 跨版本 diff（补 APK 健康检查覆盖不到的动态文案）。

⚠ 仅用于 CI/测试：不被 core/gui 引用、不参与打包。
   当前页面路径（设备页 → 菜单 → 打扫报告）是 SmartThings 专用；换 APP 需改导航/页面配置。

为什么需要它(2026-10-19):
  P0 的 selector_health 只能核对**编进 APK**的文案；SmartThings 的设备插件/WebView
  页面是运行时下发的（"打扫报告"、"正在干燥拖布" 等在 base.apk 里 0 命中）。
  本工具在真机上把关键页面实际显示的文字（无障碍树 + OCR）存成快照，并与基线 diff：
  基线有、现在没有 = 回归，提前发现插件页/服务端文案变化。

页面与动作:
  device : 进入设备页后立即快照
  menu   : 点「菜单」后快照
  report : 点「打扫报告」后快照
  (每一步失败可用 --lenient 跳过；默认失败)

用法:
  python tools/runtime_snapshot.py --device 127.0.0.1:7555 ^
      --device-name "扫地机器人 0050" --snapshot tools/runtime_baseline.json
  python tools/runtime_snapshot.py --device 127.0.0.1:7555 ^
      --snapshot reports/runtime-snapshot.json --diff tools/runtime_baseline.json

退出码:
  0 = 无回归（或有 --allow-regressions）
  1 = 有回归（基线快照里有、当前页面没有的文本）
  2 = 设备/页面错误
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_PAGES = ("device", "menu", "report")
TIME_RE = re.compile(r"\d{1,2}:\d{2}")
NUM_RE = re.compile(r"\d+(\.\d+)?")
PUNCT_ONLY_RE = re.compile(r"^[#\W_]+$", re.UNICODE)


def normalize_text(text):
    """把时间/数字归一成 #，便于跨版本比较；
    纯数字/纯符号的文本返回空串（不作为选择器）。"""
    t = (text or "").strip()
    if not t:
        return ""
    t = TIME_RE.sub("#", t)
    t = NUM_RE.sub("#", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or PUNCT_ONLY_RE.match(t):
        return ""
    return t


def parse_xml(xml, package=""):
    """从 uiautomator 层级 XML 里取文本/描述/resource-id（只取可见且属于被测 APP 的节点）。"""
    import xml.etree.ElementTree as ET

    texts, rids = [], []
    try:
        root = ET.fromstring(xml)
    except Exception:
        return texts, rids
    for node in root.iter():
        pkg = node.get("package") or ""
        if package and pkg and pkg != package:
            continue
        b = node.get("bounds") or ""
        if b.startswith("[0,0][0,0]"):
            continue
        t = (node.get("text") or "").strip()
        d = (node.get("content-desc") or "").strip()
        rid = (node.get("resource-id") or "").strip()
        for v in (t, d):
            if v:
                texts.append(v)
        if rid and (t or d):
            rids.append(rid)
    return texts, rids


def capture_page(d, package, no_ocr=False):
    """当前页面的文本快照: 无障碍树 + OCR（插件页树稀疏时 OCR 很关键）。"""
    from core import ocr
    from core.sentinel import system_ui_regions
    from core.session import warm_webview

    xml = warm_webview(d)
    texts, rids = parse_xml(xml, package)
    ocr_used = False
    if not no_ocr:
        try:
            for t, _pos in ocr.screen_texts(d, exclude=system_ui_regions(xml)):
                if t:
                    texts.append(t.strip())
                    ocr_used = True
        except Exception as e:                       # OCR 失败不影响树文本
            print("  [警告] OCR 失败: %s" % e, file=sys.stderr)
    # 去重但保序
    seen, raw = set(), []
    for t in texts:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            raw.append(t)
    norm = []
    seen_n = set()
    for t in raw:
        n = normalize_text(t)
        if n and n not in seen_n:
            seen_n.add(n)
            norm.append(n)
    return {"texts": raw, "normalized": norm, "resource_ids": sorted(set(rids)),
            "ocr_used": ocr_used, "error": None}


def click_text(d, name, timeout=10):
    try:
        if d(text=name).exists(timeout=timeout):
            d(text=name).click()
            return True
        if d(textContains=name).exists(timeout=1):
            d(textContains=name).click()
            return True
    except Exception as e:
        print("  [警告] 点击「%s」失败: %s" % (name, e), file=sys.stderr)
    return False


def capture(d, package, pages, settle, no_ocr, lenient, device_name):
    from core.session import ensure_device_page

    snap = {"captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "package": package, "device_name": device_name, "pages": {}}

    ok = ensure_device_page(d, device_name, timeout=20, rounds=2)
    if not ok:
        msg = "未能确认进入设备页"
        if not lenient:
            raise RuntimeError(msg)
        print("  [警告] %s，按当前页面快照" % msg, file=sys.stderr)
    time.sleep(settle)
    snap["pages"]["device"] = capture_page(d, package, no_ocr)

    if "menu" in pages:
        if click_text(d, "菜单"):
            time.sleep(settle)
            snap["pages"]["menu"] = capture_page(d, package, no_ocr)
        else:
            err = "点击「菜单」失败"
            if not lenient:
                raise RuntimeError(err)
            snap["pages"]["menu"] = {"texts": [], "normalized": [], "resource_ids": [],
                                     "ocr_used": False, "error": err}

    if "report" in pages:
        if click_text(d, "打扫报告"):
            time.sleep(settle)
            snap["pages"]["report"] = capture_page(d, package, no_ocr)
        else:
            err = "点击「打扫报告」失败"
            if not lenient:
                raise RuntimeError(err)
            snap["pages"]["report"] = {"texts": [], "normalized": [], "resource_ids": [],
                                       "ocr_used": False, "error": err}
    return snap


def diff_snapshots(baseline, current):
    """逐页比较 normalized 文本集合；页面本身出错(点击失败)则跳过，避免误报全删。"""
    out = {}
    pages = sorted(set((baseline or {}).get("pages", {})) | set((current or {}).get("pages", {})))
    for page in pages:
        b = (baseline or {}).get("pages", {}).get(page) or {}
        c = (current or {}).get("pages", {}).get(page) or {}
        if b.get("error") or c.get("error"):
            out[page] = {"skipped": True, "reason": c.get("error") or b.get("error"),
                         "removed": [], "added": []}
            continue
        bt = set(b.get("normalized") or [])
        ct = set(c.get("normalized") or [])
        out[page] = {"skipped": False, "reason": None,
                     "removed": sorted(bt - ct), "added": sorted(ct - bt)}
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="运行时页面文本快照 + 跨版本 diff")
    p.add_argument("--device", default="", help="设备ID(默认读 config/device.default 或 SMOKE_DEVICE)")
    p.add_argument("--device-name", default="", help="APP 设备列表里的名字(默认 SMOKE_DEVICE_NAME/config.target_device)")
    p.add_argument("--package", default="", help="APP 包名(默认 config app.package)")
    p.add_argument("--pages", default=",".join(DEFAULT_PAGES), help="要快照的页面: device,menu,report")
    p.add_argument("--settle", type=float, default=2.0, help="每页加载等待(秒)")
    p.add_argument("--no-ocr", action="store_true", help="不做 OCR(更快, 但插件页可能缺文本)")
    p.add_argument("--lenient", action="store_true", help="某页不可达时继续(记录 error)")
    p.add_argument("--keep-open", action="store_true", help="结束后不停止 APP")
    p.add_argument("--snapshot", required=True, help="本次快照输出路径")
    p.add_argument("--diff", default="", help="基线快照，做 diff")
    p.add_argument("--json", dest="json_path", default="", help="diff 报告输出路径")
    p.add_argument("--allow-regressions", action="store_true", help="有回归也返回 0(仅报告)")
    args = p.parse_args(argv)

    from core.driver import load_config

    cfg = load_config()
    device = args.device or os.environ.get("SMOKE_DEVICE") or \
        (cfg.get("device") or {}).get("default") or "127.0.0.1:7555"
    package = args.package or (cfg.get("app") or {}).get("package") or ""
    device_name = args.device_name or os.environ.get("SMOKE_DEVICE_NAME") or cfg.get("target_device") or ""
    pages = [x.strip() for x in args.pages.split(",") if x.strip()]

    import uiautomator2 as u2
    d = u2.connect(device)
    d.implicitly_wait(10)
    print("设备 %s | 包 %s | 设备名 %s | 页面 %s" % (device, package, device_name, pages))
    d.app_start(package)
    time.sleep(3)

    try:
        snap = capture(d, package, pages, args.settle, args.no_ocr, args.lenient, device_name)
    except Exception as e:
        print("采集失败: %s" % e, file=sys.stderr)
        return 2
    finally:
        if not args.keep_open:
            try:
                d.app_stop(package)
            except Exception:
                pass

    os.makedirs(os.path.dirname(os.path.abspath(args.snapshot)), exist_ok=True)
    json.dump(snap, open(args.snapshot, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n快照: %s" % args.snapshot)
    for page in pages:
        data = snap["pages"].get(page) or {}
        if data.get("error"):
            print("  %-6s 错误: %s" % (page, data["error"]))
        else:
            print("  %-6s 文本 %d 条 (归一后 %d), OCR=%s, resource-id %d 个" % (
                page, len(data.get("texts") or []), len(data.get("normalized") or []),
                data.get("ocr_used"), len(data.get("resource_ids") or [])))
            for t in (data.get("texts") or [])[:6]:
                print("         - %s" % t[:80])

    if not args.diff:
        return 0
    if not os.path.isfile(args.diff):
        print("基线不存在: %s" % args.diff, file=sys.stderr)
        return 2
    baseline = json.load(open(args.diff, encoding="utf-8"))
    result = diff_snapshots(baseline, snap)
    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)), exist_ok=True)
        json.dump({"baseline": args.diff, "snapshot": args.snapshot, "pages": result},
                  open(args.json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    regressions = 0
    print("\n=== 与基线 diff ===")
    for page in sorted(result):
        r = result[page]
        if r["skipped"]:
            print("  %-6s 跳过(%s)" % (page, r["reason"]))
            continue
        if r["removed"]:
            regressions += len(r["removed"])
            print("  %-6s [回归] %d 条:" % (page, len(r["removed"])))
            for t in r["removed"][:20]:
                print("         - %s" % t)
        if r["added"]:
            print("  %-6s [新增] %d 条: %s%s" % (
                page, len(r["added"]), ", ".join(r["added"][:8]),
                " ..." if len(r["added"]) > 8 else ""))
        if not r["removed"] and not r["added"]:
            print("  %-6s 无变化" % page)
    if regressions:
        print("\n回归总数: %d" % regressions)
        return 0 if args.allow_regressions else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
