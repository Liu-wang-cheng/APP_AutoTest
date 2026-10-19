# -*- coding: utf-8 -*-
"""选择器健康检查（P0）: 用例/前置/配置里的 UI 文本，在 APK 里还找不找得到。

背景(2026-10-19):
  AutoTest 的断言/点击大量依赖 APP 界面文案（"正在清洁"、"打扫报告"、"今天/昨天"…）。
  APP 一升级，文案或插件页一变，用例要等到真机跑起来才失败，冒烟一轮 10 分钟白跑。
  这个脚本不碰设备、不执行 APP：直接扫 APK 的 DEX / resources.arsc / assets(JS/HTML)，
  把仓库里用到的文本选择器逐个核对，并与基线比较 —— 消失的旧选择器 = 回归，提前报错。

用法:
  python tools/selector_health.py --apk app.apk \\
      --baseline tools/selector_baseline.json --json reports/selector-health.json
  python tools/selector_health.py --apk app.apk --write-baseline tools/selector_baseline.json

退出码:
  0 = 无回归（未找到的会列在 unverifiable，供人判断是否服务端/WebView 动态文案）
  1 = 有回归（基线里能找到、当前 APK 找不到）或 --strict 下有缺失
  2 = 用法/APK 读取错误

注意:
  · 只做"文本是否出现在 APK 字节里"的判断，不代表运行时一定可见；
  · WebView 插件页若从服务端下发，文案可能不在 APK 内，会落在 unverifiable；
  · 模板图片(.png) 与坐标(x,y) 不是文本选择器，自动排除。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import datetime

try:
    import yaml
except ImportError:                                   # pragma: no cover
    print("需要 PyYAML: pip install pyyaml", file=sys.stderr)
    raise

# 步骤里承载"文本选择器"的字段（来自 gui/schema.py 的 ACTIONS）
STEP_TEXT_KEYS = (
    "assert", "if", "if not", "click", "long_click", "if_click",
    "find_click", "wait_for", "switch_label", "mode_text", "add_text",
)
# 前置项里的文本字段
PRECONDITION_TEXT_KEYS = ("ready_text", "loading_text", "recharge_text")
# 前置项缺省值(与 core/session.py 保持一致)
PRECONDITION_DEFAULTS = {
    "charging": {"ready_text": "充电"},
    "enter_device": {"ready_text": "设备控制,服务"},
    "map_load": {"ready_text": "地图编辑", "loading_text": "地图正在加载"},
}
SPLIT_RE = re.compile(r"[,，、;；]")
COORD_RE = re.compile(r"^\s*\[?\s*-?\d+\s*,\s*-?\d+")
EXPR_RE = re.compile(r"^\s*电量\s*(>=|<=|==|=|>|<)\s*\d+\s*$")
# ★ 纯数字: `click: 540,660` 会被逗号拆成 "540"/"660"，不能当成文本选择器。
#   "2次"/"50%" 这类带单位的仍保留。
PURE_NUM_RE = re.compile(r"^\s*\d+(\.\d+)?%?\s*$")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")
ASSET_SUFFIXES = (".js", ".mjs", ".html", ".htm", ".json", ".xml", ".txt", ".css", ".vue")
CAP_LOCATIONS = 3


def split_texts(value):
    """把 'a,b、c' / ['a','b'] 统一拆成字符串列表。"""
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(split_texts(v))
        return out
    if not isinstance(value, str):
        return []
    return [p.strip() for p in SPLIT_RE.split(value) if p.strip()]


def clean_selector(text):
    """返回 (kind, text) 或 None；kind 为 'text' / 'resource-id'。"""
    t = (text or "").strip().strip('"\'')
    if t.startswith("="):                              # =整段相等
        t = t[1:].strip()
    if not t or len(t) < 2:
        return None
    low = t.lower()
    if low.endswith(IMAGE_SUFFIXES):
        return None
    if COORD_RE.match(t) or EXPR_RE.match(t) or PURE_NUM_RE.match(t):
        return None
    if low.startswith(("http://", "https://")):
        return None
    if ":id/" in t:                                    # resource-id 选择器
        return ("resource-id", t)
    return ("text", t)


def add_selector(out, text, source):
    cleaned = clean_selector(text)
    if not cleaned:
        return
    kind, t = cleaned
    item = out.setdefault(t, {"kind": kind, "sources": set()})
    # 同一个串既当文本又当 resource-id 时，以 text 为准（更常见）
    if item["kind"] == "resource-id" and kind == "text":
        item["kind"] = "text"
    item["sources"].add(source)


def walk_steps(steps, file_name, path, out):
    for i, step in enumerate(steps or []):
        if not isinstance(step, dict):
            continue
        desc = step.get("desc") or step.get("name") or ""
        base = "%s[%d]" % (path, i)
        label = base + (" (%s)" % desc if desc else "")
        for key in STEP_TEXT_KEYS:
            if key in step:
                for t in split_texts(step[key]):
                    add_selector(out, t, "%s: %s.%s" % (file_name, label, key))
        # if / if not 的 then/else 是嵌套步骤
        walk_steps(step.get("then"), file_name, label + ".then", out)
        walk_steps(step.get("else"), file_name, label + ".else", out)


def collect_preconditions(items, file_name, out, path="preconditions"):
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        typ = item.get("type") or ""
        label = "%s[%d]" % (path, i) + (" (%s)" % typ if typ else "")
        defaults = PRECONDITION_DEFAULTS.get(typ, {})
        for key in PRECONDITION_TEXT_KEYS:
            value = item.get(key)
            if not value:
                value = defaults.get(key)
            if value:
                for t in split_texts(value):
                    add_selector(out, t, "%s: %s.%s" % (file_name, label, key))
        if typ == "steps":
            walk_steps(item.get("steps"), file_name, label, out)


def collect_from_repo(repo, config_path=""):
    out = {}
    cases_dir = os.path.join(repo, "Test_cases")
    if os.path.isdir(cases_dir):
        for root, dirs, files in os.walk(cases_dir):
            dirs[:] = [d for d in dirs if d not in ("templates", "screenshots", "__pycache__")]
            for fn in files:
                if not fn.lower().endswith((".yaml", ".yml")):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, repo).replace("\\", "/")
                try:
                    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
                except Exception as e:
                    print("警告: 解析失败 %s: %s" % (rel, e), file=sys.stderr)
                    continue
                for ci, case in enumerate(data.get("cases") or []):
                    if isinstance(case, dict):
                        walk_steps(case.get("steps"), rel, "cases[%d].steps" % ci, out)

    pre_dir = os.path.join(repo, "Test_preconditions")
    if os.path.isdir(pre_dir):
        for fn in sorted(os.listdir(pre_dir)):
            if not fn.lower().endswith((".yaml", ".yml")):
                continue
            path = os.path.join(pre_dir, fn)
            rel = "Test_preconditions/" + fn
            try:
                data = yaml.safe_load(open(path, encoding="utf-8")) or {}
            except Exception as e:
                print("警告: 解析失败 %s: %s" % (rel, e), file=sys.stderr)
                continue
            collect_preconditions(data.get("preconditions"), rel, out)

    if config_path:
        cfg_path = config_path if os.path.isabs(config_path) else os.path.join(repo, config_path)
    else:
        cfg_path = os.path.join(repo, "config", "config.yaml")
        if not os.path.isfile(cfg_path):
            cfg_path = os.path.join(repo, "config", "config.ci.yaml")
    if os.path.isfile(cfg_path):
        try:
            cfg = yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
        except Exception as e:
            print("警告: 解析失败 config: %s" % e, file=sys.stderr)
            cfg = {}
        runner = cfg.get("runner") or {}
        for key in ("idle_texts", "fault_texts"):
            for t in split_texts(runner.get(key)):
                add_selector(out, t, "config/%s: runner.%s" % (os.path.basename(cfg_path), key))
        collect_preconditions(cfg.get("preconditions"), "config/%s" % os.path.basename(cfg_path), out)

    return out


def apk_members(zf):
    """只读可能要查的成员: dex / arsc / manifest / assets 文本。"""
    for name in zf.namelist():
        low = name.lower()
        if low.endswith(".dex") or low in ("resources.arsc", "androidmanifest.xml"):
            yield name
        elif low.startswith("assets/") and low.endswith(ASSET_SUFFIXES):
            yield name
        elif low.startswith("res/raw/") and low.endswith(ASSET_SUFFIXES):
            yield name


def scan_apk(apk_path, selectors):
    found = {t: [] for t in selectors}
    with zipfile.ZipFile(apk_path) as zf:
        for name in apk_members(zf):
            try:
                data = zf.read(name)
            except Exception:
                continue
            is_resource = name.lower() in ("resources.arsc", "androidmanifest.xml")
            for text in selectors:
                if found[text]:
                    continue                       # 已命中，不必再扫
                needles = [text.encode("utf-8")]
                if selectors[text]["kind"] == "resource-id" and ":id/" in text:
                    needles.append(text.split(":id/", 1)[1].encode("utf-8"))
                if is_resource:
                    needles.append(text.encode("utf-16-le"))
                if any(n in data for n in needles):
                    found[text].append(name)
    return found


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_report(apk, selectors, found, baseline_path, baseline):
    items = {}
    regressions, newly_found, unverifiable = [], [], []
    for text, meta in selectors.items():
        locs = found.get(text) or []
        base = (baseline or {}).get(text) or {}
        base_found = bool(base.get("found"))
        items[text] = {
            "kind": meta["kind"],
            "found": bool(locs),
            "locations": locs[:CAP_LOCATIONS],
            "sources": sorted(meta["sources"]),
        }
        if base_found and not locs:
            regressions.append(text)
        elif not base_found and locs:
            newly_found.append(text)
        elif not base_found and not locs:
            unverifiable.append(text)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        # 基线会入库: 只记文件名, 不写本机绝对路径
        "apk": {"path": os.path.basename(apk), "size": os.path.getsize(apk),
                "sha256": sha256_file(apk)},
        "baseline": baseline_path,
        "summary": {
            "selectors": len(selectors),
            "found": sum(1 for x in items.values() if x["found"]),
            "missing": sum(1 for x in items.values() if not x["found"]),
            "regressions": len(regressions),
            "newly_found": len(newly_found),
            "unverifiable": len(unverifiable),
        },
        "regressions": regressions,
        "newly_found": newly_found,
        "unverifiable": unverifiable,
        "selectors": items,
    }


def load_baseline(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        return (json.load(open(path, encoding="utf-8")) or {}).get("selectors") or {}
    except Exception as e:
        print("警告: 基线读取失败 %s: %s" % (path, e), file=sys.stderr)
        return {}


def main(argv=None):
    p = argparse.ArgumentParser(description="UI 文本选择器健康检查(扫 APK, 不碰设备)")
    p.add_argument("--apk", required=True, help="APK 文件路径")
    p.add_argument("--repo", default=".", help="仓库根目录(默认当前目录)")
    p.add_argument("--config", default="", help="指定配置文件(默认 config/config.yaml, 其次 config.ci.yaml)")
    p.add_argument("--baseline", default="", help="基线 JSON(上一次结果)")
    p.add_argument("--write-baseline", default="", help="把本次结果写成基线后退出")
    p.add_argument("--json", dest="json_path", default="", help="输出完整 JSON 报告")
    p.add_argument("--strict", action="store_true", help="任何缺失都判失败(默认只对回归判失败)")
    p.add_argument("--allow-regressions", action="store_true", help="有回归也返回 0(仅报告)")
    args = p.parse_args(argv)

    if not os.path.isfile(args.apk):
        print("APK 不存在: %s" % args.apk, file=sys.stderr)
        return 2
    try:
        selectors = collect_from_repo(os.path.abspath(args.repo), args.config)
        found = scan_apk(args.apk, selectors)
    except (zipfile.BadZipFile, OSError) as e:
        print("APK 读取失败: %s" % e, file=sys.stderr)
        return 2

    baseline = load_baseline(args.baseline)
    report = build_report(args.apk, selectors, found, args.baseline, baseline)

    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)), exist_ok=True)
        json.dump(report, open(args.json_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    if args.write_baseline:
        os.makedirs(os.path.dirname(os.path.abspath(args.write_baseline)), exist_ok=True)
        json.dump(report, open(args.write_baseline, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("基线已写入: %s" % args.write_baseline)
        print("  APK: %s (%d bytes)" % (args.apk, report["apk"]["size"]))
        print("  选择器 %d 个: 命中 %d / 未找到 %d" % (
            report["summary"]["selectors"], report["summary"]["found"],
            report["summary"]["missing"]))
        return 0

    s = report["summary"]
    print("APK: %s (%d bytes, sha256 %s)" % (
        args.apk, report["apk"]["size"], report["apk"]["sha256"][:16]))
    print("选择器 %d 个: 命中 %d / 缺失 %d / 回归 %d / 新增命中 %d / 无法判定 %d" % (
        s["selectors"], s["found"], s["missing"], s["regressions"],
        s["newly_found"], s["unverifiable"]))

    if report["regressions"]:
        print("\n[回归] 基线里有、当前 APK 找不到:")
        for t in report["regressions"][:50]:
            for src in report["selectors"][t]["sources"][:3]:
                print("  - %s  <- %s" % (t, src))
    if report["unverifiable"]:
        print("\n[无法判定] 基线也没有(可能是服务端/WebView 动态文案或本次新加):")
        for t in report["unverifiable"][:30]:
            print("  - %s" % t)
        if len(report["unverifiable"]) > 30:
            print("  ... 共 %d 个，详见 JSON" % len(report["unverifiable"]))
    if report["newly_found"]:
        print("\n[新增命中] %d 个: %s%s" % (
            len(report["newly_found"]), ", ".join(report["newly_found"][:10]),
            " ..." if len(report["newly_found"]) > 10 else ""))

    if report["regressions"] and not args.allow_regressions:
        return 1
    if args.strict and report["unverifiable"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
