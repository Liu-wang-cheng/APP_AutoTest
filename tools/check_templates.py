# -*- coding: utf-8 -*-
"""列出用例引用的模板图清单,并检查 Test_img/templates/ 里是否齐全。

    python tools/check_templates.py                      # 扫 Test_cases/ 全部文件
    python tools/check_templates.py Test_cases/x.yaml    # 只扫指定文件

模板图是从真机截图里裁剪出来的 UI 元素小图(如 开始清扫.png),SIFT 靠它定位。
缺图不会让静态校验失败,但会在真机跑到那一步报"未匹配到模板图",往往跑一半才炸。

用例里 screenshots/ 开头的路径是**运行截图**(引擎自动保存),不是模板图,不计入。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.driver import BASE_DIR, load_yaml_file
from tests.test_yaml_runner import iter_all_steps, iter_modules

TEMPLATE_DIR = os.path.join(BASE_DIR, "Test_img", "templates")

# 这些键的值可能是模板图名
IMAGE_KEYS = ("click", "click_template", "long_click", "assert", "wait_for", "compare", "diff",
              "find_click", "if_click", "if", "if not", "screenshot",
              "switch_tpl", "add_tpl")
META = {"desc", "wait", "timeout", "retry", "threshold", "duration", "circular",
        "switch_area", "switch_label", "else", "wait_after"}


def collect_template_refs(only_file=None):
    """→ {模板图名: {"cases": set(用例), "descs": [步骤描述...]}}"""
    refs = {}
    cases_dir = os.path.join(BASE_DIR, "Test_cases")
    if only_file:
        files = [os.path.basename(only_file)]
    else:
        if not os.path.isdir(cases_dir):
            return refs
        files = []
        for sub in sorted(os.listdir(cases_dir)):
            sub_full = os.path.join(cases_dir, sub)
            if os.path.isdir(sub_full):
                files += [os.path.join(sub, f) for f in sorted(os.listdir(sub_full))
                          if f.endswith((".yaml", ".yml"))]
            elif sub.endswith((".yaml", ".yml")):
                files.append(sub)

    for fn in files:
        try:
            data = load_yaml_file(os.path.join(cases_dir, fn))
        except Exception as e:
            print(f"[跳过] {fn}: {e}")
            continue
        for module, cases in iter_modules(data, fn):
            for case in cases:
                label = f"{module}/{case.get('name', '')}"
                for step, _depth in iter_all_steps(case.get("steps") or []):
                    for key, val in step.items():
                        if key in META or key not in IMAGE_KEYS:
                            continue
                        for item in (val if isinstance(val, list) else [val]):
                            if not isinstance(item, str):
                                continue
                            s = item.strip()
                            # 模板图 = .png 结尾,且不是运行截图、不含路径分隔符
                            if (s.lower().endswith(".png")
                                    and not s.startswith("screenshots/")
                                    and "/" not in s and "\\" not in s):
                                rec = refs.setdefault(s, {"cases": set(), "descs": []})
                                rec["cases"].add(label)
                                desc = str(step.get("desc", "")).strip()
                                if desc and desc not in rec["descs"]:
                                    rec["descs"].append(desc)
    return refs


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    refs = collect_template_refs(only)
    if not refs:
        print("用例里没有引用模板图")
        return 0

    from core import vision
    prefix = vision._template_prefix()
    # ★ 模板已按 APP 组子目录存放(Test_cases/<组>/templates/), 递归收集;
    #   比对统一用「去前缀名」(与用例引用名一致)
    have = set()
    # ★ 模板在 Test_cases/<组>/templates/ 下, 兼容全局 Test_img/templates/
    cases_root = os.path.join(BASE_DIR, "Test_cases")
    for walk_root in (cases_root, TEMPLATE_DIR):
        if not os.path.isdir(walk_root):
            continue
        for root_d, _dirs, fnames in os.walk(walk_root):
            for f in fnames:
                if f.lower().endswith(".png"):
                    base = os.path.splitext(f)[0]
                    if prefix and base.startswith(prefix + "_"):
                        base = base[len(prefix) + 1:]
                    have.add(base + ".png")

    missing = sorted(n for n in refs if n not in have)
    unused = sorted(have - {n for n in refs})

    scope = only or "Test_cases/ 全部文件"
    print(f"扫描范围: {scope}")
    print(f"用例共引用 {len(refs)} 张模板图, Test_img/templates/ 中已有 {len(have)} 张"
          + (f" (前缀: {prefix})" if prefix else "") + "\n")

    print("=" * 68)
    print("模板图清单" + (f" —— 缺 {len(missing)} 张" if missing else " —— 已齐全"))
    print("=" * 68)
    for name in sorted(refs):
        mark = " " if name in have else "✗"
        rec = refs[name]
        print(f"\n[{mark}] {name}   (被 {len(rec['cases'])} 个用例引用)")
        for desc in rec["descs"][:3]:
            print(f"      用于: {desc}")

    if unused:
        print(f"\n目录中 {len(unused)} 张图未被任何用例引用:")
        for name in unused:
            print(f"  - {name}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
