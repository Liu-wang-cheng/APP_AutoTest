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

def test_race_mirrors_orders_by_availability_then_latency(monkeypatch):
    def fake(mirror, version_file, timeout=0):
        name = mirror["name"]
        if name == "dead":
            return updater.MirrorResult(name, mirror["base_url"], "", -1.0, False)
        return updater.MirrorResult(name, mirror["base_url"], "",
                                    100.0 if name == "slow" else 20.0, True)

    monkeypatch.setattr(updater, "_test_one_mirror", fake)
    mirrors = [{"name": "dead", "base_url": "https://d"},
               {"name": "slow", "base_url": "https://s"},
               {"name": "fast", "base_url": "https://f"}]
    got = [r.name for r in updater.race_mirrors(mirrors, "version.json")]
    assert got == ["fast", "slow", "dead"], got


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


# ── 新版本 exe 校验(onefile: 安装包就是一个 exe) ──

def test_validate_new_exe_ok(tmp_path):
    p = tmp_path / "_update_download.exe"
    p.write_bytes(b"MZ" + b"x" * (2 << 20))        # MZ 头 + 足够大
    assert updater.validate_new_exe(str(p)) == str(p)


def test_validate_new_exe_rejects_bad_files(tmp_path):
    """错误页/截断文件/不存在的文件都必须拦下, 不能让它们流进替换流程"""
    small = tmp_path / "small.exe"
    small.write_bytes(b"MZ")                        # 太小
    with pytest.raises(ValueError, match="字节"):
        updater.validate_new_exe(str(small))

    txt = tmp_path / "fake.exe"
    txt.write_bytes(b"<html>502 Bad Gateway</html>" + b"x" * (2 << 20))
    with pytest.raises(ValueError, match="MZ"):
        updater.validate_new_exe(str(txt))          # 镜像报错页被当成程序

    with pytest.raises(ValueError, match="不存在"):
        updater.validate_new_exe(str(tmp_path / "nope.exe"))


def test_generate_update_bat_structure(tmp_path):
    """★ bat 必须包含完整的单文件替换与回滚流程, 且**不含绝对路径**(GBK 代码页下
    会被 cmd 读乱码 —— 参考项目实战踩过)。"""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "AutoTest.exe").write_bytes(b"MZ")   # 当前程序(供动态定位语义)
    bat = updater.generate_update_bat(str(app_dir), pid=12345)
    text = open(bat, encoding="utf-8").read()
    # 完整流程的关键步骤
    for must in ('tasklist /FI "PID eq 12345"',              # 等 PID 退出
                 '.bak"',                                    # 旧 exe 改名备份
                 'copy /y "_update_download.exe"',           # 复制新程序
                 "回滚",                                     # 失败回滚
                 "start \"\"",                               # 启动新版本
                 "del /f /q \"%~f0\""):                      # 自删
        assert must in text, f"bat 缺少关键步骤: {must}"
    # 用户数据绝不出现在替换范围里(bat 只碰 exe)
    assert "Test_cases" not in text and "Test_preconditions" not in text
    assert "config.yaml" not in text
    # 不嵌绝对路径: app_dir 是含盘符的绝对路径, 不得出现在 bat 里(全用 %~dp0)
    assert str(app_dir) not in text and "%~dp0" in text
    # CRLF: cmd 对裸 LF 的 bat 兼容性差
    raw = open(bat, "rb").read()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
