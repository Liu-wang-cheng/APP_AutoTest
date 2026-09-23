# -*- coding: utf-8 -*-
"""一次性迁移: 点击拆成 click(手填)/click_template(下拉选模板)两个动作。用后删除。"""

# ── 1) schema: click 恢复 text; 新增 click_template ──
p = "gui/schema.py"
s = open(p, encoding="utf-8").read()
old = '''    {"key": "click", "label": "点击", "category": "操作", "fields": [
        {"key": "click", "label": "点击模板", "type": "template", "required": True,
         "hint": "从当前 APP 组的模板中选择"},
    ]},'''
assert old in s, "click def"
new = '''    {"key": "click", "label": "点击", "category": "操作", "fields": [
        {"key": "click", "label": "目标", "type": "text", "required": True,
         "hint": "按钮名 / 图片名(.png) / x,y 坐标"},
    ]},
    {"key": "click_template", "label": "点击模板", "category": "操作", "fields": [
        {"key": "click_template", "label": "模板", "type": "template", "required": True,
         "hint": "从当前 APP 组的模板中选择"},
    ]},'''
s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="").write(s)
print("schema ok")

# ── 2) 引擎: click_template 复用 do_click 的图片分支 ──
p = "core/actions/basic.py"
s = open(p, encoding="utf-8").read()
old = '''@reg.action("long_click", priority=45)'''
assert old in s
new = '''@reg.action("click_template", priority=46)
def do_click_template(runner, step):
    """点击模板: 值 = 当前 APP 组的模板名(下拉选择), 按模板匹配点击"""
    value = step["click_template"]
    timeout = step.get("timeout", runner.click_timeout)
    runner._click_by_template(value, timeout)


@reg.action("long_click", priority=45)'''
s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="").write(s)
print("engine ok")

# ── 3) GUI: 移除勾选组合控件, template 类型 → 纯下拉 ──
p = "gui/main_window.py"
s = open(p, encoding="utf-8").read()
# 3a) 删除 _TemplateField 类
start = s.index("class _TemplateField(QWidget):")
end = s.index("def _make_field_widget(")
block = s[start:end]
assert "class _TemplateField" in block and "_make_field_widget" in block
s = s.replace(block, "", 1)
# 3b) template 分支 → 纯下拉(editable, 自动列模板)
old = '''    if t == "template":
        from core import vision
        w = _TemplateField(vision.list_templates(), str(value) if value else "")
        return w, w.current_value
'''
assert old in s, "template branch"
new = '''    if t == "template":
        from core import vision
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(vision.list_templates())
        if value:
            combo.setCurrentText(str(value))
        combo.lineEdit().setPlaceholderText(hint or "选择模板")
        return combo, combo.currentText
'''
s = s.replace(old, new)
open(p, "w", encoding="utf-8", newline="").write(s)
print("gui ok")
