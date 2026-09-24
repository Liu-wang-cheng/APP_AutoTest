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
import time
import urllib.error
import urllib.request
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
_UA = "AutoTest-Updater"          # 有的 CDN 对空 UA 不友好

#: 镜像站: base_url 取仓库文件, download_prefix 加速 Release 下载(留空=直连)
DEFAULT_MIRRORS = (
    {"name": "github",
     "base_url": "https://raw.githubusercontent.com/{repo}/main",
     "download_prefix": ""},
    {"name": "ghfast",
     "base_url": "https://ghfast.top/https://raw.githubusercontent.com/{repo}/main",
     "download_prefix": "https://ghfast.top/"},
    {"name": "jsdelivr",
     "base_url": "https://cdn.jsdelivr.net/gh/{repo}@main",
     "download_prefix": ""},
)


def default_config():
    """内置默认配置。

    ★ repository 故意留空 —— 没填就是**不检查更新**, 免得对着一个瞎猜的仓库
      发请求(尤其在企业网里, 无谓的外联会引来安全告警)。
    """
    return {
        "enabled": True,
        "repository": "",                 # owner/repo
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
    for k in ("enabled", "repository", "version_file", "check_interval_hours"):
        if k in user and user[k] is not None:
            out[k] = user[k]
    mirrors = user.get("mirrors")
    if isinstance(mirrors, list):
        cleaned = [m for m in mirrors if isinstance(m, dict) and m.get("base_url")]
        if cleaned:
            out["mirrors"] = cleaned
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
    mirror: MirrorResult = None      # 命中版本清单的那个镜像
    force: bool = False              # 本地版本低于 min_version -> 强制更新


def is_direct_github(base_url):
    """是否 GitHub 直连(权威无缓存)。

    ★ 必须比 hostname, 不能子串匹配 —— 见模块注释 ②
    """
    try:
        return urlparse(str(base_url)).hostname == "raw.githubusercontent.com"
    except Exception:
        return False


def build_url(mirror, version_file):
    return f"{mirror.base_url}/{version_file}"


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
    """并发测所有镜像, 按"可用优先、延迟升序"返回"""
    mirrors = list(mirrors or [])
    if not mirrors:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=len(mirrors)) as pool:
        futs = {pool.submit(_test_one_mirror, m, version_file, timeout): m
                for m in mirrors}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:                       # 线程内兜底
                m = futs[fut]
                results.append(MirrorResult(
                    m.get("name") or m.get("base_url", ""), m.get("base_url", ""),
                    m.get("download_prefix", ""), -1.0, False))
    results.sort(key=lambda r: (not r.success,
                                r.latency_ms if r.latency_ms > 0 else 1e9))
    return results


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
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress_cb:
                    el = max(time.monotonic() - t0, 1e-6)
                    speed = got / el
                    if total > 0:
                        progress_cb(got, total, f"{speed / 1048576:.1f} MB/s")
                    else:
                        progress_cb(got, 0, f"{got / 1048576:.1f} MB")
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
    mirrors = []
    for m in conf.get("mirrors") or []:
        base = str(m.get("base_url") or "").replace("{repo}", repo)
        if base:
            mirrors.append({"name": m.get("name") or base, "base_url": base,
                            "download_prefix": str(m.get("download_prefix") or "")})
    if not mirrors:
        return CheckResult("error", "更新配置里没有可用的镜像站")

    try:
        ranked = race_mirrors(mirrors, version_file)
        got = fetch_version_info(ranked, version_file)
        if not got:
            return CheckResult("error", "所有镜像都取不到版本清单")
        info, mirror = got
        if not is_newer(info.version, cur):
            return CheckResult("up_to_date", f"已是最新({cur})", info, mirror)
        # 本地版本低于 min_version -> 强制更新(不允许继续用旧版)
        force = bool(info.min_version) and is_newer(info.min_version, cur)
        return CheckResult("has_update",
                           f"发现新版本 {info.version}(当前 {cur})"
                           + ("，需强制更新" if force else ""),
                           info, mirror, force)
    except Exception as e:
        return CheckResult("error", f"检查更新失败: {e}")
