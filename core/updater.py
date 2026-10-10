# -*- coding: utf-8 -*-
"""OTA 更新核心逻辑: 镜像测速、版本比对、下载、校验。

★ 只做**纯逻辑**, 不 import PySide6 —— 这样它能被单测直接覆盖, 也能被独立的
  updater 脚本复用(那个脚本要在主程序退出后跑, 不该拖上一整份 GUI)。

★ 依赖只用标准库 urllib(项目原本没有 requests, 不为一个更新检查引入新依赖)。
  urllib 默认读 HTTP_PROXY/HTTPS_PROXY 环境变量, 企业代理环境够用。

★ 设计取自实战项目 TB_Import_tool 的更新器, 关键教训:
  ① 版本清单必须走 **GitHub 直连** —— CDN 有缓存, 走镜像会读到旧的 version.json
     从而误报"已是最新"。只有下载大文件才用镜像加速。
  ② 判断"是不是直连"**不能用子串匹配**: ghfast 的地址里内嵌了
     raw.githubusercontent.com, 子串匹配会把它误判成直连, 偏偏它延迟可能更低
     就被优先选中, 于是正好踩中 ① 的缓存问题。必须比 hostname。
  ③ 远程数据一律当**不可信输入**处理: 解析失败、字段缺失、JSON 畸形都不能抛异常
     打断流程 —— 一个烂版本号不该让更新功能整个瘫痪。
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urlparse

from core.logger import get_logger
from core.version import __version__, is_newer

log = get_logger()

CHECK_INTERVAL_HOURS = 8          # 启动后每 8 小时自动检测一次(用户要求)
MIRROR_TIMEOUT = 5.0
VERSION_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 60.0
PROGRESS_MIN_INTERVAL = 0.5       # 下载进度回调最小间隔(秒), 见 download()
_UA = "AutoTest-Updater"          # 有的 CDN 对空 UA 不友好

#: 镜像站: base_url 取仓库文件, download_prefix 加速 Release 下载(留空=直连)。
#: {repo}/{branch} 两个占位符都会被替换 —— 分支**不能写死 main**: 本仓库默认分支
#: 是 master, 照搬参考项目的 /main 会让版本清单 404(OTA 永远检查失败)。
#: 实测(2026-10-09, 本机):
#:   取小文件(version.json): github 直连最快 829ms > ghfast 2096ms > jsdelivr 2395ms
#:   下载大文件(3MB 实测):  github 直连**几乎不可用**(1.2s 只得到 0 字节)
#:                          gh-proxy 558 KB/s > ghfast 368 KB/s
#: 所以两边分开选: 清单优先 GitHub 直连(权威、避开 CDN 缓存), 下载按**下面的顺序**
#: 取第一个带 download_prefix 的 —— gh-proxy 排在 ghfast 前面就是因为实测它更快。
DEFAULT_MIRRORS = (
    {"name": "github",
     "base_url": "https://raw.githubusercontent.com/{repo}/{branch}",
     "download_prefix": ""},
    {"name": "gh-proxy",
     "base_url": "https://gh-proxy.com/https://raw.githubusercontent.com/{repo}/{branch}",
     "download_prefix": "https://gh-proxy.com/"},
    {"name": "ghfast",
     "base_url": "https://ghfast.top/https://raw.githubusercontent.com/{repo}/{branch}",
     "download_prefix": "https://ghfast.top/"},
    {"name": "jsdelivr",
     "base_url": "https://cdn.jsdelivr.net/gh/{repo}@{branch}",
     "download_prefix": ""},
)


def pick_download_mirror(ranked):
    """从测速结果里挑**下载用**的镜像: 可达 + 带加速前缀, 按配置顺序取第一个。

    ★ 为什么按**配置顺序**而不是测速延迟: 延迟测的是 version.json 这种小文件,
      不代表大文件下载速度 —— 实测 ghfast 延迟更低(2096ms)但下载更慢(368 KB/s),
      gh-proxy 延迟略高(2520ms)却快得多(558 KB/s)。所以顺序由 DEFAULT_MIRRORS
      的排列决定(排在前面的优先), 用户也能在 config 里调。
    ★ 与"取版本清单"分开选: 清单要**最新**(优先 GitHub 直连, 避开 CDN 缓存),
      下载要**快且下得动**(直连拉大文件实测 3MB 只得到 0 字节)。
    """
    usable = [m for m in (ranked or []) if m.success]
    for m in usable:                      # ranked 已按配置顺序稳定排序
        if m.download_prefix:
            return m
    return usable[0] if usable else None  # 都没有前缀 -> 退回最前面的可用镜像


def default_config():
    """内置默认配置。

    ★ repository 故意留空 —— 没填就是**不检查更新**, 免得对着一个瞎猜的仓库
      发请求(尤其在企业网里, 无谓的外联会引来安全告警)。
    """
    return {
        "enabled": True,
        "repository": "",                 # owner/repo
        "branch": "master",               # 版本清单所在分支(默认分支)
        "version_file": "version.json",
        "check_interval_hours": CHECK_INTERVAL_HOURS,
        "mirrors": [dict(m) for m in DEFAULT_MIRRORS],
    }


def load_update_config(cfg):
    """从 config.yaml 的 update 段取更新配置, 与内置默认合并。

    用户只填 repository / enabled 也能工作 —— mirrors 缺省用内置那份。
    """
    out = default_config()
    user = (cfg or {}).get("update") or {}
    if not isinstance(user, dict):
        return out
    for k in ("enabled", "repository", "branch", "version_file",
              "check_interval_hours"):
        if k in user and user[k] is not None:
            out[k] = user[k]
    mirrors = user.get("mirrors")
    if isinstance(mirrors, list):
        cleaned = [m for m in mirrors if isinstance(m, dict) and m.get("base_url")]
        if cleaned:
            out["mirrors"] = cleaned
    return out


def build_mirrors(conf, repo):
    """把配置里的镜像模板展开成可直接请求的列表(替换 {repo}/{branch} 占位符)。

    repo 为空返回空列表 —— 替换出 `github.com//master` 这种畸形 URL 毫无意义。
    """
    if not str(repo or "").strip():
        return []
    branch = str(conf.get("branch") or "master")
    out = []
    for m in conf.get("mirrors") or []:
        base = str(m.get("base_url") or "").replace("{repo}", repo) \
            .replace("{branch}", branch)
        if base:
            out.append({"name": m.get("name") or base, "base_url": base,
                        "download_prefix": str(m.get("download_prefix") or "")})
    return out


@dataclass
class MirrorResult:
    """镜像测速结果"""
    name: str
    base_url: str
    download_prefix: str = ""
    latency_ms: float = -1.0
    success: bool = False


@dataclass
class VersionInfo:
    """远程版本清单(version.json)"""
    version: str = ""
    sha256: str = ""
    download_url: str = ""
    release_date: str = ""
    release_notes: str = ""
    min_version: str = "0.0"


@dataclass
class CheckResult:
    """一次检查更新的结果"""
    status: str = "skipped"          # skipped / up_to_date / has_update / error
    message: str = ""                # 给日志/界面看的一句话
    info: VersionInfo = field(default_factory=VersionInfo)
    mirror: MirrorResult = None      # 命中版本清单的那个(直连优先, 求"最新")
    force: bool = False              # 本地版本低于 min_version -> 强制更新
    ranked: list = field(default_factory=list)   # 全部镜像测速结果(供**下载**另选)


def _base_url_of(mirror):
    """取镜像的 base_url —— **同时兼容 dict 与 MirrorResult**。

    ★ 这两种形态在流程里都会出现: `build_mirrors()` 产出 dict(还没测速),
      `race_mirrors()` 产出 MirrorResult(已测速)。曾经只按 MirrorResult 写,
      而 race_mirrors 传进去的是 dict —— 每个镜像都在 `mirror.base_url` 抛
      AttributeError, 被 except 吞掉后全判为"不可用", 于是**更新检查从未成功过**
      (跑 15 天、每 8 小时失败一次都是这个原因)。
    """
    if isinstance(mirror, dict):
        return str(mirror.get("base_url") or "")
    return str(getattr(mirror, "base_url", "") or "")


def _name_of(mirror):
    if isinstance(mirror, dict):
        return str(mirror.get("name") or _base_url_of(mirror))
    return str(getattr(mirror, "name", "") or _base_url_of(mirror))


def _prefix_of(mirror):
    if isinstance(mirror, dict):
        return str(mirror.get("download_prefix") or "")
    return str(getattr(mirror, "download_prefix", "") or "")


def is_direct_github(base_url):
    """是否 GitHub 直连(权威无缓存)。

    ★ 必须比 hostname, 不能子串匹配 —— 见模块注释 ②
    """
    try:
        return urlparse(str(base_url)).hostname == "raw.githubusercontent.com"
    except Exception:
        return False


def build_url(mirror, version_file):
    return f"{_base_url_of(mirror)}/{version_file}"


def _request(url, method="GET", timeout=MIRROR_TIMEOUT, extra_headers=None):
    headers = {"User-Agent": _UA}
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _test_one_mirror(mirror, version_file, timeout=MIRROR_TIMEOUT):
    """测单个镜像的响应速度(HEAD 优先, 不支持则 Range 取 1 字节)"""
    url = build_url(mirror, version_file)
    prefix = mirror.get("download_prefix", "")
    name = mirror.get("name") or mirror["base_url"]
    try:
        t0 = time.monotonic()
        with _request(url, method="HEAD", timeout=timeout) as resp:
            ok = getattr(resp, "status", 200) in (200, 302)
        latency = (time.monotonic() - t0) * 1000
        if ok:
            return MirrorResult(name, mirror["base_url"], prefix, latency, True)
    except Exception:
        pass
    try:
        t0 = time.monotonic()
        with _request(url, timeout=timeout,
                      extra_headers={"Range": "bytes=0-0"}) as resp:
            ok = getattr(resp, "status", 200) in (200, 206)
            latency = (time.monotonic() - t0) * 1000
        return MirrorResult(name, mirror["base_url"], prefix,
                            latency if ok else -1.0, ok)
    except Exception:
        return MirrorResult(name, mirror["base_url"], prefix, -1.0, False)


def race_mirrors(mirrors, version_file, timeout=MIRROR_TIMEOUT):
    """并发测所有镜像; 结果按"可用优先 + **配置顺序**"返回。

    ★ 组内按配置顺序(不是延迟)排: 下载选源靠这个顺序表达优先级 —— 延迟只反映
      小文件请求, 不代表下载速度(实测 ghfast 延迟低但下载慢)。取版本清单那边
      另有自己的"直连优先"逻辑, 不受此顺序影响。
    """
    mirrors = list(mirrors or [])
    if not mirrors:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=len(mirrors)) as pool:
        futs = {pool.submit(_test_one_mirror, m, version_file, timeout): (i, m)
                for i, m in enumerate(mirrors)}
        for fut in as_completed(futs):
            idx, m = futs[fut]
            try:
                results.append((idx, fut.result()))
            except Exception:                       # 线程内兜底
                results.append((idx, MirrorResult(
                    m.get("name") or m.get("base_url", ""), m.get("base_url", ""),
                    m.get("download_prefix", ""), -1.0, False)))
    results.sort(key=lambda pair: (not pair[1].success, pair[0]))
    return [r for _idx, r in results]


def parse_version_info(data):
    """远程 JSON → VersionInfo。字段缺失/类型不对都当空值, 不抛。"""
    if not isinstance(data, dict):
        return VersionInfo()
    def _s(k):
        v = data.get(k)
        return "" if v is None else str(v)
    return VersionInfo(version=_s("version"), sha256=_s("sha256"),
                       download_url=_s("download_url"),
                       release_date=_s("release_date"),
                       release_notes=_s("release_notes"),
                       min_version=_s("min_version") or "0.0")


def fetch_version_info(sorted_mirrors, version_file):
    """取版本清单; 返回 (VersionInfo, MirrorResult) 或 None。

    ★ 顺序上**直连优先** —— 版本清单要的是"最新", 而 CDN 有缓存。见模块注释 ①
    """
    direct = [m for m in sorted_mirrors if m.success and is_direct_github(m.base_url)]
    others = [m for m in sorted_mirrors if m.success and not is_direct_github(m.base_url)]
    for mirror in direct + others:
        try:
            with _request(build_url(mirror, version_file),
                          timeout=VERSION_TIMEOUT) as resp:
                if getattr(resp, "status", 200) != 200:
                    continue                    # 同上: 别把错误页当清单解析
                raw = resp.read()
            info = parse_version_info(json.loads(raw.decode("utf-8")))
            if info.version:
                return info, mirror
        except Exception as e:
            log.warning(f"[更新] 从 {mirror.name} 取版本清单失败: {e}")
    return None


def download(url, dest_path, progress_cb=None, timeout=DOWNLOAD_TIMEOUT):
    """流式下载到 dest_path; 失败抛异常并清掉半截文件。

    progress_cb(downloaded, total, speed_text) —— total 未知时传 0。
    """
    tmp = dest_path + ".part"
    try:
        with _request(url, timeout=timeout) as resp, open(tmp, "wb") as f:
            status = getattr(resp, "status", 200)
            if status not in (200, 206):
                # ★ 不能指望 urlopen 一定抛: 经过某些代理时 4xx/5xx 也会正常返回,
                #   不检查就会把一个错误页当成安装包写下去(还被算作"下载成功")
                raise OSError(f"下载失败: HTTP {status}")
            total = int(resp.headers.get("content-length") or 0)
            got = 0
            t0 = time.monotonic()
            last_cb = None            # None = 还没报过 -> 第一块就报, 让界面立刻有反应

            def _report(now):
                el = max(now - t0, 1e-6)
                speed = got / el
                if total > 0:
                    progress_cb(got, total, f"{speed / 1048576:.1f} MB/s")
                else:
                    progress_cb(got, 0, f"{got / 1048576:.1f} MB")

            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress_cb:
                    now = time.monotonic()
                    # ★ 节流(每 64KB 一次的话, 208MB ≈ 3300 次): 每次回调都要跨线程
                    #   发信号、界面 setText、再写一行日志 —— 不节流会把 GUI 事件队列
                    #   和运行日志一起淹掉(实测: 一次更新写 3300 行日志)。
                    #   但**收尾那一次必报**(见循环后), 否则进度到不了 100%。
                    if last_cb is None or now - last_cb >= PROGRESS_MIN_INTERVAL:
                        last_cb = now
                        _report(now)
            if progress_cb:
                _report(time.monotonic())       # 收尾: 进度必须能走到 100%
        os.replace(tmp, dest_path)      # 原子落地: 半截文件不会冒充完整包
        return True
    except Exception:
        for p in (tmp, dest_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        raise


def verify_sha256(file_path, expected):
    """校验文件 sha256; expected 为空时视为"未提供校验值" -> 通过(仅记警告)。

    ★ 提供校验值时**必须**比对成功, 否则一个被中间人换掉的包会被直接执行。
    """
    if not expected:
        log.warning("[更新] 版本清单未提供 sha256, 跳过完整性校验")
        return True
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower() == str(expected).strip().lower()


def check_for_update(cfg, current=None):
    """检查更新(纯逻辑入口)。任何失败都返回 status="error", 不抛异常。

    gui 侧只需看 result.status:
      skipped     —— 未启用/没配仓库
      up_to_date  —— 已是最新
      has_update  —— 有新版(看 info/force)
      error       —— 网络或数据问题(界面不必打扰用户, 记日志即可)
    """
    conf = load_update_config(cfg)
    cur = current or __version__
    if not conf.get("enabled", True):
        return CheckResult("skipped", "更新检查已关闭")
    repo = str(conf.get("repository") or "").strip()
    if not repo:
        return CheckResult("skipped", "未配置更新仓库(update.repository)")
    version_file = str(conf.get("version_file") or "version.json")
    branch = str(conf.get("branch") or "master")
    mirrors = build_mirrors(conf, repo)
    if not mirrors:
        return CheckResult("error", "更新配置里没有可用的镜像站")

    try:
        log.info(f"[更新] 开始检查: 仓库 {repo}(分支 {branch}), "
                 f"本地 v{cur}, 共 {len(mirrors)} 个镜像源")
        ranked = race_mirrors(mirrors, version_file)
        # 把测速结果写进日志 —— 排查"取不到版本清单"时, 这一行就能看出是全网不通
        # 还是某个镜像的问题
        detail = ", ".join(
            f"{m.name} {'可达 %.0fms' % m.latency_ms if m.success else '不可达'}"
            for m in ranked)
        ok_n = sum(1 for m in ranked if m.success)
        log.info(f"[更新] 镜像测速({ok_n}/{len(ranked)} 可达): {detail}")

        got = fetch_version_info(ranked, version_file)
        if not got:
            return CheckResult("error", f"所有镜像都取不到版本清单({detail})",
                               ranked=ranked)
        info, mirror = got
        log.info(f"[更新] 取到版本清单: 远程 v{info.version}"
                 f"{'(发布于 %s)' % info.release_date if info.release_date else ''}"
                 f", 来自 {mirror.name}")
        if not is_newer(info.version, cur):
            return CheckResult("up_to_date",
                               f"已是最新(本地 v{cur}, 远程 v{info.version})",
                               info, mirror, ranked=ranked)
        # 本地版本低于 min_version -> 强制更新(不允许继续用旧版)
        force = bool(info.min_version) and is_newer(info.min_version, cur)
        return CheckResult("has_update",
                           f"发现新版本 {info.version}(当前 {cur})"
                           + ("，需强制更新" if force else ""),
                           info, mirror, force, ranked=ranked)
    except Exception as e:
        log.warning(f"[更新] 检查过程异常: {type(e).__name__}: {e}")
        return CheckResult("error", f"检查更新失败: {e}")


# ── 下载与自替换(目录模式: 只换 exe + _internal/, 用户数据绝不碰) ──

def apply_download_prefix(url, prefix):
    """按镜像前缀加速 Release 下载(前缀为空 = 直连)。"""
    prefix = str(prefix or "").strip()
    if not prefix or not url:
        return url
    return prefix + str(url)


#: 分发包(zip)里必须有的**文件**条目: (相对路径, 说明)。少一样就说明包不对, 宁可不更新。
#: ★ 不要在这里放 `_internal` —— zip 里未必有"目录条目"(zipfile.writestr 就不会生成),
#:   真实分发包只有 `_internal/xxx` 这样的文件条目; 载荷目录的存在性用下面的前缀判断。
_REQUIRED_IN_ZIP = (
    ("AutoTest.exe", "程序本体"),
    ("_internal/VERSION", "版本号(用它核对「确实是清单里那一版」)"),
)
#: 载荷目录里的条目必须以此开头(证明 `_internal\` 真有东西)
_PAYLOAD_PREFIX = "_internal/"
#: 更新时额外抽查的运行期资源(打包漏了它们会"能启动、真机才炸")
_SPOT_RESOURCES = (
    "_internal/uiautomator2/assets/u2.jar",
    "_internal/rapidocr_onnxruntime/models/ch_PP-OCRv3_rec_infer.onnx",
    "_internal/adbutils/binaries/adb.exe",
    "_internal/config/locators.yaml",
)


def read_zip_version(zip_path):
    """读分发包里的 `_internal/VERSION`(不落盘); 读不到返回 ""。"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            for name in ("_internal/VERSION", "_internal\\VERSION"):
                try:
                    return z.read(name).decode("utf-8", "replace").strip()
                except KeyError:
                    continue
    except Exception:
        return ""
    return ""


def validate_update_zip(path, expected_version=""):
    """校验下载下来的**分发包 zip**; 通过返回 path, 不符抛 ValueError。

    ★ 只验"是不是一个像样的包": zip 能打开 + 该有的东西都在 + 版本号与清单一致。
      这样即使镜像给了一个错误页/半截包/别的版本, 也会在**替换之前**被挡下 ——
      而不是等 bat 把 _internal 换掉之后才发现装的是坏的。
    """
    if not os.path.isfile(path):
        raise ValueError(f"下载的更新包不存在: {path}")
    if os.path.getsize(path) < 1 << 20:
        raise ValueError(f"更新包只有 {os.path.getsize(path)} 字节, 不像完整包")
    with open(path, "rb") as f:
        if f.read(2) != b"PK":
            raise ValueError("更新包不是 zip(缺少 PK 头) —— 可能是错误页/镜像报错页")
    try:
        with zipfile.ZipFile(path) as z:
            names = {n.replace("\\", "/") for n in z.namelist()}
            for rel, why in _REQUIRED_IN_ZIP:
                if rel not in names:
                    raise ValueError(f"更新包里缺少 {rel}({why}) —— 包不完整")
            if not any(n.startswith(_PAYLOAD_PREFIX) for n in names):
                raise ValueError("更新包里没有载荷目录 _internal —— 包不完整")
            exe = z.read("AutoTest.exe")[:2]
            if exe != b"MZ":
                raise ValueError("更新包里的 AutoTest.exe 不是 Windows 程序")
            missing = [r for r in _SPOT_RESOURCES if r not in names]
            if missing:
                raise ValueError("更新包里缺少运行期资源(打包规则漏了?): "
                                 + ", ".join(missing))
            got = read_zip_version(path)
            if expected_version and got and got != str(expected_version).strip():
                raise ValueError(f"更新包里是 v{got}, 但清单说是 v{expected_version} —— "
                                 f"包与清单不符, 拒绝更新")
    except zipfile.BadZipFile as e:
        raise ValueError(f"更新包不是有效的 zip: {e}")
    return path


def extract_update(zip_path, dest_dir):
    """把更新包解压到 dest_dir(会先清空它); 返回解压出来的程序目录。

    ★ 自己解压而不是让 bat 解压: 解压是最容易失败的一步(磁盘满/杀软拦/包损坏),
      放在**程序还活着**的时候做, 失败就直接报错、什么都不用改;
      bat 那边只剩两次瞬时 rename, 越快越不容易被打断(TB 的做法是让 bat 拷 400MB,
      中途一断就是半截程序)。
    ★ 防 zip slip: 压缩包里的路径能带 `..\\` 或绝对路径 —— 不校验的话, 一个恶意/损坏的
      包可以把文件写到程序目录之外。
    """
    import shutil
    dest_dir = os.path.abspath(dest_dir)
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            norm = os.path.normpath(name.replace("\\", "/"))
            if os.path.isabs(norm) or norm.startswith("..") or ":" in norm:
                raise ValueError(f"更新包里有非法路径, 拒绝解压: {name}")
            target = os.path.join(dest_dir, norm)
            if not os.path.abspath(target).startswith(dest_dir + os.sep):
                raise ValueError(f"更新包里有越界路径, 拒绝解压: {name}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
    exe = os.path.join(dest_dir, "AutoTest.exe")
    if not os.path.isfile(exe) or open(exe, "rb").read(2) != b"MZ":
        raise ValueError("解压后没找到可用的 AutoTest.exe")
    return exe


#: 传给替换脚本的「目标程序名」标记文件(见 write_target_marker / generate_update_bat)
TARGET_MARKER = "_update_target.txt"
#: 传给替换脚本的「目标程序名」标记文件(见 write_target_marker / generate_update_bat)
TARGET_MARKER = "_update_target.txt"
#: 目标名里**绝不能有**的字符: 这个名字会被 cmd 再解析一次(% 会二次展开, 引号/
#: 重定向/管道符能改变命令结构) —— 遇到这类名字就不写标记文件, 让 bat 走默认名兜底,
#: 绝不冒险把名字拼进命令行。
_MARKER_BAD_CHARS = set('%"&|<>^!\r\n\t')


def write_target_marker(app_dir, exe_name):
    """把**正在运行的那个 exe 名**写给替换脚本; 写不了返回 ""。

    ★ 为什么需要它(F3): bat 早先靠"目录里字母序第一个 .exe"猜当前程序 —— 目录里
      只要有第二个 exe(用户留着的旧版本、重复下载的副本), 就会换错文件: 轻则
      更新循环, 重则把用户另一个程序覆盖掉。
    ★ 编码 UTF-8 + bat 在 `chcp 65001` **之后**读: 实测(2026-10-09, Win10 19045)
      `set /p` 读文件是**字节透明**的, 而 cmd 在 65001 下能把 UTF-8 字节的变量正确
      转成文件名; bat 那边另有 `if not exist` 校验兜底。
    """
    name = os.path.basename(str(exe_name or "").strip())
    if not name or set(name) & _MARKER_BAD_CHARS:
        return ""
    path = os.path.join(os.path.abspath(app_dir), TARGET_MARKER)
    try:
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(name)
    except OSError as e:
        log.warning(f"[更新] 写目标程序名标记失败(将按默认名兜底): {e}")
        return ""
    return path


def generate_update_bat(app_dir, pid, exe_name=None, probe_seconds=8):
    """生成**目录替换**版自更新脚本, 返回 bat 路径(写在 app_dir 下)。

    目录模式(onedir)的更新: 程序文件是 `AutoTest.exe` + `_internal\\`, 用户数据
    (config/ Test_cases/ ...)在同一层但**不在** `_internal\\` 里, 所以替换程序文件天然
    不碰用户数据。

    流程: 等 PID 退出 -> 旧 `_internal\\` 改名 `_internal_old` -> 新载荷 move 到位 ->
          exe 备份成 `.bak` 并复制新的 -> 启动 -> **确认新进程活着**(没起来就整体回滚)
          -> 清理 -> 自删。

    ★ 为什么用 move 而不是复制: 同盘 move 是**瞬时改名**, 不搬一个字节。参考的
      TB_Import_tool 是 `rename 旧 _internal` + `robocopy 新 _internal`(400MB、几千个
      文件), 中途被打断就是半截程序; 我们这边两次改名, 窗口只有毫秒级。
    ★ 解压**不在这里做**: 由程序自己(还活着的时候)解压 + 校验(见 extract_update),
      失败就什么都没动; bat 只做"改名 + 复制一个小 exe"。
    ★ 启动后必须确认新进程活着(两道判据), 失败则把 `_internal_old` 与 `.bak` 都换回来。
    ★ `.bak` 与 `_internal_old` 成功后**不删**: 新版本可能几十秒后才崩, 那时它们是唯一
      的退路; 由新版本自己跑稳之后删(core.bootstrap.cleanup_old_backup)。

    ★ 见 _BAT_RULES: echo 文本禁半角括号/外部命令走绝对路径/等待用 ping/CRLF 等。
    """
    if exe_name is None and getattr(sys, "frozen", False):
        exe_name = os.path.basename(sys.executable)
    name = os.path.basename(str(exe_name or "").strip())
    marker = write_target_marker(app_dir, name) if name else ""
    if not marker:
        log.warning("[更新] 未能写下目标程序名, 替换脚本将按 AutoTest.exe 兜底")
    probe_pings = max(2, int(probe_seconds) + 1)
    bat = f"""@echo off
chcp 65001 >nul 2>&1
title 正在更新 APP 自动化测试平台
cd /d "%~dp0"

REM ★ chcp 必须紧跟 @echo off: 本文件是 UTF-8, 而 cmd 是按**当前代码页**边读边解析的 ——
REM   切换代码页之前出现中文, 就会被按旧代码页读成乱码。切换之后 %~dp0 的展开也才与
REM   文件的 UTF-8 一致(中文安装路径靠这一点)。
REM ★ 外部命令一律走系统绝对路径: 开发机若把 Git/MSYS 放在 PATH 前面, 裸写 find/ping
REM   会命中 coreutils, "等待"变成立即返回、判断全部失真。
REM ★ 等待用 ping 不用 timeout: timeout 在 stdin 被重定向时会立刻报错退出。
if not defined SystemRoot set "SystemRoot=C:\\Windows"
set "SYS=%SystemRoot%\\System32"

echo ============================================
echo   正在更新, 请勿关闭此窗口
echo ============================================
echo.

REM 等待原进程退出(最多 30 秒)
set WAITED=0
:wait_loop
"%SYS%\\tasklist.exe" /FI "PID eq {pid}" 2>nul | "%SYS%\\find.exe" "{pid}" >nul
if %errorlevel% equ 0 (
    set /a WAITED+=1
    if %WAITED% geq 30 (
        echo [ERROR] 原进程未能退出, 更新取消
        goto :cleanup
    )
    "%SYS%\\ping.exe" -n 2 127.0.0.1 >nul 2>&1
    goto wait_loop
)
echo [OK] 原进程已退出
"%SYS%\\ping.exe" -n 3 127.0.0.1 >nul 2>&1
echo.

REM 目标程序名由程序写下(用户可能把 exe 改过名); 读不到就按默认名兜底
set "EXE_NAME="
if exist "_update_target.txt" set /p EXE_NAME=<"_update_target.txt"
if not defined EXE_NAME if exist "AutoTest.exe" set "EXE_NAME=AutoTest.exe"
if not defined EXE_NAME (
    echo [ERROR] 未找到当前程序文件, 更新取消
    pause
    goto :cleanup
)

REM 新版本要已经解压好(程序自己解压并校验过)
if not exist "_update_extracted\\_internal" (
    echo [ERROR] 没找到已解压的新版本, 请重新检查更新
    pause
    goto :cleanup
)
if not exist "_update_extracted\\%EXE_NAME%" (
    echo [ERROR] 新版本里没有 %EXE_NAME%, 更新取消
    pause
    goto :cleanup
)

REM 上次留下的旧载荷先清掉(只清我们自己写的这个名字)
if exist "_internal_old" rmdir /s /q "_internal_old" 2>nul

echo 正在切换程序文件...
if exist "_internal" (
    ren "_internal" "_internal_old"
    if %errorlevel% neq 0 (
        echo [ERROR] 无法重命名 _internal 目录 -- 权限不足或被杀毒锁定? 更新取消
        pause
        goto :cleanup
    )
)
move "_update_extracted\\_internal" "_internal" >nul
if %errorlevel% neq 0 (
    echo [ERROR] 新载荷就位失败, 正在回滚...
    if exist "_internal_old" ren "_internal_old" "_internal"
    pause
    goto :cleanup
)

if exist "%EXE_NAME%.bak" del /f /q "%EXE_NAME%.bak" 2>nul
ren "%EXE_NAME%" "%EXE_NAME%.bak"
if %errorlevel% neq 0 (
    echo [ERROR] 无法备份当前程序 -- 权限不足或被杀毒锁定? 正在回滚...
    if exist "_internal" rmdir /s /q "_internal" 2>nul
    if exist "_internal_old" ren "_internal_old" "_internal"
    pause
    goto :cleanup
)
copy /y "_update_extracted\\%EXE_NAME%" "%EXE_NAME%" >nul
if %errorlevel% neq 0 (
    echo [ERROR] 新程序复制失败, 正在回滚...
    ren "%EXE_NAME%.bak" "%EXE_NAME%"
    if exist "_internal" rmdir /s /q "_internal" 2>nul
    if exist "_internal_old" ren "_internal_old" "_internal"
    pause
    goto :cleanup
)
echo [OK] 程序文件已更新

echo 正在启动新版本...
start "" "%EXE_NAME%"

REM ★ 启动后必须确认新进程活着才敢收工
"%SYS%\\ping.exe" -n {probe_pings} 127.0.0.1 >nul 2>&1
"%SYS%\\tasklist.exe" /FI "IMAGENAME eq %EXE_NAME%" /FO CSV /NH 2>nul | "%SYS%\\find.exe" /i "%EXE_NAME%" >nul
if not errorlevel 1 goto :started
del /f /q "%EXE_NAME%" 2>nul
if exist "%EXE_NAME%" goto :started
if not exist "%EXE_NAME%.bak" (
    echo [WARN] 新版本没有起来, 且找不到旧程序备份, 无法自动回滚
    echo       请重新下载完整安装包, 配置、用例、模板都不受影响
    pause
    goto :cleanup
)
echo [ERROR] 新版本没有正常启动, 正在回滚到更新前的版本...
if exist "_internal" rmdir /s /q "_internal" 2>nul
if exist "_internal_old" ren "_internal_old" "_internal"
ren "%EXE_NAME%.bak" "%EXE_NAME%"
if not exist "%EXE_NAME%" (
    echo [ERROR] 回滚失败! 请手工把 "%EXE_NAME%.bak" 改名为 "%EXE_NAME%"
    echo       并把 _internal_old 改名为 _internal
    pause
    goto :cleanup
)
start "" "%EXE_NAME%"
echo [OK] 已恢复到更新前的版本, 配置/用例/模板都没动, 稍后联网可以再试一次更新
pause
goto :cleanup

:started
echo [OK] 新版本已启动
REM _internal_old 与 %EXE_NAME%.bak 这里**不删**: 新版本若在后面几十秒里崩了, 它们是
REM 唯一的退路。删它们由新版本自己跑稳之后做(core.bootstrap.cleanup_old_backup)

:cleanup
if exist "_update_extracted" rmdir /s /q "_update_extracted"
if exist "_update_download.zip" del /f /q "_update_download.zip"
if exist "_update_target.txt" del /f /q "_update_target.txt"
(goto) 2>nul & del /f /q "%~f0"
"""
    bat_path = os.path.join(os.path.abspath(app_dir), "_update.bat")
    # newline="\r\n": cmd 对裸 LF 的 bat 兼容性差(块语句可能被吞), 统一 CRLF
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(bat)
    return bat_path
