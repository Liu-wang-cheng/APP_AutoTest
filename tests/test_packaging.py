# -*- coding: utf-8 -*-
"""打包准备(阶段 1)的守护: 版本号单一真源 / 程序目录与数据目录拆分 / 自动备份。

这三件都是"为了能打包发布并做 OTA"才引入的 —— 它们的共同风险是**悄悄改变现有
行为**(路径解析)或**悄悄不生效**(备份没做、版本号读不到)。所以逐条钉住。
"""
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = str(Path(__file__).resolve().parent.parent)


# ── 版本号: OTA 的比较基准 ──

def test_version_parse_tolerates_garbage():
    """远程版本串是**外部数据**, 解析必须容错而不是抛异常。

    抛了就会打断整个"检查更新"流程 —— 一个畸形版本号不该让更新功能瘫痪。
    """
    from core.version import parse
    assert parse("1.2.3") == (1, 2, 3)
    assert parse("v1.2") == (1, 2)
    assert parse("") == (0,)
    assert parse(None) == (0,)
    assert parse("1.2-beta") == (1, 2)      # 后缀截断
    assert parse("垃圾") == (0,)


@pytest.mark.parametrize("remote,local,want", [
    ("1.1", "1.0", True),
    ("1.0", "1.0", False),
    ("1.0", "1.1", False),      # 远程比本地旧 -> 不算更新
    ("1.2.0", "1.2", False),    # 补零后相等 -> 不算更新(否则会反复提示更新)
    ("1.2.1", "1.2", True),
    ("2.0", "1.9.9", True),
    ("", "1.0", False),
    (None, "1.0", False),
])
def test_version_is_newer(remote, local, want):
    from core.version import is_newer
    assert is_newer(remote, local) is want


def test_gui_title_uses_core_version():
    """★ 版本号必须单一真源: GUI 显示的就是 core.version 里的那个值。

    回归守护: 版本号原先硬编码在 gui/main_window.py, 而 OTA 要在**不导入 PySide6**
    的前提下读到它(更新器/打包脚本不该为了一个字符串拖上一整份 Qt)。两处各写一份,
    迟早改了一处忘了另一处 —— 那会让"检查更新"永远判断错。
    """
    from core import version
    import gui.main_window as mw
    assert mw.APP_VERSION == version.__version__


# ── 程序目录 / 用户数据目录拆分 ──

def test_dirs_identical_in_dev_env():
    """★ 未打包时三者都是项目根 —— 这次拆分对现有使用必须**零影响**

    注: 这里不比对 CONFIG_PATH —— 它已被 conftest 的 _isolate_config_file 指向
    临时副本(那是有意的隔离), 比对它会变成在测隔离夹具而不是在测本模块。
    """
    from core.driver import APP_DIR, BASE_DIR, DATA_DIR
    assert APP_DIR == DATA_DIR == BASE_DIR == ROOT


def test_resolve_dirs_uses_exe_dir_when_frozen(monkeypatch, tmp_path):
    """★ 打包后必须取 exe 所在目录, **不能**用 __file__ 推。

    PyInstaller 把代码收在 _internal/ 下, __file__ 指向 _internal/core/driver.py,
    推出的是 _internal/ 而不是 exe 旁边 —— 于是既找不到用户的 config/ 也找不到
    Test_cases/。这里用 sys.frozen + sys.executable 模拟打包环境。
    """
    from core.driver import _resolve_dirs
    exe = tmp_path / "AutoTest.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    app_dir, data_dir = _resolve_dirs()
    assert app_dir == str(tmp_path)
    assert data_dir == str(tmp_path)


def test_user_data_paths_cover_all_user_data():
    """★ OTA 靠这份清单决定"更新时什么不能碰", 漏一项那类数据就会被覆盖。

    config.yaml 尤其要命: 它被 gitignore 忽略、没有版本保护, 丢了只能手工重建。
    """
    from core.driver import USER_DATA_PATHS
    need = {"config/config.yaml", "Test_cases", "Test_preconditions",
            "Test_img", "backups"}
    missing = need - set(USER_DATA_PATHS)
    assert not missing, f"这些用户数据没被保护, OTA 更新会覆盖它们: {missing}"
    # locators.yaml / config.example.yaml 是随版本走的程序资源, 不该被保护
    # (否则它们的修 bug 就永远发不出去)
    assert "config/locators.yaml" not in USER_DATA_PATHS


# ── 自动备份 ──

def _make_data(root):
    """造一份有代表性的用户数据 + 若干运行产物

    ★ 用 makedirs(exist_ok=True) 而不是 mkdir(): conftest 的
      _isolate_group_preconditions 夹具已经在 tmp_path 下建过 Test_preconditions/,
      直接 mkdir 会撞 FileExistsError。
    """
    os.makedirs(root / "config", exist_ok=True)
    (root / "config" / "config.yaml").write_text(
        "target_device: 我的扫地机\n", encoding="utf-8")
    os.makedirs(root / "Test_cases" / "三星", exist_ok=True)
    (root / "Test_cases" / "三星" / "a.yaml").write_text(
        "module: a\ncases: []\n", encoding="utf-8")
    os.makedirs(root / "Test_preconditions", exist_ok=True)
    (root / "Test_preconditions" / "三星.yaml").write_text(
        "preconditions: []\n", encoding="utf-8")
    os.makedirs(root / "Test_img" / "templates", exist_ok=True)
    (root / "Test_img" / "templates" / "开始清扫.png").write_bytes(b"fake")
    # ↓ 这些是运行产物: 可再生、体积大, 不该进备份
    os.makedirs(root / "Test_img" / "screenshots", exist_ok=True)
    (root / "Test_img" / "screenshots" / "big.png").write_bytes(b"x" * 500)
    os.makedirs(root / "reports", exist_ok=True)
    (root / "reports" / "r.xlsx").write_bytes(b"x")


def test_backup_contains_user_data_only(tmp_path):
    """备份要装齐"人做出来的"数据, 但不该把截图/报告这类产物也塞进去"""
    from core.backup import make_backup
    _make_data(tmp_path)
    p = make_backup(data_dir=str(tmp_path), force=True)
    assert p and os.path.isfile(p)
    with zipfile.ZipFile(p) as zf:
        names = set(zf.namelist())
    assert "config/config.yaml" in names
    assert any(n.startswith("Test_cases") for n in names), names
    assert any(n.startswith("Test_preconditions") for n in names), names
    assert any(n.startswith("Test_img/templates") for n in names), names
    assert not any("screenshots" in n for n in names), f"截图不该进备份: {names}"
    assert not any(n.startswith("reports/") for n in names), f"报告不该进备份: {names}"


def test_backup_once_per_day(tmp_path):
    """★ 同一天只备一次 —— 否则用户一天开十次程序就产生十份, 把有价值的旧备份挤掉

    用注入的固定时间戳, 而不是真实时钟: 真实时钟两次调用会落在同一秒,
    文件名相同就分不出"跳过了"还是"又备了一份"。
    """
    from core.backup import make_backup
    day1 = time.mktime((2026, 1, 15, 10, 0, 0, 0, 0, -1))
    _make_data(tmp_path)

    first = make_backup(data_dir=str(tmp_path), now=day1)
    assert first, "首次备份应该成功"
    assert "20260115" in os.path.basename(first)

    # 同一天晚些时候再启动 -> 跳过
    assert make_backup(data_dir=str(tmp_path), now=day1 + 3600) == "", \
        "当天第二次不该再备一份"

    # 跨天 -> 应该再备一份
    second = make_backup(data_dir=str(tmp_path), now=day1 + 86400)
    assert second and second != first, "跨天后应该再备一份"
    assert "20260116" in os.path.basename(second)

    # force -> 同一天也强制再备(时间戳不同)
    forced = make_backup(data_dir=str(tmp_path), now=day1 + 7200, force=True)
    assert forced and forced != first, "force=True 应强制再备一份"


def test_backup_prunes_old(tmp_path):
    """保留策略: 只留最近 keep 份, 且必须留住最新的那份"""
    from core.backup import make_backup
    _make_data(tmp_path)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    for i in range(5):                      # 造 5 份"旧备份"
        (bdir / f"2026010{i + 1}-000000.zip").write_bytes(b"old")
    p = make_backup(data_dir=str(tmp_path), keep=3, force=True)
    assert p
    left = sorted(f.name for f in bdir.glob("*.zip"))
    assert len(left) == 3, f"应只保留 3 份, 实际 {left}"
    assert os.path.basename(p) in left, "最新的那份必须留着"


def test_backup_never_raises(tmp_path, monkeypatch):
    """★ 备份是兜底手段, 它失败绝不能拖垮启动 —— 异常一律吞掉并返回空串"""
    from core import backup
    _make_data(tmp_path)

    def boom(*_a, **_k):
        raise OSError("模拟: 磁盘满")

    monkeypatch.setattr(backup.zipfile, "ZipFile", boom)
    assert backup.make_backup(data_dir=str(tmp_path), force=True) == ""
    bdir = tmp_path / "backups"
    if bdir.is_dir():
        assert not list(bdir.glob("*.zip")), "失败时不该留下半截 zip 骗人"


def test_backup_skips_when_nothing_to_back_up(tmp_path):
    """全新环境(什么都还没配)不该产生一个空备份

    ★ 用 tmp_path 下一个**干净子目录**当数据目录: conftest 的
      _isolate_group_preconditions 夹具已经在 tmp_path 下建了 Test_preconditions/,
      直接拿 tmp_path 当数据目录会被它干扰(误判成"有内容")。
    """
    from core.backup import make_backup
    clean = tmp_path / "clean_data"
    assert make_backup(data_dir=str(clean), force=True) == ""


# ── OTA 的 GUI 接入 ──

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, monkeypatch, tmp_path):
    """真实的 MainWindow(用于验证装配/按钮状态)。

    ★ 必须 stub DeviceScanThread: 真实版要跑 adb 子进程(最坏 30s)并留一个后台
      QThread; 这些测试建的窗口不会真的被销毁, 残留线程/定时器会污染**后面**的
      测试 —— 实测: 与 test_preconditions.py 一起跑时, 后者会卡死在一个
      再普通不过的对话框用例上(单独跑却通过)。
    ★ 同时停掉 _update_timer, 避免每 8 小时的定时器在测试期间触发真实网络检查。
    """
    from PySide6.QtCore import QObject, Signal

    import gui.main_window as mw
    monkeypatch.setattr(mw, "CASES_DIR", str(tmp_path / "Test_cases"), raising=False)
    monkeypatch.setattr(mw, "load_config", lambda: {"app": {}, "device": {}},
                        raising=False)

    class _NoScan(QObject):
        done = Signal(list, str)

        def __init__(self, *a, **k):
            super().__init__()

        def start(self):
            pass

    monkeypatch.setattr(mw, "DeviceScanThread", _NoScan)
    w = mw.MainWindow()
    w._update_timer.stop()
    w._first_check_timer.stop()      # 别让"启动 3 秒后检查"在测试期间触发真实网络
    # ★ 再兜一道: 把"真发检查"整个替换掉。检查是**后台线程 + 信号回调**, 回调可能
    #   在**后面测试**的 processEvents 里才被派发, 那时它会弹模态窗 → 挂死别的测试
    #   (实测: 与 test_preconditions 同跑时, 后者卡在一个普通对话框用例上)。
    monkeypatch.setattr(w, "_check_update_now", lambda: None)
    yield w
    w.worker = None
    w.close()
    # ★ 彻底销毁 + 排空事件队列: close() 只是隐藏窗口, 对象本身(以及它身上的定时器、
    #   信号连接)还在。这些窗口累积下来, 残留回调会在**后面测试**的 processEvents
    #   里被派发, 把不相干的用例拖死(实测: 与 test_preconditions 同跑时, 后者卡在
    #   一个普通对话框用例上; 单个跑却通过)。
    w.deleteLater()
    from PySide6.QtWidgets import QApplication as _QApp
    _app = _QApp.instance()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


def test_update_timer_period_follows_config(win, monkeypatch):
    """★ 每 8 小时自动检测: 周期取自配置(默认 8 小时), 不是写死的"""
    from core import updater
    assert win._update_timer.interval() == \
        int(updater.CHECK_INTERVAL_HOURS * 3600 * 1000), "默认应为 8 小时"

    # 配置里改成 2 小时 -> 新窗口的周期跟着变
    import gui.main_window as mw
    monkeypatch.setattr(mw, "load_config",
                        lambda: {"update": {"check_interval_hours": 2}}, raising=False)
    w2 = mw.MainWindow()
    try:
        assert w2._update_timer.interval() == 2 * 3600 * 1000
    finally:
        w2.close()


def test_check_skipped_while_running_cases(win, monkeypatch):
    """★ 用例执行期间**不检测**新版本(用户明确要求) —— 不建线程、不发请求"""
    started = []
    import gui.main_window as mw

    # 这个用例要测**真实**的 _check_update_now, 而 win 夹具把它 stub 掉了
    # (夹具的 stub 是为了不让 3 秒定时器在测试期间发真实网络请求) —— 这里恢复回来
    monkeypatch.setattr(win, "_check_update_now",
                        type(win)._check_update_now.__get__(win))

    class _FakeThread:
        def __init__(self, *a, **k):
            started.append(1)
    monkeypatch.setattr(mw, "UpdateCheckThread", _FakeThread)

    win.worker = object()          # 模拟"正在跑用例"
    try:
        win._check_update_now()
    finally:
        # ★ 必须清掉: 否则 teardown 的 close() 会因为它弹「正在执行,停止并退出?」
        #   的**模态**对话框 —— 断言一旦失败就会挂死(踩过)
        win.worker = None
    assert started == [], "执行用例期间不该发起检查"

    win._check_update_now()
    assert started == [1], "空闲时应该发起检查"


def _capture_dialogs(win, monkeypatch):
    """把更新弹窗换成记录器。

    ★ 必须拦: _update_message 内部是**模态** box.exec(), 测试里没有真人点按钮,
      真跑就会一直等 —— 一旦漏拦, 整个测试进程挂死(踩过两次)。
    返回 [(heading, body, kind, buttons)]; 模拟用户点默认按钮。
    """
    calls = []

    def fake(heading, body="", kind="info", buttons=None, default=0):
        texts = list(buttons or ["知道了"])
        calls.append((heading, body, kind, texts))
        return texts[default]

    monkeypatch.setattr(win, "_update_message", fake)
    return calls


def test_only_new_version_pops_dialog(win, monkeypatch):
    """自动检查只在「有新版本」时打扰用户; 已是最新/失败/没配仓库都只写日志"""
    from core import updater
    dialogs = _capture_dialogs(win, monkeypatch)

    win._update_manual = False
    win._on_update_checked(updater.CheckResult("up_to_date", "已是最新(1.2)"))
    win._on_update_checked(updater.CheckResult("error", "网络不通"))
    win._on_update_checked(updater.CheckResult("skipped", "未配置仓库"))
    assert dialogs == [], "自动检查不该为这些状态弹窗"

    info = updater.parse_version_info({"version": "2.0", "release_notes": "修了 X",
                                       "release_date": "2026-10-01"})
    win._on_update_checked(updater.CheckResult("has_update", "发现新版本 2.0",
                                               info, None, False))
    assert len(dialogs) == 1, "有新版本应该弹窗"
    heading, body, _kind, buttons = dialogs[0]
    assert "2.0" in heading and "修了 X" in body
    assert buttons == ["立即更新", "稍后再说"]


def test_force_update_mentions_mandatory(win, monkeypatch):
    """低于 min_version 时只给「立即更新」一个选择, 且标题说明"需更新"""
    from core import updater
    dialogs = _capture_dialogs(win, monkeypatch)
    info = updater.parse_version_info({"version": "3.0", "min_version": "2.0"})
    win._update_manual = False
    win._on_update_checked(updater.CheckResult("has_update", "x", info, None, True))
    assert dialogs[0][3] == ["立即更新"], dialogs[0][3]
    assert "需更新" in dialogs[0][0], dialogs[0][0]


def test_check_update_never_raises_in_gui(win, monkeypatch):
    """检查更新出岔子不能把界面搞崩(它跑在启动路径上)"""
    import gui.main_window as mw

    def boom(*_a, **_k):
        raise RuntimeError("模拟: 线程起不来")

    monkeypatch.setattr(mw, "UpdateCheckThread", boom)
    win._check_update_now()          # 不得抛


def test_update_button_exists_and_disabled_while_running(win):
    """★ 「检查更新」按钮: 存在, 且**用例执行期间置灰**(用户要求)。

    运行中即使点得到也没意义 —— 更新完还得重启, 而带着执行中的用例重启会毁掉
    这一轮, 所以不如直接禁用, 让用户一眼看出"现在不行"。
    """
    assert hasattr(win, "update_btn"), "工具栏缺少「检查更新」按钮"
    assert win.update_btn.text() == "检查更新"
    assert win.update_btn.isEnabled(), "空闲时应可点"

    win._set_locked(True)             # 模拟开始执行用例
    assert not win.update_btn.isEnabled(), "用例执行期间该按钮必须置灰"

    win._set_locked(False)            # 执行结束
    assert win.update_btn.isEnabled(), "执行结束后应恢复可点"


def test_manual_check_gives_feedback_on_every_result(win, monkeypatch):
    """★ 手动检查**必须有反馈**: 已是最新/失败/没配仓库都要说一声。

    自动检查每 8 小时静默跑一次, 只在有新版时弹窗; 手动点是用户的明确意图,
    点了没反应会让人以为按钮坏了。
    """
    from core import updater
    dialogs = _capture_dialogs(win, monkeypatch)

    # 自动检查: 已是最新 -> 不打扰
    win._update_manual = False
    win._on_update_checked(updater.CheckResult("up_to_date", "已是最新(1.1)"))
    assert dialogs == [], "自动检查不该为'已是最新'弹窗"

    # 手动检查: 已是最新 -> 必须回话
    win._update_manual = True
    win._on_update_checked(updater.CheckResult("up_to_date", "已是最新(1.1)"))
    assert dialogs and dialogs[0][2] == "info" and "最新" in dialogs[0][0], dialogs

    dialogs.clear()
    win._update_manual = True
    win._on_update_checked(updater.CheckResult("error", "所有镜像都取不到版本清单"))
    assert dialogs and dialogs[0][2] == "warn", dialogs
    # 原始技术原因要带上(排查靠它), 但不能只甩一句冷冰冰的错误
    assert "镜像" in dialogs[0][1] and "网络" in dialogs[0][1], dialogs[0][1]

    dialogs.clear()
    win._update_manual = True
    win._on_update_checked(updater.CheckResult("skipped", "未配置更新仓库"))
    assert dialogs and "update.repository" in dialogs[0][1], dialogs


def test_manual_check_blocked_while_running(win, monkeypatch):
    """运行中点「检查更新」(程序化调用/竞态)也不能真发检查"""
    win.worker = object()
    try:
        called = []
        monkeypatch.setattr(win, "_check_update_now", lambda: called.append(1))
        _capture_dialogs(win, monkeypatch)
        win.on_check_update()
        assert called == [], "执行用例期间不该发起检查"
    finally:
        win.worker = None


def test_update_button_not_reenabled_while_still_locked(win, monkeypatch):
    """检查完成的回调不得把"运行中"的置灰又点亮(否则按钮状态与锁定状态打架)"""
    from core import updater
    _capture_dialogs(win, monkeypatch)
    win._set_locked(True)
    win._update_manual = False
    win._on_update_checked(updater.CheckResult("up_to_date", "已是最新(1.1)"))
    assert not win.update_btn.isEnabled(), "锁定中不该被检查回调点亮"
    win._set_locked(False)


# ── 首次运行的资源铺出(打包后才会暴露的问题) ──

def _seed_src(root):
    """造一份"包内资源"(模拟 _internal/ 里的样子)"""
    os.makedirs(root / "config", exist_ok=True)
    (root / "config" / "locators.yaml").write_text(
        "预约清扫:\n  添加按钮: 添加预约.png\n", encoding="utf-8")
    (root / "config" / "config.example.yaml").write_text(
        "app:\n  name: 示例\n", encoding="utf-8")


def test_bootstrap_seeds_program_resources(tmp_path):
    """★ config/locators.yaml 在包里(程序资源), 程序却读 exe 旁的数据目录 ——
    不铺一次就找不到, 而定位器缺失会让 ${段.键} 引用**安静地**全部失效。"""
    from core import bootstrap
    src, dst = tmp_path / "pkg", tmp_path / "data"
    _seed_src(src)
    dst.mkdir()

    seeded = bootstrap.ensure_data_dirs(app_dir=str(src), data_dir=str(dst))
    assert "config/locators.yaml" in seeded
    assert (dst / "config" / "locators.yaml").is_file()
    # 没有用户配置时, 从模板生成一份, 免得程序一上来就读不到配置
    assert (dst / "config" / "config.yaml").is_file()


def test_bootstrap_seeds_default_cases_only_when_missing(tmp_path):
    """★ 默认用例/模板: 本地**缺**的文件解压出来当默认值; 已有的**绝不覆盖**。

    用户要求(2026-09-24): 更新/重装不替换本地的用例或模板之类的数据,
    只有本地没有这些文件才解压出来变成默认的。
    """
    from core import bootstrap
    src, dst = tmp_path / "pkg", tmp_path / "data"
    # 包内自带一套默认用例与模板
    (src / "Test_cases" / "三星").mkdir(parents=True)
    (src / "Test_cases" / "三星" / "a.yaml").write_text("module: a\n",
                                                        encoding="utf-8")
    (src / "Test_cases" / "三星" / "templates").mkdir()
    (src / "Test_cases" / "三星" / "templates" / "t.png").write_bytes(b"png")
    (src / "Test_img" / "templates").mkdir(parents=True)
    (src / "Test_img" / "templates" / "README.md").write_text("r",
                                                              encoding="utf-8")
    dst.mkdir()
    # 用户本地已有一个改过的同名用例
    (dst / "Test_cases" / "三星").mkdir(parents=True)
    (dst / "Test_cases" / "三星" / "a.yaml").write_text("module: 用户改过的\n",
                                                        encoding="utf-8")

    bootstrap.ensure_data_dirs(app_dir=str(src), data_dir=str(dst))

    # 缺的补上
    assert (dst / "Test_cases" / "三星" / "templates" / "t.png").read_bytes() == b"png"
    assert (dst / "Test_img" / "templates" / "README.md").read_text() == "r"
    # 已有的绝不覆盖
    assert (dst / "Test_cases" / "三星" / "a.yaml").read_text(
        encoding="utf-8") == "module: 用户改过的\n"


def test_bootstrap_never_overwrites_user_data(tmp_path):
    """★ 已存在的文件一律不动 —— 用户改过的定位器/配置不能被初始化覆盖"""
    from core import bootstrap
    src, dst = tmp_path / "pkg", tmp_path / "data"
    _seed_src(src)
    os.makedirs(dst / "config", exist_ok=True)
    (dst / "config" / "locators.yaml").write_text("用户改过的\n", encoding="utf-8")
    (dst / "config" / "config.yaml").write_text("target_device: 我的扫地机\n",
                                                encoding="utf-8")
    bootstrap.ensure_data_dirs(app_dir=str(src), data_dir=str(dst))
    assert (dst / "config" / "locators.yaml").read_text(encoding="utf-8") == "用户改过的\n"
    assert "我的扫地机" in (dst / "config" / "config.yaml").read_text(encoding="utf-8")


def test_bootstrap_noop_in_dev_env(tmp_path):
    """开发环境源与目标同目录 -> 不做任何事(不能把项目里的文件复制一遍)"""
    from core import bootstrap
    _seed_src(tmp_path)
    assert bootstrap.ensure_data_dirs(app_dir=str(tmp_path), data_dir=str(tmp_path)) == []


def test_bootstrap_never_raises(tmp_path, monkeypatch):
    """初始化失败不能拦住启动"""
    from core import bootstrap
    src, dst = tmp_path / "pkg", tmp_path / "data"
    _seed_src(src)
    dst.mkdir()

    def boom(*_a, **_k):
        raise OSError("模拟: 目录只读")

    monkeypatch.setattr(bootstrap.shutil, "copy2", boom)
    bootstrap.ensure_data_dirs(app_dir=str(src), data_dir=str(dst))   # 不得抛


def test_cleanup_update_leftovers(tmp_path):
    """★ 上次更新被杀软打断留下的残留必须启动时清掉:
    半截下载(_update_download.exe)与旧程序备份(*.exe.bak)。"""
    from core import bootstrap
    (tmp_path / "_update_download.exe").write_bytes(b"partial")
    (tmp_path / "AutoTest.exe.bak").write_bytes(b"old")
    cleaned = bootstrap.cleanup_update_leftovers(app_dir=str(tmp_path))
    assert "_update_download.exe" in cleaned
    assert "AutoTest.exe.bak" in cleaned
    assert not (tmp_path / "_update_download.exe").exists()
    assert not (tmp_path / "AutoTest.exe.bak").exists()
    # 没有残留时是安静的无操作
    assert bootstrap.cleanup_update_leftovers(app_dir=str(tmp_path)) == []


# ── 下载与自替换的 GUI 流程 ──

def _update_host():
    """更新逻辑测试的哑宿主: 只提供 _apply_update_and_restart / _on_update_checked
    用到的属性, 不构造 MainWindow —— 它的装配已由其他测试覆盖, 而且在部分 monkeypatch
    组合下构造会卡死(实测), 没必要为验证更新逻辑再冒险。"""
    class _Host:
        worker = None
        _update_bat_path = ""
        dialogs = []

        def _start_update_download(self, r):
            pass

        def _update_message(self, heading, body="", kind="info", buttons=None,
                            default=0):
            """替身: 记录并返回默认按钮 —— 真实实现是模态 exec(), 测试里不能真跑"""
            texts = list(buttons or ["知道了"])
            self.dialogs.append((heading, body, kind, texts))
            return texts[default]
    return _Host()


def test_download_done_flow(monkeypatch, tmp_path):
    """确认更新 -> 启动替换脚本并 os._exit(0); bat 缺失/用例执行中则报错不退出"""
    import gui.main_window as mw
    import subprocess
    host = _update_host()
    called = {}
    monkeypatch.setattr(mw.QMessageBox, "information",
                        lambda *a, **k: mw.QMessageBox.Ok)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)

    def fake_popen(*a, **k):
        called["popen"] = (a, k)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # os._exit 是真实退进程 —— 用哨兵异常拦截, 验证确实以 0 退出
    class _Exit(Exception):
        pass

    def fake_exit(code):
        called["exit"] = code
        raise _Exit()

    monkeypatch.setattr(mw.os, "_exit", fake_exit)

    bat = tmp_path / "_update.bat"
    bat.write_text("@echo off\r\n", encoding="utf-8")
    host._update_bat_path = str(bat)
    with pytest.raises(_Exit):
        mw.MainWindow._apply_update_and_restart(host)
    assert called.get("exit") == 0, \
        "必须以 os._exit(0) 退出 —— 否则 DLL 句柄不释放, 替换会失败"
    assert called.get("popen"), "没有启动替换脚本"
    assert called["popen"][1].get("creationflags") is not None, \
        "更新窗口必须独立可见(CREATE_NEW_CONSOLE), 藏起来用户会以为没反应"
    assert called["popen"][1]["cwd"] == str(tmp_path), "bat 必须在自己的目录里执行"

    # bat 不存在时: 报错而不是退进程
    host._update_bat_path = str(tmp_path / "不存在.bat")

    def no_exit(_code):
        raise AssertionError("bat 不存在却退出了进程")

    monkeypatch.setattr(mw.os, "_exit", no_exit)
    mw.MainWindow._apply_update_and_restart(host)      # 不得退出

    # 用例执行中: 拒绝重启更新(带着正在跑的用例 os._exit 会毁掉这一轮)
    def never(_code):
        raise AssertionError("执行用例时仍退出了进程")

    monkeypatch.setattr(mw.os, "_exit", never)
    host.worker = object()
    host.dialogs = []
    host._update_bat_path = str(bat)       # bat 存在, 但必须被 worker 守卫拦住
    mw.MainWindow._apply_update_and_restart(host)
    assert host.dialogs, "执行用例时应弹窗拒绝而不是退出"
    assert host.dialogs[0][2] == "warn", host.dialogs
    host.worker = None


def test_download_worker_emits_bat(monkeypatch, tmp_path):
    """UpdateDownloadThread: mock 掉下载/校验/bat 生成, 验证成功路径发 (bat, "")"""
    import gui.main_window as mw
    from core import updater

    monkeypatch.setattr(updater, "apply_download_prefix",
                        lambda u, p: u or "https://e/AutoTest.exe")
    monkeypatch.setattr(updater, "download", lambda url, dest, progress_cb=None: None)
    monkeypatch.setattr(updater, "verify_sha256", lambda f, s: True)
    monkeypatch.setattr(updater, "validate_new_exe", lambda p: p)
    monkeypatch.setattr(updater, "generate_update_bat",
                        lambda app, pid: str(tmp_path / "_update.bat"))
    # run() 里是 from core.driver import DATA_DIR(调用时才取), 钉住它
    monkeypatch.setattr("core.driver.DATA_DIR", str(tmp_path), raising=False)

    th = mw.UpdateDownloadThread(
        {"update": {}},
        updater.VersionInfo(version="2.0",
                            download_url="https://e/AutoTest_v2.0.exe"),
        None)
    results = []
    th.done.connect(lambda bat, err: results.append((bat, err)))
    th.run()      # 直接调 run(QThread.start 会真开线程, 时序不好控制)
    assert results == [(str(tmp_path / "_update.bat"), "")], results


def test_download_worker_reports_sha_mismatch(monkeypatch, tmp_path):
    """校验失败必须报错并删掉坏包, 不能让坏包流入替换流程"""
    import gui.main_window as mw
    from core import updater

    removed = []
    monkeypatch.setattr(updater, "apply_download_prefix",
                        lambda u, p: u or "https://e/pkg.zip")

    def fake_download(url, dest, progress_cb=None):
        open(dest, "wb").close()

    monkeypatch.setattr(updater, "download", fake_download)
    monkeypatch.setattr(updater, "verify_sha256", lambda f, s: False)

    real_remove = os.remove

    def spy_remove(p):
        removed.append(p)
        real_remove(p)

    monkeypatch.setattr(mw.os, "remove", spy_remove)
    monkeypatch.setattr("core.driver.DATA_DIR", str(tmp_path), raising=False)

    th = mw.UpdateDownloadThread(
        {"update": {}},
        updater.VersionInfo(version="2.0", download_url="https://e/pkg.zip"),
        None)
    results = []
    th.done.connect(lambda bat, err: results.append((bat, err)))
    th.run()
    assert results and results[0][0] == "" and "校验失败" in results[0][1]
    assert removed, "坏包没被删除"

