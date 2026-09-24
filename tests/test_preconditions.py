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

    def press(self, key):
        self.pressed.append(key)

    def app_stop(self, pkg):
        pass

    def dump_hierarchy(self):
        return ""


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
                        lambda d, cfg, enter_page=True: calls.append("restart"))
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
        for col in (1, 2):            # 编辑/删除按钮
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
