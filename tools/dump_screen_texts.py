# -*- coding: utf-8 -*-
"""读取当前设备屏幕上的文本(**只读**: 只 dump 界面层级, 不点击/不输入/不改状态)。

排查"为什么某个文本读不到"时用:
    python tools/dump_screen_texts.py                 # 列出全部可见文本
    python tools/dump_screen_texts.py 吸尘 清洁        # 只看含这些关键词的

输出分两部分:
    1) text 属性里的文本(断言/前置的 textContains 匹配的就是它)
    2) content-desc 属性(无障碍描述, 有些控件把文案放这里, text 是空的)
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uiautomator2 as u2            # noqa: E402

from core import app_detect          # noqa: E402
from core.driver import load_config  # noqa: E402
from core.logger import get_logger   # noqa: E402
from core import session as _session  # noqa: E402

log = get_logger()


def main(argv):
    kws = [k for k in argv if k.strip()]
    cfg = load_config()
    app_detect.ensure_connected(cfg)
    dev_id = _session.get_device_id(cfg, "auto")
    print(f"设备: {dev_id}")
    d = u2.connect(dev_id)

    root = ET.fromstring(d.dump_hierarchy())
    texts, descs = [], []
    for node in root.iter():
        t = (node.get("text") or "").strip()
        c = (node.get("content-desc") or "").strip()
        if t:
            texts.append(t)
        if c and c != t:
            descs.append(c)

    def show(title, items):
        uniq = sorted(set(items))
        if kws:
            uniq = [x for x in uniq if any(k in x for k in kws)]
        print(f"\n=== {title}: {len(uniq)} 条{' (已按关键词过滤)' if kws else ''} ===")
        for x in uniq:
            print(f"  {x!r}")

    show("text 属性(断言/前置 textContains 匹配的就是它)", texts)
    show("content-desc 属性", descs)
    if kws:
        hit_t = [x for x in set(texts) if any(k in x for k in kws)]
        hit_d = [x for x in set(descs) if any(k in x for k in kws)]
        print(f"\n结论: text 命中 {len(hit_t)} 条 / content-desc 命中 {len(hit_d)} 条")
        if not hit_t and not hit_d:
            print("→ 该关键词在无障碍树里完全没有: 大概率是 Canvas/自绘/图片文字, "
                  "uiautomator 读不到, 需要改用模板图匹配(click_template/断言图片)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
