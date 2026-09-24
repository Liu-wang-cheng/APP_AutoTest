# -*- coding: utf-8 -*-
"""发布脚本(tools/release.py)纯逻辑部分的守护。

发布是**对外动作**, 发错了收不回 —— 这里钉的都是"发错版本"的闸门:
版本三处不一致、包里混进用户数据、remote 解析错仓库。
"""
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import release  # noqa: E402


# ── remote 解析: 发错仓库 = 版本发到别人家去 ──

@pytest.mark.parametrize("url,want", [
    ("git@github.com:Liu-wang-cheng/APP_AutoTest.git", "Liu-wang-cheng/APP_AutoTest"),
    ("https://github.com/Liu-wang-cheng/APP_AutoTest.git", "Liu-wang-cheng/APP_AutoTest"),
    ("https://github.com/Liu-wang-cheng/APP_AutoTest", "Liu-wang-cheng/APP_AutoTest"),
    ("git@github.com:a/b", "a/b"),
    ("", ""),
    ("not-a-remote", ""),
])
def test_parse_repo_from_remote(url, want):
    assert release.parse_repo_from_remote(url) == want


# ── CHANGELOG 取节: 更新说明的来源 ──

def test_load_changelog_section(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(
        "# 履历\n\n## [1.0] - 2026-09-21\n\n首个版本。\n\n- 内容A\n\n"
        "## [1.1] - 2026-10-01\n\n第二个版本。\n\n- 内容B\n- 内容C\n",
        encoding="utf-8")
    date, notes = release.load_changelog_section(str(p), "1.1")
    assert date == "2026-10-01"
    assert "内容B" in notes and "内容C" in notes and "内容A" not in notes
    # 没有该节 -> 空串(发版闸门会拦下)
    assert release.load_changelog_section(str(p), "9.9") == ("", "")


# ── 版本一致性: 三处只改一处就发版 = 更新判断永远错 ──

def _make_root(tmp_path, version="1.1", changelog=True):
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    if changelog:
        (tmp_path / "CHANGELOG.md").write_text(
            f"## [{version}] - 2026-10-01\n\n某版本\n", encoding="utf-8")


def test_version_consistency_passes(tmp_path, monkeypatch):
    _make_root(tmp_path)
    monkeypatch.setattr(release, "CODE_VERSION", "1.1")
    assert release.check_version_consistency(str(tmp_path), "1.1") == []


def test_version_consistency_catches_drift(tmp_path, monkeypatch):
    """VERSION / core.version / CHANGELOG 三处任何一处不一致都必须拦下"""
    _make_root(tmp_path, version="1.1")
    monkeypatch.setattr(release, "CODE_VERSION", "1.2")     # 代码版本漂了
    errs = release.check_version_consistency(str(tmp_path), "1.1")
    assert any("__version__" in e for e in errs)

    monkeypatch.setattr(release, "CODE_VERSION", "1.1")
    (tmp_path / "VERSION").write_text("1.0\n", encoding="utf-8")  # VERSION 没跟着改
    errs = release.check_version_consistency(str(tmp_path), "1.1")
    assert any("VERSION" in e for e in errs)

    (tmp_path / "CHANGELOG.md").unlink()                    # 没写更新说明
    errs = release.check_version_consistency(str(tmp_path), "1.1")
    assert any("CHANGELOG" in e for e in errs)


# ── 打包: 用户数据绝不能进更新包 ──

def _make_dist(root):
    root.mkdir(parents=True, exist_ok=True)      # write_bytes 不会自建父目录
    (root / "AutoTest.exe").write_bytes(b"MZ")
    (root / "_internal" / "core").mkdir(parents=True)
    (root / "_internal" / "core" / "driver.py").write_bytes(b"#")
    (root / "_internal" / "PySide6").mkdir()
    (root / "_internal" / "PySide6" / "Qt6Core.dll").write_bytes(b"dll")
    # ↓ 验证打包时启动过 exe 会生成的运行时数据
    (root / "config").mkdir()
    (root / "config" / "config.yaml").write_text("target_device: 用户的\n", encoding="utf-8")
    (root / "backups").mkdir()
    (root / "backups" / "20260924-000000.zip").write_bytes(b"b")
    (root / "reports").mkdir()
    (root / "reports" / "r.xlsx").write_bytes(b"x")


def test_package_zip_contains_program_only(tmp_path):
    """★ 更新包只能有 exe + _internal —— config/backups 混进去,
    下次更新就会覆盖用户机器上的同名用户数据。"""
    dist = tmp_path / "AutoTest"
    _make_dist(dist)
    zp = tmp_path / "AutoTest_v1.1.zip"
    release.package_zip(str(dist), str(zp))
    with zipfile.ZipFile(zp) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    assert "AutoTest.exe" in names
    assert any(n.startswith("_internal/") for n in names)
    for bad in ("config/", "backups/", "reports/"):
        assert not any(n.startswith(bad) for n in names), \
            f"用户数据 {bad} 混进了更新包: {names}"


def test_package_zip_rejects_incomplete_dist(tmp_path):
    """没有 exe 或 _internal 的产物目录 = 打包没完成, 必须拒绝而不是发个坏包"""
    d = tmp_path / "bad"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        release.package_zip(str(d), str(tmp_path / "x.zip"))


# ── 更新器的分支支持: 本仓库默认分支是 master, 不是 main ──

def test_build_mirrors_substitutes_branch():
    """★ 镜像 URL 的 {branch} 必须按配置替换 —— 照搬参考项目的 /main 会让
    版本清单 404(OTA 永远检查失败), 本仓库默认分支是 master。"""
    from core.updater import build_mirrors, default_config
    conf = default_config()
    conf["repository"] = "Liu-wang-cheng/APP_AutoTest"
    got = {m["name"]: m["base_url"] for m in build_mirrors(conf, conf["repository"])}
    assert "master" in got["github"], got
    assert "/main" not in got["github"] and "@main" not in got["jsdelivr"]
    # 分支可配
    conf["branch"] = "release"
    got2 = build_mirrors(conf, conf["repository"])
    assert any("release" in m["base_url"] for m in got2)


def test_build_mirrors_empty_repo_gives_no_urls():
    from core.updater import build_mirrors, default_config
    assert build_mirrors(default_config(), "") == []
