# -*- coding: utf-8 -*-
"""OTA 更新核心逻辑的守护(全程 mock, 不发真实网络请求)。

这一块的失效方式都很隐蔽: 读到了**缓存里的旧版本清单** -> 误报"已是最新";
解析远程数据抛异常 -> 整个检查更新瘫痪; 校验被绕过 -> 执行了一个坏包。
所以逐条钉住。
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import updater  # noqa: E402


class _Resp:
    """urllib 响应的替身(支持 with 与分块 read)"""

    def __init__(self, body=b"", status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self, n=None):
        if n is None:
            body, self._body = self._body, b""
            return body
        body, self._body = self._body[:n], self._body[n:]
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── 镜像直连判定: 那个"误判直连读到旧版本"的坑 ──

def test_is_direct_github_rejects_mirror_that_embeds_raw_url():
    """★ ghfast 的地址里内嵌了 raw.githubusercontent.com, **不能**判成直连。

    回归守护: 曾用子串匹配 `"raw.githubusercontent" in base_url` —— ghfast 会被误判
    成直连, 而它的 CDN 有缓存、延迟可能更低, 于是被优先取到, 读到**旧的
    version.json**, 结果误报"已是最新", 新版本永远推不下去。
    """
    assert updater.is_direct_github(
        "https://raw.githubusercontent.com/o/r/main") is True
    assert updater.is_direct_github(
        "https://ghfast.top/https://raw.githubusercontent.com/o/r/main") is False
    assert updater.is_direct_github("https://cdn.jsdelivr.net/gh/o/r@main") is False
    assert updater.is_direct_github("") is False
    assert updater.is_direct_github(None) is False


def test_fetch_prefers_direct_even_if_a_mirror_is_faster():
    """★ 取版本清单必须**直连优先** —— 镜像的 CDN 缓存会给出旧版本号"""
    direct = updater.MirrorResult("github", "https://raw.githubusercontent.com/o/r/main",
                                  "", latency_ms=900.0, success=True)
    fast_mirror = updater.MirrorResult("ghfast",
                                       "https://ghfast.top/https://raw.githubusercontent.com/o/r/main",
                                       "https://ghfast.top/", latency_ms=10.0, success=True)
    # 故意把快的镜像排在前面(模拟测速结果)
    ranked = [fast_mirror, direct]
    asked = []

    def fake_request(url, method="GET", timeout=0, extra_headers=None):
        asked.append(url)
        if "ghfast.top" in url:
            # 镜像返回的是**旧**版本(CDN 缓存)
            return _Resp(json.dumps({"version": "1.0"}).encode())
        return _Resp(json.dumps({"version": "9.9"}).encode())

    import core.updater as up
    orig = up._request
    up._request = fake_request
    try:
        info, mirror = updater.fetch_version_info(ranked, "version.json")
    finally:
        up._request = orig

    assert info.version == "9.9", "取到了镜像的缓存版本 —— 直连优先失效了"
    assert mirror.name == "github"
    assert "ghfast" not in asked[0], f"第一个请求不该打给镜像: {asked}"


# ── 配置合并 ──

def test_load_update_config_defaults_and_override():
    d = updater.load_update_config({})
    assert d["repository"] == ""            # 没配仓库 -> 不检查(别瞎外联)
    assert d["enabled"] is True
    assert d["check_interval_hours"] == updater.CHECK_INTERVAL_HOURS
    assert len(d["mirrors"]) >= 2

    c = updater.load_update_config({
        "update": {"enabled": False, "repository": "o/r",
                   "mirrors": [{"name": "x", "base_url": "https://x/{repo}"}]}})
    assert c["enabled"] is False and c["repository"] == "o/r"
    assert [m["name"] for m in c["mirrors"]] == ["x"]


def test_load_update_config_ignores_broken_mirrors():
    """镜像列表写成垃圾时回退到内置那份, 而不是留个空列表让检查直接失败"""
    c = updater.load_update_config({"update": {"mirrors": "不是列表"}})
    assert len(c["mirrors"]) >= 2
    c2 = updater.load_update_config({"update": {"mirrors": [{}, {"name": "无url"}]}})
    assert len(c2["mirrors"]) >= 2


# ── 远程数据是外部输入: 畸形也不能抛 ──

@pytest.mark.parametrize("data", [
    None, "字符串", [], 123,
    {},                                    # 字段全缺
    {"version": None, "sha256": None},
    {"version": 1.5},                      # 非字符串
])
def test_parse_version_info_tolerates_garbage(data):
    info = updater.parse_version_info(data)
    assert isinstance(info.version, str)
    assert updater.parse_version_info({"version": "1.1"}).version == "1.1"


# ── 测速排序 ──

def test_race_mirrors_orders_by_availability_then_config_order(monkeypatch):
    """可用优先; 组内按**配置顺序**(不是延迟)。

    ★ 为什么按配置顺序: 延迟只反映小文件请求, 不代表大文件下载速度 —— 实测
      ghfast 延迟更低但下载更慢(368 vs 558 KB/s)。下载选源靠这个顺序表达优先级,
      所以不能按延迟重排(否则会选中下载更慢的那个)。
    """
    def fake(mirror, version_file, timeout=0):
        return updater.MirrorResult(mirror["name"], mirror["base_url"], "",
                                    # 故意让"慢的"排在前面: 若按延迟排就会反过来
                                    900.0 if mirror["name"] == "first" else 20.0,
                                    True)

    monkeypatch.setattr(updater, "_test_one_mirror", fake)
    mirrors = [{"name": "first", "base_url": "https://1"},
               {"name": "second", "base_url": "https://2"}]
    got = [r.name for r in updater.race_mirrors(mirrors, "version.json")]
    assert got == ["first", "second"], f"应保持配置顺序(不按延迟重排): {got}"

    # 不可用的排到后面
    def fake2(mirror, version_file, timeout=0):
        ok = mirror["name"] != "dead"
        return updater.MirrorResult(mirror["name"], mirror["base_url"], "", 10.0, ok)
    monkeypatch.setattr(updater, "_test_one_mirror", fake2)
    mirrors2 = [{"name": "dead", "base_url": "https://d"},
                {"name": "live", "base_url": "https://l"}]
    assert [r.name for r in updater.race_mirrors(mirrors2, "v.json")] == ["live", "dead"]


def test_pick_download_mirror_prefers_config_order(monkeypatch):
    """★ 下载选源: 取第一个"可达且带加速前缀"的, 按配置顺序 —— 与取版本清单分开。

    清单要最新(优先 GitHub 直连, 避开 CDN 缓存), 下载要快且下得动(直连拉大文件
    实测 3MB 只得到 0 字节)。两者混用会让下载走直连、永远下不完。
    """
    ranked = [
        updater.MirrorResult("github", "https://gh/", "", 100.0, True),      # 无前缀
        updater.MirrorResult("ghfast", "https://gf/", "https://gf/", 50.0, True),
        updater.MirrorResult("ghproxy", "https://gp/", "https://gp/", 900.0, True),
    ]
    # ghfast 延迟更低, 但 gh-proxy 配在它前面 -> 应选 gh-proxy(顺序即优先级)
    assert updater.pick_download_mirror(ranked).name == "ghfast"
    reordered = [ranked[0], ranked[2], ranked[1]]
    assert updater.pick_download_mirror(reordered).name == "ghproxy"

    # 全都没前缀 -> 退回第一个可用的; 全失败 -> None
    noprefix = [updater.MirrorResult("github", "https://gh/", "", 100.0, True)]
    assert updater.pick_download_mirror(noprefix).name == "github"
    assert updater.pick_download_mirror(
        [updater.MirrorResult("x", "https://x/", "", -1.0, False)]) is None
    assert updater.pick_download_mirror([]) is None
    assert updater.pick_download_mirror(None) is None


def test_default_mirrors_download_order_is_by_measured_speed():
    """默认镜像顺序要按**实测下载速度**排: gh-proxy(558 KB/s) 在 ghfast(368 KB/s) 前

    顺序即下载优先级, 排错了就会选中较慢的那个(延迟低≠下载快)。
    """
    names = [m["name"] for m in updater.default_config()["mirrors"]]
    assert "gh-proxy" in names and "ghfast" in names
    assert names.index("gh-proxy") < names.index("ghfast"), names
    # 直连排在最前(取清单优先用它), 但它没有下载前缀
    assert names[0] == "github"
    assert updater.default_config()["mirrors"][0]["download_prefix"] == ""


def test_race_mirrors_empty_is_safe():
    assert updater.race_mirrors([], "version.json") == []
    assert updater.race_mirrors(None, "version.json") == []


# ── 下载与校验 ──

def test_download_streams_and_reports_progress(monkeypatch, tmp_path):
    payload = b"x" * 200000
    import core.updater as up
    monkeypatch.setattr(up, "_request",
                        lambda url, **kw: _Resp(payload, 200,
                                                {"content-length": str(len(payload))}))
    seen = []
    dest = tmp_path / "pkg.zip"
    assert updater.download("https://e/pkg", str(dest),
                            progress_cb=lambda d, t, s: seen.append((d, t, s)))
    assert dest.read_bytes() == payload
    assert seen and seen[-1][0] == len(payload)      # 进度到 100%
    assert not (tmp_path / "pkg.zip.part").exists()  # 临时文件已改名


def test_download_cleans_up_on_failure(monkeypatch, tmp_path):
    """下载中断不能留下半截文件冒充完整包"""
    import core.updater as up
    monkeypatch.setattr(up, "_request",
                        lambda url, **kw: _Resp(b"partial", 500))
    dest = tmp_path / "pkg.zip"
    with pytest.raises(Exception):
        updater.download("https://e/pkg", str(dest))
    assert not dest.exists()
    assert not (tmp_path / "pkg.zip.part").exists()


def test_verify_sha256(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    good = hashlib.sha256(b"hello").hexdigest()
    assert updater.verify_sha256(str(p), good) is True
    assert updater.verify_sha256(str(p), good.upper()) is True   # 大小写不敏感
    assert updater.verify_sha256(str(p), "0" * 64) is False
    # 没提供校验值时放行(只警告) —— 不能因为清单没写 sha256 就永远更不了
    assert updater.verify_sha256(str(p), "") is True


# ── 总入口的各分支 ──

def _patch_check(monkeypatch, *, enabled=True, repo="o/r", remote=None,
                 mirrors_ok=True):
    monkeypatch.setattr(updater, "race_mirrors",
                        lambda m, vf, timeout=0: [
                            updater.MirrorResult("github",
                                                 "https://raw.githubusercontent.com/o/r/main",
                                                 "", 20.0, mirrors_ok)])
    if remote is None:
        monkeypatch.setattr(updater, "fetch_version_info", lambda r, vf: None)
    else:
        monkeypatch.setattr(updater, "fetch_version_info",
                            lambda r, vf: (updater.parse_version_info(remote),
                                           r[0]))
    return {"update": {"enabled": enabled, "repository": repo}}


def test_check_skipped_without_repository():
    r = updater.check_for_update({})
    assert r.status == "skipped" and "repository" in r.message


def test_check_skipped_when_disabled(monkeypatch):
    cfg = _patch_check(monkeypatch, enabled=False)
    assert updater.check_for_update(cfg).status == "skipped"


def test_check_up_to_date(monkeypatch):
    cfg = _patch_check(monkeypatch, remote={"version": "1.0"})
    r = updater.check_for_update(cfg, current="1.0")
    assert r.status == "up_to_date"


def test_check_has_update(monkeypatch):
    cfg = _patch_check(monkeypatch, remote={"version": "2.0",
                                            "download_url": "https://e/a.zip",
                                            "sha256": "abc",
                                            "release_notes": "修了 X"})
    r = updater.check_for_update(cfg, current="1.0")
    assert r.status == "has_update"
    assert r.info.version == "2.0" and r.info.release_notes == "修了 X"
    assert r.force is False


def test_check_force_when_below_min_version(monkeypatch):
    """本地版本低于清单里的 min_version -> 强制更新(不允许继续用旧版)"""
    cfg = _patch_check(monkeypatch, remote={"version": "3.0", "min_version": "2.0"})
    r = updater.check_for_update(cfg, current="1.5")
    assert r.status == "has_update" and r.force is True


def test_check_error_when_no_mirror_reachable(monkeypatch):
    cfg = _patch_check(monkeypatch, remote=None)
    assert updater.check_for_update(cfg, current="1.0").status == "error"


def test_check_never_raises(monkeypatch):
    """检查更新出任何岔子都不能抛 —— 它跑在 GUI 启动路径上"""
    def boom(*_a, **_k):
        raise RuntimeError("模拟: DNS 挂了")

    monkeypatch.setattr(updater, "race_mirrors", boom)
    cfg = {"update": {"enabled": True, "repository": "o/r"}}
    r = updater.check_for_update(cfg)
    assert r.status == "error" and "DNS" in r.message


# ── 自替换(目录模式): 下载前缀 / 解包校验 / 生成替换脚本 ──

def test_apply_download_prefix():
    assert updater.apply_download_prefix("https://github.com/x/y.zip",
                                         "https://ghfast.top/") == \
        "https://ghfast.top/https://github.com/x/y.zip"
    assert updater.apply_download_prefix("https://github.com/x/y.zip", "") == \
        "https://github.com/x/y.zip"          # 空前缀 = 直连
    assert updater.apply_download_prefix("https://github.com/x/y.zip", None) == \
        "https://github.com/x/y.zip"
    assert updater.apply_download_prefix("", "https://p/") == ""


# ── 真实路径回归: 只 mock 网络层, 让 race_mirrors/build_url/_test_one_mirror 真跑 ──

def test_race_mirrors_real_path_with_dict_mirrors(monkeypatch):
    """★ 回归: race_mirrors 收到的是 **dict**(还没测速), 而 build_url 曾按
    MirrorResult 对象写 → 每个镜像都在 `mirror.base_url` 抛 AttributeError,
    被 `except Exception: pass` 吞掉 → **所有镜像永久判为不可用**。

    线上实测: 那个 exe 跑了 15 天、每 8 小时准点检查一次, 每次都
    「所有镜像都取不到版本清单」—— 自动更新从来没成功过一次。

    这个用例**不 mock 内部函数**(之前正是因为把 _test_one_mirror 整个替换掉,
    真实代码路径从未被执行, 测试全绿却功能全坏)。这里只替换最底层的 `_request`。
    """
    import core.updater as up
    calls = []

    def fake_request(url, method="GET", timeout=0, extra_headers=None):
        calls.append((method, url))
        return _Resp(b'{"version":"9.9"}', 200)

    monkeypatch.setattr(up, "_request", fake_request)
    mirrors = [{"name": "github",
                "base_url": "https://raw.githubusercontent.com/o/r/master",
                "download_prefix": ""}]
    ranked = up.race_mirrors(mirrors, "version.json")

    assert calls, "根本没发出任何请求"
    assert len(ranked) == 1
    assert ranked[0].success is True, \
        f"镜像被判为不可用(真实路径有类型 bug): success={ranked[0].success}"
    assert ranked[0].latency_ms >= 0
    assert "version.json" in calls[0][1]


def test_check_for_update_end_to_end_with_dict_mirrors(monkeypatch):
    """端到端(只 mock 网络): 从配置 → 镜像展开 → 测速 → 取清单 → 比版本, 全真跑。

    这条守住的是"整体能工作", 而不只是某个函数不抛异常。
    """
    import core.updater as up

    def fake_request(url, method="GET", timeout=0, extra_headers=None):
        body = b'{"version":"9.9","download_url":"https://e/a.exe","sha256":"x"}'
        return _Resp(body, 200)

    monkeypatch.setattr(up, "_request", fake_request)
    cfg = {"update": {"enabled": True, "repository": "o/r", "branch": "master"}}
    r = up.check_for_update(cfg, current="1.0")

    assert r.status == "has_update", f"{r.status}: {r.message}"
    assert r.info.version == "9.9"
    assert r.mirror is not None and r.mirror.base_url.endswith("/master")


def test_build_url_accepts_both_dict_and_mirror_result():
    """两种形态都要能取 URL —— 流程里 dict(build_mirrors 产物)与 MirrorResult
    (race_mirrors 产物) 都会出现, 只认一种就会在另一处炸。"""
    import core.updater as up
    as_dict = {"name": "d", "base_url": "https://a/", "download_prefix": ""}
    as_obj = up.MirrorResult("o", "https://b/", "", 1.0, True)
    assert up.build_url(as_dict, "version.json") == "https://a//version.json"
    assert up.build_url(as_obj, "version.json") == "https://b//version.json"


def test_default_mirrors_have_no_leading_slash_issue():
    """内置镜像 base_url 末尾不能带斜杠(否则拼出 //version.json)"""
    import core.updater as up
    for m in up.default_config()["mirrors"]:
        assert not m["base_url"].endswith("/"), m


# ── 新版本 exe 校验(onefile: 安装包就是一个 exe) ──





# ── 目标程序名的传递(F3) ──




# ── 下载进度节流(F8) ──

def test_download_progress_is_throttled_but_reaches_100(monkeypatch, tmp_path):
    """进度回调不能每 64KB 一次(208MB ≈ 3300 次 -> 3300 行日志), 但**必须有收尾那次**,
    否则界面停在 99%"""
    import core.updater as up
    payload = b"x" * (4 << 20)                 # 4MB / 64KB = 64 个块
    monkeypatch.setattr(up, "_request",
                        lambda url, **kw: _Resp(payload, 200,
                                                {"content-length": str(len(payload))}))
    monkeypatch.setattr(up, "PROGRESS_MIN_INTERVAL", 3600.0)   # 让节流必然生效
    seen = []
    updater.download("https://e/pkg", str(tmp_path / "pkg"),
                     progress_cb=lambda d, t, s: seen.append((d, t, s)))
    # 64 个数据块 -> 最多"第一块 + 收尾"两次
    assert len(seen) <= 2, f"节流没生效: 回调了 {len(seen)} 次(共 64 个数据块)"
    assert seen[-1][0] == len(payload), "收尾那次没报满进度(界面会停在 99%)"


# ── 替换脚本的**真实执行**(F2/F3 跑一遍真 cmd 才算证明; 纯文本断言证明不了回滚能work) ──

WIN_ONLY = pytest.mark.skipif(os.name != "nt", reason="替换脚本是 Windows 批处理")
DEAD_PID = 999999      # 必然不存在的进程 -> "等原进程退出"立刻通过


def _instant_exit_exes():
    """系统自带的、**无参数启动后立刻退出**的程序, 拿来当"新版本一起来就崩"的替身。

    刻意不用 notepad 之类: 会弹窗、要留着进程、还得善后。ping/where 打印一句用法就
    退出, 不留任何东西。返回多个是为了让"旧版本/新版本"内容不同 —— 否则回滚有没有
    生效根本看不出来。
    """
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cands = [os.path.join(sysroot, "System32", n)
             for n in ("ping.exe", "where.exe", "whoami.exe", "hostname.exe")]
    return [p for p in cands if os.path.isfile(p)]


def _run_bat(app_dir, bat, timeout=120):
    """跑一遍替换脚本, 返回它的输出(文本)。

    ★ 输出必须落**文件**, 不能用 capture_output: bat 里 `start` 拉起的进程会继承
      父进程的管道写句柄, 只要它不退出, 读管道这端就永远等不到 EOF —— 测试会卡在
      一个完全看不出原因的地方(2026-10-09 实测: 整个 pytest 停住, 桌面上还不断冒出
      被拉起的进程)。写文件就没这个耦合。
    """
    import subprocess
    import tempfile
    with tempfile.TemporaryFile() as f:
        p = subprocess.Popen(["cmd", "/c", bat], cwd=str(app_dir),
                             stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT)
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)
            raise AssertionError(f"替换脚本 {timeout}s 没跑完(卡住了)")
        f.seek(0)
        return f.read().decode("utf-8", "replace")



@WIN_ONLY
def test_update_bat_refuses_ambiguous_target(tmp_path):
    """★ F3 的实证: 目录里有**第二个 exe** 且没有标记文件时, 必须停手报错。

    老写法按"字母序第一个 .exe"猜, 会把用户另一个程序当成待替换的目标覆盖掉
    (或造成更新循环)。宁可不更新, 也不能换错文件。
    """
    app = tmp_path / "app"
    app.mkdir()
    a = app / "AutoTest.exe"
    b = app / "AutoTest_old.exe"
    a.write_bytes(b"MZ-AAA")
    b.write_bytes(b"MZ-BBB")
    (app / "_update_download.exe").write_bytes(b"MZ-NEW")

    bat = updater.generate_update_bat(str(app), pid=DEAD_PID, exe_name=None)
    assert not (app / "_update_target.txt").exists(), "这条测试要的是'没有标记文件'"
    out = _run_bat(app, bat)
    assert "[ERROR]" in out, f"没有拒绝歧义目标, 输出: {out[-400:]}"
    # 两个 exe 都原封不动 —— 一个字节都没被替换
    assert a.read_bytes() == b"MZ-AAA" and b.read_bytes() == b"MZ-BBB"
    assert not (app / "AutoTest.exe.bak").exists()
    assert not (app / "AutoTest_old.exe.bak").exists()



# ── 一次性解压目录(_MEI*)的清理: 不修就每更新一次漏 400MB ──


# ── 更新包(zip)的校验与解压(目录模式) ──

def _make_pkg(tmp_path, version="1.6", exe_body=b"MZ" + b"x" * 2048,
              with_resources=True, name="pkg.zip"):
    """造一个像样的分发包: AutoTest.exe + _internal/VERSION + 抽查资源"""
    import zipfile
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AutoTest.exe", exe_body)
        z.writestr("_internal/_pad.bin", os.urandom(1 << 20))   # 过 1MB 下限(防半截包)
        z.writestr("_internal/VERSION", version + "\n")
        z.writestr("_internal/config/locators.yaml", "a: 1\n")
        if with_resources:
            for r in updater._SPOT_RESOURCES:
                if r.endswith("locators.yaml"):
                    continue
                z.writestr(r, b"x" * 1024)
    return p


def test_validate_update_zip_ok(tmp_path):
    p = _make_pkg(tmp_path, "1.6")
    assert updater.validate_update_zip(str(p), "1.6") == str(p)
    assert updater.read_zip_version(str(p)) == "1.6"


@pytest.mark.parametrize("name,version,why", [
    ("notzip.zip", None, "不是 zip(错误页/截断)"),
    ("empty.zip", None, "太小"),
])
def test_validate_update_zip_rejects_junk(tmp_path, name, version, why):
    p = tmp_path / name
    p.write_bytes(b"<html>502 Bad Gateway</html>" + b"x" * (2 << 20))
    with pytest.raises(ValueError):
        updater.validate_update_zip(str(p), version)
    with pytest.raises(ValueError, match="不存在"):
        updater.validate_update_zip(str(tmp_path / "没有这个.zip"), "1.6")


def test_validate_update_zip_rejects_incomplete_and_mismatched(tmp_path):
    """★ 缺件、版本对不上都必须**在替换之前**拦下 —— 否则 bat 把 _internal 换掉之后
    才发现装的是坏的, 用户就没程序用了。"""
    import zipfile
    # 缺 _internal
    p = tmp_path / "no_internal.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AutoTest.exe", b"MZ" + b"x" * 2048)
        z.writestr("_pad.bin", os.urandom(1 << 20))
    with pytest.raises(ValueError, match="_internal"):
        updater.validate_update_zip(str(p), "1.6")
    # 版本与清单不符
    p2 = _make_pkg(tmp_path, "1.5", name="wrong_ver.zip")
    with pytest.raises(ValueError, match="不符"):
        updater.validate_update_zip(str(p2), "1.6")
    # 抽查资源缺失(打包漏文件那类)
    p3 = _make_pkg(tmp_path, "1.6", with_resources=False, name="no_res.zip")
    with pytest.raises(ValueError, match="运行期资源"):
        updater.validate_update_zip(str(p3), "1.6")


def test_extract_update_writes_files(tmp_path):
    p = _make_pkg(tmp_path, "1.6")
    dest = tmp_path / "extracted"
    exe = updater.extract_update(str(p), str(dest))
    assert os.path.isfile(exe)
    assert (dest / "_internal" / "VERSION").read_text().strip() == "1.6"


def test_extract_update_blocks_zip_slip(tmp_path):
    """★ 压缩包里的路径能带 `..\\` —— 不校验就能写到程序目录之外(经典 zip slip)"""
    import zipfile
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AutoTest.exe", b"MZ" + b"x" * 2048)
        z.writestr("_internal/VERSION", "1.6\n")
        z.writestr("../../evil.txt", b"pwned")
    with pytest.raises(ValueError, match="路径"):
        updater.extract_update(str(p), str(tmp_path / "out"))
    assert not (tmp_path.parent / "evil.txt").exists()


# ── 替换脚本(目录替换版) ──

def test_update_bat_structure(tmp_path):
    r"""★ bat 必须是"目录替换"流程, 且不含已知的 cmd 陷阱。"""
    app = tmp_path / "app"
    app.mkdir()
    (app / "AutoTest.exe").write_bytes(b"MZ")
    bat = updater.generate_update_bat(str(app), pid=12345, exe_name="AutoTest.exe")
    text = open(bat, encoding="utf-8").read()
    lines = text.splitlines()
    for must in (r'"%SYS%\tasklist.exe" /FI "PID eq 12345"',   # 等 PID 退出
                 r'ren "_internal" "_internal_old"',           # 旧载荷改名(不是复制!)
                 r'move "_update_extracted\_internal" "_internal"',  # 新载荷瞬时到位
                 r'ren "%EXE_NAME%" "%EXE_NAME%.bak"',         # 旧 exe 备份
                 'copy /y "_update_extracted\\%EXE_NAME%"',    # 新 exe 就位
                 "正在回滚",                                    # 失败回滚
                 'start ""', "del /f /q \"%~f0\""):             # 启动新版本 / 自删
        assert must in text, f"bat 缺少关键步骤: {must}"
    # 用 move 而不是 robocopy 拷 400MB —— 这是相对参考项目的关键改进
    assert "robocopy" not in text.lower(), "又在搬整个 _internal 了(应该是瞬时改名)"
    # 外部命令走绝对路径 / 等待用 ping
    for name in ("tasklist", "find", "ping"):
        assert f"%SYS%\\{name}.exe" in text, f"{name} 没有走绝对路径"
    assert "timeout.exe" not in text, "等待退回 timeout 了(重定向下等待为 0 秒)"
    assert "\t" not in text and "\f" not in text
    assert lines[0] == "@echo off" and lines[1].startswith("chcp 65001")
    # echo 文本不许出现会被 cmd 当成命令结构的字符(半角括号是 v1.3 的死因)
    bad = [ln.strip() for ln in lines
           if ln.strip().lower().startswith("echo") and set(ln) & set("()&|<>^")]
    assert not bad, f"这些 echo 会让 cmd 中止批处理: {bad}"
    # 变量展开后紧跟 \" 的陷阱
    assert not [ln for ln in lines if '%\\"' in ln], "有 %VAR%\\\" 陷阱"
    # 用户数据绝不出现在替换范围里
    for guarded in ("config.yaml", "Test_cases", "Test_preconditions", "Test_img"):
        assert guarded not in text, f"bat 里出现了用户数据路径: {guarded}"
    raw = open(bat, "rb").read()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_target_marker_still_written(tmp_path):
    updater.write_target_marker(str(tmp_path), "AutoTest.exe")
    assert (tmp_path / updater.TARGET_MARKER).read_text(encoding="utf-8") == "AutoTest.exe"


# ── 真跑一遍 cmd: 目录替换的成败两条路 ──

def _make_app_dir(tmp_path, old_src, new_src, old_ver="1.0", new_ver="2.0",
                 exe_name="zz_swap_probe.exe"):
    """造出"更新前"的目录 + 已由程序解压好的新版本"""
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / exe_name).write_bytes(open(old_src, "rb").read())
    (app / "_internal" / "VERSION").write_text(old_ver, encoding="utf-8")
    (app / "_internal" / "old_only.dll").write_bytes(b"OLD")
    ex = app / "_update_extracted"
    (ex / "_internal").mkdir(parents=True)
    (ex / exe_name).write_bytes(open(new_src, "rb").read())
    (ex / "_internal" / "VERSION").write_text(new_ver, encoding="utf-8")
    (ex / "_internal" / "new_only.dll").write_bytes(b"NEW")
    return app


@WIN_ONLY
def test_update_bat_rolls_back_when_new_version_dies(tmp_path):
    """★ 新版本起不来时, `_internal\\` 与 exe 都必须换回更新前的样子(目录级回滚)。"""
    cands = _instant_exit_exes()
    if len(cands) < 2:
        pytest.skip("找不到两个无参即退的替身程序")
    # ★ exe 名必须**独一无二**: 存活判定是 tasklist 按镜像名查, 用 "AutoTest.exe"
    #   这种名字会被机器上任何一个同名进程(比如我们自己的测试残留)骗过去, 于是本该
    #   回滚的场景走成成功路径(实测踩过 —— 与当年用 python.exe 当替身是同一类坑)。
    name = "zz_rollback_probe.exe"
    app = _make_app_dir(tmp_path, cands[0], cands[1], exe_name=name)
    old_exe = (app / name).read_bytes()
    bat = updater.generate_update_bat(str(app), pid=DEAD_PID,
                                      exe_name=name, probe_seconds=2)
    out = _run_bat(app, bat)
    assert "[ERROR]" in out, out[-300:]
    assert (app / "_internal" / "VERSION").read_text(encoding="utf-8") == "1.0", \
        "回滚后 _internal 不是更新前那一份"
    assert (app / "_internal" / "old_only.dll").exists(), "旧载荷内容没回来"
    assert not (app / "_internal" / "new_only.dll").exists(), "新载荷没被换走"
    assert (app / name).read_bytes() == old_exe, "exe 没换回旧的"
    assert not (app / "_internal_old").exists(), "回滚后不应残留 _internal_old"
    assert not (app / "_update_extracted").exists(), "解压目录没清掉"
    assert not os.path.exists(bat), "替换脚本没有自删"


@WIN_ONLY
def test_update_bat_swaps_and_keeps_rollback_copy_on_success(tmp_path):
    """★ 成功路径: 载荷与 exe 都换成新的, 且**保留** `_internal_old` 与 `.bak` 作退路。

    "新版本活着"这个信号是借来的: 把目标 exe 命名成唯一名字, 测试自己先拉起一个同名
    进程当信号(结束就收掉)。★ 绝不能用 PATH 里有的名字(python.exe/cmd.exe) ——
    bat 里的 start 会真把交互式解释器拉起来, 挂着不退还攥着管道。
    """
    import shutil
    import subprocess
    cands = _instant_exit_exes()
    if not cands:
        pytest.skip("找不到替身程序")
    name = "zz_update_probe.exe"
    app = _make_app_dir(tmp_path, cands[0], cands[0], exe_name=name)
    sig_dir = tmp_path / "sig"
    sig_dir.mkdir()
    sig_exe = sig_dir / name
    shutil.copy2(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "cmd.exe"), str(sig_exe))
    alive = subprocess.Popen([str(sig_exe)], cwd=str(sig_dir), stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        bat = updater.generate_update_bat(str(app), pid=DEAD_PID, exe_name=name,
                                          probe_seconds=2)
        out = _run_bat(app, bat)
    finally:
        alive.kill()
        alive.wait(timeout=10)
    assert "新版本已启动" in out, out[-400:]
    assert (app / "_internal" / "VERSION").read_text(encoding="utf-8") == "2.0"
    assert (app / "_internal" / "new_only.dll").exists()
    assert (app / "_internal_old" / "VERSION").read_text(encoding="utf-8") == "1.0", \
        "_internal_old 被删了 —— 新版本若几十秒后崩了就没有退路"
    assert (app / f"{name}.bak").exists(), "exe 备份被删了"
    assert not (app / "_update_extracted").exists()
    assert not os.path.exists(bat)


# ── 「能不能收到更新提示」: 起真的本地 HTTP 服务跑一遍(不 mock 网络) ──

def _serve_manifest(payload: bytes, record=None):
    """起一个只服务 version.json 的本地 HTTP 服务; 返回 (port, shutdown)。

    ★ 为什么不用 mock: 我们被"mock 太狠、真实路径从没跑过"坑过 —— v1.1 的检查更新
      连着 15 天全废, 就因为测试把 _test_one_mirror 整个换掉了, AttributeError 被
      except 吞掉、每个镜像都判成不可用。所以这里走真 socket。
    """
    import http.server
    import socketserver
    import threading

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if record is not None:
                record.append(self.path)
            if self.path.rstrip("/").endswith("version.json"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv.shutdown


def _local_cfg(port):
    return {"update": {"enabled": True, "repository": "o/r", "branch": "master",
                       "version_file": "version.json",
                       "mirrors": [{"name": "local",
                                    "base_url": f"http://127.0.0.1:{port}/{{repo}}/{{branch}}",
                                    "download_prefix": ""}]}}


def test_check_for_update_sees_newer_version_over_real_http():
    """★ 本地起真 HTTP 服务冒充更新源: 能真的"收到新版本"(status=has_update)。

    这条覆盖的是打包后的程序实际会走的那条路: 测速 -> 取清单 -> 比对本地版本。
    """
    import json
    payload = json.dumps({"version": "9.9", "sha256": "0" * 64,
                          "download_url": "http://127.0.0.1/x.zip",
                          "release_date": "2026-10-11",
                          "release_notes": "假清单", "min_version": "1.0"},
                         ensure_ascii=False).encode("utf-8")
    seen = []
    port, shutdown = _serve_manifest(payload, seen)
    try:
        res = updater.check_for_update(_local_cfg(port), current="1.7")
        assert res.status == "has_update", res.message
        assert res.info.version == "9.9"
        assert "9.9" in res.message
        assert seen and any("version.json" in p for p in seen), seen
        # 同一份清单, 本地版本更高时必须是"已是最新"(不能瞎提示)
        res2 = updater.check_for_update(_local_cfg(port), current="9.9")
        assert res2.status == "up_to_date", res2.message
    finally:
        shutdown()


def test_check_for_update_reports_error_when_source_is_down():
    """更新源连不上 -> error(界面不打扰用户, 但要记日志), 绝不能抛异常崩掉检查"""
    res = updater.check_for_update(_local_cfg(1), current="1.7")   # 1 端口没人听
    assert res.status == "error", res.message
