# -*- coding: utf-8 -*-
"""前置条件: 可编辑/可新增(列表驱动) + 配置读写 + 通用文本检查。

2026-09-23 用户要求: 前置条件不止可选, 还要能改参数、能新增;
新增项要填「名称」和「操作内容」(检测文本→执行操作)。
"""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import session  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeDev:
    """可控设备桩: present 里的文本视为"存在", click/press 记录调用"""

    def __init__(self, present=()):
        self.present = set(present)
        self.pressed = []
        self.clicked = []

    def __call__(self, **kw):
        owner = self
        key = next((k for k in ("textContains", "text", "textMatches", "description")
                    if k in kw), None)

        class R:
            def exists(self, timeout=1):
                if key is None:
                    return False
                if key == "textMatches":     # 多文本走正则(见 session._any_present)
                    return any(re.search(kw[key], t) for t in owner.present)
                return kw[key] in owner.present

            def click(self):
                owner.clicked.append(kw.get(key))

        return R()

    def click(self, x, y):
        """设备级点击(坐标直点, 如前置条件「点击空白处」)"""
        self.clicked_at = (x, y)

    def press(self, key):
        self.pressed.append(key)

    def app_stop(self, pkg):
        pass

    def dump_hierarchy(self):
        # 多文本判定是读层级里 text 的子串(见 session._any_present) → 桩要能"看见" present
        return "".join(f'<node text="{p}"/>' for p in sorted(self.present))


# ── 配置读写(注释/换行符保留) ──

def test_save_preconditions_keeps_comments_and_crlf(tmp_path):
    """写前置条件段: 保留注释、保留 CRLF、不动其它键"""
    from core.driver import load_preconditions, save_preconditions
    p = tmp_path / "config.yaml"
    p.write_bytes("# 顶部注释\r\napp:\r\n  name: 涂鸦智能\r\n\r\ntarget_device: 111\r\n"
                  .encode("utf-8"))
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 80}]
    save_preconditions(items, str(p))
    text = p.read_bytes().decode("utf-8")
    assert "# 顶部注释" in text and "\r\n" in text, "注释与 CRLF 必须保留"
    assert "target_device: 111" in text, "其它键不受影响"
    assert load_preconditions(str(p)) == items


def test_save_preconditions_replaces_existing_section(tmp_path):
    """已有 preconditions 段 → 整段替换, 不重复追加"""
    from core.driver import load_preconditions, save_preconditions
    p = tmp_path / "config.yaml"
    p.write_text("app:\n  name: x\npreconditions:\n- type: restart\n  enabled: true\n"
                 "target_device: y\n", encoding="utf-8")
    save_preconditions([{"type": "charging", "enabled": True}], str(p))
    text = p.read_text(encoding="utf-8")
    assert text.count("preconditions:") == 1, "不能重复插入"
    assert "target_device: y" in text and "app:" in text
    assert load_preconditions(str(p)) == [{"type": "charging", "enabled": True}]


def test_load_preconditions_default_none(tmp_path):
    """未配置返回 None(调用方回退默认值)"""
    from core.driver import load_preconditions
    p = tmp_path / "c.yaml"
    p.write_text("app:\n  name: x\n", encoding="utf-8")
    assert load_preconditions(str(p)) is None


# ── 列表驱动执行 ──

def test_prepare_items_runs_in_order_and_reports(monkeypatch):
    """按列表顺序执行, 结果含 key/desc/ok; 未勾选(disabled)的被跳过"""
    calls = []
    monkeypatch.setattr(session, "restart_app",
                        lambda d, cfg, enter_page=True, should_cancel=None:
                        calls.append("restart"))
    monkeypatch.setattr(session, "ensure_charging",
                        lambda d, **kw: calls.append("charging") or True)
    monkeypatch.setattr(session, "ensure_battery",
                        lambda d, **kw: calls.append("battery") or False)
    items = [
        {"type": "restart", "enabled": True},
        {"type": "charging", "enabled": True},
        {"type": "battery", "enabled": True, "min_level": 80},
        {"type": "map_load", "enabled": False},        # 未勾选 → 跳过
    ]
    res = session.prepare_items(FakeDev(), {"target_device": "SE3L"}, items)
    assert calls == ["restart", "charging", "battery"], "应按顺序执行且跳过未勾选"
    assert len(res) == 3
    assert [r["ok"] for r in res] == [True, True, False]
    assert res[2]["desc"] == "电量门槛≥80%", f"展示名应带参数: {res[2]['desc']}"


def test_item_label_distinguishes_instances():
    """同类型多实例的展示名要能区分(报告里靠它识别)"""
    assert session._item_label({"type": "battery", "min_level": 50}) == "电量门槛≥50%"
    assert session._item_label({"type": "battery", "min_level": 80}) == "电量门槛≥80%"
    a = session._item_label({"type": "text_check", "name": "首页就绪",
                             "wait_text": "首页"})
    assert a == "首页就绪(出现「首页」)", f"实际: {a}"
    assert session._item_label({"type": "restart"}) == "重启 APP"


# ── 通用文本检查(用户新增的自定义前置项) ──

def test_text_check_passes_when_text_appears(monkeypatch):
    """等待文本出现 → 已出现即通过"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=("首页",))
    assert session.ensure_text_check(dev, wait_text="首页", timeout=10) is True


def test_text_check_passes_when_text_absent(monkeypatch):
    """等待文本消失 → 已不存在即通过"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=())          # 页面上没有该文本
    assert session.ensure_text_check(dev, absent_text="加载中", timeout=10) is True


def test_text_check_timeout_runs_configured_action(monkeypatch):
    """超时后按配置执行操作: back=按返回键 / click:文本=点击该文本"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=())          # 目标文本永不出现
    assert session.ensure_text_check(dev, wait_text="永远不出现", timeout=0.01,
                                     on_timeout="back") is False
    assert dev.pressed == ["back"], "超时应按返回键"
    dev2 = FakeDev(present=("重试",))
    session.ensure_text_check(dev2, wait_text="永远不出现", timeout=0.01,
                              on_timeout="click:重试")
    assert dev2.clicked == ["重试"], "超时应点击指定文本"
    dev3 = FakeDev(present=())
    session.ensure_text_check(dev3, wait_text="永不出现", timeout=0.01,
                              on_timeout="none")
    assert not dev3.pressed and not dev3.clicked, "none 不应有任何操作"


def test_text_check_skips_when_no_text_configured(monkeypatch):
    """未填任何文本 → 跳过并视为通过(不阻塞执行)"""
    assert session.ensure_text_check(FakeDev(), name="空检查") is True


# ── 地图加载的判断文本可配置 ──

def test_map_load_custom_texts(monkeypatch):
    """地图加载的就绪/加载中文案可配置(换 APP 时文案不同)"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=("地图就绪了",))
    assert session.ensure_map_loaded(dev, timeout=0.01, rounds=1,
                                     ready_text="地图就绪了",
                                     loading_text="地图渲染中") is True
    dev2 = FakeDev(present=("地图就绪了", "地图渲染中"))     # 仍在加载
    assert session.ensure_map_loaded(dev2, timeout=0.01, rounds=1,
                                     ready_text="地图就绪了",
                                     loading_text="地图渲染中") is False


# ── 多文本判断(同一处文案在不同 APP/机型上不一样) ──

def test_split_texts_separators():
    """一个字段可填多个文本: 逗号/顿号/分号/换行 都能分隔, 空项丢弃"""
    assert session._split_texts("地图编辑,地图") == ["地图编辑", "地图"]
    assert session._split_texts("地图编辑，地图、清扫地图") == ["地图编辑", "地图", "清扫地图"]
    assert session._split_texts("地图编辑;地图\n清扫地图") == ["地图编辑", "地图", "清扫地图"]
    assert session._split_texts("  ") == []
    assert session._split_texts("") == []
    assert session._split_texts(["A", " B "]) == ["A", "B"]      # 已是列表也接受


def test_map_load_multi_texts(monkeypatch):
    """★ 就绪/加载中文本各支持多个: 就绪命中任一即可; 加载中任一仍在 ⇒ 未就绪。

    真机背景: 只填一个「地图编辑」时, 用户设备上 6 轮重进都判定未就绪。
    """
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    # 实际文案是「地图」, 配置里写了两个 → 应判定就绪
    ok = FakeDev(present=("地图",))
    assert session.ensure_map_loaded(ok, timeout=0.01, rounds=1,
                                     ready_text="地图编辑,地图",
                                     loading_text="地图正在加载") is True
    # 就绪文案命中, 但加载中文案命中(多个里任一个) → 仍未就绪
    loading = FakeDev(present=("地图", "加载中"))
    assert session.ensure_map_loaded(loading, timeout=0.01, rounds=1,
                                     ready_text="地图编辑,地图",
                                     loading_text="地图正在加载,加载中") is False
    # 就绪文案一个都没命中 → 未就绪
    miss = FakeDev(present=("首页",))
    assert session.ensure_map_loaded(miss, timeout=0.01, rounds=1,
                                     ready_text="地图编辑,地图",
                                     loading_text="地图正在加载") is False


def test_text_check_multi_texts(monkeypatch):
    """★ 文本检查同样支持多个: 等待出现=任一命中; 等待消失=全部不在"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    assert session.ensure_text_check(FakeDev(present=("主页",)), wait_text="首页,主页",
                                     timeout=0.01) is True
    assert session.ensure_text_check(FakeDev(present=("首页",)), wait_text="首页,主页",
                                     timeout=0.01) is True
    assert session.ensure_text_check(FakeDev(present=("首页",)), wait_text="主页,设置",
                                     timeout=0.01) is False
    # 等待消失: 只要还有任一个在, 就不算通过
    assert session.ensure_text_check(FakeDev(present=("请稍候",)),
                                     absent_text="加载中,请稍候", timeout=0.01) is False
    assert session.ensure_text_check(FakeDev(present=("首页",)),
                                     absent_text="加载中,请稍候", timeout=0.01) is True


# ── 类型定义完整性 ──

def test_precondition_types_cover_defaults():
    """默认前置项的每个 type 都要在可选类型表里(对话框才能编辑)"""
    for item in session.DEFAULT_PRECONDITIONS:
        assert item["type"] in session.PRECONDITION_TYPES, item["type"]
    # 「自定义步骤」类型必须有名称 + 步骤字段(用户要求: 像用例一样写步骤)
    st = session.PRECONDITION_TYPES["steps"]
    keys = [p["key"] for p in st["params"]]
    assert "name" in keys and "steps_yaml" in keys, f"字段: {keys}"
    # 原「检测文本」类型已按用户要求移除(自定义步骤已覆盖)
    assert "text_check" not in session.PRECONDITION_TYPES


# ── GUI: 前置条件设置对话框(2026-09-23) ──

def test_precondition_dialogs_do_not_auto_close(qapp, monkeypatch, tmp_path):
    """★ 对话框按钮不得响应回车/焦点变化(autoDefault), 否则点某个勾选框就会关窗"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 50}]
    dlg = mw.PreconditionsDialog(items, None)
    dlg.show()
    try:
        qapp.processEvents()
        from PySide6.QtWidgets import QPushButton as _PB
        # 顺序列(第1列)是装着 ↑↓ 的容器, 里面两个按钮也不能是默认按钮
        arrows = dlg.table.cellWidget(0, 1).findChildren(_PB)
        assert len(arrows) == 2, f"顺序列应有 ↑↓ 两个按钮: {arrows}"
        assert all(not b.autoDefault() and not b.isDefault() for b in arrows), \
            "↑↓ 按钮不应是默认按钮"
        for col in (2, 3):            # 编辑/删除按钮
            b = dlg.table.cellWidget(0, col)
            assert not b.autoDefault() and not b.isDefault(), \
                f"表格第{col}列按钮不应是默认按钮"
        # ★ 第一列是表格原生 checkState(不用 cellWidget —— 后者会闪烁)
        assert dlg.table.cellWidget(0, 0) is None, "第一列不应嵌控件"
        it = dlg.table.item(0, 0)
        assert it is not None and (it.flags() & Qt.ItemIsUserCheckable), "应为原生勾选"
        it.setCheckState(Qt.Unchecked)          # 模拟取消勾选
        qapp.processEvents()
        assert dlg.isVisible(), "改动勾选不应关闭对话框"
        assert dlg.items[0]["enabled"] is False, "勾选变化应同步到 items"
        it.setCheckState(Qt.Checked)
        qapp.processEvents()
        assert dlg.items[0]["enabled"] is True
    finally:
        dlg.close()
    # 编辑对话框同样不能自动关闭
    from PySide6.QtWidgets import QPushButton
    ed = mw.PreconditionEditDialog({"type": "battery", "enabled": True,
                                    "min_level": 50}, None)
    ed.show()
    try:
        qapp.processEvents()
        btns = [b for b in ed.findChildren(QPushButton) if b.text() in ("确定", "取消")]
        assert btns, "编辑对话框应有确定/取消按钮"
        assert all(not b.autoDefault() for b in btns), "编辑对话框按钮不应是默认按钮"
    finally:
        ed.close()


def test_add_precondition_menu_lists_all_types(qapp, monkeypatch, tmp_path):
    """「添加前置条件」是下拉菜单, 列出全部可选类型(与添加步骤同风格)"""
    from PySide6.QtWidgets import QPushButton
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True}]
    dlg = mw.PreconditionsDialog(items, None)
    try:
        btn = next(b for b in dlg.findChildren(QPushButton)
                   if "添加前置条件" in b.text())
        menu = btn.menu()
        assert menu is not None, "添加按钮应带下拉菜单"
        labels = [a.text() for a in menu.actions()]
        assert len(labels) == len(session.PRECONDITION_TYPES), \
            f"菜单应列出全部类型: {labels}"
        assert any("自定义" in x for x in labels), "应含自定义类型"
        # 菜单项各自绑定到 _add_of_type(不在此调用 —— 它会弹出模态对话框,
        # 离屏下 exec() 会阻塞; 对话框行为由 PreconditionEditDialog 的测试覆盖)
    finally:
        dlg.close()


# ── 前置条件可以写「用例步骤」(用户要求: 和添加测试步骤一样) ──

def test_precondition_steps_type_runs_steps(monkeypatch):
    """type=steps 的前置项 → 用 ActionRunner 顺序执行这些步骤"""
    calls = []

    class FakeRunner:
        def __init__(self, d, cfg, case_name="", **kw):
            calls.append(("init", case_name))

        def run_steps(self, steps):
            calls.append(("run", steps))
            return True

    monkeypatch.setattr("core.runner.ActionRunner", FakeRunner)
    steps = [{"desc": "点击开始清扫", "click": "开始清扫.png"},
             {"desc": "等待充电", "assert": "充电中", "timeout": 0}]
    items = [{"type": "steps", "enabled": True, "name": "前置准备", "steps": steps}]
    res = session.prepare_items(FakeDev(), {"runner": {"step_interval": 0}}, items)
    assert res[0]["ok"] is True
    assert ("init", "前置准备") in calls, "应用前置条件名创建 runner"
    assert ("run", steps) in calls, "应把步骤交给 runner 执行"
    assert res[0]["desc"] == "前置准备(2步)", f"展示名: {res[0]['desc']}"


def test_precondition_steps_type_reports_failure(monkeypatch):
    """步骤执行失败 → 该前置项判失败(供阻断逻辑使用)"""
    class FailRunner:
        def __init__(self, *a, **kw):
            pass

        def run_steps(self, steps):
            return False

    monkeypatch.setattr("core.runner.ActionRunner", FailRunner)
    items = [{"type": "steps", "enabled": True, "name": "坏的步骤",
              "steps": [{"desc": "x", "click": "y"}]}]
    res = session.prepare_items(FakeDev(), {}, items)
    assert res[0]["ok"] is False


def test_precondition_steps_empty_is_skipped(monkeypatch):
    """没写步骤 → 视为通过(不阻塞), 且不建 runner"""
    def _boom(*a, **kw):
        raise AssertionError("空步骤不应创建 runner")

    monkeypatch.setattr("core.runner.ActionRunner", _boom)
    items = [{"type": "steps", "enabled": True, "name": "空", "steps": []}]
    res = session.prepare_items(FakeDev(), {}, items)
    assert res[0]["ok"] is True


def test_steps_editor_is_card_based(qapp):
    """★ steps 类型用「卡片式步骤编辑器」(与用例编辑同一套组件), 不是 YAML 文本框"""
    from gui import main_window as mw
    dlg = mw.PreconditionEditDialog(
        {"type": "steps", "enabled": True, "name": "前置准备",
         "steps": [{"desc": "点击开始清扫", "click": "开始清扫.png"}]}, None)
    try:
        ed = dlg._edits["steps_yaml"][0]
        assert isinstance(ed, mw._StepsEditor), "应为卡片式编辑器"
        assert len(ed.steps()) == 1, "已有步骤应回填"
        # 添加步骤(与用例编辑相同的入口)
        ed.add_step("back")
        ed.add_step("__wait")
        assert len(ed.steps()) == 3
        # 卡片已渲染(StepCard), 且末步自动展开
        cards = [ed.cards_lay.itemAt(i).widget() for i in range(ed.cards_lay.count())]
        cards = [c for c in cards if isinstance(c, mw.StepCard)]
        assert len(cards) == 3, f"应渲染 3 张卡片, 实际 {len(cards)}"
        # 移动/删除(与用例编辑同名同义)
        ed.host.move_step(0, 1)
        assert ed.steps()[1]["desc"] == "点击开始清扫"
        ed.host.del_step(0)
        assert len(ed.steps()) == 2
        # 取值写回
        v = dlg.values()
        assert v["type"] == "steps" and len(v["steps"]) == 2
    finally:
        dlg.close()


def test_steps_editor_supports_else_substeps(qapp):
    """★ 前置条件的步骤编辑也要支持 else 子步骤(与用例编辑一致, 用户要求)"""
    from gui import main_window as mw
    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True}, None)
    try:
        ed = dlg._edits["steps_yaml"][0]
        ed.add_step("if")                      # 条件动作
        ed.host.steps[0]["if"] = "充电中"
        ed.host.add_sub(0, "click")            # 添加子步骤
        ed.host.steps[0]["else"][0]["click"] = "确认"
        cards = [ed.cards_lay.itemAt(i).widget() for i in range(ed.cards_lay.count())]
        cards = [c for c in cards if isinstance(c, mw.StepCard)]
        assert len(cards) == 2, "父卡片 + else 子卡片"
        v = dlg.values()
        assert v["steps"][0]["if"] == "充电中"
        assert v["steps"][0]["else"][0]["click"] == "确认", "else 子步骤应保留"
    finally:
        dlg.close()


def test_add_precondition_opens_edit_dialog(qapp, monkeypatch):
    """新增前置条件应弹出编辑框(用户要求), 而不是直接用默认值加进列表"""
    from gui import main_window as mw
    dlg = mw.PreconditionsDialog([{"type": "restart", "enabled": True}], None)
    opened = []

    class _FakeEdit:
        def __init__(self, item=None, parent=None, app_group=""):
            opened.append(item.get("type") if item else None)

        def exec(self):
            return 0            # 取消 → 不应加入列表

    monkeypatch.setattr(mw, "PreconditionEditDialog", _FakeEdit)
    before = len(dlg.items)
    dlg._add_of_type("battery")
    assert opened == ["battery"], "应弹出编辑框且预选该类型"
    assert len(dlg.items) == before, "取消后不应加入列表"


def test_steps_dialog_fits_cards(qapp):
    """★ 步骤编辑对话框要够大: 卡片不能比对话框还宽(用户实测被压/溢出)"""
    from gui import main_window as mw
    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True,
                                     "name": "前置准备", "steps": []}, None)
    dlg.show()
    try:
        qapp.processEvents()
        assert dlg.width() >= 700, f"步骤对话框应更大: {dlg.width()}"
        ed = dlg._edits["steps_yaml"][0]
        ed.add_step("click")
        ed.add_step("assert")
        qapp.processEvents()
        cards = [ed.cards_lay.itemAt(i).widget() for i in range(ed.cards_lay.count())]
        cards = [c for c in cards if isinstance(c, mw.StepCard)]
        assert cards, "应渲染卡片"
        assert cards[0].width() <= dlg.width(), \
            f"卡片({cards[0].width()})不应超出对话框({dlg.width()})"
        # 字段表单列要能拉伸(否则步骤区被标签列挤窄)
        from PySide6.QtWidgets import QFormLayout
        assert dlg.form.fieldGrowthPolicy() == QFormLayout.AllNonFixedFieldsGrow
    finally:
        dlg.close()


def test_steps_editor_has_app_group_picker(qapp, monkeypatch, tmp_path):
    """★ 步骤编辑器要有「APP 组」选择 —— 模板/基准图下拉据此取对应组的模板"""
    import gui.main_window as mw
    # 造两个组目录, 各放一张模板
    root = tmp_path / "Test_cases"
    for g, tpl in (("组A", "涂鸦_模板A.png"), ("组B", "涂鸦_模板B.png")):
        tdir = root / g / "templates"
        tdir.mkdir(parents=True)
        (tdir / tpl).write_bytes(b"fake")
    monkeypatch.setattr(mw, "BASE_DIR", str(tmp_path), raising=False)
    from core import vision as _vision
    monkeypatch.setattr(_vision, "BASE_DIR", str(tmp_path), raising=False)
    # 前缀从 config 读; 这里直接固定, 否则读不到(测试目录没有 config)
    monkeypatch.setattr(_vision, "_template_prefix", lambda: "涂鸦", raising=False)

    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True,
                                     "name": "x", "steps": []}, None,
                                    app_group="组A")
    try:
        ed = dlg._edits["steps_yaml"][0]
        items = [ed.group_combo.itemText(i) for i in range(ed.group_combo.count())]
        assert items == ["组A", "组B"], f"组下拉应列出全部组: {items}"
        assert ed.group_combo.currentText() == "组A"
        from core import vision
        assert "模板A.png" in vision.list_templates(), "应取到组A的模板"
        # 切到组B → 模板上下文跟着换
        ed.group_combo.setCurrentText("组B")
        qapp.processEvents()
        assert "模板B.png" in vision.list_templates(), "切换组后应取组B的模板"
    finally:
        dlg.close()


def test_steps_editor_syncs_context_on_first_open(qapp, monkeypatch, tmp_path):
    """★ 首次打开就要能取到模板(用户实测: 之前要手动切一次 APP 组才有)

    组下拉默认选中第一个组, 且初始化时就把模板上下文设好
    (setCurrentIndex 不触发 currentTextChanged, 必须显式同步)。
    """
    import gui.main_window as mw
    from core import vision
    root = tmp_path / "Test_cases"
    for g in ("组A", "组B"):
        tdir = root / g / "templates"
        tdir.mkdir(parents=True)
        (tdir / f"涂鸦_{g}模板.png").write_bytes(b"fake")
    monkeypatch.setattr(mw, "BASE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(vision, "BASE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(vision, "_template_prefix", lambda: "涂鸦", raising=False)
    vision.set_template_app_group("")          # 模拟: 主窗口还没设上下文

    dlg = mw.PreconditionEditDialog({"type": "steps", "enabled": True,
                                     "name": "x", "steps": []}, None,
                                    app_group="")
    try:
        ed = dlg._edits["steps_yaml"][0]
        assert ed.group_combo.currentText() == "组A", "应默认选中第一个组"
        assert vision.current_app_group() == "组A", "初始化就该同步模板上下文"
        assert "组A模板.png" in vision.list_templates(), "首次打开即可取到模板"
    finally:
        dlg.close()


# ── 前置条件: 点击空白处(清掉悬浮通知) ──

def test_tap_blank_uses_default_point_and_override():
    """不填坐标 → 用实测默认空白点; 填了 → 用填的坐标"""
    dev = FakeDev()
    assert session.tap_blank(dev) is True
    assert dev.clicked_at == session.DEFAULT_BLANK_POINT, dev.clicked_at
    dev2 = FakeDev()
    session.tap_blank(dev2, (100, 200))
    assert dev2.clicked_at == (100, 200)


def test_parse_point_accepts_full_and_half_width_comma():
    """坐标分隔符与其它多值字段一致: 中文输入法打出的全角逗号也要认"""
    assert session._parse_point("540,660") == (540, 660)
    assert session._parse_point("540，660") == (540, 660)      # 全角
    assert session._parse_point("540、660") == (540, 660)      # 顿号
    assert session._parse_point(" 540 , 660 ") == (540, 660)
    assert session._parse_point("") is None                    # 空 → 用默认点
    assert session._parse_point("abc") is None
    assert session._parse_point("1,2,3") is None               # 不是两个数 → 默认点
    # 非法坐标不能静默失效: 走默认点
    dev = FakeDev()
    session.tap_blank(dev, session._parse_point("abc"))
    assert dev.clicked_at == session.DEFAULT_BLANK_POINT


def test_tap_blank_runs_as_precondition_item():
    """★ 走前置条件列表执行: type=tap_blank → 点空白; 结果进 prepare_items 列表"""
    dev = FakeDev()
    items = [{"type": "tap_blank", "enabled": True, "point": "300，400"}]
    results = session.prepare_items(dev, {}, items)
    assert dev.clicked_at == (300, 400), dev.clicked_at
    assert len(results) == 1 and results[0]["ok"] is True
    assert "点击空白处" in results[0]["desc"], results[0]["desc"]
    assert "300,400" in results[0]["desc"], results[0]["desc"]


def test_tap_blank_type_is_editable_in_gui():
    """GUI「添加前置条件」里能选到它, 并且坐标可编辑"""
    assert "tap_blank" in session.PRECONDITION_TYPES
    spec = session.PRECONDITION_TYPES["tap_blank"]
    keys = [p["key"] for p in spec["params"]]
    assert "point" in keys, spec


# ── WebView 文本预热 ──

def test_warm_webview_swallows_device_errors():
    """dump 失败(设备抖动)不能拖垮判断"""
    class Boom:
        def dump_hierarchy(self):
            raise RuntimeError("设备连接抖动")
    assert session.warm_webview(Boom()) == ""


def test_map_load_and_text_check_warm_webview(monkeypatch):
    """前置的文本等待(地图加载/文本检查)同样要在轮询里预热 WebView"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = FakeDev(present=("地图编辑",))
    calls = {"n": 0}
    # 计数同时要返回"能看到 present"的层级(判断类现在读层级)
    dev.dump_hierarchy = lambda: (
        calls.__setitem__("n", calls["n"] + 1)
        or "".join(f'<node text="{p}"/>' for p in sorted(dev.present)))
    assert session.ensure_map_loaded(dev, timeout=0.01, rounds=1,
                                     ready_text="地图编辑") is True
    assert calls["n"] >= 1, "地图加载没有预热"

    dev2 = FakeDev(present=("首页",))
    calls2 = {"n": 0}
    dev2.dump_hierarchy = lambda: (
        calls2.__setitem__("n", calls2["n"] + 1)
        or "".join(f'<node text="{p}"/>' for p in sorted(dev2.present)))
    assert session.ensure_text_check(dev2, wait_text="首页", timeout=0.01) is True
    assert calls2["n"] >= 1, "文本检查没有预热"


# ── 前置条件: 进入设备页(文本确认 + 失败重进) ──

class _SwitchDev(FakeDev):
    """点设备名后"页面切换": present 从列表页文本变成设备页文本"""

    def __init__(self, before=(), after=()):
        super().__init__(present=before)
        self._after = set(after)

    def __call__(self, **kw):
        r = super().__call__(**kw)
        owner = self
        orig_click = r.click

        def click():
            orig_click()
            owner.present = set(owner._after)      # 模拟进入设备页

        r.click = click
        return r


def test_enter_device_ok_when_already_on_device_page(monkeypatch):
    """已经在设备页(判定文本命中) → 直接通过, 不该乱点/乱按返回"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = _SwitchDev(before=("设备控制", "扫地机器人0087"))
    assert session.ensure_device_page(dev, "扫地机器人0087",
                                      ready_text="设备控制", timeout=1) is True
    assert dev.pressed == [] and dev.clicked == [], "已在设备页不该再操作"


def test_enter_device_clicks_name_then_confirms(monkeypatch):
    """不在设备页 → 点设备名进入 → 用判定文本确认"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = _SwitchDev(before=("主页", "收藏", "扫地机器人0087"),
                     after=("设备控制", "扫地机器人0087"))
    assert session.ensure_device_page(dev, "扫地机器人0087", ready_text="设备控制",
                                      timeout=1, rounds=3) is True
    assert "扫地机器人0087" in dev.clicked, f"应点击设备名进入: {dev.clicked}"


def test_enter_device_retries_then_fails(monkeypatch):
    """判定文本一直出现不了 → 每轮都重进, 用完轮数返回 False(会阻断本轮)"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    # 永远停在列表页(点完也不切换)
    dev = _SwitchDev(before=("主页", "收藏", "扫地机器人0087"), after=("主页",))
    assert session.ensure_device_page(dev, "扫地机器人0087", ready_text="设备控制",
                                      timeout=1, rounds=2) is False
    assert dev.clicked.count("扫地机器人0087") >= 1, "失败后必须再执行进入操作"


def test_enter_device_does_not_click_webview_title(monkeypatch):
    """⚠ 设备页上设备名是铺满全屏的 WebView 标题 —— 判定文本读不到时也不能去点它
    (盲点会点到页面中央的按钮, 如「暂停」)"""
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    dev = _SwitchDev(before=("扫地机器人0087",))        # 既无列表页标志也无设备页标志
    assert session.ensure_device_page(dev, "扫地机器人0087", ready_text="设备控制",
                                      timeout=1, rounds=1) is False
    assert dev.clicked == [], f"不确定在列表页时不能点设备名: {dev.clicked}"


def test_enter_device_ready_text_is_editable_param():
    """判定文本必须可编辑(GUI 前置条件里能改)"""
    spec = session.PRECONDITION_TYPES["enter_device"]
    keys = {p["key"]: p for p in spec["params"]}
    assert "ready_text" in keys, spec
    assert keys["ready_text"]["type"] == "text"
    assert keys["ready_text"]["default"] == session.DEFAULT_DEVICE_READY_TEXT
    # 自定义判定文本要真的生效
    import inspect
    assert "ready_text" in inspect.signature(session.ensure_device_page).parameters


def test_default_preconditions_include_enter_device_first():
    """★ 用户要求: 进设备页要在**默认执行项**里, 且排在 charging/map_load 之前
    (那两项都要在设备页上操作); 判定文本用默认值"""
    types = [it["type"] for it in session.DEFAULT_PRECONDITIONS]
    assert "enter_device" in types, f"默认执行项缺少进设备页: {types}"
    assert types.index("enter_device") < types.index("charging"), types
    assert types.index("enter_device") < types.index("map_load"), types
    item = next(it for it in session.DEFAULT_PRECONDITIONS if it["type"] == "enter_device")
    assert item.get("ready_text") == session.DEFAULT_DEVICE_READY_TEXT, item
    assert item.get("enabled", True) is True, "默认要启用"


# ── 前置条件顺序可手动调整(↑↓) ──

def test_precondition_dialog_reorder_updates_order(qapp):
    """★ 用户要求: 前置条件的执行顺序可手动调整 —— 列表顺序就是执行顺序"""
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 50},
             {"type": "tap_blank", "enabled": True, "point": "1,2"}]
    dlg = mw.PreconditionsDialog(items, None)
    try:
        assert [it["type"] for it in dlg.values()] == ["restart", "battery", "tap_blank"]
        dlg._move(2, -1)          # 第三条上移
        assert [it["type"] for it in dlg.values()] == ["restart", "tap_blank", "battery"]
        dlg._move(0, 1)           # 第一条下移
        assert [it["type"] for it in dlg.values()] == ["tap_blank", "restart", "battery"]
        # 边界: 首行上移/末行下移 都是 no-op
        dlg._move(0, -1)
        dlg._move(2, 1)
        assert [it["type"] for it in dlg.values()] == ["tap_blank", "restart", "battery"]
    finally:
        dlg.close()


def test_precondition_dialog_arrows_enabled_by_position(qapp):
    """首行的 ↑ 与末行的 ↓ 应置灰(避免点了没反应)"""
    from PySide6.QtWidgets import QPushButton as _PB
    from gui import main_window as mw
    items = [{"type": "restart", "enabled": True},
             {"type": "battery", "enabled": True, "min_level": 50}]
    dlg = mw.PreconditionsDialog(items, None)
    try:
        first = dlg.table.cellWidget(0, 1).findChildren(_PB)
        last = dlg.table.cellWidget(1, 1).findChildren(_PB)
        assert not first[0].isEnabled() and first[1].isEnabled(), "首行↑应置灰"
        assert last[0].isEnabled() and not last[1].isEnabled(), "末行↓应置灰"
    finally:
        dlg.close()


# ── 前置条件与 APP 组绑定 ──

def test_group_preconditions_saved_to_separate_dir(monkeypatch, tmp_path):
    """★ 用户要求: 前置条件按 APP 组存放, 且放**独立目录**(不能落在用例目录里,
    否则会被用例列表当成用例显示出来)"""
    from core import driver
    monkeypatch.setattr(driver, "BASE_DIR", str(tmp_path))
    items = [{"type": "restart", "enabled": True},
             {"type": "enter_device", "enabled": True, "ready_text": "设备控制"}]
    p = driver.save_group_preconditions("三星", items)
    assert p.endswith(os.path.join("Test_preconditions", "三星.yaml")), p
    assert "Test_cases" not in p, f"不能写进用例目录: {p}"
    assert driver.load_group_preconditions("三星") == items
    assert driver.load_group_preconditions("没配过的组") is None


def test_preconditions_file_never_counts_as_case():
    """兜底: 组目录里若残留 preconditions.yaml, 不能被当作用例扫出来"""
    from core.driver import is_case_file
    assert is_case_file("/x/Test_cases/三星/全局清扫.yaml") is True
    assert is_case_file("/x/Test_cases/三星/preconditions.yaml") is False
    assert is_case_file("/x/Test_cases/三星/preconditions.yml") is False


def test_preconditions_for_group_fallback_chain(monkeypatch, tmp_path):
    """取值优先级: 组配置 → config.yaml 全局 → 内置默认"""
    from core import driver
    monkeypatch.setattr(driver, "BASE_DIR", str(tmp_path))   # ★ 组配置路径基于 driver.BASE_DIR
    # ① 组里没配、全局也没配 → 内置默认项(注意要连全局一起隔离, 别读真实 config)
    monkeypatch.setattr(driver, "load_preconditions", lambda: None)
    items, src = session.preconditions_for_group("组A")
    assert src == "default" and items, (src, items)
    # ② 只有全局 → 用全局(老用户参数不丢)
    global_items = [{"type": "charging", "enabled": True, "timeout": 1200}]
    monkeypatch.setattr(driver, "load_preconditions", lambda: global_items)
    items, src = session.preconditions_for_group("组A")
    assert src == "global" and items == global_items
    # ③ 组里有配置 → 优先用组里的
    group_items = [{"type": "tap_blank", "enabled": True, "point": "9,9"}]
    driver.save_group_preconditions("组A", group_items)
    items, src = session.preconditions_for_group("组A")
    assert src == "group" and items == group_items, (src, items)


def test_group_preconditions_path_is_isolated_in_tests():
    """守护: 测试里组配置的落盘路径必须指向临时目录。

    conftest 的 `_isolate_group_preconditions` 钉住 group_preconditions_path 这个
    唯一出口 —— 一旦那条 fixture 被删/失效, 测试就会写进用户真实的
    Test_preconditions/(曾经因此覆盖过用户的 7 条配置), 这里当场报红。
    """
    from pathlib import Path as _P

    from core.driver import group_preconditions_path
    p = _P(group_preconditions_path("任意组")).resolve()
    real = (_P(__file__).resolve().parent.parent / "Test_preconditions" / "任意组.yaml")
    assert p != real.resolve(), "测试里指向了真实目录: conftest 的隔离失效了"
    assert "Test_preconditions" in p.name or p.name.endswith(".yaml")


def test_precondition_order_arrows_are_visible(qapp):
    """★ 箭头按钮必须真的放得下箭头。

    用户报过"顺序按钮没有箭头方向显示": 全局 QSS 给 QPushButton 设了
    `padding: 4px 12px`, 而箭头按钮只有 26px 宽 —— 左右内边距吃掉 24px,
    字被整个截掉。必须内联覆盖 padding + 固定尺寸。
    """
    from PySide6.QtWidgets import QPushButton as _PB
    from gui import main_window as mw
    dlg = mw.PreconditionsDialog([{"type": "restart", "enabled": True},
                                  {"type": "battery", "enabled": True}], None)
    try:
        for b in dlg.table.cellWidget(0, 1).findChildren(_PB):
            assert b.text() in ("↑", "↓"), b.text()
            need = b.fontMetrics().horizontalAdvance(b.text()) + 4
            assert b.width() >= need, \
                f"'{b.text()}' 按钮太窄({b.width()} < {need}), 箭头会被全局 padding 截掉"
            assert "padding" in b.styleSheet(), "小按钮必须内联覆盖全局 padding"
    finally:
        dlg.close()


# ── 2026-09-24 审查修复: 按组取前置 / 组前置写入安全 ──

def test_resolve_group_preconditions_empty_list_does_not_fall_back():
    """★ 某组前置「全部取消勾选」(空列表) 不得回退去执行别的组的前置。

    回归守护: 原实现是 `pre_by_group.get(group) or self.pre_items` —— 空列表 falsy,
    于是把「这组有意不做前置」当成「没配过」, 实际跑了当前界面组的前置。
    实测: ZZZ 组界面 0 条, 实际执行 1 条(可能是重启 APP / 等充电这类重动作)。
    """
    from gui.runner_thread import resolve_group_preconditions
    fallback = [{"type": "restart", "enabled": True}]
    by_group = {"AAA": [{"type": "charging", "enabled": True}], "ZZZ": []}

    assert resolve_group_preconditions(by_group, "ZZZ", fallback) == [], \
        "空列表被当成'没配过', 回退到了别组的前置"
    assert resolve_group_preconditions(by_group, "AAA", fallback) == by_group["AAA"]
    # 真的没配过的组才用 fallback
    assert resolve_group_preconditions(by_group, "未配过的组", fallback) == fallback
    # 旧调用方传空/None 也要兜住
    assert resolve_group_preconditions({}, "任意组", fallback) == fallback
    assert resolve_group_preconditions(None, "任意组", fallback) == fallback


def test_group_preconditions_save_failure_keeps_file():
    """★ 序列化失败时原组配置必须完好(不能只剩 0 字节)。

    回归守护: 原实现把 `yaml.safe_dump` 写在 `open(path, "w")` **之后** —— items 里
    混入 YAML 无法表示的对象时 safe_dump 抛 RepresenterError, 而此刻文件已被截断
    (实测 129 字节 → 0 字节); 异常抛出后用户并不知道内容已经没了。
    """
    from core import driver
    good = [{"type": "charging", "enabled": True, "ready_text": "充电"}]
    p = driver.save_group_preconditions("组安全", good)
    before = open(p, encoding="utf-8").read()
    assert before

    with pytest.raises(Exception):
        driver.save_group_preconditions("组安全", good + [{"type": "x", "obj": object()}])

    assert open(p, encoding="utf-8").read() == before, "序列化失败把原配置清空了"
    assert driver.load_group_preconditions("组安全") == good


def test_steps_precondition_gets_device_name_and_cancel():
    """★ 「自定义步骤」前置新建的 runner 必须拿到 device_name 与取消回调。

    回归守护:
      · 漏传 device_name → "插件页整页读不出文本就重进设备页"用不了, 同一条断言
        在前置里失败、在正式用例里却能过(实测拿到的是空串)。
      · 没有取消回调 → 用户点停止后这串前置会一路跑完(实测 4s 的等待步骤跑满 4.0s,
        因为新建的 runner 拿不到主 runner 的 stop())。
    """
    from core import registry as reg
    import core.actions        # noqa: F401

    seen = {}
    cancel = {"v": False}
    try:
        @reg.action("probe_pre_ctx", priority=1)
        def _probe(runner, step):
            seen["device_name"] = runner.device_name
            cancel["v"] = True          # 第 1 步之后请求停止

        @reg.action("probe_pre_after", priority=1)
        def _after(runner, step):
            seen["second_ran"] = True   # 这步不该被执行

        class _D:
            serial = "d"

            @property
            def info(self):
                return {}

            def dump_hierarchy(self):
                return '<hierarchy><node text="x"/></hierarchy>'

            def click(self, *a, **k):
                pass

        cfg = {"target_device": "我的扫地机",
               "runner": {"step_interval": 0, "default_timeout": 1}}
        session._run_one(_D(), cfg,
                         {"type": "steps", "name": "探针",
                          "steps": [{"desc": "查上下文", "probe_pre_ctx": True},
                                    {"desc": "停止后不该执行", "probe_pre_after": True}]},
                         None, lambda: cancel["v"])
    finally:
        reg.ACTIONS.pop("probe_pre_ctx", None)
        reg.ACTIONS.pop("probe_pre_after", None)

    assert seen.get("device_name") == "我的扫地机", \
        "steps 前置的 runner 漏传 device_name(与正式用例行为不一致)"
    assert "second_ran" not in seen, \
        "取消信号没穿透到 steps 前置(点停止后它还会跑完剩余步骤)"
