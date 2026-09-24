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
