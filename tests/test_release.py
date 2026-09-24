# -*- coding: utf-8 -*-
"""发布脚本(tools/release.py)纯逻辑部分的守护。

发布是**对外动作**, 发错了收不回 —— 这里钉的都是"发错版本"的闸门:
版本三处不一致、包里混进用户数据、remote 解析错仓库。
"""
import os
import sys
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


# ── 产物校验: onefile 的发布物就是一个 exe ──

def _make_dist(root):
    """造一份合法的打包产物(dist/AutoTest.exe)"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "AutoTest.exe").write_bytes(b"MZ" + b"x" * (2 << 20))


def test_find_dist_exe_ok(tmp_path):
    _make_dist(tmp_path)
    p = release.find_dist_exe(str(tmp_path))
    assert os.path.isfile(p) and p.endswith("AutoTest.exe")


def test_find_dist_exe_rejects_bad_artifacts(tmp_path):
    """缺 exe / 太小 / 不是可执行文件, 都必须在发布前拦下(不能发个坏包)"""
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        release.find_dist_exe(str(d))            # 没打包就发布

    d2 = tmp_path / "small"
    d2.mkdir()
    (d2 / "AutoTest.exe").write_bytes(b"MZ")
    with pytest.raises(ValueError, match="字节"):
        release.find_dist_exe(str(d2))           # 构建中断的半成品

    d3 = tmp_path / "notexe"
    d3.mkdir()
    (d3 / "AutoTest.exe").write_bytes(b"<html>error</html>" + b"x" * (2 << 20))
    with pytest.raises(ValueError, match="可执行"):
        release.find_dist_exe(str(d3))           # 未知内容


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
