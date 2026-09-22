# -*- coding: utf-8 -*-
"""一次性迁移: RunWorker 组传播 + click 字段模板下拉。用后删除。"""

# ── 1) runner_thread.py: 每条用例开始时设置模板组 ──
p = "gui/runner_thread.py"
s = open(p, encoding="utf-8").read()
old = """                    self.status.emit(f"[第{rnd + 1}/{total_rounds}轮] 执行用例: {case_name}")
                    self.case_started.emit(fp, case_name)   # 用例列表状态灯 → 黄"""
assert old in s, "worker case_started"
new = """                    self.status.emit(f"[第{rnd + 1}/{total_rounds}轮] 执行用例: {case_name}")
                    self.case_started.emit(fp, case_name)   # 用例列表状态灯 → 黄
                    from core import vision as _vision
                    _vision.set_template_app_group(
                        os.path.basename(os.path.dirname(fp)))   # 模板按 APP 组子目录"""
s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="").write(s)
print("worker ok")

# ── 2) schema: click 字段 type → template ──
p = "gui/schema.py"
s = open(p, encoding="utf-8").read()
import re
m = re.search(r'\{"key": "click", "label": "点击目标", "type": "text"', s)
if not m:
    # 找 click 动作的字段定义
    idx = s.index('"key": "click"')
    seg = s[idx:idx + 400]
    print("click def:", seg[:300], file=__import__("sys").stderr)
else:
    s = s.replace('"key": "click", "label": "点击目标", "type": "text"',
                  '"key": "click", "label": "点击模板", "type": "template"')
    open(p, "w", encoding="utf-8", newline="").write(s)
    print("schema click ok")
