# -*- coding: utf-8 -*-
"""测试隔离:任何测试都只能写「临时副本」,绝不碰用户真实的 config/config.yaml。

★ 事故(2026-09-24)
    `test_history_add_keeps_popup_full_height` 调用 `_save_env_field_of`(它会经
    `update_config` 写盘), 但只 mock 了 `gui.main_window.CONFIG_PATH` —— 而
    `update_config` 的**默认落盘位置**是 `core.driver.CONFIG_PATH`,两者不是一回事,
    于是每跑一次测试就把 `target_device = 又一个新的名字` 写进用户真实的配置文件。
    用户看到的现象是「设备名明明删掉了, 下次启动又出现」——因为他在 GUI 里删掉之后,
    我又跑了一次测试把它写了回去。铁证: config.yaml 的 mtime 与日志里那行
    「配置已保存: target_device = ...」精确到同一毫秒。

★ 隔离方式
    把 `core.driver.CONFIG_PATH` 指向**真实配置的临时副本**:读取依旧真实
    (load_config / load_preconditions 行为完全不变), 而所有写入(update_config /
    save_preconditions 的默认路径)只落在副本里。这样即使将来有人漏 mock, 也**不可能**
    改到真实文件 —— 结构性保证, 而不是靠每个测试自觉。

★ 同源问题:pytest 与应用**共用**同一个 logger 与同一个日志文件
    (`reports/test.log`), 跑测试会把测试行灌进用户的运行日志, 连带着让日志分析失真
    (排查上面的 config 污染时就被带偏过)。同理由 `_isolate_log_file` 把文件 handler
    换到临时文件。

    QSettings(历史记录)不在此处理: 相关测试统一用 `fake_settings` 内存替身,
    且 tests/test_gui.py 里已有「测试前后真实历史必须一致」的验证。
"""
import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 用 `from core.driver import BASE_DIR` 把名字绑定到**自己模块**的那批模块。
# ★ 必须逐个钉: from-import 绑定的是各自独立的模块全局, patch `core.driver.BASE_DIR`
#   对它们完全无效(见 _isolate_base_dir 的事故记录)。
# 这份清单由 test_conftest_guard.test_每个 BASE_DIR 绑定都被隔离 扫描守护。
BASE_DIR_MODULES = (
    "core.actions.asserts",
    "core.actions.map_ops",
    "core.excel_report",
    "core.paths",
    "core.runner",
    "core.vision",
    "gui.main_window",
    "gui.step_card",
    "gui.style",
)


@pytest.fixture(autouse=True)
def _isolate_config_file(monkeypatch, tmp_path):
    """每个测试:CONFIG_PATH → 真实 config.yaml 的临时副本(读真实 / 写副本)"""
    import core.driver as driver

    real = driver.CONFIG_PATH
    sandbox = tmp_path / "config.yaml"
    if os.path.isfile(real):
        shutil.copy2(real, sandbox)      # 字节级复制, CRLF 原样保留
    else:
        sandbox.write_text("", encoding="utf-8")
    monkeypatch.setattr(driver, "CONFIG_PATH", str(sandbox), raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_group_preconditions(monkeypatch, tmp_path):
    """APP 组的前置条件落盘路径默认指向临时目录。

    ★ 事故(2026-09-24): 组前置条件先是放在 Test_cases/<组>/(被用例列表当用例扫出来),
      挪到 Test_preconditions/ 之后, 有个测试的隔离点没跟着改(patch 的是
      vision.BASE_DIR, 而路径已改成基于 driver.BASE_DIR) → 它把 2 条测试值写进了
      用户真实的 Test_preconditions/三星.yaml, 覆盖了用户的 7 条配置。
    这里直接钉住 group_preconditions_path 这个**唯一出口**: 测试里无论怎么写,
    都落临时目录(除非测试自己显式覆盖)。结构性保证, 不依赖"记得 mock"。
    """
    import core.driver as driver
    sandbox = tmp_path / "Test_preconditions"
    sandbox.mkdir(parents=True, exist_ok=True)
    # ★ 读**真实内容**、写仍落沙箱(2026-10-19): 只把路径钉走的话, 读也是空的 ——
    #   于是 `--mode real` 的真机用例(冒烟)读不到 Test_preconditions/<组>.yaml, 会静默
    #   回退到 config.yaml 的全局预设(实测: 跑出来的是"退出省电模式", 而不是组里配的
    #   "结束烘干拖布", 前置与 GUI 完全不是一套)。这里先把真实文件复制进沙箱:
    #   读 ✓ 拿到用户的组配置; 写 ✓ 仍只写沙箱, 不会覆盖用户文件。
    real_dir = os.path.join(getattr(driver, "BASE_DIR", ""), "Test_preconditions")
    if os.path.isdir(real_dir):
        for fn in os.listdir(real_dir):
            if fn.endswith((".yaml", ".yml")):
                try:
                    shutil.copy2(os.path.join(real_dir, fn), sandbox / fn)
                except OSError:
                    pass
    monkeypatch.setattr(driver, "group_preconditions_path",
                        lambda g: str(sandbox / f"{g}.yaml"), raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_qsettings(monkeypatch):
    """把 QSettings 换成内存替身 —— 测试绝不写用户的注册表。

    ★ 为什么需要: GUI 会把「历史记录」和「APP 组展开/收起状态」存进
      `HKCU\\Software\\vacuum_test\\case_studio`。测试里大量 `w.close()` 会触发
      `closeEvent` → `_save_expanded_groups()` 写真实注册表, 把用户"哪些组是展开的"
      覆盖成测试临时目录里的组(甚至空)。这与项目规则「禁止脚本改动用户真实状态」
      直接冲突 —— 同 config.yaml 一样, 必须结构性隔离而不是靠每个测试自觉。
    ★ 少数用例需要真实 QSettings 行为时, 自己 patch 回来(见 test_gui 的 fake_settings)。
    """
    from gui import main_window as mw

    class _MemSettings:
        store = {}

        def __init__(self, *a, **k):
            pass

        def value(self, key, default=None):
            return _MemSettings.store.get(key, default)

        def setValue(self, key, val):
            _MemSettings.store[key] = val

        def sync(self):
            pass

        def remove(self, key):
            _MemSettings.store.pop(key, None)

    # ★ patch 模块级名字(gui.main_window.QSettings), **不要**去 setattr PySide6 的
    #   类型本身 —— 对包装类型改类属性会死锁(项目里踩过)
    monkeypatch.setattr(mw, "QSettings", _MemSettings, raising=False)
    yield


@pytest.fixture(autouse=True)
def _expand_groups_in_tests(monkeypatch, request):
    """测试里默认把 APP 组**全部展开**。

    ★ 界面默认是**收起**的(用户要求: 用例列表默认收回 APP 组)。但收起时用例行
      根本不生成, 而绝大多数 GUI 测试要直接操作用例行(勾选/排序/切换用例) ——
      逐个测试去展开既啰嗦又容易漏(实测: 默认折叠一上线, test_gui 里 24 个用例
      立刻失败)。所以这里在测试环境统一展开。
    ★ 想验证"默认折叠"本身, 给测试打标记 `@pytest.mark.real_collapse` 豁免即可
      (见 test_case_groups_collapsed_by_default)。
    """
    if request.node.get_closest_marker("real_collapse"):
        yield
        return
    from gui import main_window as mw
    orig = mw.MainWindow._fill_case_list

    def _fill_expanded(self, *a, **k):
        self._collapsed_groups.clear()             # 测试里全展开
        self._known_groups.update(self._list_group_dirs())   # 别被"默认折叠"重新收起
        return orig(self, *a, **k)

    monkeypatch.setattr(mw.MainWindow, "_fill_case_list", _fill_expanded)
    yield


@pytest.fixture(autouse=True)
def _no_real_update_check(monkeypatch):
    """测试里一律不发真实的更新检查(它要联网、还可能在回调里弹模态窗)。

    ★ 检查更新是**后台线程 + 信号回调**: 回调可能在**别的测试**的 processEvents
      里才被派发(那时窗口已关、还会弹模态), 既拖慢又污染。检查逻辑本身由
      tests/test_updater.py 专门覆盖。
    """
    from gui import main_window as mw
    real = mw.MainWindow._check_update_now

    def _guarded(self):
        # 少数用例要测**真实**的检查逻辑, 自己把 _allow_real_check 置 True 即可
        if getattr(self, "_allow_real_check", False):
            return real(self)

    monkeypatch.setattr(mw.MainWindow, "_check_update_now", _guarded, raising=False)
    yield


@pytest.fixture(autouse=True)
def _disable_ocr_by_default(monkeypatch, pytestconfig):
    """默认关掉 OCR 兜底 —— 单元测试不该真跑它。

    ★ 为什么必须关: `text_present` 在"原生没命中 **且** 页面文本稀疏(<5 条)"时会去
      OCR。而测试桩的层级 XML 通常只有一两个节点, 天然满足"稀疏" —— 于是每个判断
      都会去截图跑识别: 桩没有 screenshot 方法就每次吞一个异常, 一轮测试能拖到
      60 秒以上(实测: 接入判断类兜底后 test_spot_clean 从 5 秒涨到 77 秒)。
      真机上的 OCR 行为由 tests/test_ocr_fallback.py 专门覆盖, 那里会自行 mock 启用。

    ★ **真机模式(`--mode real`)不能关**(2026-10-19): 插件页(WebView)的卡片标题
      经常**不在无障碍树里**(实测: 机器人"正在干燥拖布"时, 树里 0 条含"干燥"的节点,
      只能靠 OCR), 关了 OCR 的话 `assert: 干燥拖布` 会一直等到超时/硬超时。
    """
    if pytestconfig.getoption("--mode") == "real":
        yield
        return
    from core import ocr
    monkeypatch.setattr(ocr, "available", lambda: False, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_base_dir(monkeypatch, tmp_path, pytestconfig):
    """把各模块的 BASE_DIR 钉到临时目录 —— 跑测试绝不往仓库里写产物。

    ★ **真机模式(`--mode real`)不隔离**(2026-10-19): 真机运行就该和 GUI 走同一套
      真实路径 —— 截图落 `Test_cases/<组>/screenshots/`(已在 .gitignore), 冒烟失败时
      workflow 才收得上现场。钉到临时目录还有更直接的问题: 沙箱里没有 `Test_cases/`,
      模板解析成 `...pytest-xxx\\repo\\Test_cases\\三星\\templates\\开始清扫.png`,
      不存在 → 用例**第一步**就 `WinError 3`(实测)。

    ★ 事故(2026-09-24 审查发现)
      项目里普遍写 `from core.driver import BASE_DIR`, 这是把名字**绑定到各自模块**;
      patch `core.driver.BASE_DIR` 对它们**完全无效**。`tests/test_room_zones.py`
      就是这么以为隔离好了, 结果每跑一次单测都把假的分区标注图写进用户真实的
      `Test_img/debug/`(实测: 跑 test_room_zones + test_runner_core 后真实目录多出
      map_screen/roi/mask/zone.png)。而根 conftest 模块级的 `_clean_debug_dir()` 又会
      把用户真机刚生成的诊断图删掉 —— 一个写、一个删, 用户无法分辨哪些是真机结果。

    这里逐模块钉住(sandbox 下自带 config/ 以免个别模块 makedirs 失败),
    并由 `test_conftest_guard` 扫描守护: 将来谁新增模块漏进清单会被测出来。
    """
    if pytestconfig.getoption("--mode") == "real":
        yield
        return

    sandbox = tmp_path / "repo"
    (sandbox / "config").mkdir(parents=True, exist_ok=True)
    for name in BASE_DIR_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue                # 未安装的可选依赖(如无 PySide6)跳过
        if hasattr(mod, "BASE_DIR"):
            monkeypatch.setattr(mod, "BASE_DIR", str(sandbox), raising=False)
    # ★ 2026-10-11: 原来这里还要单独钉 vision.TEMPLATE_DIR(全局模板目录是 import 时
    #   拼好的模块级常量) —— 那个目录已删除, 模板一律走 group_templates_dir(基于
    #   core.driver.BASE_DIR, 上面的循环已经隔离了)。
    yield


@pytest.fixture
def mk_run_dirs(tmp_path):
    """造"运行目录"(截图封顶类测试共用, 2026-10-15 从 test_packaging 内联迁来)。

    用法: mk_run_dirs("Test_cases/组甲/screenshots", [("甲用例_r..._第1次", 3000),
    ("甲用例_r..._第2次", 3001)], n_files=2)
    —— 每个目录里放 n_files 个文件, 目录与文件的 mtime 都设为给定值(便于"留最新")。
    """
    def _make(d, specs, n_files=2, size=1024):
        base = d if os.path.isabs(str(d)) else tmp_path / d
        out = []
        for spec in specs:
            name, mtime = spec if isinstance(spec, (list, tuple)) else (spec, None)
            p = base / name
            p.mkdir(parents=True, exist_ok=True)
            for i in range(n_files):
                f = p / f"step{i:02d}.png"
                f.write_bytes(b"x" * size)
                if mtime:
                    os.utime(f, (mtime, mtime))
            if mtime:
                os.utime(p, (mtime, mtime))
            out.append(p)
        return out
    return _make


@pytest.fixture
def mk_stale_files(tmp_path):
    """造 n 个 mtime 递增的文件 —— 「留最新 N 个」断言的根基(审查 2026-10-12 #31)。

    test_idle_cleanup / test_packaging 都要这个错峰模式; 以前各处手抄 12+ 份,
    base 偏移还各不相同(漏改一处就是假绿/假红)。d 传相对 tmp_path 的路径或绝对路径。
    """
    def _make(d, n, size=10, base=1000, prefix="f", ext=".png"):
        d = Path(d)
        if not d.is_absolute():
            d = tmp_path / d
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(n):
            f = d / f"{prefix}{i}{ext}"
            f.write_bytes(b"x" * size)
            os.utime(f, (base + i, base + i))
            out.append(f)
        return out
    return _make


@pytest.fixture(autouse=True)
def _reset_idle_gate():
    """每个测试前后把「空闲闸门」清零(core/idle.py)。

    ★ 为什么必须: 闸门是**模块级全局状态** —— 某条测试跑了 RunWorker(进入执行态)或
      排队了一件推迟的清理, 若不清掉, 下一条测试的清理就会被莫名推迟/补做, 变成
      难查的顺序依赖。
    ★ 只丢弃排队项、**不执行**它们: 那些清理此刻指向的是上一条测试的临时目录, 补做
      没有意义, 还可能在别的测试里凭空删文件。
    """
    from core import idle
    idle.reset()
    yield
    idle.reset()


@pytest.fixture(autouse=True)
def _fast_idle_flush():
    """★ 空闲宽限改 0.05s(用户 2026-10-14 加了 120s 宽限): 测试不等两分钟 ——
    既有用例的 end_run→flush 行为在 0.05s 下与原语义一致(立即≈很快)。"""
    from core import idle
    idle.FLUSH_IDLE_DELAY = 0.05
    yield
    idle.FLUSH_IDLE_DELAY = 0.05


@pytest.fixture(autouse=True)
def _reset_status_words():
    """★ 状态词/streak 在每个测试前后还原默认(用户 2026-10-13):
    set_status_words() 改的是模块级全局, 不清掉会泄漏给下一个测试 ——
    表现为"全量跑失败、单跑通过"的诡异现象(fault_texts 被前一条改成"缺纸"等)。
    """
    from core import session
    session.reset_status_words()
    yield
    session.reset_status_words()


@pytest.fixture(autouse=True)
def _reset_template_app_group():
    """★ APP 组上下文在每个测试前后还原(结构整理 2026-10-15 —— 补上此前漏掉的一处全局)。

    `core.vision._template_app_group` 是**模块级全局**: 界面/引擎在跑用例前会
    `set_template_app_group(组名)`, 而测试里谁 set 了就一直留给下一个测试 ——
    实测踩到: test_preconditions 把组设成「组A」后, 后面 test_actions_basic 的
    截图断言里路径变成了 `Test_cases/组A/...`(它按"未分组"断言), 全量跑红、单跑绿。

    与 _reset_status_words 同一类问题、同一种修法: 前后都还原, 谁也别留给谁。
    """
    from core import vision
    vision.set_template_app_group("")
    yield
    vision.set_template_app_group("")


@pytest.fixture(scope="session", autouse=True)
def _isolate_log_file(tmp_path_factory):
    """测试期日志写到临时文件, 不污染用户的 reports/test.log。

    logger 是全局单例("vacuum_test"), `setup_logger` 只应在应用启动时调用一次;
    这里把它的文件 handler 换成指向临时文件的(真实文件即使已被打开也会被关闭),
    所以无论 logger 何时被初始化, 测试都不可能再往用户的日志里写。
    """
    import logging

    from core import logger as core_logger

    log = core_logger.get_logger()          # 未初始化则先按默认路径初始化
    for h in list(log.handlers):
        if isinstance(h, core_logger.RotatingLogHandler):
            log.removeHandler(h)
            h.close()                       # 释放对 reports/test.log 的句柄
    sandbox = tmp_path_factory.mktemp("logs") / "test.log"
    fh = core_logger.RotatingLogHandler(str(sandbox))
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    yield
