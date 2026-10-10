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
    bat = updater.generate_update_bat(str(app_dir), pid=12345,
                                      exe_name="AutoTest.exe")
    text = open(bat, encoding="utf-8").read()
    lines = text.splitlines()      # 文本模式读取已把 CRLF 归一成 \n, 别按 \r\n 切
    # 完整流程的关键步骤(含反斜杠的一律用 raw string, 别被 Python 转义坑了)
    for must in (r'"%SYS%\tasklist.exe" /FI "PID eq 12345"',  # 等 PID 退出
                 '.bak"',                                    # 旧 exe 改名备份
                 'copy /y "_update_download.exe"',           # 复制新程序
                 "正在回滚",                                 # 失败回滚
                 "start \"\"",                               # 启动新版本
                 "del /f /q \"%~f0\""):                      # 自删
        assert must in text, f"bat 缺少关键步骤: {must}"
    # ★ 外部命令必须是绝对路径: 裸写 find/timeout 会被 PATH 里的 Git/MSYS 抢占
    #   (实测 "timeout: invalid time interval '/t'" -> 等待变成立即返回)
    for name in ("tasklist", "find", "ping"):
        assert f'%SYS%\\{name}.exe' in text, f"{name} 没有走绝对路径"
    assert "\t" not in text and "\f" not in text, \
        "bat 里混进了制表符/换页符 —— f-string 把 \\t \\f 当转义了"
    # ★ 等待必须是 ping: timeout 在 stdin 被重定向时立刻报错退出, 观察窗口缩成 0 秒,
    #   判据①("等够时间还活着吗")就退化成"start 后立刻查一次"的竞态 —— 而真实执行
    #   测试在这种环境下恰好捕捉不到它(退回去照样全绿), 所以在这里显式钉住。
    assert "timeout.exe" not in text, "等待退回 timeout 了 —— 重定向下等待为 0 秒"
    assert r'"%SYS%\ping.exe"' in text, "没有走 ping 计时"
    # ★ 变量展开后紧跟 \" 的写法: 变量为空时展开成 "\" 会被 cmd 当成转义引号,
    #   整行判定 "The syntax of the command is incorrect." → 整个更新中止(实测 rc=255)
    trap = [ln.strip() for ln in text.splitlines() if '%\\"' in ln]
    assert not trap, f'这些行有 "%VAR%\\" 陷阱(空变量时 bat 会中止): {trap}'
    # ★ chcp 必须紧跟 @echo off: 切换代码页之前出现中文会被按旧代码页读乱码
    assert lines[0] == "@echo off" and lines[1].startswith("chcp 65001"), \
        f"chcp 位置不对: {lines[:2]}"
    # ── F3: 目标程序名走标记文件, 不靠"目录里字母序第一个 .exe"猜 ──
    assert 'set /p EXE_NAME=<"_update_target.txt"' in text, \
        "没有从标记文件读目标程序名 —— 目录里多一个 exe 就会换错文件"
    assert "无法确认该替换哪一个" in text, "多个候选时没有停手(会换错文件)"
    assert (app_dir / "_update_target.txt").read_text(encoding="utf-8") == "AutoTest.exe"
    # ── F2: 启动后必须确认新进程活着, 否则回滚 ──
    assert '/FI "IMAGENAME eq %EXE_NAME%" /FO CSV /NH' in text, \
        "没有确认新版本是否真的起来了 —— 新版本起不来时没有回滚"
    assert "if not errorlevel 1 goto :started" in text
    assert "回滚失败" in text, "回滚本身失败时必须告诉用户手工怎么办"
    # 成功后**不再由 bat 删 .bak**(新版本可能几十秒后才崩, 那时它是唯一退路)
    assert ":old_cleanup_loop" not in text, \
        "bat 又开始无条件删旧版本备份了 —— 新版本起不来就没退路了"
    # 用户数据绝不出现在替换范围里(bat 只碰 exe)
    assert "Test_cases" not in text and "Test_preconditions" not in text
    assert "config.yaml" not in text
    # 不嵌绝对路径: app_dir 是含盘符的绝对路径, 不得出现在 bat 里(全用 %~dp0)
    assert str(app_dir) not in text and "%~dp0" in text
    # CRLF: cmd 对裸 LF 的 bat 兼容性差
    raw = open(bat, "rb").read()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_update_bat_echoes_contain_no_cmd_metachars(tmp_path):
    """★ 扫描守护: bat 的 echo 文本里不得出现半角 () & | < > ^。

    2026-10-09 实证: `if 1 neq 0 ( echo 坏(括号?), 走开 )` 在 cmd 里 **必定** 中止整个
    批处理(rc=255, 与代码页无关) —— 一个半角 `)` 就把 if 块提前闭合了。原脚本里
    "无法重命名当前程序(权限不足/杀毒锁定?)" 正是这样, 而它紧跟 `ren`: 每次更新都是
    旧 exe 已改名 .bak -> bat 静默死掉 -> 新版没复制/没启动/没回滚, 用户一个 exe 都不剩。
    纯文本断言证明不了"bat 能跑完", 这条扫描是第二道网(第一道是下面的真实执行)。
    """
    app = tmp_path / "app"
    app.mkdir()
    (app / "AutoTest.exe").write_bytes(b"MZ")
    bat = updater.generate_update_bat(str(app), pid=1, exe_name="AutoTest.exe")
    bad = []
    for ln in open(bat, encoding="utf-8"):
        s = ln.strip()
        if not s.lower().startswith("echo"):
            continue
        body = s[4:].lstrip(".").lstrip()
        hit = sorted(set(body) & set("()&|<>^"))
        if hit:
            bad.append((s, hit))
    assert not bad, f"这些 echo 会让 cmd 中止批处理: {bad}"


# ── 目标程序名的传递(F3) ──

def test_target_marker_holds_running_exe_name(tmp_path):
    """标记文件 = 当时正在运行的那个 exe 名; bat 用它替换, 不再猜"""
    p = updater.write_target_marker(str(tmp_path), "APP_AutoTest.exe")
    assert p and open(p, encoding="utf-8").read() == "APP_AutoTest.exe"
    # 中文名同样要能传达(实测: cmd 在 chcp 65001 下能正确用 UTF-8 字节的变量操作文件)
    updater.write_target_marker(str(tmp_path), "自动化测试平台.exe")
    assert open(p, encoding="utf-8").read() == "自动化测试平台.exe"


@pytest.mark.parametrize("name", [
    "", "   ", None,
    'a"b.exe',                    # 引号能改变 cmd 的命令结构
    "a%b.exe",                    # % 会被 cmd 二次展开
    "a&b.exe", "a|b.exe", "a<b.exe", "a>b.exe", "a^b.exe", "a!b.exe",
    "a\r\nb.exe",                 # 换行 -> 变量里多出命令
])
def test_target_marker_refuses_dangerous_names(tmp_path, name):
    """名字里含 cmd 会二次解析的字符时**不写标记文件** —— 宁可退回"唯一候选"
    逻辑(有歧义就停手), 也不能把危险字符拼进命令行"""
    assert updater.write_target_marker(str(tmp_path), name) == ""
    assert not (tmp_path / "_update_target.txt").exists()


def test_target_marker_uses_only_basename(tmp_path):
    """只取文件名: bat 在 %~dp0 下工作, 传绝对路径没有意义还可能带来编码问题"""
    updater.write_target_marker(str(tmp_path), r"D:\some dir\App.exe")
    assert open(tmp_path / "_update_target.txt", encoding="utf-8").read() == "App.exe"


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
def test_update_bat_rolls_back_when_new_version_dies(tmp_path):
    """★ F2 的核心实证: 新版本**起不来**时, 必须把旧版本换回去。

    老写法是"start 之后固定 3 秒无条件删 .bak" —— 新版本起不来时用户手里就只剩一个
    坏程序, 没有任何退路。这条测试真跑一遍 cmd: 用一个会立刻退出的程序当新版本,
    跑完必须看到"旧版本的文件内容原样回来"。
    """
    import shutil
    cands = _instant_exit_exes()
    if len(cands) < 2:
        pytest.skip("找不到两个无参数即退出的系统程序做替身")
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "AutoTest.exe"
    shutil.copy2(cands[0], exe)                    # 更新前的程序
    before = exe.read_bytes()
    shutil.copy2(cands[1], app / "_update_download.exe")   # 新版本(一起来就退出)
    new_bytes = (app / "_update_download.exe").read_bytes()
    assert new_bytes != before, "两个替身内容相同, 这条测试证明不了回滚"

    bat = updater.generate_update_bat(str(app), pid=DEAD_PID,
                                      exe_name="AutoTest.exe", probe_seconds=2)
    out = _run_bat(app, bat)
    assert "[ERROR]" in out, f"没有走到失败分支, 输出: {out[-400:]}"
    assert exe.read_bytes() == before, \
        "没有回滚! 手里剩下的是起不来的新版本 —— 用户只能手工重装"
    assert not (app / "AutoTest.exe.bak").exists(), "回滚后备份没有归位"
    # 残渣一律清干净(含 200MB 包与标记文件)
    assert not (app / "_update_download.exe").exists()
    assert not (app / "_update_target.txt").exists()
    assert not bat or not os.path.exists(bat), "替换脚本没有自删"


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


@WIN_ONLY
def test_update_bat_keeps_backup_on_success(tmp_path):
    """★ 成功路径的实证(F2 的另一半): 确认新进程活着之后, .bak **仍然保留**。

    老写法是"start 之后固定 3 秒无条件删 .bak"; 新写法把删它交给新版本自己跑稳之后
    (core.bootstrap.cleanup_old_backup) —— 因为新版本可能几十秒后才崩, 那时 .bak 是
    用户唯一的退路。
    ★ "新版本活着"这个信号是**测试自己造**的: 用一个不在 PATH 里的唯一名字, 提前把
      同名进程拉起来(拿 cmd.exe 的副本, 无参数 + 管道 stdin -> 它会一直等着), 由测试
      握着句柄、结束时收掉。放在**另一个目录**, 免得它就是被替换的那个文件。
    ★★ 绝对不能用 python.exe / cmd.exe 这类**PATH 里有的名字**: bat 里的
      `start "" "<名字>"` 会真的把它拉起来 —— 交互式 python 会挂在桌面上不走, 还
      继承父进程的管道句柄, 于是测试永远等不到子进程结束(2026-10-09 实测: 桌面上
      不断冒 python 窗口, pytest 整个卡死)。
    """
    import shutil
    import subprocess
    cands = _instant_exit_exes()
    if not cands:
        pytest.skip("找不到可用的替身程序")
    name = "zz_update_probe.exe"          # 唯一名字: 不在 PATH, start 只会命中本目录的副本
    app = tmp_path / "app"
    app.mkdir()
    exe = app / name
    shutil.copy2(cands[0], exe)                       # 旧版本(会立刻退出, 无所谓内容)
    shutil.copy2(cands[1] if len(cands) > 1 else cands[0],
                 app / "_update_download.exe")
    new_bytes = (app / "_update_download.exe").read_bytes()

    sig_dir = tmp_path / "sig"
    sig_dir.mkdir()
    sig_exe = sig_dir / name
    shutil.copy2(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "cmd.exe"), str(sig_exe))
    alive = subprocess.Popen([str(sig_exe)], cwd=str(sig_dir),
                             stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        bat = updater.generate_update_bat(str(app), pid=DEAD_PID,
                                          exe_name=name, probe_seconds=2)
        out = _run_bat(app, bat)
    finally:
        alive.kill()
        alive.wait(timeout=10)
    assert "新版本已启动" in out, f"没走到成功分支, 输出: {out[-400:]}"
    assert exe.read_bytes() == new_bytes, "新版本没有被复制到位"
    assert (app / f"{name}.bak").exists(), \
        ".bak 被删了 —— 新版本若在后面几十秒里崩了, 用户就没有退路了"
    assert not (app / "_update_download.exe").exists(), "残渣没清掉"
    assert not (app / "_update_target.txt").exists()
    assert not os.path.exists(bat), "替换脚本没有自删"


# ── 一次性解压目录(_MEI*)的清理: 不修就每更新一次漏 400MB ──

def test_tempdir_marker_only_for_mei_dirs(tmp_path):
    """只给"名字确实像 _MEI*"的目录写标记 —— 这个内容会被脚本拿去 rd /s /q"""
    mei = tmp_path / "_MEI123456"
    mei.mkdir()
    p = updater.write_tempdir_marker(str(tmp_path), str(mei))
    assert p and open(p, encoding="utf-8").read() == str(mei)
    # 不是 _MEI* / 不存在 / 空 —— 一律不写(宁可留下临时目录, 也不给脚本一个可删的真实路径)
    for bad in (str(tmp_path), str(tmp_path / "不存在"), "", None):
        os.path.exists(os.path.join(str(tmp_path), updater.TEMPDIR_MARKER)) and \
            os.remove(os.path.join(str(tmp_path), updater.TEMPDIR_MARKER))
        assert updater.write_tempdir_marker(str(tmp_path), bad) == "", bad


def test_update_bat_cleans_old_extraction_dir_safely(tmp_path):
    """bat 必须收掉旧进程的一次性解压目录, 但**必须带 _MEI 校验** —— 标记文件的内容
    会直接进 rd /s /q, 没有校验就等于"文件写什么就删什么"。"""
    app = tmp_path / "app"
    app.mkdir()
    (app / "AutoTest.exe").write_bytes(b"MZ")
    mei = tmp_path / "_MEI999"
    mei.mkdir()
    bat = updater.generate_update_bat(str(app), pid=1, exe_name="AutoTest.exe",
                                      temp_dir=str(mei))
    text = open(bat, encoding="utf-8").read()
    assert 'set /p OLDTMP=<"_update_tempdir.txt"' in text, "没有读解压目录标记"
    assert '%OLDTMP:_MEI=%' in text, "缺少 _MEI 校验 —— 标记内容会被无脑删除"
    assert 'rd /s /q "%OLDTMP%"' in text
    # 清理阶段要连标记文件一起删
    assert 'del /f /q "_update_tempdir.txt"' in text
    # 目标名标记的写法不能被顶掉
    assert 'set /p EXE_NAME=<"_update_target.txt"' in text


@WIN_ONLY
def test_update_bat_deletes_old_extraction_dir_but_nothing_else(tmp_path):
    """真跑一遍: 标记指向 _MEI* 目录 -> 删掉; 指向**普通目录** -> 一个字节都不动。

    标记文件的内容会被脚本拿去 rd /s /q —— 这是整个更新流程里唯一"按文件内容删目录"
    的地方, 所以必须有名字校验兜底(F2 同源的谨慎)。
    """
    cands = _instant_exit_exes()
    if not cands:
        pytest.skip("找不到替身程序")
    app = tmp_path / "app"
    app.mkdir()
    (app / "AutoTest.exe").write_bytes(b"MZ-marker")

    # (1) 指向 _MEI* 目录 -> 应被删除
    mei = tmp_path / "_MEIfake"
    (mei / "sub").mkdir(parents=True)
    (mei / "sub" / "x.dll").write_bytes(b"x" * 1024)
    bat = updater.generate_update_bat(str(app), pid=DEAD_PID,
                                      exe_name="AutoTest.exe", probe_seconds=1,
                                      temp_dir=str(mei))
    assert (app / updater.TEMPDIR_MARKER).exists()
    _run_bat(app, bat)
    assert not mei.exists(), "旧进程的一次性解压目录没被清掉(每次更新漏 400MB)"

    # (2) 指向普通目录 -> 必须原封不动(名字里没有 _MEI)
    keep = tmp_path / "我的资料"
    keep.mkdir()
    (keep / "别删我.txt").write_text("重要", encoding="utf-8")
    bat2 = updater.generate_update_bat(str(app), pid=DEAD_PID,
                                       exe_name="AutoTest.exe", probe_seconds=1,
                                       temp_dir=str(keep))
    # write_tempdir_marker 本身就会拒写 —— 手动放一个假的标记文件模拟"被人塞了路径"
    open(app / updater.TEMPDIR_MARKER, "w", encoding="utf-8",
         newline="\r\n").write(str(keep))
    _run_bat(app, bat2)
    assert keep.exists() and (keep / "别删我.txt").exists(), \
        "标记文件里的普通目录被删了 —— 名字校验形同虚设"
