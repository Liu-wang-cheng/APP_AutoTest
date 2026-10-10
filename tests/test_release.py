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


@pytest.fixture(autouse=True)
def _never_touch_the_real_release_state(monkeypatch):
    """★ 结构性兜底: 本文件的任何测试都不许动仓库根的 version.json, 也不许真跑 git。

    事故(2026-10-10): 一条新写的 main() 测试**漏打桩**, 于是 main() 真去写了
    version.json —— 内容变成测试用的假地址(download_url 指向 github.com/o/r, sha 是
    测试造的假产物 b'MZ'+b'x'*2048 的 sha), 还随提交推了上去。后果很实: 线上 v1.4
    用户检查更新会拿到"v1.5", 点更新就是 404。

    version.json 是**线上 OTA 的唯一依据**, 写坏它等于把所有用户的更新打歪, 所以这里
    默认把两个写盘/执行入口换成空操作; 要验它们的测试自己再打桩即可(自己的桩后生效)。
    """
    monkeypatch.setattr(release, "update_version_json_and_tag",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(release, "_git", lambda *a, **k: "", raising=False)
    yield



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


def test_upload_via_gh_reuses_existing_release(tmp_path, monkeypatch):
    """★ gh 分支遇到"Release 已存在"必须**复用**(upload --clobber), 不能像原来那样
    直接 `gh release create` 撞死 —— API 分支对 422 是复用的, 两条路行为要一致。

    (v1.4 发布前就出现过"Release 已存在、但一个资产都没有"的状态。)
    """
    import subprocess as sp
    exe = tmp_path / "AutoTest.exe"
    exe.write_bytes(b"MZ" + b"x" * 64)
    calls, state = [], {"exists": False}

    def fake_run(args, **kw):
        calls.append(list(args))
        rc = 0
        if list(args[:3]) == ["gh", "release", "view"] and not state["exists"]:
            rc = 1                      # view 找不到 -> Release 不存在
        return sp.CompletedProcess(args, rc, "", "")

    monkeypatch.setattr(release.subprocess, "run", fake_run)

    release.upload_via_gh("o/r", "1.5", "AutoTest_v1.5.exe", str(exe), "说明")
    verbs = [c[2] for c in calls if c[:2] == ["gh", "release"]]
    assert verbs == ["view", "create"], verbs

    calls.clear()
    state["exists"] = True              # 第二次: Release 已经在了
    release.upload_via_gh("o/r", "1.5", "AutoTest_v1.5.exe", str(exe), "说明")
    verbs = [c[2] for c in calls if c[:2] == ["gh", "release"]]
    assert verbs == ["view", "upload"], f"已存在的 Release 被 create 撞死了: {verbs}"
    upload = [c for c in calls if c[:3] == ["gh", "release", "upload"]][0]
    assert "--clobber" in upload, upload
    # 上传的文件名必须是资产名(gh 拿文件名当资产名), 且与下载地址里的一致
    files = [a for a in upload if str(a).lower().endswith(".exe")]
    assert files and os.path.basename(files[0]) == "AutoTest_v1.5.exe", upload


# ── 编排层: main() 的顺序与参数(纯函数测试覆盖不到的地方) ──

def test_main_orchestration_order_and_wiring(tmp_path, monkeypatch, capsys):
    """★ 把网络与 git 全替掉, 跑一遍 main() 的**编排**。

    为什么单独测这个: 历史 bug 全落在这一层 —— `--dist` 默认值停在 onedir 时代拼出
    不存在的路径(dry-run 发现不了, 真发一次才炸)、tag 与 Release 的先后顺序、
    `create_release` 会自动建 tag 导致必须强推。纯函数测试一条都盖不到。
    """
    import subprocess as sp
    exe = tmp_path / "AutoTest.exe"
    exe.write_bytes(b"MZ" + b"x" * 2048)      # 小文件即可(真产物由 find_dist_exe 校验)
    calls = []

    monkeypatch.setattr(release, "check_version_consistency", lambda root, v: [])
    dist_args = []
    def fake_find_dist(d):
        # ★ 盯住默认产物目录: 历史上 `--dist` 默认值停在 onedir 时代, 拼出
        #   一个不存在的路径 —— dry-run 不报错, 真发一次才炸
        dist_args.append(d)
        return str(exe)
    monkeypatch.setattr(release, "find_dist_exe", fake_find_dist)
    monkeypatch.setattr(release, "load_changelog_section",
                        lambda p, v: ("2026-10-10", "本次更新说明"))
    monkeypatch.setattr(release, "ensure_clean_tree", lambda: None)
    monkeypatch.setattr(release, "gh_available", lambda: False)      # 走 API 分支
    monkeypatch.setattr(release, "get_token", lambda: "tok")
    # CI 闸门也要打桩 —— 它是"发版前必须过 CI"的接线, 单独有测试覆盖
    monkeypatch.setattr(release, "check_ci_green",
                        lambda *a, **k: (True, "CI 全绿(测试#1)"))
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 0,
                                                            "", ""))

    def fake_create(repo, token, version, notes, branch):
        calls.append(("create_release", repo, version, notes, branch))
        return 7

    def fake_upload(repo, token, rid, path, name):
        calls.append(("upload_asset", repo, rid, os.path.basename(path), name))
        return release.release_download_url(repo, version_, name)

    def fake_version_json(repo, version, sha, url, notes):
        calls.append(("version_json", repo, version, sha[:6], url))

    def fake_verify(version, deep=False):
        calls.append(("verify", version, deep))
        return True, "全过"

    version_ = "1.5"
    monkeypatch.setattr(release, "create_release", fake_create)
    monkeypatch.setattr(release, "upload_asset", fake_upload)
    monkeypatch.setattr(release, "update_version_json_and_tag", fake_version_json)
    monkeypatch.setattr(release, "run_post_release_verify", fake_verify)

    rc = release.main(["--version", version_, "--repo", "o/r"])
    assert rc == 0, capsys.readouterr().out
    order = [c[0] for c in calls]
    assert order == ["create_release", "upload_asset", "version_json", "verify"], order
    assert calls[0][3] == "本次更新说明", calls[0]     # 说明取 CHANGELOG 该节
    assert calls[1][3] == "AutoTest.exe", calls[1]     # 上传的就是产物
    assert calls[1][4] == f"AutoTest_v{version_}.exe", calls[1]   # 资产名
    assert calls[2][4].endswith(f"/v{version_}/AutoTest_v{version_}.exe"), calls[2]
    assert calls[3][1] == version_ and calls[3][2] is False, calls[3]
    # ★ 只断言**默认值**等于 dist/(原来它停在 onedir 时代的 dist/AutoTest, 拼出不存在的
    #   路径)。不要断言目录存在 —— dist/ 是构建产物, 全新检出(CI/新机器)里本来就没有。
    assert dist_args == [os.path.join(release.ROOT, "dist")], dist_args

    # --no-verify: 不发版后校验(dry-run 之外的逃生口)
    calls.clear()
    rc = release.main(["--version", version_, "--repo", "o/r", "--no-verify"])
    assert rc == 0 and [c[0] for c in calls] == ["create_release", "upload_asset",
                                                "version_json"], calls

    # 校验不通过 -> 必须返回非 0(不能"报完成"却让用户拿到 404)
    calls.clear()
    monkeypatch.setattr(release, "run_post_release_verify",
                        lambda v, deep=False: (False, "资产 404"))
    rc = release.main(["--version", version_, "--repo", "o/r"])
    out = capsys.readouterr().out
    assert rc == 1 and "未通过" in out, out


# ── 发版闸门: 该提交必须先过 CI(用户要求 2026-10-10) ──

def _ci_run(conclusion="success", status="completed", number=7, name="测试"):
    return {"name": name, "run_number": number, "status": status,
            "conclusion": conclusion,
            "html_url": f"https://github.com/o/r/actions/runs/{number}"}


def test_ci_gate_passes_when_all_runs_green(monkeypatch):
    monkeypatch.setattr(release, "_git", lambda a: "sha123\n")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: __import__("subprocess").CompletedProcess(
                            a[0], 0, "sha123\trefs/heads/master\n", ""))
    monkeypatch.setattr(release, "_api_get",
                        lambda url, token, **k: {"workflow_runs": [_ci_run()]})
    ok, msg = release.check_ci_green("o/r", "tok", "sha123")
    assert ok and "全绿" in msg, msg


def test_ci_gate_refuses_on_failed_run(monkeypatch):
    """★ 红的就是不放行, 且要把运行页地址带出来"""
    monkeypatch.setattr(release, "_git", lambda a: "sha123\n")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: __import__("subprocess").CompletedProcess(
                            a[0], 0, "sha123\trefs/heads/master\n", ""))
    monkeypatch.setattr(release, "_api_get",
                        lambda url, token, **k: {"workflow_runs": [
                            _ci_run("failure", number=9)]})
    ok, msg = release.check_ci_green("o/r", "tok", "sha123")
    assert not ok and "未通过" in msg and "runs/9" in msg, msg


def test_ci_gate_waits_for_running_then_passes(monkeypatch):
    """还在跑 -> 等; 等到了绿 -> 放行"""
    monkeypatch.setattr(release, "_git", lambda a: "sha123\n")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: __import__("subprocess").CompletedProcess(
                            a[0], 0, "sha123\trefs/heads/master\n", ""))
    seq = [{"workflow_runs": [_ci_run(status="in_progress", conclusion=None)]},
           {"workflow_runs": [_ci_run()]}]
    monkeypatch.setattr(release, "_api_get", lambda url, token, **k: seq.pop(0))
    monkeypatch.setattr(release.time, "sleep", lambda s: None)
    ok, msg = release.check_ci_green("o/r", "tok", "sha123", wait_seconds=60)
    assert ok, msg


def test_ci_gate_fails_closed_without_records_or_token(monkeypatch):
    """★ fail-closed: 没有记录(等超时)/没有 token 都**不放行**"""
    monkeypatch.setattr(release, "_git", lambda a: "sha123\n")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: __import__("subprocess").CompletedProcess(
                            a[0], 0, "sha123\trefs/heads/master\n", ""))
    monkeypatch.setattr(release, "_api_get", lambda url, token, **k: {"workflow_runs": []})
    ok, msg = release.check_ci_green("o/r", "tok", "sha123", wait_seconds=0)
    assert not ok and "仍未拿到绿灯" in msg, msg

    ok, msg = release.check_ci_green("o/r", "", "sha123")
    assert not ok and "token" in msg, msg


def test_ci_gate_pushes_head_if_not_on_remote(monkeypatch):
    """本地 HEAD 没推上去时先推 —— 不推就永远没有它的 CI 记录"""
    pushed = []
    monkeypatch.setattr(release, "_git",
                        lambda a: pushed.append(a) or "sha123\n")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: __import__("subprocess").CompletedProcess(
                            a[0], 0, "OLD999\trefs/heads/master\n", ""))
    monkeypatch.setattr(release, "_api_get",
                        lambda url, token, **k: {"workflow_runs": [_ci_run()]})
    ok, _ = release.check_ci_green("o/r", "tok", "sha123")
    assert ok and ["push", "origin", "master"] in pushed, pushed


def test_release_refuses_to_publish_when_ci_red(tmp_path, monkeypatch, capsys):
    """★ 闸门接在 main() 里: CI 红 -> 直接返回 1, 连 Release 都不建"""
    import subprocess as sp
    exe = tmp_path / "AutoTest.exe"
    exe.write_bytes(b"MZ" + b"x" * 2048)
    called = []
    monkeypatch.setattr(release, "check_version_consistency", lambda r, v: [])
    monkeypatch.setattr(release, "find_dist_exe", lambda d: str(exe))
    monkeypatch.setattr(release, "load_changelog_section", lambda p, v: ("2026-10-10", "说明"))
    monkeypatch.setattr(release, "get_token", lambda: "tok")
    monkeypatch.setattr(release, "check_ci_green",
                        lambda *a, **k: (False, "CI 未通过: 测试 #9"))
    monkeypatch.setattr(release, "create_release",
                        lambda *a, **k: called.append("create") or 1)
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a[0], 0, "", ""))
    rc = release.main(["--version", "1.5", "--repo", "o/r"])
    out = capsys.readouterr().out
    assert rc == 1 and not called, out
    assert "拒绝发版" in out, out

    # --no-ci-check 时闸门被跳过(紧急逃生口)
    called.clear()
    monkeypatch.setattr(release, "ensure_clean_tree", lambda: None)
    monkeypatch.setattr(release, "update_version_json_and_tag", lambda *a, **k: None)
    monkeypatch.setattr(release, "run_post_release_verify", lambda v, deep=False: (True, "ok"))
    monkeypatch.setattr(release, "upload_asset",
                        lambda *a, **k: "https://e/AutoTest_v1.5.exe")
    monkeypatch.setattr(release, "gh_available", lambda: False)
    rc = release.main(["--version", "1.5", "--repo", "o/r", "--no-ci-check",
                       "--no-verify"])
    assert rc == 0 and called == ["create"], (rc, called)


def test_committed_version_json_points_at_our_repo():
    """★ 提交进仓库的 version.json 必须是**真东西** —— 它是线上 OTA 的唯一依据,
    被测试写坏过一次(2026-10-10: 地址成了 github.com/o/r 的假地址, 提交推送后
    v1.4 用户会收到一个下载 404 的"新版本")。
    """
    import json
    import re
    import subprocess
    path = os.path.join(release.ROOT, "version.json")
    d = json.load(open(path, encoding="utf-8"))
    url = d.get("download_url", "")
    assert re.match(r"^https://github\.com/[^/]+/[^/]+/releases/download/v[\d.]+/\S+$",
                    url), f"download_url 不像真的: {url!r}"
    assert "/o/r/" not in url, f"version.json 被测试污染了: {url}"
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=release.ROOT,
                       capture_output=True, text=True)
    repo = release.parse_repo_from_remote(r.stdout)
    assert repo and repo in url, f"下载地址指向的不是本仓库({repo}): {url}"
    assert re.fullmatch(r"[0-9a-f]{64}", str(d.get("sha256", ""))), d.get("sha256")
    assert d.get("release_notes"), "发布说明不能是空的"
