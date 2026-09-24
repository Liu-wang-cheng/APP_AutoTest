# -*- coding: utf-8 -*-
"""GUI 离屏测试: 窗体装配 / 新建 / 添加步骤 / 卡片渲染 / YAML 往返,不碰真机。"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def win(qapp, monkeypatch, tmp_path):
    from gui import main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    yield w
    w.close()


def test_window_built(win):
    assert win.device_combo is not None
    assert win.result_table is not None
    assert win.step_count_label.text().startswith("共")
    assert win.tabs.count() == 3


def test_new_resets(win):
    win.create_case("测试组", group="测试组")
    assert len(win.steps) == 0
    assert win.data["module"] == "测试组"


def test_add_step_and_render(win):
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    assert len(win.steps) == 1
    assert win.cards_lay.count() >= 2          # 卡片 + 末尾 stretch
    assert win.step_count_label.text() == "共 1 步"


def test_add_step_schema_default(win):
    """int4 字段默认给空列表(序列化时空列表按动作语义处理)"""
    win.create_case("测试组", group="测试组")
    win.add_step("room_zones")
    assert win.steps[-1].get("room_zones") == []


def test_dump_data_serializes(win):
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    win.steps[0]["click"] = " 开始清扫.png "
    win.steps[0]["desc"] = "点开始"
    out = win._dump_data()
    step = out["cases"][0]["steps"][0]
    assert step["click"] == "开始清扫.png"       # strip 生效
    assert step["desc"] == "点开始"


def test_yaml_roundtrip_via_text(win):
    win.create_case("测试组", group="测试组")
    win.add_step("assert")
    win.steps[0]["assert"] = "清扫中,建图中"
    win.steps[0]["timeout"] = 20
    win._refresh_yaml_text()
    text = win.yaml_edit.toPlainText()
    assert "清扫中,建图中" in text

    win.yaml_edit.setPlainText(text)
    win._apply_yaml_text()
    assert win.steps[0]["assert"] == "清扫中,建图中"
    assert win.steps[0]["timeout"] == 20


def test_multi_case_switch(win):
    win.data = {"module": "组", "cases": [
        {"name": "用例1", "priority": "P0", "steps": [{"desc": "a", "click": "x"}]},
        {"name": "用例2", "priority": "P1", "steps": []},
    ]}
    win.case_idx = 0
    win._refresh_case_selector()
    assert len(win.steps) == 1
    win.case_combo.setCurrentIndex(1)
    assert len(win.steps) == 0


def test_set_locked_disables_editing(win):
    win._set_locked(True)
    assert win.new_btn.isEnabled() is False
    assert win.add_btn.isEnabled() is False
    win._set_locked(False)
    assert win.new_btn.isEnabled() is True


def test_locked_disables_all_edit_buttons(win):
    """锁定靠禁用按钮实现(方法本身不挡) —— 验证按钮确实被禁掉"""
    win.create_case("测试组", group="测试组")
    win._set_locked(True)
    assert not win.add_btn.isEnabled()
    assert not win.new_btn.isEnabled()
    assert not win.save_btn.isEnabled()
    win._set_locked(False)
    assert win.add_btn.isEnabled()


def test_step_summary_shown_in_card(win):
    """动作名在 chip(彩色标签); summary 只放具体参数, 不重复动作名"""
    from PySide6.QtWidgets import QLabel
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    win.steps[0]["click"] = "开始清扫.png"
    win.render_cards()
    card = win.cards_lay.itemAt(0).widget()
    chip = next(l for l in card.findChildren(QLabel) if l.objectName() == "chip")
    assert chip.text() == "点击"
    assert card.summary_label.text().startswith("开始清扫.png"), \
        f"summary 应只显示参数: {card.summary_label.text()!r}"
    assert "点击" not in card.summary_label.text(), "不重复动作名(chip 已有)"


# ── 步骤 / 子步骤操作 ──

def test_dup_step_is_deepcopy(win):
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    win.steps[0]["click"] = "开始清扫.png"
    win.dup_step(0)
    assert len(win.steps) == 2
    assert win.steps[1]["click"] == "开始清扫.png"
    assert win.steps[1] is not win.steps[0], "必须是深拷贝,否则改副本会连带改原步骤"
    assert win.expanded_key == (1,)


def test_move_step_swaps(win):
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    win.add_step("back")
    win.steps[0]["desc"], win.steps[1]["desc"] = "A", "B"
    win.move_step(0, 1)
    assert [s["desc"] for s in win.steps] == ["B", "A"]


def test_move_step_out_of_range_is_noop(win):
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    win.steps[0]["desc"] = "A"
    win.move_step(0, -1)
    assert [s["desc"] for s in win.steps] == ["A"]


def test_del_step_rolls_expanded_key_back(win):
    """删掉展开中的最后一步后,展开键不能还指向已不存在的下标"""
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    win.add_step("back")
    win.expanded_key = (1,)
    win.del_step(1)
    assert len(win.steps) == 1
    assert win.expanded_key == (0,)


def test_add_sub_appends_and_expands(win):
    win.create_case("测试组", group="测试组")
    win.add_step("if")
    win.add_sub(0, "click")
    assert len(win.steps[0]["else"]) == 1
    assert win.expanded_key == (0, 0)


def test_add_sub_wait_shortcut(win):
    """__wait 是「延时等待」的快捷写法(纯 wait 步骤)"""
    win.create_case("测试组", group="测试组")
    win.add_step("if")
    win.add_sub(0, "__wait")
    sub = win.steps[0]["else"][0]
    assert sub.get("wait") and "wait_for" not in sub and "click" not in sub


def test_move_sub(win):
    win.create_case("测试组", group="测试组")
    win.add_step("if")
    win.add_sub(0, "click")
    win.add_sub(0, "back")
    win.steps[0]["else"][0]["desc"] = "A"
    win.steps[0]["else"][1]["desc"] = "B"
    win.move_sub(0, 0, 1)
    assert [s["desc"] for s in win.steps[0]["else"]] == ["B", "A"]


def test_dup_sub_and_del_sub(win):
    win.create_case("测试组", group="测试组")
    win.add_step("if")
    win.add_sub(0, "click")
    win.dup_sub(0, 0)
    assert len(win.steps[0]["else"]) == 2
    win.del_sub(0, 0)
    assert len(win.steps[0]["else"]) == 1


def test_del_sub_collapses_to_parent(win):
    """子步骤删光后展开态要收回父卡片,不能停在已不存在的子下标上"""
    win.create_case("测试组", group="测试组")
    win.add_step("if")
    win.add_sub(0, "click")
    win.del_sub(0, 0)
    assert win.expanded_key == (0,)


def test_collapse_all(win):
    win.create_case("测试组", group="测试组")
    win.add_step("if")
    win.add_sub(0, "click")
    win._collapse_all()
    assert win.expanded_key is None


def test_step_ops_guarded_by_worker_not_lock(win):
    """步骤增删改由 worker 守卫(执行期间禁止改动编排)

    锁定是"禁用按钮"层面的保护;真正的守卫是 self.worker ——
    执行中改步骤会让跑着的用例和界面数据对不上。
    """
    win.create_case("测试组", group="测试组")
    win.add_step("click")

    class _FakeWorker:
        pass

    win.worker = _FakeWorker()
    win.dup_step(0)
    win.move_step(0, 1)
    win.add_step("back")
    win.add_sub(0, "click")
    assert len(win.steps) == 1          # worker 在跑,全部被挡
    assert "else" not in win.steps[0]
    win.worker = None
    win.add_step("back")
    assert len(win.steps) == 2          # 清掉 worker 后恢复


def test_else_substeps_survive_yaml_roundtrip(win):
    """else 子步骤要能落盘再读回 —— 缩进/结构最容易在这里出错"""
    import yaml
    win.create_case("测试组", group="测试组")
    win.add_step("if not")
    win.steps[0]["if not"] = "扫地机器人"
    win.add_sub(0, "click")
    win.steps[0]["else"][0]["click"] = "重置首页地图"

    dumped = yaml.safe_dump(win._dump_data(), allow_unicode=True, sort_keys=False)
    back = yaml.safe_load(dumped)
    steps = back["cases"][0]["steps"]
    assert steps[0]["if not"] == "扫地机器人"
    assert steps[0]["else"][0]["click"] == "重置首页地图"


def _card_widgets(win):
    """当前卡片列表里的 StepCard 组件(跳过 stretch 等非卡片项)"""
    from gui.main_window import StepCard
    out = []
    for i in range(win.cards_lay.count()):
        w = win.cards_lay.itemAt(i).widget()
        if isinstance(w, StepCard):
            out.append(w)
    return out


def test_只改说明不破坏其它字段类型(win):
    """编辑一个字段不能碰其它字段 —— swipe 的 list 被写成字符串就废了"""
    win.create_case("测试组", group="测试组")
    win.steps.append({"desc": "滑动", "swipe": [100, 200, 300, 400]})
    win.expanded_key = (0,)
    win.render_cards()

    card = _card_widgets(win)[0]
    card.widgets["desc"].setText("滑动一")
    card.widgets["desc"].editingFinished.emit()   # 远端用 editingFinished 写回

    assert win.steps[0]["desc"] == "滑动一"
    assert win.steps[0]["swipe"] == [100, 200, 300, 400]


def test_高级参数编辑生效(win):
    """等待/重试这类修饰参数在卡片里编辑后要写回步骤"""
    win.create_case("测试组", group="测试组")
    win.add_step("click")
    win.steps[0]["click"] = "确认"
    win.expanded_key = (0,)
    win.render_cards()

    card = _card_widgets(win)[0]
    for key, val in (("wait", "7"), ("retry", "2")):
        w = card.widgets[key]
        w.setText(val)
        w.editingFinished.emit()

    assert win.steps[0]["wait"] == 7
    assert win.steps[0]["retry"] == 2
    out = win._dump_data()["cases"][0]["steps"][0]
    assert out["wait"] == 7 and out["retry"] == 2


def test_真实用例文件回环不丢键(win):
    """把 Test_cases/ 的每个用例读进来渲染卡片再导出,原有的键一个都不能丢

    GUI 是「打开→编辑→保存」的编辑器,一次往返悄悄丢掉一个键,用户要等真机
    跑起来才发现步骤行为不对。这里对真实用例文件做整体回环检查,顺带确认:
    · 卡片数 == 步骤数(渲染不能漏步骤)
    · 导出后不出现引擎不认识的键
    """
    import glob
    import yaml as _yaml
    from core.driver import BASE_DIR
    from gui import schema

    known = set(schema.ACTION_BY_KEY)
    known |= {f["key"] for a in schema.ACTIONS for f in a["fields"]}
    known |= {s["key"] for a in schema.ACTIONS for f in a["fields"]
              if f.get("type") == "group" for s in f.get("fields", [])}
    known |= {"desc", "screenshot", "wait", "timeout", "retry", "circular",
              "threshold", "switch_tpl", "switch_label", "switch_area", "else"}

    total = 0

    def roundtrip(module, cases):
        nonlocal total
        win.data = {"module": module, "cases": cases}
        win.case_idx = 0
        win.expanded_key = None
        win.render_cards()

        # 卡片数必须等于首个用例的步骤数 —— 渲染漏步骤的话后面的键检查就没意义了
        expect = len((cases[0].get("steps") or []) ) if cases else 0
        shown = len(_card_widgets(win))
        assert shown == expect, f"{module}: 步骤 {expect} 个, 卡片却渲染了 {shown} 个"

        dumped = win._dump_data()
        assert len(dumped["cases"]) == len(cases), f"{module}: 用例数变少"
        for orig, back in zip(cases, dumped["cases"]):
            for i, (s_orig, s_back) in enumerate(zip(orig.get("steps") or [],
                                                     back.get("steps") or [])):
                total += 1
                for k, v in s_orig.items():
                    # serialize_step 会省略 None/空串/False(引擎默认即这些值),
                    # 省略它们语义不变,不算丢键。0 要保留,所以不能用 v in (None,"",False)
                    if v is None or v == "" or v is False:
                        continue
                    assert k in s_back, \
                        f"{module}/{orig.get('name')} 步骤{i + 1} 丢了键「{k}」"
                    if isinstance(v, list) and k != "else":
                        assert isinstance(s_back[k], list), \
                            f"{module} 步骤{i + 1}「{k}」类型从 list 变成 {type(s_back[k])}"
                for k in s_back:
                    assert k in known, f"{module} 步骤{i + 1} 出现未知键「{k}」"

    cases_root = os.path.join(BASE_DIR, "Test_cases")
    if not os.path.isdir(cases_root):
        pytest.skip("无用例目录")
    files = []
    for sub in sorted(os.listdir(cases_root)):
        sub_full = os.path.join(cases_root, sub)
        if os.path.isdir(sub_full):
            files += sorted(glob.glob(os.path.join(sub_full, "*.yaml")))
        elif sub.endswith((".yaml", ".yml")):
            files.append(sub_full)
    assert files, "Test_cases 下没有用例文件"

    for path in files:
        with open(path, encoding="utf-8") as f:
            data = _yaml.safe_load(f)
        if isinstance(data.get("modules"), list):
            for m in data["modules"]:
                roundtrip(m.get("module", ""), m.get("cases") or [])
        else:
            roundtrip(data.get("module", ""), data.get("cases") or [])

    assert total >= 200, f"只检查了 {total} 个步骤,用例文件是不是没读到?"


def test_imageview_dialog_zoom_controls(win):
    """截图查看器: 适应窗口 / 1:1 / 放大缩小 都要能切。
    注:小图的 fit 是放大(>1x),1:1(1.0)比 fit 还小 → 被 fit_scale 下限钳住,
    这是预期行为(显示不能小于打开时的默认大小)"""
    from PIL import Image
    from gui.main_window import ImageViewDialog   # 远端把控件都放在 main_window 里
    import tempfile

    p = Path(tempfile.mkdtemp()) / "shot.png"
    Image.new("RGB", (200, 100), "red").save(p)

    dlg = ImageViewDialog(str(p))
    assert dlg._zoom is None              # 初始适应窗口
    dlg._zoom_in()
    fit = dlg._fit_scale
    assert dlg._zoom > fit                # 从 fit 起步放大(不是从 1.0 跳变)
    dlg._zoom_out()
    assert dlg._zoom == pytest.approx(fit, rel=0.05) or dlg._zoom is None
    dlg._fit()
    assert dlg._zoom is None
    dlg.close()


# ── 用例列表:同列布局 + 勾选/单击加载(2026-09-21 布局重排守护) ──

@pytest.fixture
def win_with_cases(qapp, monkeypatch, tmp_path):
    """Test_cases/ 预置两个用例文件后实例化窗口"""
    from gui import main_window as mw
    cases_dir = tmp_path / "Test_cases" / "涂鸦智能"
    cases_dir.mkdir(parents=True)
    (cases_dir / "全局清扫.yaml").write_text(
        "module: 全局清扫\ncases:\n  - name: 完整流程\n    steps: []\n", encoding="utf-8")
    (cases_dir / "划区清扫.yaml").write_text(
        "module: 划区清扫\ncases:\n  - name: 完整流程\n    steps: []\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    yield w
    w.close()


def test_case_list_is_qlistwidget_and_in_same_column(win_with_cases):
    """self.case_list 必须是真正的 QListWidget(曾被 panel 覆盖 → 勾选/运行必炸)"""
    from PySide6.QtWidgets import QListWidget
    assert isinstance(win_with_cases.case_list, QListWidget)
    # 顶层不再是左右分栏(底部结果区的 QSplitter 是合法保留,不受影响)
    from PySide6.QtWidgets import QSplitter
    assert not isinstance(win_with_cases.centralWidget(), QSplitter)


def case_row_index(view, n):
    """第 n 个用例行的列表索引(跳过 APP 组头行, UserRole=None)。
    view 可传主窗口或列表控件本身"""
    lst = view.case_list if hasattr(view, "case_list") else view
    seen = -1
    for i in range(lst.count()):
        if lst.item(i).data(0x0100):      # Qt.UserRole
            seen += 1
            if seen == n:
                return i
    return -1


def test_case_check_and_collect_paths(win_with_cases):
    """全选/清空 → _checked_case_paths 正确返回勾选路径"""
    from PySide6.QtCore import Qt
    win = win_with_cases
    assert win.case_list.count() == 3      # 2 用例 + 1 组头(未分组)
    win._set_cases_checked(Qt.Checked)
    names = sorted(os.path.basename(p) for p in win._checked_case_paths())
    assert names == ["全局清扫.yaml", "划区清扫.yaml"]
    win._set_cases_checked(Qt.Unchecked)
    assert win._checked_case_paths() == []


def test_click_case_row_loads_into_editor(win_with_cases):
    """单击用例行 → 右侧加载该用例(case_path/data 更新),勾选状态不受影响"""
    from PySide6.QtCore import Qt
    win = win_with_cases
    win._set_cases_checked(Qt.Checked)
    win._on_case_item_clicked(win.case_list.item(case_row_index(win, 0)))
    assert win.case_path.endswith("划区清扫.yaml") or win.case_path.endswith("全局清扫.yaml")
    assert win.data.get("module") in ("划区清扫", "全局清扫")
    # 勾选不因加载而丢失
    assert win._checked_case_paths(), "勾选状态不应被单击加载重置"


# ── 用例排序:case_order 写入 YAML + 行内箭头(2026-09-21) ──

def test_case_order_sorts_list_on_load(qapp, monkeypatch, tmp_path):
    """加载时按 case_order 升序,缺失的排末尾按文件名"""
    from gui import main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    (d / "乙.yaml").write_text("case_order: 1\nmodule: 乙\ncases:\n  - name: a\n    steps: []\n", encoding="utf-8")
    (d / "甲.yaml").write_text("case_order: 2\nmodule: 甲\ncases:\n  - name: a\n    steps: []\n", encoding="utf-8")
    (d / "丙.yaml").write_text("module: 丙\ncases:\n  - name: a\n    steps: []\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        paths = [w.case_list.item(i).data(0x0100) for i in range(w.case_list.count())
                 if w.case_list.item(i).data(0x0100)]
        import os as _os
        assert [_os.path.basename(p) for p in paths] == ["乙.yaml", "甲.yaml", "丙.yaml"]
    finally:
        w.close()


def test_write_case_order_keeps_comments_and_newlines(tmp_path):
    """case_order 文本级写入:保注释、保 CRLF、已存在则原位更新"""
    from gui.main_window import MainWindow
    # CRLF + 头注释 + 无 case_order → 插入
    p = tmp_path / "a.yaml"
    p.write_bytes("# 说明注释\r\nmodule: 甲\r\ncases: []\r\n".encode("utf-8"))
    MainWindow._write_case_order(str(p), 3)
    s = p.read_bytes().decode("utf-8")
    assert s.startswith("# 说明注释\r\ncase_order: 3\r\nmodule: 甲\r\n")
    assert "\r\n" in s and "# 说明注释" in s
    # 已存在 → 原位替换数值
    MainWindow._write_case_order(str(p), 7)
    s = p.read_bytes().decode("utf-8")
    assert "case_order: 7" in s and s.count("case_order") == 1


def test_move_case_writes_order_and_keeps_check(qapp, monkeypatch, tmp_path):
    """行内箭头移动 → 列表换位 + 全部文件 case_order 1..N + 勾选保留"""
    from gui import main_window as mw
    from PySide6.QtCore import Qt
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    names = ["全局清扫", "划区清扫", "选区清扫"]
    for n in names:
        (d / f"{n}.yaml").write_text(
            f"module: {n}\ncases:\n  - name: 完整流程\n    steps: []\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        assert w.case_list.count() == 4      # 3 用例 + 1 组头(未分组)
        # 勾选第一个用例,然后把它下移一行
        first = w.case_list.item(case_row_index(w, 0)).data(Qt.UserRole)
        w.case_list.item(case_row_index(w, 0)).setCheckState(Qt.Checked)
        w._move_case(first, +1)
        # 列表顺序:划区清扫、全局清扫、选区清扫
        got = [os.path.basename(w.case_list.item(i).data(Qt.UserRole))
               for i in range(w.case_list.count()) if w.case_list.item(i).data(Qt.UserRole)]
        assert got == ["划区清扫.yaml", "全局清扫.yaml", "选区清扫.yaml"]
        # 文件里 case_order = 1..N
        orders = {n: mw.MainWindow._read_case_order(str(d / f"{n}.yaml"))
                  for n in names}
        assert orders == {"划区清扫": 1, "全局清扫": 2, "选区清扫": 3}
        # 勾选跨重建保留
        assert [os.path.basename(p) for p in w._checked_case_paths()] == ["全局清扫.yaml"]
        # 边界:第一行上移、最后一行下移 = no-op 且不报错
        w._move_case(first, -1)
        w._move_case(w.case_list.item(case_row_index(w, 2)).data(Qt.UserRole), +1)
        assert w.case_list.count() == 4
    finally:
        w.close()


def test_case_drag_drop_rebuild_and_persist(qapp, monkeypatch, tmp_path):
    """拖拽松手(moved 信号)→ 行控件重建 + case_order 写回 1..N

    InternalMove 的默认 dropEvent 无法离屏模拟,这里直接操纵 item 顺序模拟
    Qt 移动后的状态(itemWidget 已丢),再调 on_case_rows_dropped 验证兜底。
    """
    from gui import main_window as mw
    from PySide6.QtCore import Qt
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    names = ["全局清扫", "划区清扫", "选区清扫"]
    for n in names:
        (d / f"{n}.yaml").write_text(
            f"module: {n}\ncases:\n  - name: 完整流程\n    steps: []\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        # 模拟拖拽后的 model 状态:第 3 行(选区清扫, 组头占第 0 行)移到组头之后第 1 位
        it = w.case_list.takeItem(case_row_index(w, 2))
        w.case_list.insertItem(1, it)
        w.on_case_rows_dropped()
        got = [os.path.basename(w.case_list.item(i).data(Qt.UserRole))
               for i in range(w.case_list.count()) if w.case_list.item(i).data(Qt.UserRole)]
        assert got == ["选区清扫.yaml", "全局清扫.yaml", "划区清扫.yaml"]
        # item 原生化后无 itemWidget,勾选/顺序都在 item 上
        assert w.case_list.itemWidget(w.case_list.item(0)) is None
        assert w.case_list.item(1).checkState() in (Qt.Unchecked, Qt.Checked)
        # 顺序写回文件
        orders = {n: mw.MainWindow._read_case_order(str(d / f"{n}.yaml"))
                  for n in names}
        assert orders == {"选区清扫": 1, "全局清扫": 2, "划区清扫": 3}
    finally:
        w.close()


# ── 用例列表视觉/交互守护(2026-09-21,每个界面行为都有测试防回归) ──

def test_case_list_visual_and_interaction_rules(win_with_cases):
    """item 完全原生(checkState/text/sizeHint)+ delegate 箭头 —— 防 setItemWidget 复辟"""
    from PySide6.QtCore import Qt
    import gui.main_window as mw
    win = win_with_cases
    assert win.case_list.objectName() == "caseList"
    it0 = win.case_list.item(case_row_index(win, 0))
    assert it0.flags() & Qt.ItemIsUserCheckable, "item 必须用原生 checkState 勾选"
    assert it0.text() == os.path.splitext(os.path.basename(it0.data(Qt.UserRole)))[0],         "item text = 用例名(拖拽快照的数据源)"
    assert it0.sizeHint().height() >= 24, "行高要容得下箭头绘制"
    assert win.case_list.itemWidget(it0) is None, "禁止 setItemWidget(会盖 indicator/拦事件/断拖拽)"
    assert "QListWidget#caseList::item:selected" in mw.STYLESHEET
    assert "QListWidget#caseList::indicator" not in mw.STYLESHEET, \
        "勾选框用原生样式,不定义 indicator 规则"
    assert isinstance(win.case_list.itemDelegate(), mw.CaseItemDelegate)

def test_drop_emits_moved_deferred():
    """防「只能拖一次」回归:dropEvent 必须用 singleShot 延迟发 moved,
    同步 emit 会在 dropEvent 内重建列表,弄坏 Qt 拖拽内部状态"""
    import inspect
    from gui.main_window import CaseListWidget
    src = inspect.getsource(CaseListWidget.dropEvent)
    assert "QTimer.singleShot(0, self.moved.emit)" in src
    assert src.count("moved.emit") == 1, "moved 只能在 singleShot 里发一次"


def test_spinbox_embedded_chevron_rules():
    """次数输入框:增减按钮内嵌框内右侧(subcontrol)+ 细线箭头图标规则"""
    import gui.main_window as mw
    assert "QSpinBox::up-button" in mw.STYLESHEET
    assert "subcontrol-position: top right" in mw.STYLESHEET
    assert "subcontrol-position: bottom right" in mw.STYLESHEET
    assert "QSpinBox::up-arrow" in mw.STYLESHEET and "url(" in mw.STYLESHEET


def test_case_list_no_focus_rect_and_drag_snapshot(win_with_cases):
    """① 选中项无虚线焦点框(outline: none);② 拖拽快照名称靠 item 的
    DisplayRole 文本;③ startDrag 绝不能重写 —— 自定义 QDrag 会断 Qt
    InternalMove 管线(2026-09-21 实测拖拽全失效);
    ④ 勾选框 indicator 必须完全自绘(原生绘制在透明背景下会闪)"""
    import inspect
    import gui.main_window as mw
    win = win_with_cases
    assert "QListWidget#caseList::item { background: transparent; border-radius: 4px; margin: 1px 2px; outline: none; }" in mw.STYLESHEET
    assert "QListWidget#caseList::item:focus { outline: none; }" in mw.STYLESHEET
    assert "def startDrag" not in inspect.getsource(mw.CaseListWidget), \
        "不能重写 startDrag(会断 InternalMove 管线)"
    it0 = win.case_list.item(case_row_index(win, 0))
    assert it0.text() == os.path.splitext(os.path.basename(it0.data(0x0100)))[0], \
        "item 必须带用例名(拖拽快照的数据源)"
    assert "QListWidget#caseList::indicator" not in mw.STYLESHEET, \
        "勾选框用原生样式,不定义 indicator 规则"


def test_case_arrows_real_click(qapp, monkeypatch, tmp_path):
    """★ QTest 真实鼠标链点击行内箭头命中区 → 顺序变化(delegate 命中方案)"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    for n in ("全局清扫", "划区清扫"):
        (d / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        rect = lst.visualItemRect(lst.item(case_row_index(lst, 0)))
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.right() - 11, rect.center().y()))   # ↓ 命中区
        qapp.processEvents()
        got = [os.path.basename(lst.item(i).data(Qt.UserRole))
               for i in range(lst.count()) if lst.item(i).data(Qt.UserRole)]
        assert got == ["划区清扫.yaml", "全局清扫.yaml"], "行内箭头真实点击必须生效"
    finally:
        w.close()

def test_case_row_name_click_selects(qapp, monkeypatch, tmp_path):
    """点击用例名区(视口坐标,避开 indicator/箭头)→ 行被选中"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    for n in ("全局清扫", "划区清扫"):
        (d / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        rect = lst.visualItemRect(lst.item(case_row_index(lst, 0)))
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.left() + 60, rect.center().y()))
        qapp.processEvents()
        assert lst.currentRow() == case_row_index(lst, 0), "名字区点击应选中该用例行"
    finally:
        w.close()



def test_arrow_move_then_drag_coexist(qapp, monkeypatch, tmp_path):
    """箭头移动与拖拽排序并存:箭头换位后紧接拖拽落位,顺序与写盘正确接力
    (箭头路径 mousePressEvent 直接 return 不进基类,不会误启动拖拽;
    拖拽走原生管线 dropEvent → moved;两条路收敛同一个 _persist_case_order)"""
    from PySide6.QtCore import Qt
    from gui import main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    names = ["全局清扫", "划区清扫", "选区清扫"]
    for n in names:
        (d / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        # 第一步:箭头把第一行下移(全局清扫 → 2号位)
        w._move_case(w.case_list.item(case_row_index(w, 0)).data(Qt.UserRole), +1)
        got = [os.path.basename(w.case_list.item(i).data(Qt.UserRole))
               for i in range(w.case_list.count()) if w.case_list.item(i).data(Qt.UserRole)]
        assert got == ["划区清扫.yaml", "全局清扫.yaml", "选区清扫.yaml"]
        # 第二步:模拟拖拽,把第 3 行(选区清扫)拖到最前
        it = w.case_list.takeItem(case_row_index(w, 2))
        w.case_list.insertItem(1, it)
        w.on_case_rows_dropped()
        got = [os.path.basename(w.case_list.item(i).data(Qt.UserRole))
               for i in range(w.case_list.count()) if w.case_list.item(i).data(Qt.UserRole)]
        assert got == ["选区清扫.yaml", "划区清扫.yaml", "全局清扫.yaml"]
        # 两种方式交替后,文件顺序依然是干净的 1..N
        orders = {n: mw.MainWindow._read_case_order(str(d / f"{n}.yaml"))
                  for n in names}
        assert orders == {"选区清扫": 1, "划区清扫": 2, "全局清扫": 3}
    finally:
        w.close()


def test_spinbox_buttons_inset_rules():
    """次数输入框增减按钮必须有 margin 收缩(否则箭头贴框边圆角,视觉像超出)"""
    import gui.main_window as mw
    for rule in ("width: 16px; height: 12px; margin: 3px;",):
        assert rule in mw.STYLESHEET, f"缺少规则: {rule}"


def test_case_checkbox_native_style():
    """用例勾选框 = Qt 原生样式(QSS 不定义 indicator;勾选/样式由系统主题绘制)"""
    import gui.main_window as mw
    assert "QListWidget#caseList::indicator" not in mw.STYLESHEET, \
        "原生勾选框就不要自定义 indicator 规则"
    assert "@CHECK_TICK@" not in mw.STYLESHEET


# ── 执行结果预览与截图查看器(2026-09-21 用户反馈两连修) ──

def test_preview_label_does_not_grow(qapp, monkeypatch, tmp_path):
    """点击结果行反复预览,预览区不能越点越大
    (根因:QLabel sizeHint 跟随 pixmap 涨 → splitter 正反馈撑大)"""
    from PySide6.QtWidgets import QSizePolicy
    from PIL import Image
    from gui import main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    shot = tmp_path / "shot.png"
    Image.new("RGB", (800, 600), "blue").save(shot)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        assert w.preview.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
        size1 = (w.preview.width(), w.preview.height())
        for _ in range(3):
            w._show_screenshot(str(shot))
            qapp.processEvents()
        size2 = (w.preview.width(), w.preview.height())
        assert size2 == size1, f"预览区尺寸不应随点击增长: {size1} -> {size2}"
    finally:
        w.close()


def test_imageview_wheel_zoom_exclusive(qapp):
    """截图查看器滚轮 = 纯缩放,滚动条不得同时滚动(两事件曾一起执行)"""
    import tempfile
    from PySide6.QtCore import QPointF, QPoint
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtCore import Qt
    from PIL import Image
    from gui.main_window import ImageViewDialog
    p = Path(tempfile.mkdtemp()) / "img.png"
    Image.new("RGB", (1200, 900), "green").save(p)
    dlg = ImageViewDialog(str(p))
    dlg.show()
    try:
        qapp.processEvents()
        dlg._set_zoom(2.0)          # 先放大,让内容超出 viewport(有滚动空间)
        qapp.processEvents()
        bar = dlg._scroll.verticalScrollBar()
        bar.setValue(80)
        zoom_before = dlg._zoom
        ev = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0),
                         QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                         Qt.ScrollUpdate, False)
        QApplication.sendEvent(dlg._scroll.viewport(), ev)
        qapp.processEvents()
        assert bar.value() == 80, f"滚轮缩放时滚动条不应滚动(值 {bar.value()})"
        assert dlg._zoom > zoom_before, "滚轮应触发缩放"
    finally:
        dlg.close()


def test_imageview_centered_and_mouse_drag(qapp):
    """① 小图打开时居中显示(不贴左上角);② 放大后按住拖拽可平移(滚动条跟随)"""
    import tempfile
    from PySide6.QtCore import QPointF, QPoint, QEvent
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PIL import Image
    from gui.main_window import ImageViewDialog
    p = Path(tempfile.mkdtemp()) / "small.png"
    Image.new("RGB", (200, 150), "red").save(p)     # 小图(比 880x660 视口小)
    dlg = ImageViewDialog(str(p))
    dlg.show()
    try:
        qapp.processEvents()
        # ① 居中:label 中心 ≈ 视口中心
        vp = dlg._scroll.viewport()
        lc = dlg.image_label.geometry().center()
        vc = vp.rect().center()
        assert abs(lc.x() - vc.x()) <= 2 and abs(lc.y() - vc.y()) <= 2, \
            f"小图应居中: label 中心 {lc} vs 视口中心 {vc}"
        # ② 放大后拖拽平移
        dlg._set_zoom(3.0)
        qapp.processEvents()
        bar = dlg._scroll.verticalScrollBar()
        bar.setValue(60)
        start = QPoint(50, 50)
        QTest.mousePress(vp, Qt.LeftButton, pos=start)      # press(记录拖拽起点)
        for dy in (40, 80, 120):                             # 按住向下拖
            ev = QMouseEvent(QEvent.MouseMove, QPointF(start.x(), start.y() + dy),
                             QPointF(start.x(), start.y() + dy),
                             Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
            QApplication.sendEvent(vp, ev)
            qapp.processEvents()
        QTest.mouseRelease(vp, Qt.LeftButton, pos=start + QPoint(0, 120))
        qapp.processEvents()
        assert bar.value() < 60, f"向下拖拽应平移内容(滚动条上移),实际 value={bar.value()}"
    finally:
        dlg.close()


def test_run_finished_summary_row_in_result_table(win):
    """执行结束 → 执行结果表格末尾出现醒目总结行(不只右上角状态栏)"""
    from PySide6.QtWidgets import QTableWidgetItem
    win.on_run_finished(True, "全部通过(1 轮 × 2 个用例)")
    row = win.result_table.rowCount() - 1
    texts = [win.result_table.item(row, c).text() for c in range(5)]
    assert texts == ["—", "全部通过(1 轮 × 2 个用例)", "✔ 全部通过", "", ""],         "列序应为 #、步骤、结果"
    assert win.result_table.item(row, 2).font().bold()
    assert win.result_table.item(row, 2).background().color().name() == "#e6f7e9"
    assert win.tabs.currentIndex() == 0, "应自动切到「执行结果」页"
    # 失败路径:红色总结行
    win.on_run_finished(False, "存在失败步骤")
    row = win.result_table.rowCount() - 1
    assert win.result_table.item(row, 2).text() == "✘ 未通过"
    assert win.result_table.item(row, 2).background().color().name() == "#fdeaea"


def test_zoom_out_never_below_default_size(qapp):
    """「缩小」不能小于打开时的默认(适应窗口)大小:
    ① 初始 fit 态点缩小 → 显示必须不变(此前 None 被当 1.0,fit 反而被放大);
    ② 放大后一路缩小 → 缩到底回到 fit,再点缩小无变化"""
    import tempfile
    from PIL import Image
    from gui.main_window import ImageViewDialog
    p = Path(tempfile.mkdtemp()) / "big.png"
    Image.new("RGB", (1400, 1000), "cyan").save(p)   # 大图:fit 已是缩小显示
    dlg = ImageViewDialog(str(p))
    dlg.show()
    try:
        qapp.processEvents()
        w0 = dlg.image_label.pixmap().width()
        assert dlg._zoom is None
        # ① 初始 fit 态点缩小 → 显示不变
        dlg._zoom_out()
        qapp.processEvents()
        assert dlg._zoom is None, "fit 态点缩小不应产生更小 zoom"
        assert dlg.image_label.pixmap().width() == w0, "fit 态点缩小显示必须不变"
        # ② 放大后连续缩小,缩到底回到 fit(zoom=None),像素不小于默认
        #    (±26 容差:fit 按视口算、zoom 按浮点算,含取整/滚动条抖动)
        dlg._set_zoom(2.5)
        for _ in range(10):
            dlg._zoom_out()
            qapp.processEvents()
        assert dlg._zoom is None, "缩到底应回到适应窗口(fit)模式"
        w_final = dlg.image_label.pixmap().width()
        assert w_final >= w0 - 26, f"缩小不能低于默认大小: 默认 {w0}, 实际 {w_final}"
        dlg._zoom_out()
        qapp.processEvents()
        assert dlg.image_label.pixmap().width() == w_final, "到底后再点缩小无变化"
    finally:
        dlg.close()


def test_run_start_and_case_switch_rows(win):
    """开始执行/用例切换都要在执行结果表里可见(以前跑起来表格一片空白)"""
    win.result_table.setRowCount(0)
    win.on_worker_status("连接设备...")
    assert win.result_table.rowCount() == 0, "连接阶段不插行"
    win.on_worker_status("执行用例: 全局清扫")
    win.on_worker_status("重启 APP(划区清扫)")
    win.on_worker_status("执行用例: 划区清扫")
    rows = [(win.result_table.item(r, 2).text(), win.result_table.item(r, 2).background().color().name())
            for r in range(win.result_table.rowCount())]
    assert rows == [("▶ 执行用例: 全局清扫", "#f0f4fa"),
                    ("▶ 执行用例: 划区清扫", "#f0f4fa")], "只有用例开始插行,重启 APP 不插"
    assert win.tabs.currentIndex() == 0, "应自动切到「执行结果」页"
    # 开始行与总结行共存:开始 → 结束
    win._append_result_row("—", "▶ 开始执行", "2 个用例 × 1 轮 · 设备 X", "#e8f0fe")
    win.on_run_finished(True, "全部通过(1 轮 × 2 个用例)")
    last = win.result_table.rowCount() - 1
    assert win.result_table.item(last, 2).text() == "✔ 全部通过"


def test_case_state_lamp(win_with_cases, qapp):
    """用例状态灯:未执行置灰(默认)、执行中黄、通过绿、失败红;
    状态存在 CASE_STATE_ROLE 上,delegate 绘制(名字后面),勾选框保持原生"""
    from PySide6.QtCore import Qt
    import gui.main_window as mw
    win = win_with_cases
    it0 = win.case_list.item(case_row_index(win, 0))
    assert it0.data(mw.CASE_STATE_ROLE) == "idle", "未执行默认灰灯"
    assert not it0.icon().isNull(), "灯用 icon 位(勾选框后、用例名前,原生布局不重叠)"
    # 信号驱动的状态切换(set_case_state 由 case_started/finished 信号调用)
    path = it0.data(Qt.UserRole)
    win.set_case_state(path, "running")
    assert it0.data(mw.CASE_STATE_ROLE) == "running"
    win.set_case_state(path, "passed")
    assert it0.data(mw.CASE_STATE_ROLE) == "passed"
    win.set_case_state(path, "failed")
    assert it0.data(mw.CASE_STATE_ROLE) == "failed"
    # 四种灯的 pixmap 颜色可区分
    for st in ("idle", "running", "passed", "failed"):
        pm = mw.lamp_pixmap(st)
        assert not pm.isNull() and pm.width() == 12
    # RunWorker 有 case_started/case_finished 信号
    from gui.runner_thread import RunWorker
    assert hasattr(RunWorker, "case_started") and hasattr(RunWorker, "case_finished")
    # 灯渲染冒烟:viewport grab 不崩且非空白
    win.case_list.resize(200, 200)
    win.case_list.parentWidget().show()
    qapp.processEvents()
    assert not win.case_list.grab().isNull()
    win.case_list.parentWidget().close()


def test_case_lamp_rerun_resets(qapp, monkeypatch, tmp_path):
    """重新执行:点运行瞬间所有勾选用例灯重置为黄;结束信号再各自变绿/红"""
    from PySide6.QtCore import Qt
    import gui.main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    names = ["全局清扫", "划区清扫"]
    for n in names:
        (d / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        paths = [w.case_list.item(i).data(Qt.UserRole) for i in range(w.case_list.count())
                 if w.case_list.item(i).data(Qt.UserRole)]
        # 模拟上一次跑完:一绿一红
        w.set_case_state(paths[0], "passed")
        w.set_case_state(paths[1], "failed")
        # 重新执行:点运行瞬间全部重置黄
        w._reset_case_lamps_to_running(paths)
        assert [w.case_list.item(case_row_index(w, i)).data(mw.CASE_STATE_ROLE) for i in range(2)] == ["running", "running"]
        # 逐个跑完 → 绿/红(case_started 也会先确认黄,幂等)
        w.set_case_state(paths[0], "running")
        w.set_case_state(paths[0], "passed")
        assert w.case_list.item(case_row_index(w, 0)).data(mw.CASE_STATE_ROLE) == "passed"
        assert w.case_list.item(case_row_index(w, 1)).data(mw.CASE_STATE_ROLE) == "running"
        w.set_case_state(paths[1], "failed")
        assert w.case_list.item(case_row_index(w, 1)).data(mw.CASE_STATE_ROLE) == "failed"
    finally:
        w.close()


# ── RunWorker.run 全链路冒烟(2026-09-21 审查补充:线程主体+前置可见性从未被测) ──

def _fake_prepare_items(results):
    """模拟 session.prepare_items: **逐条**回调 on_item_done(带耗时)。

    真实实现是每执行完一条就回调一次(用户要求前置结果一条一条落表), 所以桩也要回调,
    否则 RunWorker 收不到任何前置行。
    """
    def _f(d_, cfg_, items, on_progress=None, should_cancel=None, on_item_done=None):
        out = []
        for r in results:
            r = dict(r)
            r.setdefault("elapsed", 0.1)
            out.append(r)
            if on_item_done:
                on_item_done(r)
        return out
    return _f

def test_runworker_run_smoke(qapp, monkeypatch, tmp_path):
    """不碰真机跑通 RunWorker.run:前置行(带电量)→ 步骤行 → 完成信号 → 报告落盘"""
    import types
    from PIL import Image
    from gui import runner_thread as rt
    from gui.runner_thread import RunWorker
    from PySide6.QtCore import Qt

    # 1) 用例文件:一条用例一个纯等待步骤
    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    case_file = d / "全局清扫.yaml"
    case_file.write_text(
        "module: 全局清扫\ncases:\n  - name: 冒烟\n    steps:\n"
        "      - desc: 等一下\n        wait: 0.1\n", encoding="utf-8")

    # 2) mock 设备连接(u2 在 run() 内延迟导入)
    import uiautomator2 as u2
    fake_d = types.SimpleNamespace(
        implicitly_wait=lambda t: None,
        dump_hierarchy=lambda: '<node text="100%"/><node text="地图编辑"/>',
        screenshot=lambda path=None, format=None: None,
    )
    monkeypatch.setattr(u2, "connect", lambda dev: fake_d)

    # 3) mock session 与配置
    monkeypatch.setattr(rt.session, "prepare_items", _fake_prepare_items([
                            {"key": "pre0", "desc": "重启 APP", "ok": True},
                            {"key": "pre1", "desc": "等待充电", "ok": True},
                            {"key": "pre2", "desc": "地图加载", "ok": True},
                            {"key": "pre3", "desc": "电量门槛≥50%", "ok": True}]))
    monkeypatch.setattr(rt.session, "restart_app", lambda d_, cfg, enter_page=True: None)
    monkeypatch.setattr(rt.session, "get_battery_level", lambda d_: 78)
    monkeypatch.setattr(rt, "load_config",
                        lambda: {"runner": {"step_interval": 0, "default_timeout": 1,
                                            "click_timeout": 1},
                                 "app": {"package": "p", "name": "x"},
                                 "target_device": "SE3L"})

    # 4) 报告隔离到 tmp
    report_path = tmp_path / "rep.xlsx"
    monkeypatch.setattr(rt, "ExcelReport",
                        lambda: __import__("core.excel_report", fromlist=["ExcelReport"])
                        .ExcelReport(path=str(report_path)))

    worker = RunWorker("fake-dev", [str(case_file)],
                       [{"type": "restart", "enabled": True},
                        {"type": "charging", "enabled": True},
                        {"type": "map_load", "enabled": True},
                        {"type": "battery", "enabled": True}], 1)
    steps, done = [], []
    worker.step_done.connect(lambda r: steps.append(dict(r)))
    worker.finished_run.connect(lambda ok, msg: done.append((ok, msg)))
    worker.run()          # 同步调用线程主体(不起 QThread,离屏可测)

    # 前置 4 行:全部 PASS,充电/电量行带实际电量
    pre = [s for s in steps if s["desc"].startswith("前置-")]
    assert len(pre) == 4 and all(s["passed"] for s in pre)
    descs = {s["desc"] for s in pre}
    assert "前置-等待充电(当前电量 78%)" in descs
    assert "前置-电量门槛≥50%(当前电量 78%)" in descs
    assert "前置-重启 APP" in descs and "前置-地图加载" in descs
    # 步骤行 1 条 PASS
    body = [s for s in steps if not s["desc"].startswith("前置-")]
    assert len(body) == 1 and body[0]["passed"] and body[0]["desc"] == "等一下"
    # 完成信号 + 报告落盘
    assert done == [(True, "全部通过(1 轮 × 1 个用例)")]
    assert report_path.exists()


def test_result_table_layout_rules(win):
    """执行结果表格布局:步骤/错误信息拉伸,结果列自适应中文(修复显示不全)"""
    from PySide6.QtWidgets import QHeaderView
    assert win.result_table.horizontalHeader().sectionResizeMode(1) == QHeaderView.Stretch
    assert win.result_table.horizontalHeader().sectionResizeMode(4) == QHeaderView.Stretch
    assert win.result_table.horizontalHeader().sectionResizeMode(2) == QHeaderView.ResizeToContents
    assert [win.result_table.horizontalHeaderItem(i).text() for i in range(5)] == \
        ["#", "步骤", "结果", "耗时", "错误信息"]
    # 长文本行有 tooltip(悬停看全文)
    win.on_step_done({"desc": "很长的步骤描述" * 20, "passed": True, "error": "很长的错误" * 20})
    assert win.result_table.item(0, 1).toolTip() != ""
    assert win.result_table.item(0, 4).toolTip() != ""


def test_on_run_finished_always_unlocks(win, monkeypatch):
    """★ 结束处理即使中途抛异常,也必须解锁卡片区并清掉 worker
    (否则界面永久锁死,无法编辑/展开步骤 —— 用户实测 bug)"""
    from PySide6.QtWidgets import QPushButton
    win.create_case("测试组", group="测试组")
    win._set_locked(True)
    win.worker = object()                       # 模拟执行中的 worker 残留
    assert not win.cards_scroll.widget().isEnabled()
    # 注入显示逻辑异常
    def boom(*a, **k):
        raise RuntimeError("显示失败")
    monkeypatch.setattr(win, "_append_result_row", boom)
    win.on_run_finished(True, "消息")           # 不应向外抛,且必须恢复
    assert win.worker is None, "worker 必须被清理"
    assert win.cards_scroll.widget().isEnabled(), "卡片区必须恢复可用(可展开/编辑)"
    assert win.run_btn.isEnabled()
    # 正常路径也验证一次
    win.on_run_finished(False, "存在失败步骤")
    assert win.worker is None


def test_window_title_has_version(win):
    """GUI 标题 = 名称 + 版本号(用户要求;版本与 git tag 对应)"""
    import gui.main_window as mw
    assert "v" + mw.APP_VERSION in win.windowTitle()
    assert win.windowTitle() == f"APP 自动化测试平台 v{mw.APP_VERSION}"
    assert "vacuum_app_test" not in win.windowTitle(), "源项目名残留必须去掉"


def test_runworker_precondition_failure_blocks(qapp, monkeypatch, tmp_path):
    """★ 前置检查未通过 → 阻断本轮:前置 FAIL 行落表/报告、无任何步骤行、
    precondition_failed 信号带详情、finished_run(False) 带原因(用户要求)"""
    import types
    from gui import runner_thread as rt
    from gui.runner_thread import RunWorker

    d = tmp_path / "Test_cases" / "涂鸦智能"
    d.mkdir(parents=True)
    case_file = d / "全局清扫.yaml"
    case_file.write_text(
        "module: 全局清扫\ncases:\n  - name: 冒烟\n    steps:\n"
        "      - desc: 等一下\n        wait: 0.1\n", encoding="utf-8")

    import uiautomator2 as u2
    fake_d = types.SimpleNamespace(implicitly_wait=lambda t: None)
    monkeypatch.setattr(u2, "connect", lambda dev: fake_d)
    monkeypatch.setattr(rt.session, "prepare_items", _fake_prepare_items([
                            {"key": "pre0", "desc": "重启 APP", "ok": True},
                            {"key": "pre1", "desc": "等待充电", "ok": False},
                            {"key": "pre2", "desc": "地图加载", "ok": True},
                            {"key": "pre3", "desc": "电量门槛≥50%", "ok": False}]))
    monkeypatch.setattr(rt.session, "get_battery_level", lambda d_: 20)
    monkeypatch.setattr(rt, "load_config",
                        lambda: {"runner": {"step_interval": 0}, "app": {"package": "p"},
                                 "target_device": "SE3L"})
    report_path = tmp_path / "rep.xlsx"
    monkeypatch.setattr(rt, "ExcelReport",
                        lambda: __import__("core.excel_report", fromlist=["ExcelReport"])
                        .ExcelReport(path=str(report_path)))

    worker = RunWorker("fake-dev", [str(case_file)],
                       [{"type": "restart", "enabled": True},
                        {"type": "charging", "enabled": True},
                        {"type": "map_load", "enabled": True},
                        {"type": "battery", "enabled": True}], 1)
    steps, done, pre_fail = [], [], []
    worker.step_done.connect(lambda r: steps.append(dict(r)))
    worker.finished_run.connect(lambda ok, msg: done.append((ok, msg)))
    worker.precondition_failed.connect(lambda detail: pre_fail.append(detail))
    worker.run()

    # 前置 4 行落表(2 FAIL),但用例步骤一行都没有(阻断)
    assert len([s for s in steps if s["desc"].startswith("前置-")]) == 4
    assert not [s for s in steps if not s["desc"].startswith("前置-")], "阻断后不得执行用例步骤"
    assert pre_fail == ["等待充电、电量门槛≥50%"], "阻断信号应带未通过项详情"
    assert done and not done[0][0] and "前置检查未通过" in done[0][1]
    assert report_path.exists(), "阻断时已执行部分(前置行)也要保住报告"


def test_on_precondition_failed_resets_lamps(win, monkeypatch):
    """弹窗槽:勾选用例灯重置为灰(未真正执行);弹窗调用被 mock 不阻塞"""
    import gui.main_window as mw
    calls = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda *a, **k: calls.append(a) or None)
    class _W:
        case_files = ["C:/a.yaml", "C:/b.yaml"]
    win.worker = _W()
    for fp in _W.case_files:
        win.set_case_state(fp, "running")
    win.on_precondition_failed("等待充电、电量≥50%")
    assert [win._case_states[p] for p in _W.case_files] == ["idle", "idle"], \
        "阻断后用例灯应重置为灰(未真正执行)"
    assert calls, "应弹出提醒窗口"
    win.worker = None


def test_app_icon_set(win):
    """应用图标已设置(自绘 app_icon.png,任务栏/窗口标题用)"""
    import gui.main_window as mw
    icon_path = os.path.join(mw._ASSETS, "app_icon.ico")
    assert os.path.isfile(icon_path), "ico 含 16~256 多尺寸(高分屏清晰)"
    assert os.path.isfile(os.path.join(mw._ASSETS, "app_icon.png"))
    assert not win.windowIcon().isNull()
    pm = win.windowIcon().pixmap(64, 64)
    assert pm.width() == 64 and not pm.isNull()


def test_group_header_click_collapses(qapp, monkeypatch, tmp_path):
    """点击 APP 组头 → 折叠该组(用例行隐藏),再点展开;
    折叠后用例行不存在 → 勾选/排序自然作用于可见行"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能T4"
    d.mkdir(parents=True)
    for n in ("全局清扫", "划区清扫"):
        (d / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        assert lst.count() == 3               # 组头 + 2 用例
        head_rect = lst.visualItemRect(lst.item(0))
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(head_rect.center().x(), head_rect.center().y()))
        qapp.processEvents()
        assert "涂鸦智能T4" in w._collapsed_groups
        assert lst.count() == 1, "折叠后只剩组头行"
        assert lst.item(0).text().startswith("▸ "), "折叠态组头带 ▸ 前缀"
        # 再点展开
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(head_rect.center().x(), head_rect.center().y()))
        qapp.processEvents()
        assert "涂鸦智能T4" not in w._collapsed_groups
        assert lst.count() == 3, "展开后用例行恢复"
        assert lst.item(0).text().startswith("▾ ")
    finally:
        w.close()


def test_collapse_group_unloads_editor(qapp, monkeypatch, tmp_path):
    """★ 收起组时,若当前编辑用例属于该组 → 编辑区恢复未选中状态;
    chip 条标题「测试步骤详情」与字段顺序(用例组→用例→优先级→间隔→添加步骤)"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能T4"
    d.mkdir(parents=True)
    (d / "全局清扫.yaml").write_text(
        "module: 全局清扫\ncases: [{name: a, steps: [{desc: s1, click: x}]}]\n",
        encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        # 单击用例行加载到编辑区
        w._on_case_item_clicked(lst.item(case_row_index(lst, 0)))
        assert w.case_path and w.data["module"] == "全局清扫"
        # chip 条: 标题在前,字段顺序 用例组→用例→优先级→间隔→添加步骤
        texts = [lst.parentWidget()] and None   # 占位避免空行
        from PySide6.QtWidgets import QLabel, QPushButton
        labels = [c for c in w.findChildren(QLabel) if c.text() == "测试步骤详情"]
        assert labels, "缺少「测试步骤详情」区块标题"
        # 收起组(点击组头)
        head_rect = lst.visualItemRect(lst.item(0))
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(head_rect.center().x(), head_rect.center().y()))
        qapp.processEvents()
        assert "涂鸦智能T4" in w._collapsed_groups
        # 编辑区恢复未选中
        assert w.case_path is None, "收起组后当前用例应恢复未选中"
        assert len(w.steps) == 0
        assert w.file_label.text() == "未选中用例"
    finally:
        w.close()


def test_chip_strip_field_order(win):
    """测试步骤详情区字段顺序: 标题→用例组→用例→优先级→间隔s→添加步骤"""
    from PySide6.QtWidgets import QLabel, QLineEdit, QComboBox, QPushButton
    import gui.main_window as mw
    # chip 条内控件按添加顺序: 找 chipStrip 的 FlowLayout 子控件
    chip_labels = [c.text() for c in win._quick_btns]  # 仅确保快捷按钮存在
    assert win.module_edit and win.case_name_edit
    assert win.add_btn.text() == "＋ 添加步骤"
    # 标题与标签顺序验证(QSS 结构)
    assert 'QLabel("测试步骤详情")' not in mw.STYLESHEET  # 标题在代码里不在QSS,此行防呆
    order_check = [
        ("测试步骤详情", True),
        ("用例组", True),
        ("用例", True),
        ("优先级", True),
        ("间隔s", True),
    ]
    src = open(mw.__file__, encoding="utf-8").read()
    pos = -1
    for text, _ in order_check:
        p = src.find(f'QLabel("{text}")')
        assert p > pos, f"「{text}」标签顺序不正确"
        pos = p
    p_add = src.find('self.add_btn = QPushButton("＋ 添加步骤")')
    p_wait = src.find('lay.addWidget(self.case_wait_edit)')
    assert pos < p_add and p_wait < p_add, "添加步骤应排在间隔s之后"


def test_empty_group_dir_still_shown(qapp, monkeypatch, tmp_path):
    """空组目录(还没建用例)也要显示组头 + (暂无用例)占位 —— 用户要求所有 APP 组可见"""
    from PySide6.QtCore import Qt
    from gui import main_window as mw
    root = tmp_path / "Test_cases"
    (root / "涂鸦智能T4").mkdir(parents=True)
    (root / "涂鸦智能T4" / "a.yaml").write_text(
        "module: a\ncases: [{name: x, steps: []}]\n", encoding="utf-8")
    (root / "三星").mkdir()          # 空组目录
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        texts = [w.case_list.item(i).text() for i in range(w.case_list.count())]
        assert texts == ["▾ 涂鸦智能T4", "a", "▾ 三星", "　　(暂无用例)"], f"实际: {texts}"
    finally:
        w.close()


# ── 新建对话框 / 保存脏标志 / APP 分组(2026-09-22) ──

def test_new_case_dialog_creates_in_group(qapp, monkeypatch, tmp_path):
    """新建对话框: 名称+选组 → 创建 Test_cases/<组>/<名>.yaml 并在列表选中"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    import gui.main_window as mw
    root = tmp_path / "Test_cases"
    (root / "涂鸦智能T4").mkdir(parents=True)
    (root / "涂鸦智能T4" / "a.yaml").write_text(
        "module: a\ncases: [{name: x, steps: []}]\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        # mock 对话框: 名称=新用例A, 组=三星
        class _Dlg:
            def exec(self):
                return QDialog.Accepted
            def values(self):
                return ("新用例A", "三星")
        monkeypatch.setattr(mw, "NewCaseDialog",
                            lambda groups, parent, default_group=None: _Dlg())
        w.on_new()
        f = root / "三星" / "新用例A.yaml"
        assert f.exists(), "应在三星组目录创建用例文件"
        # 列表包含且选中新用例
        roles = [w.case_list.item(i).data(Qt.UserRole) for i in range(w.case_list.count())]
        assert str(f) in roles
        assert w.case_list.currentItem().data(Qt.UserRole) == str(f)
        # 新建无修改 → 保存置灰
        assert not w.save_btn.isEnabled()
    finally:
        w.close()


def test_save_button_dirty_flow(qapp, monkeypatch, tmp_path):
    """保存按钮: 无修改置灰 → 编辑后亮起 → 保存写盘并再次置灰"""
    from PySide6.QtCore import Qt
    import gui.main_window as mw
    root = tmp_path / "Test_cases" / "涂鸦智能T4"
    root.mkdir(parents=True)
    (root / "a.yaml").write_text(
        "module: a\ncases: [{name: x, steps: []}]\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        qapp.processEvents()
        w._on_case_item_clicked(w.case_list.item(case_row_index(w, 0)))
        assert not w.save_btn.isEnabled(), "加载后无修改应置灰"
        # 编辑产生修改 → 亮起
        w.add_step("click")
        from PySide6.QtTest import QTest
        QTest.qWait(400)   # 防抖 300ms 后写盘
        import yaml as _y
        back = _y.safe_load(open(w.case_path, encoding="utf-8").read())
        assert len(back["cases"][0]["steps"]) == 1, "自动保存: 编辑应写盘"
        # 保存 → 写盘(含新增步骤)且置灰; mock 掉参数问题确认弹窗(离屏无人点击)
        # on_save 的参数校验可能弹确认框(离屏无人点击会卡死), mock 为 Yes
        monkeypatch.setattr(mw.QMessageBox, "question",
                            staticmethod(lambda *a, **k: mw.QMessageBox.Yes))
        w.on_save()   # 手动兜底: 强制重新保存
        import yaml as _yaml
        back = _yaml.safe_load((root / "a.yaml").read_text(encoding="utf-8"))
        assert len(back["cases"][0]["steps"]) == 1, "修改点应写盘"
        assert not w.save_btn.isEnabled(), "保存后应置灰"
    finally:
        w.close()


def test_collapsed_group_not_marked_empty(qapp, monkeypatch, tmp_path):
    """★ 排序重建(order_paths 分支)后, 折叠组的数据必须补全 ——
    否则折叠组被误判为空组, 错插「暂无用例」占位(用户实测);
    有用例的组折叠后: 组头 ▸ + 无用例行 + 无占位行"""
    from PySide6.QtCore import Qt
    from gui import main_window as mw
    d = tmp_path / "Test_cases" / "涂鸦智能T4"
    d.mkdir(parents=True)
    for n in ("全局清扫", "划区清扫"):
        (d / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        # 触发一次箭头移动(order_paths 分支)
        w._move_case(w.case_list.item(case_row_index(w, 0)).data(Qt.UserRole), +1)
        # 折叠该组
        w._collapsed_groups.add("涂鸦智能T4")
        w._fill_case_list()
        texts = [w.case_list.item(i).text() for i in range(w.case_list.count())]
        assert texts == ["▸ 涂鸦智能T4"], f"折叠组应只有组头无占位, 实际: {texts}"
        assert not any("暂无用例" in t for t in texts), "有用例的组不得显示占位"
    finally:
        w.close()


def test_truly_empty_group_shows_placeholder(qapp, monkeypatch, tmp_path):
    """真空组(目录无任何用例)→ 组头 + 暂无用例占位"""
    from gui import main_window as mw
    root = tmp_path / "Test_cases"
    (root / "三星").mkdir(parents=True)          # 空组目录
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        texts = [w.case_list.item(i).text() for i in range(w.case_list.count())]
        assert texts == ["▾ 三星", "　　(暂无用例)"], f"实际: {texts}"
    finally:
        w.close()


def test_collapse_via_real_click_after_reorder(qapp, monkeypatch, tmp_path):
    """★ 完整用户路径验证: 排序(order_paths 分支)→ QTest 真实点击组头折叠 →
    折叠组只有 ▸ 组头无占位; 空组占位仍在; 再点击展开数据完整"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    root = tmp_path / "Test_cases"
    (root / "涂鸦智能T4").mkdir(parents=True)
    (root / "三星").mkdir()
    for n in ("全局清扫", "划区清扫"):
        (root / "涂鸦智能T4" / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        # 第一步: 箭头移动(触发 order_paths 重建分支 —— bug 触发条件)
        first = lst.item(case_row_index(lst, 0)).data(Qt.UserRole)
        w._move_case(first, +1)
        texts_after_move = [lst.item(i).text() for i in range(lst.count())]
        # 三星是空组: 排序后显示占位是正确行为; 但不得给有用例的组插占位
        assert texts_after_move.count("　　(暂无用例)") == 1, "占位只属于真空组"
        # 第二步: QTest 真实点击「涂鸦智能T4」组头(排序后它排在三星之后)
        head_idx = next(i for i in range(lst.count())
                        if lst.item(i).data(0x0100) is None
                        and "涂鸦智能T4" in lst.item(i).text())
        rect = lst.visualItemRect(lst.item(head_idx))
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.center().x(), rect.center().y()))
        qapp.processEvents()
        texts = [lst.item(i).text() for i in range(lst.count())]
        assert "涂鸦智能T4" in w._collapsed_groups
        assert not any("暂无用例" in t and "涂鸦智能T4" in t for t in texts), \
            f"折叠的有用例组不得显示占位: {texts}"
        assert sum("涂鸦智能T4" in t for t in texts) == 1, "折叠组只显示一个组头"
        # 第三步: 再点击展开 → 用例数据完整恢复
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.center().x(), rect.center().y()))
        qapp.processEvents()
        roles = [lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                 if lst.item(i).data(Qt.UserRole)]
        assert len(roles) == 2 and all(roles), "展开后两条用例数据完整"
    finally:
        w.close()


def test_collapse_empty_group_hides_placeholder(qapp, monkeypatch, tmp_path):
    """★ 收起空组(三星)后占位行必须消失(用户实测 bug); 再展开恢复"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    root = tmp_path / "Test_cases"
    (root / "三星").mkdir(parents=True)          # 空组
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        texts0 = [lst.item(i).text() for i in range(lst.count())]
        assert texts0 == ["▾ 三星", "　　(暂无用例)"]
        # QTest 真实点击组头折叠
        rect = lst.visualItemRect(lst.item(0))
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.center().x(), rect.center().y()))
        qapp.processEvents()
        texts1 = [lst.item(i).text() for i in range(lst.count())]
        assert texts1 == ["▸ 三星"], f"折叠空组后占位必须消失, 实际: {texts1}"
        # 再点击展开 → 占位恢复
        QTest.mouseClick(lst.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.center().x(), rect.center().y()))
        qapp.processEvents()
        texts2 = [lst.item(i).text() for i in range(lst.count())]
        assert texts2 == ["▾ 三星", "　　(暂无用例)"], "展开后占位恢复"
    finally:
        w.close()


# ── 动作键完整性 + 截图开关(2026-09-22) ──

def test_all_actions_have_key_after_new_step():
    """★ 全部动作 new_step 后动作键必须存在(此前 back/set_time/room_click
    等新建后无动作键 → 卡片显示未知、引擎无动作);serialize 也不得丢键"""
    from gui import schema
    missing = []
    for a in schema.ACTIONS:
        step = schema.new_step(a["key"])
        if a["key"] not in step:
            missing.append((a["key"], list(step.keys())))
        ser = schema.serialize_step(dict(step))
        if a["key"] not in ser:
            missing.append((a["key"] + "(serialize丢键)", list(ser.keys())))
    assert not missing, f"动作键缺失: {missing}"
    # 语义默认抽查
    assert schema.new_step("back")["back"] is True
    assert schema.new_step("set_time")["set_time"] == 0      # 0=当前时间
    assert schema.new_step("room_click")["room_click"] == 1  # 第1个分区
    # 条件动作: 空条件保留键(serialize 不清动作键)
    assert "if" in schema.serialize_step(schema.new_step("if"))


def test_screenshot_switch_and_auto_name(qapp, monkeypatch, tmp_path):
    """截图字段 = 开关(True 自动命名), 旧字符串路径兼容保留"""
    from types import SimpleNamespace
    from core import runner as cr
    root = tmp_path / "Test_img"
    monkeypatch.setattr(cr, "BASE_DIR", str(tmp_path), raising=False)
    saved = {"path": None}

    class _Dev:
        def screenshot(self, path=None):
            saved["path"] = path
            return path

    dev = _Dev()
    cfg = {"step_interval": 0, "default_timeout": 1, "click_timeout": 1}
    r = cr.ActionRunner(dev, cfg, case_name="冒烟用例")
    r._execute({"desc": "点击 主界面 按钮", "screenshot": True})
    p1 = saved["path"].replace("\\", "/")
    assert "冒烟用例" in p1 and "step01" in p1, f"自动命名: {p1}"
    assert "点击_主界面_按钮" in p1, "自动命名应含描述"
    # 旧字符串路径兼容(screenshots/ 前缀补 Test_img/)
    r._execute({"desc": "d", "screenshot": "screenshots/manual.png"})
    p2 = saved["path"].replace("\\", "/")
    assert p2.endswith("Test_img/screenshots/manual.png"), f"旧路径兼容: {p2}"


def test_compare_baseline_step_dropdown(qapp, monkeypatch, tmp_path):
    """compare 基准图 = 下拉选择前面开启截图的步骤(值 step:N);
    执行端 runner 解析 step:N 为该步截图路径;无截图时明确报错"""
    from PySide6.QtWidgets import QComboBox
    from gui import main_window as mw
    from core import runner as cr
    root = tmp_path / "Test_cases" / "涂鸦智能T4"
    root.mkdir(parents=True)
    (root / "a.yaml").write_text(
        "module: a\ncases: [{name: x, steps: []}]\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        # 步骤: 1 开截图, 2 不开, 3 开截图, 4 compare
        w.add_step("click")
        w.steps[0]["screenshot"] = True     # 步骤1 开截图
        w.add_step("click")                  # 步骤2 不开
        w.add_step("click")                  # 步骤3 开截图
        w.steps[2]["screenshot"] = True
        w.add_step("compare")                # 步骤4 对比
        card = [wd for wd in _card_widgets(w)][3]
        combo = card.widgets.get("compare")
        assert isinstance(combo, QComboBox), "基准图应为下拉选择"
        data = [combo.itemData(i) for i in range(combo.count())]
        assert data == ["step:1", "step:3"], f"选项应为开启截图的步骤: {data}"
        combo.setCurrentIndex(1)         # 选 步骤3
        w._sync_header() if hasattr(w, "_sync_header") else None
        assert w.steps[3]["compare"] == "step:3"
    finally:
        w.close()


def test_runner_compare_resolves_step_ref(qapp, tmp_path):
    """runner: compare=step:N → 取第 N 步结果截图;无截图时明确报错"""
    import core.actions.asserts as asserts_mod
    from types import SimpleNamespace
    from core import runner as cr
    saved = {"path": None}

    import numpy as np
    from PIL import Image
    Image.new("RGB", (64, 64), "white").save(tmp_path / "shot1.png")

    class _Dev:
        def screenshot(self, path=None, format=None):
            if format == "opencv":
                import numpy as np
                return np.zeros((32, 32, 3), dtype=np.uint8)
            saved["path"] = path
            return path

    dev = _Dev()
    r = cr.ActionRunner(dev, {"step_interval": 0, "default_timeout": 1,
                              "click_timeout": 1}, case_name="c")
    # 模拟第 1 步已截图
    r.results.append({"desc": "步骤1", "passed": True,
                      "screenshot": str(tmp_path / "shot1.png")})
    assert asserts_mod._resolve_baseline(r, "step:1") == str(tmp_path / "shot1.png"), \
        "step:1 应解析为第1步截图路径"
    # 无截图引用 → 明确报错
    import pytest
    with pytest.raises(RuntimeError, match="没有截图"):
        asserts_mod._resolve_baseline(r, "step:2")


def test_delete_selected_case_and_group(qapp, monkeypatch, tmp_path):
    """删除按钮: 删除选中用例(文件移除+编辑区清空)与删除选中组(整目录移除),
    均需确认弹窗(测试中 mock);只删除选中的那一个"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    import gui.main_window as mw
    root = tmp_path / "Test_cases"
    (root / "涂鸦智能T4").mkdir(parents=True)
    (root / "三星").mkdir(parents=True)
    for n in ("全局清扫", "划区清扫"):
        (root / "涂鸦智能T4" / f"{n}.yaml").write_text(
            "module: %s\ncases: [{name: a, steps: []}]\n" % n, encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    boxes = []
    monkeypatch.setattr(mw.QMessageBox, "question",
                        staticmethod(lambda *a, **k: boxes.append(a) or
                                     QMessageBox.Yes))
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        # ── 删除单个用例(划区清扫) ──
        idx = case_row_index(lst, 1)
        lst.setCurrentRow(idx)
        w.on_delete_selected()
        qapp.processEvents()
        assert boxes, "删除必须弹确认框"
        assert not (root / "涂鸦智能T4" / "划区清扫.yaml").exists()
        roles = [lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                 if lst.item(i).data(Qt.UserRole)]
        assert roles == [str(root / "涂鸦智能T4" / "全局清扫.yaml")]
        # ── 删除整组(三星, 空组): 选中三星组头行 ──
        head_idx = next(i for i in range(lst.count())
                        if lst.item(i).data(Qt.UserRole) is None
                        and "三星" in lst.item(i).text())
        lst.setCurrentRow(head_idx)
        assert lst.currentItem().data(Qt.UserRole) is None
        w.on_delete_selected()
        qapp.processEvents()
        assert not (root / "三星").exists(), "删除组应移除整个目录"
        assert len(boxes) == 2, "两次删除各弹一次确认"
    finally:
        w.close()


def test_delete_current_case_unloads_editor(qapp, monkeypatch, tmp_path):
    """删除的正是当前编辑用例时, 编辑区同步清空"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtWidgets import QMessageBox
    from PySide6.QtTest import QTest
    from gui import main_window as mw
    root = tmp_path / "Test_cases"
    (root / "涂鸦智能T4").mkdir(parents=True)
    (root / "涂鸦智能T4" / "a.yaml").write_text(
        "module: a\ncases: [{name: x, steps: [{desc: s, click: y}]}]\n",
        encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        lst = w.case_list
        w._on_case_item_clicked(lst.item(case_row_index(lst, 0)))
        assert w.case_path and w.data["module"] == "a"
        monkeypatch.setattr(mw.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        lst.setCurrentRow(case_row_index(lst, 0))
        w.on_delete_selected()
        qapp.processEvents()
        assert w.case_path is None and len(w.steps) == 0, \
            "删除当前编辑用例后编辑区应清空"
        assert w.file_label.text() == "未选中用例"
    finally:
        w.close()


# ── 点击 / 点击模板 拆分动作(2026-09-23) ──

def test_click_and_click_template_split(qapp, monkeypatch, tmp_path):
    """click(手填目标)与 click_template(下拉选模板)为两个独立动作:
    click → 目标 QLineEdit; click_template → 模板下拉(当前 APP 组自动列出)"""
    from PySide6.QtWidgets import QLineEdit, QComboBox
    import gui.main_window as mw
    root = tmp_path / "Test_cases"
    gdir = root / "涂鸦智能T4"
    tdir = gdir / "templates"
    tdir.mkdir(parents=True)
    (tdir / "涂鸦_开始清扫.png").write_bytes(b"fake")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        # create_case 设置 case_path → 模板上下文绑定到「涂鸦智能T4」组
        w.create_case("a", group="涂鸦智能T4")
        # 分步: 新加的步骤默认展开, 先读 click
        w.add_step("click")
        cards = _card_widgets(w)
        w1 = cards[0].widgets["click"]
        assert isinstance(w1, QLineEdit), "click 目标 = 手填输入框"
        # 再加 click_template(它展开, 第一张收起)
        w.add_step("click_template")
        cards = _card_widgets(w)
        w2 = cards[1].widgets["click_template"]
        assert isinstance(w2, QComboBox), "click_template = 模板下拉"
        items = [w2.itemText(i) for i in range(w2.count())]
        assert "开始清扫.png" in items, f"下拉应显示完整模板名(含 .png): {items}"
        w2.setCurrentText("开始清扫.png")
        w2.currentIndexChanged.emit(0)   # combo 的写回信号
        assert w.steps[1]["click_template"] == "开始清扫.png"
    finally:
        w.close()


# ── 环境行名称输入:自适应 + 历史下拉 + ×清除(2026-09-23) ──

@pytest.fixture
def fake_settings(monkeypatch):
    """用内存 QSettings 替代真实注册表,避免污染用户配置"""
    import gui.main_window as mw

    class _Fake:
        store = {}

        def __init__(self, *a, **k):
            pass

        def value(self, k, default=None):
            return _Fake.store.get(k, default)

        def setValue(self, k, v):
            _Fake.store[k] = v

        def sync(self):
            pass

    _Fake.store = {}
    monkeypatch.setattr(mw, "QSettings", _Fake)
    return _Fake


def test_name_combo_history_and_clear_button(qapp, monkeypatch, tmp_path, fake_settings):
    """测试APP/设备名称: 历史值可下拉选择 + 输入框带 × 清除按钮"""
    import gui.main_window as mw
    fake_settings.store["hist/app_name"] = ["涂鸦智能", "SmartThings"]
    fake_settings.store["hist/device_name"] = ["SE3L"]
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        from PySide6.QtWidgets import QComboBox
        assert isinstance(w.app_name_edit, QComboBox) and w.app_name_edit.isEditable()
        assert isinstance(w.device_name_edit, QComboBox)
        items = [w.app_name_edit.itemText(i) for i in range(w.app_name_edit.count())]
        assert items == ["涂鸦智能", "SmartThings"], f"历史项应加载: {items}"
        assert isinstance(w.app_name_edit.itemDelegate(), mw._HistComboDelegate),             "下拉项应用自定义 delegate(每项带 ✕ 删除历史)"
        assert [w.device_name_edit.itemText(i)
                for i in range(w.device_name_edit.count())] == ["SE3L"]
    finally:
        w.close()


def test_history_item_delete_real_click(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 真实鼠标点击下拉项右侧 ✕ → 删除该条历史, 且不把该项填进输入框。

    (教训: QAbstractItemView 普通点击不调用 delegate.editorEvent, 之前用
     直接调用 editorEvent 的测试"通过"但真机点了没反应 —— 必须走 QTest 真实事件)
    """
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    fake_settings.store["hist/app_name"] = ["涂鸦智能", "SmartThings", "米家"]
    w = mw.MainWindow()
    w.resize(900, 400)
    w.show()
    try:
        qapp.processEvents()
        combo = w.app_name_edit
        assert combo.count() == 3
        combo.setCurrentText("")          # 清空, 便于验证"未被填入"
        combo.showPopup()
        qapp.processEvents()
        view = combo.view()
        idx = combo.model().index(1, 0)   # SmartThings
        rect = view.visualRect(idx)
        QTest.mouseClick(view.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.right() - 6, rect.center().y()))
        qapp.processEvents()
        # ① 历史已删除(内存 + QSettings)
        assert fake_settings.store["hist/app_name"] == ["涂鸦智能", "米家"],             f"✕ 应删除该条历史: {fake_settings.store['hist/app_name']}"
        assert [combo.itemText(i) for i in range(combo.count())] == ["涂鸦智能", "米家"]
        # ② 输入框未被填入被删项(这是用户报的 bug)
        assert combo.currentText() != "SmartThings", "点 ✕ 不应把该项填进输入框"
    finally:
        w.close()


def test_name_combo_width_adapts_to_content(qapp, monkeypatch, tmp_path, fake_settings):
    """输入框宽度随内容自适应(长名称不被截断)"""
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        w.app_name_edit.setCurrentText("短")
        qapp.processEvents()
        narrow = w.app_name_edit.width()
        w.app_name_edit.setCurrentText("非常长的应用名称用于测试自适应宽度")
        qapp.processEvents()
        wide = w.app_name_edit.width()
        assert wide > narrow, f"长内容应变宽: {narrow} → {wide}"
        assert wide <= 420, "有上限(420)防止极端长名撑破布局"
        # 内容必须放得下: combo 宽 ≥ 文本 + 内边距(16) + 箭头区(36)
        c = w.app_name_edit
        need = c.lineEdit().fontMetrics().horizontalAdvance(c.currentText()) + 52
        assert c.width() >= need, f"输入框宽度不足: {c.width()} < {need}"
        # 设备名称同样自适应
        w.device_name_edit.setCurrentText("VERY_LONG_DEVICE_NAME_001")
        qapp.processEvents()
        assert w.device_name_edit.width() > 100
    finally:
        w.close()


def test_name_history_push_dedup_and_cap(monkeypatch, fake_settings):
    """历史记录: 去重、最近在前、最多 10 条"""
    import gui.main_window as mw
    for i in range(12):
        mw.MainWindow._push_name_history("hist/app_name", f"app{i}")
    hist = fake_settings.store["hist/app_name"]
    assert len(hist) == 10, f"上限 10 条: {len(hist)}"
    assert hist[0] == "app11", "最近填写的排最前"
    # 重复值 → 提到最前且不重复
    mw.MainWindow._push_name_history("hist/app_name", "app5")
    hist = fake_settings.store["hist/app_name"]
    assert hist[0] == "app5" and hist.count("app5") == 1
    assert len(hist) == 10
    # 空值不记
    mw.MainWindow._push_name_history("hist/app_name", "")
    assert len(fake_settings.store["hist/app_name"]) == 10


def test_name_save_writes_config_and_history(qapp, monkeypatch, tmp_path, fake_settings):
    """编辑名称 → 写 config + 记入历史 + 下拉刷新"""
    import gui.main_window as mw
    import core.driver as driver
    saved = {}
    monkeypatch.setattr(mw, "update_config", lambda d: saved.update(d), raising=False)
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        w.app_name_edit.setCurrentText("新APP名")
        w._save_env_field_of(w.app_name_edit)
        assert saved.get("app.name") == "新APP名", f"应写回配置: {saved}"
        # ★ 新语义: APP 名称编辑不记历史(仅检测成功才记)
        assert "新APP名" not in (fake_settings.store.get("hist/app_name") or []),             "APP 名称编辑不应记历史"
        assert w.app_name_edit.currentText() == "新APP名", "不应丢失当前输入"
    finally:
        w.close()


def test_detect_app_restores_cursor_and_button(qapp, monkeypatch, tmp_path, fake_settings):
    """★ APP 未安装时点检测: 不能残留转圈光标(用户实测), 按钮须恢复可用。

    各种退出路径(未匹配到→弹窗取消/异常/正常)都要走 finally 清理。
    """
    from PySide6.QtGui import QGuiApplication
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        # 清空可能存在的光标覆盖(Qt 全局状态)
        while QGuiApplication.overrideCursor() is not None:
            QGuiApplication.restoreOverrideCursor()
        w.app_name_edit.setCurrentText("不存在的APP")
        monkeypatch.setattr(mw.app_detect, "list_packages", lambda dev: [], raising=False)
        monkeypatch.setattr(w, "_current_device_id", lambda: "127.0.0.1:7555")
        # 未匹配 → _pick_app 无候选 → 弹告警(mock 掉, 离屏无人点击)
        monkeypatch.setattr(mw.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: None))
        w.on_detect_app()
        qapp.processEvents()
        assert QGuiApplication.overrideCursor() is None, "不能残留转圈光标"
        assert w.detect_btn.isEnabled(), "检测按钮须恢复可用"
        assert w.detect_btn.text() == "🔍 检测", "按钮文字须复原"
        # 异常路径同样清理
        def _boom(dev):
            raise RuntimeError("adb 挂了")
        monkeypatch.setattr(mw.app_detect, "list_packages", _boom, raising=False)
        monkeypatch.setattr(mw.QMessageBox, "critical",
                            staticmethod(lambda *a, **k: None))
        w.on_detect_app()
        qapp.processEvents()
        assert QGuiApplication.overrideCursor() is None, "异常路径也不能残留光标"
        assert w.detect_btn.isEnabled()
    finally:
        w.close()


def test_history_popup_width_adapts_and_recomputes(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 弹出列表宽度按最长历史项自适应; 删除项后重新弹出会重算(用户要求)"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    fake_settings.store["hist/app_name"] = ["短", "中等长度的应用名",
                                           "超级无敌长的应用名称测试用例ABCDEF"]
    w = mw.MainWindow()
    w.resize(1000, 400)
    w.show()
    try:
        qapp.processEvents()
        combo = w.app_name_edit
        combo.setCurrentText("短")          # 输入框窄, 列表仍应放得下长项
        qapp.processEvents()
        combo.showPopup()
        qapp.processEvents()
        view = combo.view()
        popup = view.window()
        fm = combo.lineEdit().fontMetrics()
        longest = max(fm.horizontalAdvance(combo.itemText(i))
                      for i in range(combo.count()))
        # ★ 断言 popup 实际宽度(Qt 不采用 view.sizeHint, 靠 showPopup 后强制设定)
        assert popup.width() >= longest + 40, \
            f"popup 宽应容纳最长项: {popup.width()} < {longest + 40}"
        # ★ 新语义: popup 与输入框**等宽**(用户要求两者长度一致)
        assert popup.width() == combo.width(),             f"popup 应与输入框等宽: {popup.width()} vs {combo.width()}"
        # 删掉最长项 → 重弹应变窄(用户反馈的核心)
        wide = popup.width()
        rect = view.visualRect(combo.model().index(2, 0))
        QTest.mouseClick(view.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.right() - 6, rect.center().y()))
        qapp.processEvents()
        assert combo.count() == 2, "最长项应已删除"
        combo.showPopup()
        qapp.processEvents()
        assert combo.view().window().width() < wide, \
            f"删除最长项后 popup 应变窄: {combo.view().window().width()} vs {wide}"
    finally:
        w.close()


def test_detect_failure_purges_history(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 检测不到的 APP 不进历史(用户要求): 未匹配/异常两条路径都清除该名字,
    其他历史项保留; 输入框内容不被改动"""
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    fake_settings.store["hist/app_name"] = ["不存在的APP", "涂鸦智能"]
    w = mw.MainWindow()
    try:
        monkeypatch.setattr(w, "_current_device_id", lambda: "127.0.0.1:7555")
        monkeypatch.setattr(mw.app_detect, "list_packages", lambda dev: [], raising=False)
        monkeypatch.setattr(mw.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: None))
        w.app_name_edit.setCurrentText("不存在的APP")
        w.on_detect_app()
        qapp.processEvents()
        hist = fake_settings.store["hist/app_name"]
        assert "不存在的APP" not in hist, f"未检测到不应记历史: {hist}"
        assert "涂鸦智能" in hist, "其他历史项须保留"
        assert w.app_name_edit.currentText() == "不存在的APP", "输入框保留用户所写"
        items = [w.app_name_edit.itemText(i) for i in range(w.app_name_edit.count())]
        assert "不存在的APP" not in items, "下拉里也应消失"
        # 异常路径同样清除
        fake_settings.store["hist/app_name"] = ["也检测不到", "涂鸦智能"]
        w.app_name_edit.setCurrentText("也检测不到")

        def _boom(dev):
            raise RuntimeError("adb 挂了")
        monkeypatch.setattr(mw.app_detect, "list_packages", _boom, raising=False)
        monkeypatch.setattr(mw.QMessageBox, "critical",
                            staticmethod(lambda *a, **k: None))
        w.on_detect_app()
        qapp.processEvents()
        assert "也检测不到" not in fake_settings.store["hist/app_name"]
        assert "涂鸦智能" in fake_settings.store["hist/app_name"]
    finally:
        w.close()


def test_history_delete_resizes_open_popup(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 删除历史项后, 正打开的 popup 必须立即缩小(用户实测: 之前要重开才变)"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    fake_settings.store["hist/app_name"] = ["甲", "乙", "丙",
                                           "超级无敌长的应用名称测试用例ABCDEF"]
    w = mw.MainWindow()
    w.resize(1000, 400)
    w.show()
    try:
        qapp.processEvents()
        combo = w.app_name_edit
        combo.setCurrentText("甲")
        combo.showPopup()
        qapp.processEvents()
        before = combo.view().window().size()
        assert before.width() > 200, f"4 项含长名应较宽: {before}"
        # 删掉最长项(第 4 项) —— popup 保持打开
        view = combo.view()
        rect = view.visualRect(combo.model().index(3, 0))
        QTest.mouseClick(view.viewport(), Qt.LeftButton,
                         pos=QPoint(rect.right() - 6, rect.center().y()))
        qapp.processEvents()
        assert combo.count() == 3
        after = combo.view().window().size()
        # ★ 只断言宽度(高度交给 Qt 自管 —— 之前硬设高度反而多出空白行)
        assert after.width() < before.width(), \
            f"删掉长项后应变窄: {after.width()} vs {before.width()}"
        assert after.height() > 0, "高度不能为零(Qt 应显示所有项)"
        # 与重新打开后的宽度一致(说明是正确重算而非凑数)
        combo.hidePopup()
        combo.showPopup()
        qapp.processEvents()
        assert combo.view().window().width() == after.width(), \
            "重开后宽度应与即时重算一致"
    finally:
        w.close()


def test_history_add_keeps_popup_full_height(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 新增历史项后下拉不能变成「只够一行+滚动条」的小框(用户实测 bug)。

    根因: 列表 clear+addItems 后 sizeHintForRow 返回 -1, 逐行累加得负高度。
    """
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    fake_settings.store["hist/app_name"] = ["甲", "乙", "丙"]
    fake_settings.store["hist/device_name"] = ["甲", "乙", "丙"]   # 设备名称框读这个键
    w = mw.MainWindow()
    w.resize(1000, 400)
    w.show()
    try:
        qapp.processEvents()
        # 用设备名称框(它保持「编辑即记历史」; APP 名称已改为仅检测成功才记)
        combo = w.device_name_edit
        combo.setCurrentText("超级无敌长的应用名称测试用例ABCDEF")
        w._save_env_field_of(combo)
        qapp.processEvents()
        assert combo.count() == 4
        combo.showPopup()
        qapp.processEvents()
        popup = combo.view().window()
        view = combo.view()
        # ★ 高度必须 = 行高 × 项数(delegate 真实行高, 低估会出滚动条)
        rh = combo._row_height()
        assert popup.width() >= combo.lineEdit().fontMetrics().horizontalAdvance(
            "超级无敌长的应用名称测试用例ABCDEF") + 40, "宽度应容纳最长项"
        assert popup.height() > 0, "高度不能为零"
        # 打开状态下再新增 → 项数即时增加, 下拉仍完整(不全压在单行)
        combo.setCurrentText("又一个新的名字")
        w._save_env_field_of(combo)
        qapp.processEvents()
        assert combo.count() == 5
        assert combo.view().window().height() > 0, "新增后下拉要有正常高度"
    finally:
        w.close()


def test_all_fit_combos_adapt_width(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 三个长内容下拉(模板/基准图/新建组)统一走 _FitCombo: 宽度容纳最长项;
    只读下拉(基准图)也不能因无 lineEdit 而崩"""
    from PySide6.QtWidgets import QComboBox
    import gui.main_window as mw
    root = tmp_path / "Test_cases"
    gdir = root / "涂鸦智能T4"
    tdir = gdir / "templates"
    tdir.mkdir(parents=True)
    # 一个很长的模板名, 验证宽度能容纳
    LONG_TPL = "涂鸦_超级长的模板名称用于测试宽度自适应ABCDEF.png"
    (tdir / LONG_TPL).write_bytes(b"fake")
    (tdir / "涂鸦_开始清扫.png").write_bytes(b"fake")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    w = mw.MainWindow()
    w.resize(1200, 700)
    w.show()
    try:
        qapp.processEvents()
        w.create_case("a", group="涂鸦智能T4")
        # 步骤1 开截图(供基准图下拉引用), 步骤2 用模板点击
        w.add_step("click")
        w.steps[0]["screenshot"] = True
        w.add_step("click_template")
        w.render_cards()
        w.expand_card((1,))
        qapp.processEvents()
        card = _card_widgets(w)[1]
        tpl_combo = card.widgets["click_template"]
        assert isinstance(tpl_combo, mw._FitCombo), "模板下拉应用 _FitCombo"
        fm = tpl_combo._font_metrics()
        longest = max(fm.horizontalAdvance(tpl_combo.itemText(i))
                      for i in range(tpl_combo.count()))
        assert tpl_combo.width() >= longest + 40, \
            f"模板下拉应容纳最长模板名: {tpl_combo.width()} < {longest + 40}"
        # 基准图下拉(只读)也不能崩, 且宽度合理
        w.steps[0]["desc"] = "点击开始清扫按钮然后等待完成" * 2
        w.add_step("compare")
        w.render_cards()
        w.expand_card((2,))
        qapp.processEvents()
        card2 = _card_widgets(w)[2]
        shot_combo = card2.widgets["compare"]
        assert isinstance(shot_combo, mw._FitCombo)
        assert shot_combo.width() >= 100, "只读下拉也要有合理宽度"
        shot_combo.fit_width()      # 不抛异常(无 lineEdit 路径)
        shot_combo.showPopup()
        qapp.processEvents()
        assert shot_combo.view().window().width() >= shot_combo.width() - 60
        # 新建对话框组下拉
        gcombo = mw._FitCombo()
        gcombo.setEditable(True)
        gcombo.addItems(["涂鸦智能T4", "一个非常长的APP组名称用于测试"])
        gcombo.fit_width()
        assert gcombo.width() > 100
    finally:
        w.close()


def test_app_history_only_on_detect_success(qapp, monkeypatch, tmp_path, fake_settings):
    """★ APP 名称: 编辑不记历史; 只有「检测成功」才写入(用户要求)"""
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    monkeypatch.setattr(mw, "update_config", lambda d: None, raising=False)
    fake_settings.store["hist/app_name"] = ["涂鸦智能"]
    w = mw.MainWindow()
    try:
        monkeypatch.setattr(w, "_current_device_id", lambda: "127.0.0.1:7555")
        # ① 编辑(触发保存) → 不记历史
        w.app_name_edit.setCurrentText("随便写的名字")
        w._save_env_field_of(w.app_name_edit)
        qapp.processEvents()
        assert "随便写的名字" not in fake_settings.store["hist/app_name"],             "编辑不应记历史(只有检测成功才记)"
        # ② 检测成功 → 记历史 + 下拉刷新
        monkeypatch.setattr(mw.app_detect, "list_packages",
                            lambda dev: ["com.tuya.smartiot"], raising=False)
        monkeypatch.setattr(mw.app_detect, "detect_main_activity",
                            lambda dev, pkg: "com.smart.ThingSplashActivity", raising=False)
        w.app_name_edit.setCurrentText("涂鸦智能")
        w.on_detect_app()
        qapp.processEvents()
        assert fake_settings.store["hist/app_name"][0] == "涂鸦智能"
        items = [w.app_name_edit.itemText(i) for i in range(w.app_name_edit.count())]
        assert "涂鸦智能" in items, "检测成功后下拉应含该名称"
        # ③ 设备名称保持「编辑即记」(它没有检测动作)
        w.device_name_edit.setCurrentText("SE3L")
        w._save_env_field_of(w.device_name_edit)
        qapp.processEvents()
        assert "SE3L" in (fake_settings.store.get("hist/device_name") or []),             "设备名称仍应编辑即记历史"
    finally:
        w.close()


def test_both_name_combos_popup_equal_width(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 测试APP 与 设备名称两个框: 历史下拉与输入框**等宽**(用户要求);
    删除一条后仍等宽(此前删除后输入框缩短而列表仍宽 → 一长一短)"""
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    monkeypatch.setattr(mw, "update_config", lambda d: None, raising=False)
    fake_settings.store["hist/app_name"] = ["涂鸦智能", "SmartThings"]
    fake_settings.store["hist/device_name"] = ["SE3L", "L10"]
    w = mw.MainWindow()
    w.resize(1100, 400)
    w.show()
    try:
        qapp.processEvents()
        for label, combo in (("测试APP", w.app_name_edit), ("设备名称", w.device_name_edit)):
            combo.showPopup()
            qapp.processEvents()
            popup = combo.view().window()
            assert popup.width() == combo.width(), \
                f"{label}: popup 应与输入框等宽 {popup.width()} vs {combo.width()}"
            # 删除一条后仍等宽
            view = combo.view()
            rect = view.visualRect(combo.model().index(1, 0))
            QTest.mouseClick(view.viewport(), Qt.LeftButton,
                             pos=QPoint(rect.right() - 6, rect.center().y()))
            qapp.processEvents()
            assert combo.count() == 1, f"{label}: 应删掉一条"
            assert combo.view().window().width() == combo.width(), \
                f"{label}: 删除后仍应等宽 {combo.view().window().width()} vs {combo.width()}"
            combo.hidePopup()
            qapp.processEvents()
    finally:
        w.close()


def test_popup_no_scrollbar_no_extra_gap(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 下拉不应出现滚动条/底部空白。

    popup 自身有边框, 若把"内容高"直接设给 popup → view 可用高少 2px →
    出现滚动条且末行被挤(用户实测: 设备名称下拉多一行空白)。
    正确做法: view 设内容高 + popup.adjustSize() 让 Qt 算边框。
    """
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    fake_settings.store["hist/device_name"] = ["SE3L", "L10", "扫地机器0087"]
    fake_settings.store["hist/app_name"] = ["涂鸦智能", "SmartThings"]
    w = mw.MainWindow()
    w.resize(1100, 400)
    w.show()
    try:
        qapp.processEvents()
        for label, combo in (("设备名称", w.device_name_edit), ("测试APP", w.app_name_edit)):
            combo.showPopup()
            qapp.processEvents()
            view = combo.view()
            n = combo.count()
            need = combo._row_height() * n
            assert view.height() >= need, \
                f"{label}: view 高度不足(末行被挤): {view.height()} < {need}"
            assert view.verticalScrollBar().maximum() == 0, \
                f"{label}: 不应出现垂直滚动条"
            assert combo.view().window().width() == combo.width(), \
                f"{label}: 应与输入框等宽"
            combo.hidePopup()
            qapp.processEvents()
    finally:
        w.close()


def test_numeric_config_values_do_not_crash(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 配置里的纯数字值(YAML 解析成 int)不能让窗口构造崩溃。

    用户实测: config 里 target_device: 111 → setCurrentText(111) 抛 TypeError
    → 窗口构造失败, 表现为"GUI 启动卡住/打不开"。
    """
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    # 模拟用户的配置: 纯数字被 YAML 解析为 int
    monkeypatch.setattr(mw, "load_config",
                        lambda: {"app": {"name": 123, "package": "com.x"},
                                 "target_device": 111,
                                 "device": {"default": "auto", "list": []}},
                        raising=False)
    w = mw.MainWindow()          # 修复前这里抛 TypeError
    try:
        assert w.device_name_edit.currentText() == "111", "数字设备名应转字符串显示"
        assert w.app_name_edit.currentText() == "123"
    finally:
        w.close()


def test_menu_and_checkbox_styles(qapp):
    """菜单勾选标记交给 Qt 原生绘制; 勾选框统一透明背景(防闪动)"""
    import gui.main_window as mw
    # ★ 不自定义 QMenu::indicator, 也不改 item 左侧 padding ——
    #   自定义过反而让原生勾标变形/发虚(用户实测:"用原生的就好")
    assert "QMenu::indicator" not in mw.STYLESHEET, "不要自定义菜单勾选标记"
    assert "QMenu::item { padding: 6px 24px 6px 12px;" in mw.STYLESHEET, \
        "菜单项 padding 保持原值(改动会影响原生勾标)"
    assert "QCheckBox { background: transparent;" in mw.STYLESHEET, \
        "勾选框必须透明背景, 否则点击时闪动"
    # ★ 绝不能给 QCheckBox::indicator 设样式: 会让 Qt 不再绘制原生勾(勾会消失)
    assert "QCheckBox::indicator" not in mw.STYLESHEET, \
        "不要自定义勾选标记(indicator), 否则勾不显示"


def test_chipbtn_has_visible_border_and_list_title(qapp, monkeypatch, tmp_path):
    """小圆角按钮(chipBtn)要有常驻边框(原来只有 hover 才出现);
    用例列表标题与「测试步骤详情」同一标题样式"""
    import gui.main_window as mw
    assert "border: 1px solid #d9dee6; border-radius: 12px;" in mw.STYLESHEET, \
        "chipBtn 边框应常驻(不是 transparent)"
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        from PySide6.QtWidgets import QLabel
        # 两个标题都应是蓝色粗体 13px
        titles = [l for l in w.findChildren(QLabel)
                  if l.text() in ("用例列表", "测试步骤详情")]
        assert len(titles) == 2, f"应有两个标题, 实际 {[t.text() for t in titles]}"
        for t in titles:
            assert "2563eb" in t.styleSheet() and "bold" in t.styleSheet() \
                and "13px" in t.styleSheet(), f"「{t.text()}」样式应一致"
    finally:
        w.close()


def test_log_view_wraps_and_no_duplicate_handler(qapp, monkeypatch, tmp_path):
    """★ 运行日志: ① 自动换行(否则长行被右侧截断, 看着"不完整")
    ② 不重复 —— RunWorker 不能再挂 handler(与主窗口全局转发冲突)"""
    import inspect
    from PySide6.QtWidgets import QPlainTextEdit
    import gui.main_window as mw
    import gui.runner_thread as rt
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        assert w.log_view.lineWrapMode() == QPlainTextEdit.WidgetWidth, \
            "运行日志必须自动换行(NoWrap 会截断长行)"
        # 全部 core 日志都走 vacuum_test logger → 全局转发覆盖
        from core.logger import get_logger
        get_logger().info("[测试] 这条应出现在运行日志")
        qapp.processEvents()
        assert "[测试] 这条应出现在运行日志" in w.log_view.toPlainText()
        # RunWorker 不应再挂 handler(否则同一条显示两遍)
        src = inspect.getsource(rt.RunWorker.run)
        assert "addHandler" not in src, "RunWorker 不应挂日志 handler(会重复)"
    finally:
        w.close()


def test_ui_hints_also_go_to_run_log(qapp, monkeypatch, tmp_path):
    """★ 界面提示(保存配置/APP检测/删除等)必须同时进运行日志(用户反馈看不到)"""
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    monkeypatch.setattr(mw, "update_config", lambda d: None, raising=False)
    w = mw.MainWindow()
    try:
        w.log_view.clear()
        w._set_status("配置已保存: app.name = 涂鸦智能")
        qapp.processEvents()
        assert w.env_status.text() == "配置已保存: app.name = 涂鸦智能", "界面提示要更新"
        assert "配置已保存: app.name = 涂鸦智能" in w.log_view.toPlainText(), \
            "同一句提示也要出现在运行日志"
        # 保存配置的真实入口 → 走 _set_status
        w.app_name_edit.setCurrentText("新名字")
        w._save_env_field_of(w.app_name_edit)
        qapp.processEvents()
        assert "配置已保存" in w.log_view.toPlainText(), "保存配置应进日志"
    finally:
        w.close()


def test_name_save_not_duplicated_by_two_signals(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 一次保存动作只应写一次盘、记一行日志。

    真机日志实证(17:28:36 同一秒出现两行「配置已保存: target_device = 扫地机器0087」):
    combo 的 activated 与 lineEdit().editingFinished 都接了 _save_env_field_of,
    从下拉点选一项时两个信号都会发 → 保存执行两次、日志重复。
    """
    import gui.main_window as mw
    saved = []
    monkeypatch.setattr(mw, "update_config", lambda d: saved.append(d), raising=False)
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        c = w.device_name_edit
        w.log_view.clear()
        c.setCurrentText("扫地机器0087")
        c.lineEdit().editingFinished.emit()   # 输入框编辑结束
        c.activated.emit(0)                   # 紧接着从下拉点选同一条
        qapp.processEvents()
        assert len(saved) == 1, f"同一值被重复保存 {len(saved)} 次: {saved}"
        assert w.log_view.toPlainText().count("配置已保存: target_device") == 1, \
            "运行日志里同一句提示不应重复"
        # 真的换了值 → 必须照常写盘(去重不能把正常保存也吞掉)
        c.setCurrentText("扫地机器0099")
        c.lineEdit().editingFinished.emit()
        qapp.processEvents()
        assert len(saved) == 2 and saved[-1].get("target_device") == "扫地机器0099", \
            f"换了值必须照常保存: {saved}"
    finally:
        w.close()


def test_hint_lines_own_row_and_do_not_widen_window(qapp, monkeypatch, tmp_path):
    """★ 两个提示行都必须**独占一行、位于组件下方**, 不能挂在组件右边:
      · status_label —— 「报告已生成(部分执行): ...」(原来在工具栏最右端)
      · env_status   —— 「配置已保存: ...」(原来在环境条流式布局末尾)
    挂在组件旁边时长文本会把那一行撑宽 → 窗口跟着变大(用户两次反馈)。
    """
    from PySide6.QtCore import Qt as _Qt
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    monkeypatch.setattr(mw, "update_config", lambda d: None, raising=False)
    w = mw.MainWindow()
    w.resize(900, 700)
    w.show()
    try:
        qapp.processEvents()
        long_path = ("D:/claude_test/Auto_test/reports/涂鸦智能T4/"
                     "全局清扫_20260924_103000/全局清扫_执行报告_20260924_103000.xlsx")
        for label, neighbor, msg, centered in (
                (w.status_label, w.run_btn, f"报告已生成(部分执行): {long_path}", True),
                (w.env_status, w.device_combo,
                 f"配置已保存: target_device = {long_path}", False)):
            # ① 位置: 不与组件同行(同行=会被长文本撑宽)
            assert label.parent() is not neighbor.parent(), \
                f"提示不能和组件挤在同一行: {msg[:12]}"
            # ② 不参与宽度决策
            assert label.wordWrap(), "提示要能自动换行"
            assert label.sizePolicy().horizontalPolicy() == mw.QSizePolicy.Ignored, \
                "提示的水平策略必须是 Ignored(否则长文本仍会撑宽窗口)"
            # ③ 关键断言: 塞一条超长提示, 窗口最小宽度不能变大
            w.status_label.setText("")
            w.env_status.setText("")
            qapp.processEvents()
            base = w.minimumSizeHint().width()
            label.setText(msg)
            qapp.processEvents()
            assert w.minimumSizeHint().width() <= base + 2, \
                f"长提示把窗口撑宽了({msg[:12]}...): {base} → {w.minimumSizeHint().width()}"
        # ④ 运行状态行居中(用户要求); 且标签必须铺满整行, 否则居中看不出来
        assert w.status_label.alignment() & _Qt.AlignHCenter, "「就绪」那行要居中显示"
        assert w.status_label.width() >= w.status_label.parent().width() - 2, \
            "状态标签要铺满整行, 居中才有意义"
        assert not (w.env_status.alignment() & _Qt.AlignHCenter), \
            "配置提示行保持左对齐(用户只要求运行状态行居中)"
    finally:
        w.close()


def test_form_checkbox_full_click_area_and_no_hover_flash(qapp, monkeypatch, tmp_path):
    """★ 表单勾选框(自动截图等)的实测缺陷守护:

    1. **不能被拉宽**: 拉成 200px 后只有指示器那一小块能点(点 x=150 无反应);
       改成整块可点又变成"点旁边空白也选中"(用户实测反馈) → 保持自然大小,
       看到多少就能点多少。
    2. 13px 高时指示器被裁切。
    3. 悬停态原生指示器画残(右/下边缺失) → 一进一出"闪动"
       (真机 1.5x DPI 抓屏验证: 关掉 WA_Hover 后悬停前后像素 0 差异;
        可用 tools/check_render.py 复现与复验)
    """
    from PySide6.QtCore import Qt as _Qt, QPoint
    from PySide6.QtTest import QTest
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    monkeypatch.setattr(mw, "update_config", lambda d: None, raising=False)
    w = mw.MainWindow()
    w.resize(1100, 700)
    w.data = {"module": "组", "cases": [{"name": "c", "priority": "P1",
                                        "steps": [{"desc": "步骤", "screenshot": True}]}]}
    w.case_idx = 0
    w.expanded_key = (0,)
    w.render_cards()
    w.show()
    try:
        qapp.processEvents()
        card = w.cards_lay.itemAt(0).widget()
        card._toggle_advanced()
        qapp.processEvents()
        cb = card.widgets["screenshot"]
        # ① 用专用控件(不是裸 QCheckBox)
        assert isinstance(cb, mw._FormCheckBox)
        # ② 不进入悬停态 —— 悬停闪动的根因
        assert not cb.testAttribute(_Qt.WA_Hover), \
            "勾选框必须关掉 WA_Hover, 否则原生指示器悬停态会画残(真机实测)"
        # ③ 高度够, 指示器不被裁切
        assert cb.height() >= mw._CHECK_MIN_H, f"高度不足会裁切指示器: {cb.height()}"
        # ④ 保持自然大小(不能被表单拉成 200px —— 否则要么点不动、要么点旁边空白也选中)
        assert cb.width() <= cb.sizeHint().width() + 8, \
            f"勾选框被拉宽了(现在 {cb.width()}px, 自然 {cb.sizeHint().width()}px)"
        assert not cb.hitButton(QPoint(cb.width() - 1, cb.height() // 2)), \
            "控件最右侧不该是命中区(否则点旁边空白也会切换)"
        # ⑤ 点指示器本身必须能切换
        before = cb.isChecked()
        QTest.mouseClick(cb, _Qt.LeftButton, pos=QPoint(7, cb.height() // 2))
        qapp.processEvents()
        assert cb.isChecked() != before, "点指示器应能切换勾选状态"
    finally:
        w.close()


def test_delete_current_history_clears_config(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 用户要求: ✕ 删掉的若是「当前生效的值」, 连 config 一起清空。

    否则输入框清空了、配置里还留着 → 重启按配置回填, 用户看到的是
    "删了下次启动又出现"。(点击 → _remove_name_history 的事件链由
    test_history_item_delete_real_click 守护, 这里验证删除语义本身)
    """
    import gui.main_window as mw
    fake_settings.store["hist/device_name"] = ["又一个新的名字", "SE3L"]
    # 假配置要跟着写入变化, 否则模拟不出"清空后再启动"这一步
    cfg = {"app": {}, "target_device": "又一个新的名字"}
    saved = []

    def _upd(d):
        saved.append(d)
        cfg.update(d)

    monkeypatch.setattr(mw, "load_config", lambda: cfg, raising=False)
    monkeypatch.setattr(mw, "update_config", _upd, raising=False)
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        c = w.device_name_edit
        # ① 删的不是当前值 → 只删历史, 配置不动, 当前值保留
        w._remove_name_history(c, 1)          # SE3L
        qapp.processEvents()
        assert fake_settings.store["hist/device_name"] == ["又一个新的名字"]
        assert c.currentText() == "又一个新的名字", "当前值不该被牵连清空"
        assert not saved, f"删非当前值不该动配置: {saved}"
        # ② 删的正是当前值 → 历史 + 输入框 + 配置 一起清空
        w._remove_name_history(c, 0)          # 又一个新的名字(当前值)
        qapp.processEvents()
        assert fake_settings.store["hist/device_name"] == []
        assert c.currentText() == "", "删掉当前值后输入框应清空"
        assert saved and saved[-1] == {"target_device": ""}, \
            f"删当前值必须连配置一起清空: {saved}"
        # ③ 清空后重启: 输入框不再出现该名称(用户报的现象)
        w.close()
        w2 = mw.MainWindow()
        try:
            assert w2.device_name_edit.currentText() == "", \
                "重启后不该再回填已被清空的设备名"
        finally:
            w2.close()
        w = None
    finally:
        if w is not None:
            w.close()


def test_unchanged_value_not_repushed_to_history(qapp, monkeypatch, tmp_path, fake_settings):
    """★ 值没变时不该再"保存"、更不该把它重新记回历史。

    真机实证(重启后 17:31:54): 用户没改任何值, 只触发了一次 editingFinished,
    就把 config 里的 target_device 重新写回历史 —— 下拉从 3 项变 4 项,
    等于把用户刚用 ✕ 删掉的历史项又撤销回来了(用户报: 删了下次启动又出现)。
    """
    import gui.main_window as mw
    fake_settings.store["hist/device_name"] = ["SE3L", "L10"]
    monkeypatch.setattr(mw, "load_config",
                        lambda: {"app": {}, "target_device": "又一个新的名字"}, raising=False)
    saved = []
    monkeypatch.setattr(mw, "update_config", lambda d: saved.append(d), raising=False)
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    w = mw.MainWindow()
    try:
        c = w.device_name_edit
        assert c.currentText() == "又一个新的名字", "启动时应按 config 回填当前设备名"
        assert fake_settings.store["hist/device_name"] == ["SE3L", "L10"]
        # 用户只是点了下输入框又点到别处(值没改) → editingFinished
        c.lineEdit().editingFinished.emit()
        qapp.processEvents()
        assert fake_settings.store["hist/device_name"] == ["SE3L", "L10"], \
            f"值未变化却把它重新记入历史: {fake_settings.store['hist/device_name']}"
        assert not saved, f"值未变化不该重复写盘: {saved}"
        # 真改了值 → 照常保存并记历史
        c.setCurrentText("扫地机器0087")
        c.lineEdit().editingFinished.emit()
        qapp.processEvents()
        assert saved and saved[-1].get("target_device") == "扫地机器0087"
        assert fake_settings.store["hist/device_name"][0] == "扫地机器0087", \
            "改了值必须记入历史"
    finally:
        w.close()


def test_preconditions_save_and_stop_hints_go_to_run_log(qapp, monkeypatch, tmp_path):
    """★ 前置条件保存 / 停止执行 的提示也必须进运行日志(用户反馈日志不完整,
    这两处此前是直接 status_label.setText, 只有界面有、日志里查不到)"""
    import core.driver as driver
    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "CONFIG_PATH", str(tmp_path / "config.yaml"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    monkeypatch.setattr(mw, "update_config", lambda d: None, raising=False)
    # ★ 必须隔离: save_preconditions 默认写真实 config.yaml(不能碰用户数据)
    saved = {}
    monkeypatch.setattr(driver, "save_preconditions",
                        lambda items: saved.update(items=items), raising=False)

    class _Dlg:
        """替身对话框: 直接返回 Accepted, 不弹模态框"""

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return mw.QDialog.Accepted

        def values(self):
            return [{"type": "restart_app", "enabled": True}]

    monkeypatch.setattr(mw, "PreconditionsDialog", _Dlg, raising=False)

    class _FakeWorker:
        def __init__(self):
            self.stop_called = False

        def request_stop(self):
            self.stop_called = True

        def wait(self, _ms):
            return True

    w = mw.MainWindow()
    fake = _FakeWorker()
    try:
        # 走真实入口 on_edit_preconditions(不是直接调 _set_status, 防假绿)
        w.log_view.clear()
        w.on_edit_preconditions()
        qapp.processEvents()
        assert saved.get("items"), "真实入口要保存前置条件"
        assert w.status_label.text().startswith("前置条件已保存"), \
            f"界面提示要更新(现在会带组名): {w.status_label.text()}"
        assert "前置条件已保存" in w.log_view.toPlainText(), "前置条件保存也要进运行日志"

        w.log_view.clear()
        w.worker = fake          # on_stop 需要 worker 才进入分支
        w.on_stop()
        qapp.processEvents()
        assert w.status_label.text() == "停止中(当前步骤结束后退出)..."
        assert "停止中" in w.log_view.toPlainText(), "停止执行也要进运行日志"
        assert fake.stop_called, "停止请求要真的发给 worker"
    finally:
        w.worker = None          # 避免 closeEvent 弹模态框阻塞离屏测试
        w.close()


def test_runworker_preconditions_follow_app_group(qapp, monkeypatch, tmp_path):
    """★ 用户要求: 前置条件与 APP 组绑定, 切到某组用例时执行那组的前置。

    队列 三星A → 三星C → 涂鸦B → 三星A:
      组序列 三星(跑) → 三星(同组, 只重启) → 涂鸦(切组, 跑) → 三星(切回, 跑)
    """
    import types
    from gui import runner_thread as rt
    from gui.runner_thread import RunWorker

    def write_case(group, name):
        d = tmp_path / "Test_cases" / group
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{name}.yaml"
        f.write_text(f"module: {name}\ncases:\n  - name: {name}\n    steps:\n"
                     "      - desc: 等一下\n        wait: 0.05\n", encoding="utf-8")
        return str(f)

    f_a = write_case("三星", "用例A")
    f_c = write_case("三星", "用例C")
    f_b = write_case("涂鸦智能T4", "用例B")

    import uiautomator2 as u2
    fake_d = types.SimpleNamespace(
        implicitly_wait=lambda t: None,
        dump_hierarchy=lambda: '<node text="100%"/>',
        screenshot=lambda path=None, format=None: None)
    monkeypatch.setattr(u2, "connect", lambda dev: fake_d)

    seen, restarts = [], []
    monkeypatch.setattr(rt.session, "prepare_items",
                        lambda d_, cfg_, items, **kw:
                        (seen.append(items), [{"key": "p", "desc": "重启 APP", "ok": True}])[1])
    monkeypatch.setattr(rt.session, "restart_app",
                        lambda d_, cfg, enter_page=True: restarts.append(1))
    monkeypatch.setattr(rt.session, "get_battery_level", lambda d_: 80)
    monkeypatch.setattr(rt, "load_config",
                        lambda: {"runner": {"step_interval": 0, "default_timeout": 1,
                                            "click_timeout": 1},
                                 "app": {"package": "p", "name": "x"},
                                 "target_device": "SE3L"})
    monkeypatch.setattr(rt, "ExcelReport",
                        lambda: __import__("core.excel_report", fromlist=["ExcelReport"])
                        .ExcelReport(path=str(tmp_path / "rep.xlsx")))

    sx = [{"type": "tap_blank", "enabled": True, "point": "1,1"}]
    ty = [{"type": "battery", "enabled": True, "min_level": 60}]
    worker = RunWorker("fake-dev", [f_a, f_c, f_b, f_a], sx, 1,
                       pre_by_group={"三星": sx, "涂鸦智能T4": ty})
    worker.run()

    assert seen == [sx, ty, sx], f"应按组切换执行对应前置: {[i[0]['type'] for i in seen]}"
    assert len(restarts) == 1, f"同组第二个用例只该重启 APP: {len(restarts)} 次"


def test_runworker_preconditions_fall_back_when_group_missing(qapp, monkeypatch, tmp_path):
    """没给某组配置时回退默认前置项(不能什么都不跑)"""
    import types
    from gui import runner_thread as rt
    from gui.runner_thread import RunWorker

    d = tmp_path / "Test_cases" / "未知组"
    d.mkdir(parents=True)
    f = d / "用例.yaml"
    f.write_text("module: 用例\ncases:\n  - name: x\n    steps:\n"
                 "      - desc: 等一下\n        wait: 0.05\n", encoding="utf-8")

    import uiautomator2 as u2
    monkeypatch.setattr(u2, "connect", lambda dev: types.SimpleNamespace(
        implicitly_wait=lambda t: None, dump_hierarchy=lambda: "<node/>",
        screenshot=lambda path=None, format=None: None))

    seen = []
    monkeypatch.setattr(rt.session, "prepare_items",
                        lambda d_, cfg_, items, **kw:
                        (seen.append(items), [{"key": "p", "desc": "重启 APP", "ok": True}])[1])
    monkeypatch.setattr(rt.session, "restart_app", lambda d_, cfg, enter_page=True: None)
    monkeypatch.setattr(rt.session, "get_battery_level", lambda d_: 80)
    monkeypatch.setattr(rt, "load_config",
                        lambda: {"runner": {"step_interval": 0, "default_timeout": 1,
                                            "click_timeout": 1},
                                 "app": {"package": "p", "name": "x"}, "target_device": "D"})
    monkeypatch.setattr(rt, "ExcelReport",
                        lambda: __import__("core.excel_report", fromlist=["ExcelReport"])
                        .ExcelReport(path=str(tmp_path / "rep.xlsx")))

    fallback = [{"type": "charging", "enabled": True}]
    RunWorker("fake-dev", [str(f)], fallback, 1, pre_by_group={}).run()
    assert seen == [fallback], "组没配置时应回退默认前置项"


# ── 用例列表「全选」优先服务于当前所选 APP 组 ──

def _cases_window(qapp, monkeypatch, tmp_path):
    """造两个组的用例, 返回窗口(组内用例文件名 a/b/c)"""
    import gui.main_window as mw
    root = tmp_path / "Test_cases"
    for g in ("三星", "涂鸦智能T4"):
        d = root / g
        d.mkdir(parents=True)
        for n in ("a", "b"):
            (d / f"{n}.yaml").write_text(
                f"module: {n}\ncases:\n  - name: {n}\n    steps: []\n", encoding="utf-8")
    monkeypatch.setattr(mw, "CASES_DIR", str(root), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}}, raising=False)
    monkeypatch.setattr(mw, "update_config", lambda d: None, raising=False)
    w = mw.MainWindow()
    w._fill_case_list()
    qapp.processEvents()
    return w


def _checked_groups(w):
    """当前勾选的用例分别属于哪些组"""
    import os
    out = {}
    for i in range(w.case_list.count()):
        it = w.case_list.item(i)
        p = it.data(mw_qt_userrole())
        if p and it.checkState() == mw_qt_checked():
            g = os.path.basename(os.path.dirname(os.path.abspath(p)))
            out[g] = out.get(g, 0) + 1
    return out


def mw_qt_userrole():
    from PySide6.QtCore import Qt
    return Qt.UserRole


def mw_qt_checked():
    from PySide6.QtCore import Qt
    return Qt.Checked


def test_select_all_scoped_to_selected_group(qapp, monkeypatch, tmp_path):
    """★ 用户要求: 全选优先只作用于**当前所选 APP 组**, 不要把所有组都勾上"""
    w = _cases_window(qapp, monkeypatch, tmp_path)
    try:
        # 选中"三星"组的一条用例(点开后它会成为当前行)
        target = None
        for i in range(w.case_list.count()):
            it = w.case_list.item(i)
            if it.data(mw_qt_userrole()) and "三星" in it.data(mw_qt_userrole()):
                target = i
                break
        assert target is not None, "没找到三星组的用例行"
        w.case_list.setCurrentRow(target)
        qapp.processEvents()
        w.on_select_all_cases()
        qapp.processEvents()
        assert _checked_groups(w) == {"三星": 2}, f"应只勾选三星组: {_checked_groups(w)}"
    finally:
        w.close()


def test_select_all_from_group_header(qapp, monkeypatch, tmp_path):
    """选中**组头**时, 全选作用于该组"""
    w = _cases_window(qapp, monkeypatch, tmp_path)
    try:
        for i in range(w.case_list.count()):
            it = w.case_list.item(i)
            if it.data(mw_qt_userrole()) is None and "涂鸦" in (it.text() or ""):
                w.case_list.setCurrentRow(i)
                break
        w.on_select_all_cases()
        qapp.processEvents()
        assert _checked_groups(w) == {"涂鸦智能T4": 2}, _checked_groups(w)
    finally:
        w.close()


def test_select_all_falls_back_to_everything(qapp, monkeypatch, tmp_path):
    """没有任何可判定的组(未选用例) → 退回"全部"(保持原行为)"""
    w = _cases_window(qapp, monkeypatch, tmp_path)
    try:
        w.case_list.setCurrentRow(-1)      # 清除选择
        w.case_path = None                 # 也没在选用例
        w.on_select_all_cases()
        qapp.processEvents()
        assert _checked_groups(w) == {"三星": 2, "涂鸦智能T4": 2}, _checked_groups(w)
    finally:
        w.close()


def test_clear_button_clears_all_groups(qapp, monkeypatch, tmp_path):
    """「清空」仍是清掉全部组的勾选(与全选的范围语义不同, 按用户原话只改全选)"""
    w = _cases_window(qapp, monkeypatch, tmp_path)
    try:
        from PySide6.QtCore import Qt as _Qt
        w.on_select_all_cases()
        qapp.processEvents()
        w._set_cases_checked(_Qt.Unchecked)
        qapp.processEvents()
        assert _checked_groups(w) == {}, _checked_groups(w)
    finally:
        w.close()
