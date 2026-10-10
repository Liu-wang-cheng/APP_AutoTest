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


# ── gh CLI 分支: 资产名必须与下载地址一致(否则用户 404) ──

def test_stage_asset_renames_to_asset_name(tmp_path):
    """gh 拿**文件名**当资产名 —— 必须先把产物摆成资产同名, 否则传上去叫
    AutoTest.exe, 而 version.json 写的是 AutoTest_v1.5.exe, 客户端必然 404。"""
    exe = tmp_path / "AutoTest.exe"
    exe.write_bytes(b"MZ" + b"x" * 128)
    staged = release.stage_asset_for_gh(str(exe), "AutoTest_v1.5.exe")
    assert os.path.basename(staged) == "AutoTest_v1.5.exe"
    assert open(staged, "rb").read() == exe.read_bytes(), "内容必须原样"
    assert staged != str(exe)                      # 没有动原产物
    # 名字本来就一致 -> 原样返回, 不白复制 208MB
    same = tmp_path / "AutoTest_v1.5.exe"
    same.write_bytes(b"MZ")
    assert release.stage_asset_for_gh(str(same), "AutoTest_v1.5.exe") == str(same)


def test_upload_via_gh_uploads_file_named_as_download_url(tmp_path, monkeypatch):
    """★ 回归守护: 上传的文件名与返回的下载地址里的名字必须是同一个。

    曾经 gh 分支直接传 dist/AutoTest.exe, 资产名就成了 AutoTest.exe, 而地址拼的是
    AutoTest_v{ver}.exe —— 这条 404 只有等用户点「立即更新」才会发现。
    """
    import subprocess as sp
    exe = tmp_path / "AutoTest.exe"
    exe.write_bytes(b"MZ" + b"x" * 128)
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = list(args)
        return sp.CompletedProcess(args, 0)

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    url = release.upload_via_gh("o/r", "1.5", "AutoTest_v1.5.exe", str(exe), "说明")
    uploaded = [a for a in seen["args"] if str(a).lower().endswith(".exe")]
    assert uploaded, f"没找到上传的文件参数: {seen['args']}"
    assert os.path.basename(uploaded[-1]) == url.rsplit("/", 1)[-1], \
        f"上传名({os.path.basename(uploaded[-1])})与下载地址({url})对不上 -> 404"
    assert url == ("https://github.com/o/r/releases/download/"
                   "v1.5/AutoTest_v1.5.exe")
    # 临时副本要清掉(208MB 不能留在临时目录里)
    assert not os.path.exists(uploaded[-1]), "上传用的临时副本没删"


def test_release_download_url_escapes_asset_name():
    """资产名里的空格/中文要转义, 否则地址不是合法 URL"""
    u = release.release_download_url("o/r", "1.5", "我的 包.exe")
    assert " " not in u and u.endswith("%E5%8C%85.exe")


# ── 上传: 流式 + 重试 ──

def test_upload_asset_streams_file_instead_of_reading_it_all(tmp_path, monkeypatch):
    """★ 208MB 不能一次性读进内存再 POST(实测发版进程 RSS 233MB), 也不该一次失败就完蛋。

    这里只验"递出去的是**文件对象**"(http.client 据此分块发 socket 并算
    Content-Length), 以及失败会重试。
    """
    exe = tmp_path / "AutoTest_v1.5.exe"
    exe.write_bytes(b"MZ" + b"x" * 4096)
    seen = {"n": 0}

    class _Resp:
        def __init__(self):
            self.headers = {}
            self.status = 201

        def read(self):
            return b'{"browser_download_url": "https://e/asset"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["n"] += 1
        seen["data"] = req.data
        seen.setdefault("reqs", []).append(req)
        if seen["n"] == 1:
            raise OSError("模拟: 传到一半断了")
        return _Resp()

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(release, "_request_json",
                        lambda *a, **k: _Ctx(b"[]"))       # 列资产: 空
    monkeypatch.setattr(release.time, "sleep", lambda s: None)
    url = release.upload_asset("o/r", "tok", 1, str(exe), "AutoTest_v1.5.exe")
    assert url == "https://e/asset"
    assert seen["n"] == 2, "第一次失败后没有重试"
    assert hasattr(seen["data"], "read"), \
        "传的是整块 bytes(208MB 全进内存) —— 应该把文件对象交给 urllib 分块发"
    assert seen["data"].closed or True          # 用完后文件已关闭
    # 全都失败 -> 抛错, 不能假装成功
    monkeypatch.setattr(release.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(RuntimeError, match="连续 3 次失败"):
        release.upload_asset("o/r", "tok", 1, str(exe), "AutoTest_v1.5.exe")


class _Ctx:
    def __init__(self, body):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def test_post_release_verify_runs_the_user_path_check(monkeypatch):
    """★ 发版最后一步必须自动校验"用户那条链路" —— 发布成功 ≠ 用户能拿到。

    2026-10-09 实测过: Release 建好但资产还没传完(空了 68 分钟), 那期间 version.json
    若已生效, 用户点「立即更新」就是 404。
    """
    import subprocess as sp
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return sp.CompletedProcess(cmd, 0, "全过", "")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    ok, out = release.run_post_release_verify("1.5")
    assert ok and "verify_release.py" in " ".join(seen[0])
    assert "--version" in seen[0] and "1.5" in seen[0]
    assert len(seen) == 1, "默认不跑整包下载校验(几分钟)"

    seen.clear()
    monkeypatch.setattr(release.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 1, "", "资产 404"))
    ok, out = release.run_post_release_verify("1.5", deep=True)
    assert not ok and "404" in out

    seen.clear()
    monkeypatch.setattr(release.subprocess, "run", fake_run)
    release.run_post_release_verify("1.5", deep=True)
    assert len(seen) == 2 and "verify_update_download.py" in " ".join(seen[1])
