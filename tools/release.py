# -*- coding: utf-8 -*-
"""一键发布: 校验打包产物 -> 传 GitHub Release -> 更新 version.json -> 推 tag。

用法:
    python tools/release.py --version 1.1 --dry-run    # 先预览将做什么(不碰网络)
    python tools/release.py --version 1.1              # 正式发布
    python tools/release.py --version 1.1 --notes "补充说明"

流程(参考 TB_Import_tool 的 release.py, 按本项目改造):
  ① 版本一致性: VERSION == core/version.py == CHANGELOG 最新节, 不一致直接拒绝
     (三处只改了一处就发版, 用户的"检查更新"会永远判断错)
  ② 产物校验: dist/AutoTest.exe(onefile, 发布物就是一个 exe)
  ③ sha256 -> 创建 Release(v{ver}) -> 上传 asset
  ④ 写 version.json(仓库根) -> commit -> push -> tag v{ver} -> push tag
     (OTA 读的就是仓库根这份 version.json)

前置: git push 权限(SSH); token 走 gh CLI, 或 GITHUB_TOKEN / ~/.github_token。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.version import __version__ as CODE_VERSION          # noqa: E402

API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"
UA = "AutoTest-Release"


# ── 纯逻辑(可测) ──

def parse_repo_from_remote(url):
    """git remote 地址 → owner/repo。支持 SSH/HTTPS 两种形态; 解析不出返回 ""。"""
    url = str(url or "").strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


def load_changelog_section(path, version):
    """CHANGELOG 里 [version] 那一节的 (日期, 正文); 没有该节返回 ("", "")。

    版本头格式: `## [1.1] - 2026-10-01`(与现有 CHANGELOG.md 一致)。
    """
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return "", ""
    pat = re.compile(r"^## \[" + re.escape(version) + r"\] - (\S+)\s*$", re.M)
    m = pat.search(text)
    if not m:
        return "", ""
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    body = rest[:nxt.start()] if nxt else rest
    return m.group(1), body.strip()


def check_version_consistency(root, version):
    """三处版本必须一致, 返回错误信息列表(空=通过)。

    ★ 这是发版闸门: core/version.py(运行时读的)、VERSION 文件(打进包里给更新
      bat 验证)、CHANGELOG(更新说明的来源) —— 只改一处就发版, 轻则界面版本号
      与实际不符, 重则"检查更新"永远判错。
    """
    errs = []
    vfile = os.path.join(root, "VERSION")
    try:
        v = open(vfile, encoding="utf-8").read().strip()
        if v != version:
            errs.append(f"VERSION 文件是 {v!r}, 与 --version {version!r} 不一致")
    except OSError:
        errs.append("根目录没有 VERSION 文件")
    if CODE_VERSION != version:
        errs.append(f"core/version.py 的 __version__ 是 {CODE_VERSION!r}, "
                    f"与 --version {version!r} 不一致")
    _date, notes = load_changelog_section(os.path.join(root, "CHANGELOG.md"), version)
    if not notes:
        errs.append(f"CHANGELOG.md 里没有 [ {version} ] 这一节(更新说明无从取)")
    return errs


def find_dist_exe(dist_dir):
    """定位并校验打包产物(目录模式: dist/AutoTest/AutoTest.exe + _internal\\)。

    校验 exe 的 MZ 头与载荷目录是否在 —— 打包失败/产物不完整必须在这里拦下,
    而不是发个坏包。
    """
    exe = os.path.join(dist_dir, "AutoTest", "AutoTest.exe")
    if not os.path.isfile(exe):
        raise FileNotFoundError(
            f"{exe} 不存在(先跑构建: build.bat 或 pyinstaller AutoTest.spec)")
    with open(exe, "rb") as f:
        if f.read(2) != b"MZ":
            raise ValueError(f"{exe} 不是 Windows 可执行文件")
    if not os.path.isdir(os.path.join(os.path.dirname(exe), "_internal")):
        raise ValueError(f"{os.path.dirname(exe)} 下没有 _internal\\ —— 目录模式打包没生效")
    return exe


#: 打进 zip 的**程序文件**: exe + _internal\(其余是用户数据, 绝不进包)
_PACKAGE_ITEMS = ("AutoTest.exe", "_internal")

#: 载荷(_internal)的合理下限, 防止把半截产物发出去(与 verify_package 一致)
_MIN_PAYLOAD_MB = 150


def make_package_zip(version, dist_dir, out_dir=None):
    """把目录模式产物打成分发包 `AutoTest_v{ver}.zip`; 返回 zip 路径。

    ★ 只装 `AutoTest.exe` + `_internal\\` —— 用户数据(config/ Test_cases/ Test_img/
      backups/ reports/)与它们在同一个目录里, 一旦整目录打包就会把**使用者的数据**
      发出去, 也会在更新时覆盖掉别人的数据。
    ★ zip 内的布局 = 解压后直接可运行(exe 与 _internal 在根), 更新脚本据此做目录切换。
    """
    exe = find_dist_exe(dist_dir)
    app_dir = os.path.dirname(exe)
    payload = os.path.join(app_dir, "_internal")
    mb = sum(os.path.getsize(os.path.join(b, f))
             for b, _d, fs in os.walk(payload) for f in fs) / 1048576
    if mb < _MIN_PAYLOAD_MB:
        raise ValueError(f"_internal\\ 只有 {mb:.0f}MB(低于 {_MIN_PAYLOAD_MB}MB), "
                         f"像是没打全, 拒绝打包")
    out_dir = out_dir or dist_dir
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"AutoTest_v{version}.zip")
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for item in _PACKAGE_ITEMS:
            src = os.path.join(app_dir, item)
            if os.path.isfile(src):
                z.write(src, item)
                n += 1
            else:
                for base, _dirs, files in os.walk(src):
                    for fn in files:
                        full = os.path.join(base, fn)
                        rel = os.path.relpath(full, app_dir).replace(os.sep, "/")
                        z.write(full, rel)
                        n += 1
    return zip_path


def release_download_url(repo, version, asset):
    """Release 资产的下载地址(= 写进 version.json、客户端 OTA 去下的那一条)。"""
    return (f"https://github.com/{repo}/releases/download/"
            f"v{version}/{quote(asset)}")


def stage_asset_for_gh(exe_path, asset_name):
    """把产物摆成**资产同名**的临时文件, 供 gh CLI 上传; 返回该路径。

    ★ 为什么必须这一步: `gh release create <tag> <文件>` 是拿**文件的 basename**
      当资产名的, 直接传 `dist/AutoTest.exe` 就会传成 `AutoTest.exe` —— 而
      version.json 里写的是 `AutoTest_v{ver}.exe`, 客户端按它下载必然 404。走 API
      分支时资产名是我们自己指定的(所以从前没暴露), 一旦机器上装了 gh 就会踩。
    ★ 名字已经一致就原样返回, 不白复制 208MB。
    """
    if os.path.basename(exe_path) == asset_name:
        return exe_path
    d = tempfile.mkdtemp(prefix="release_asset_")
    dst = os.path.join(d, asset_name)
    shutil.copy2(exe_path, dst)
    return dst


def _gh_release_exists(tag):
    """该 tag 是否已有 Release(gh release view 的退出码即答案)"""
    try:
        r = subprocess.run(["gh", "release", "view", tag], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def upload_via_gh(repo, version, asset, exe_path, notes):
    """用 gh CLI 建 Release 并上传资产; 返回下载地址(与 version.json 里写的一致)。

    ★ 已存在的 Release 要**复用**而不是撞死: `gh release create` 遇到同名 tag 直接
      报错退出, 而 API 分支对 422 是复用 —— 两条路行为必须一致(v1.4 发布时就遇到过
      "Release 已存在、只是没资产"的情形)。
    """
    tag = f"v{version}"
    staged = stage_asset_for_gh(exe_path, asset)
    try:
        if _gh_release_exists(tag):
            subprocess.run(["gh", "release", "upload", tag, staged, "--clobber"],
                           cwd=ROOT, check=True)
        else:
            subprocess.run(["gh", "release", "create", tag, "--target", "master",
                            "--title", tag, "--notes", notes, staged],
                           cwd=ROOT, check=True)
    finally:
        if staged != exe_path:                 # 临时副本用完即删(208MB 别留着)
            shutil.rmtree(os.path.dirname(staged), ignore_errors=True)
    return release_download_url(repo, version, asset)


def _api_get(url, token, timeout=30):
    with _request_json(url, token, timeout=timeout) as r:
        return json.load(r)


def check_ci_green(repo, token, sha, wait_seconds=1200, poll_seconds=20, log=print):
    """★ 发版闸门: 这个提交必须**跑过 CI 且全绿**(用户要求 2026-10-10)。

    返回 (是否放行, 说明)。发出去的版本收不回来, 而 CI 是唯一能证明"换台干净机器也跑得通"
    的环节 —— 所以这里**fail-closed**: 查不到、还在跑、有失败, 一律不放行。
      ① 本地 HEAD 还没推上去 -> 先 push(不推就没有它的 CI 记录, 门永远等不到)
      ② 轮询该 sha 的运行记录, 还在跑就等(最多 wait_seconds 秒)
      ③ 全绿 -> 放行; 有失败或超时 -> 拒绝, 并把运行页地址带出来方便定位
    """
    if not token:
        return False, "没有 GitHub token, 查不了 CI(设 GITHUB_TOKEN 或 ~/.github_token)"
    # ① 先确保这个提交推上去了
    try:
        r = subprocess.run(["git", "ls-remote", "origin", "master"], cwd=ROOT,
                           capture_output=True, text=True, timeout=120)
        if sha not in (r.stdout or ""):
            log(f"   本地 HEAD({sha[:7]}) 还没推到 origin/master, 先推上去让 CI 跑")
            _git(["push", "origin", "master"])
    except Exception as e:
        return False, f"确认远端状态失败: {e}"

    url = f"{API}/repos/{repo}/actions/runs?head_sha={sha}&per_page=50"
    deadline = time.monotonic() + max(0, wait_seconds)
    last = ""
    while True:
        try:
            runs = _api_get(url, token).get("workflow_runs") or []
        except Exception as e:
            return False, f"查询 CI 运行记录失败: {type(e).__name__}: {e}"
        if runs:
            running = [x for x in runs if x.get("status") != "completed"]
            bad = [x for x in runs
                   if x.get("status") == "completed"
                   and x.get("conclusion") != "success"]
            if bad:
                x = bad[0]
                return False, (f"CI 未通过: {x.get('name')} #{x.get('run_number')} "
                               f"结论={x.get('conclusion')}\n         {x.get('html_url')}")
            if not running:
                names = ", ".join(f"{x.get('name')}#{x.get('run_number')}" for x in runs)
                return True, f"CI 全绿({names})"
            last = f"{len(running)} 个运行还在跑"
        else:
            last = "还没有该提交的 CI 记录"
        if time.monotonic() >= deadline:
            return False, (f"等了 {wait_seconds} 秒仍未拿到绿灯({last}) —— "
                           f"先确认 CI 是否正常触发: {ROOT}/.github/workflows/tests.yml")
        log(f"   {last}, 等 {poll_seconds} 秒再看…")
        time.sleep(poll_seconds)


def run_post_release_verify(version, deep=False):
    """发版最后一步: 跑"用户那条链路"的校验; 返回 (是否全过, 输出文本)。

    ★ 为什么接进发版流程: 发布脚本只保证"东西传上去了", 而用户能不能按 version.json
      拿到**内容正确**的包是另一回事(资产名对不上、CDN 还没生效、sha 写错……)。
      2026-10-09 就出现过"Release 建好了、资产还没传完 68 分钟"的状态 —— 那期间
      version.json 若已生效, 用户点更新就是 404。
    """
    cmds = [[sys.executable, os.path.join(ROOT, "tools", "verify_release.py"),
             "--version", version]]
    if deep:
        cmds.append([sys.executable,
                     os.path.join(ROOT, "tools", "verify_update_download.py")])
    outs, ok = [], True
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=3600)
        outs.append(f"$ {' '.join(os.path.basename(c) for c in cmd)}\n"
                    f"{r.stdout.strip()}")
        if r.returncode != 0:
            ok = False
            outs[-1] += f"\n{(r.stderr or '').strip()}"
    return ok, "\n".join(outs)


def compute_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 网络与 git(薄封装) ──

def gh_available():
    try:
        subprocess.run(["gh", "--version"], capture_output=True, timeout=15)
        return True
    except Exception:
        return False


def get_token():
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        return tok
    p = os.path.expanduser("~/.github_token")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _request_json(url, token, method="GET", data=None, ctype="application/json",
                  timeout=(10, 600)):
    # ★ urllib 的 timeout 只接受**单个数字**(socket 级 per-op 超时), 不像 requests
    #   接受 (连接, 读取) 元组 —— 传元组会在建连时 TypeError(实测踩过)。
    #   取元组的较大值: 上传大文件时每个收发操作都可能持续较久。
    t = max(timeout) if isinstance(timeout, (tuple, list)) else timeout
    req = urllib.request.Request(
        url, method=method,
        data=data.encode("utf-8") if isinstance(data, str) else data,
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": UA, "Content-Type": ctype})
    return urllib.request.urlopen(req, timeout=t)


def create_release(repo, token, version, notes, branch):
    """创建 Release; 已存在(422)则复用。返回 release_id。"""
    body = json.dumps({"tag_name": f"v{version}", "target_commitish": branch,
                       "name": f"v{version}", "body": notes,
                       "draft": False, "prerelease": False})
    try:
        with _request_json(f"{API}/repos/{repo}/releases", token, "POST", body) as r:
            return json.load(r)["id"]
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise
    with _request_json(f"{API}/repos/{repo}/releases/tags/v{version}", token) as r:
        return json.load(r)["id"]


def _upload_stream(url, token, path, timeout=600):
    """流式上传: 把**打开的文件对象**交给 urllib, 由它按块发送。

    ★ 不能 `open(path,'rb').read()` 一次性读进内存再 POST: 包有 208MB, 发版进程 RSS
      实测 233MB, 而且断在 90% 就得从头再来。传文件对象时 http.client 会分块写 socket,
      内存里只有几 KB。
    ★ **必须显式带 Content-Length**: 少了它 GitHub 直接回
      `400 {"message":"Bad Content-Length"}`(实测 2026-10-10, 发 v1.5 时被拒了三次)。
      urllib 对文件对象不一定替你算(算不出就改用 chunked, 而上传接口不接受 chunked),
      所以这里自己按文件大小写上 —— 试出来的, 不是推出来的。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        req = urllib.request.Request(
            url, data=f, method="POST",
            headers={"Authorization": f"token {token}", "User-Agent": UA,
                     "Content-Type": "application/octet-stream",
                     "Content-Length": str(size),
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["browser_download_url"]


def upload_asset(repo, token, release_id, path, name, attempts=3):
    """上传 asset(先删同名旧文件, 便于重发); 返回下载 URL。

    ★ 带重试: 208MB 传几分钟, 网络抖一下就白传 —— 失败重来比让人手工重发便宜。
    """
    with _request_json(f"{API}/repos/{repo}/releases/{release_id}/assets",
                       token) as r:
        assets = json.load(r)
    for a in assets or []:
        if a.get("name") == name:
            req = urllib.request.Request(
                f"{API}/repos/{repo}/releases/assets/{a['id']}", method="DELETE",
                headers={"Authorization": f"token {token}", "User-Agent": UA})
            urllib.request.urlopen(req, timeout=(10, 60))
            print(f"  [DEL] 旧 asset {name} 已删除")
    url = (f"{UPLOAD}/repos/{repo}/releases/{release_id}/assets"
           f"?name={quote(name)}")
    last = None
    for i in range(1, max(1, attempts) + 1):
        try:
            return _upload_stream(url, token, path)
        except Exception as e:
            last = e
            print(f"  [RETRY {i}/{attempts}] 上传失败: "
                  f"{type(e).__name__}: {e}")
            if i < attempts:
                time.sleep(2 * i)
    raise RuntimeError(f"上传 {name} 连续 {attempts} 次失败: {last}")


def update_version_json_and_tag(repo, version, sha256, download_url, notes):
    """写仓库根 version.json -> commit -> push -> tag v{ver} -> push tag。

    ★ 注意: push 会把本地 master 一起推上去 —— 发版本就该发布已提交的代码,
      但**未提交的改动不会**被包含(脚本会先拒绝脏工作区)。
    """
    data = {"version": version, "sha256": sha256, "download_url": download_url,
            "release_date": __import__("time").strftime("%Y-%m-%d"),
            "release_notes": notes, "min_version": "1.0"}
    vpath = os.path.join(ROOT, "version.json")
    with open(vpath, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    _git(["add", "version.json"])
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff.returncode != 0:
        _git(["commit", "-m", f"release v{version}: update version.json"])
    else:
        print("  [GIT] version.json 无变化, 跳过 commit")
    _git(["tag", "-f", f"v{version}"])
    # ★ 分两步推, 且 tag 要**强推**: create_release() 走 GitHub API 建 Release 时,
    #   GitHub 已经**自动创建了同名 tag**(指向当时的 master HEAD); 而本地这个 tag
    #   指向"含 version.json 的提交" —— 一起推会被 "already exists" 整条拒绝
    #   (v1.1/v1.2 两次发布都死在这里, 且 git 的 stderr 被吞掉看不出原因)。
    _git(["push", "origin", "master"])
    _git(["push", "-f", "origin", f"v{version}"])


def _git(args):
    r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    if r.returncode != 0:
        # ★ 必须把 git 自己的 stderr 带出来: 只抛 CalledProcessError 的话,
        #   "tag already exists" 这类真实原因全被吞掉, 排查只能靠手动重跑
        raise RuntimeError(
            f"git {' '.join(args)} 失败(退出码 {r.returncode}):\n"
            f"{(r.stderr or r.stdout or '').strip()}")
    return r.stdout


def ensure_clean_tree():
    """工作区必须干净(允许未跟踪文件) —— 发版内容必须等于已提交内容。"""
    r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.stdout.strip():
        raise SystemExit("[ERROR] 工作区有未提交改动, 先提交再发版:\n"
                         + r.stdout)


# ── 主流程 ──

def main(argv=None):
    # ★ 输出编码兜底: 英文 Windows(cp1252)下打印中文会 UnicodeEncodeError 崩掉;
    #   重定向到文件/管道时同样按 ANSI 代码页, 与有没有控制台无关(CI 上实测全崩)
    from core.console import force_utf8_stdout
    force_utf8_stdout()
    # ★ 输出重定向到文件时 stdout 是**块缓冲**: 208MB 要传几分钟, 日志一直空白, 看着
    #   像卡死(实测被骗过)。改行缓冲, 进度实时可见。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="校验产物、打分发 zip 并发布一个版本")
    ap.add_argument("--version", required=True, help="要发布的版本号, 如 1.1")
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"),
                    help="产物目录(内含 AutoTest.exe, 默认 dist/)")
    ap.add_argument("--asset", default="",
                    help="asset 文件名(默认 AutoTest_v{ver}.exe —— onefile 发布物就是 exe)")
    ap.add_argument("--notes", default="", help="更新说明(默认取 CHANGELOG 该节)")
    ap.add_argument("--min-version", default="1.0", help="低于此版本强制更新")
    ap.add_argument("--ci-check", action=argparse.BooleanOptionalAction, default=True,
                    help="发版前要求该提交 CI 全绿(默认开; --no-ci-check 跳过)")
    ap.add_argument("--ci-wait", type=int, default=1200,
                    help="等 CI 出结论的最长秒数(默认 1200)")
    ap.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                    help="发布后校验用户那条链路(默认开; --no-verify 关掉)")
    ap.add_argument("--deep", action="store_true",
                    help="连「整包真下一遍」也校验(几分钟, 需网络)")
    ap.add_argument("--repo", default="", help="owner/repo(默认从 git remote 解析)")
    ap.add_argument("--pack-only", action="store_true",
                    help="只校验产物并打成分发 zip(不联网、不改 git; build.bat 用)")
    ap.add_argument("--dry-run", action="store_true", help="只打印将做什么")
    args = ap.parse_args(argv)

    # ① 版本一致性闸门
    errs = check_version_consistency(ROOT, args.version)
    if errs:
        for e in errs:
            print(f"[ERROR] {e}")
        return 1

    # ② 产物校验 + 打分发包(目录模式: exe + _internal\ -> zip)
    asset = args.asset or f"AutoTest_v{args.version}.zip"
    print(f"[1/4] 校验产物并打包: {args.dist}")
    if args.dry_run:
        if not os.path.isdir(args.dist):
            print(f"  [dry-run] {args.dist} 不存在, 实际运行会在这里失败")
        exe_path = os.path.join(args.dist, asset)
    else:
        find_dist_exe(args.dist)                  # 产物不完整 -> 直接抛, 不发坏包
        exe_path = make_package_zip(args.version, args.dist)
        print(f"  发布包 {os.path.getsize(exe_path) / 1048576:.0f} MB"
              f" ({os.path.basename(exe_path)})")
    if args.pack_only:
        print(f"完成(只打包, 不联网不发版): {exe_path}")
        return 0

    # ③ 更新说明: 默认取 CHANGELOG 对应节
    date, notes = load_changelog_section(os.path.join(ROOT, "CHANGELOG.md"),
                                         args.version)
    notes = args.notes or notes or f"v{args.version} release"
    print(f"[2/4] 更新说明({date or '无日期'}):\n  {notes[:200]}"
          + ("…" if len(notes) > 200 else ""))

    # ④ 目标仓库
    repo = args.repo
    if not repo:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                           capture_output=True, text=True)
        repo = parse_repo_from_remote(r.stdout)
    if not repo:
        print("[ERROR] 解析不出 owner/repo(--repo 或 git remote origin)")
        return 1

    print(f"[3/4] 发布到 {repo} (v{args.version}, 分支 master)"
          + ("  [dry-run: 不执行]" if args.dry_run else ""))
    if not args.dry_run:
        if args.ci_check:
            print("[0/5] CI 闸门: 这个提交必须跑过 CI 且全绿")
            ok, msg = check_ci_green(repo, get_token(),
                                     _git(["rev-parse", "HEAD"]).strip(),
                                     wait_seconds=args.ci_wait)
            print("   " + msg)
            if not ok:
                print("\n[ERROR] CI 未通过, 拒绝发版 —— 发出去的版本收不回来。")
                print("        修好后重新提交推送, 等 CI 绿了再发;")
                print("        确有必要时用 --no-ci-check 跳过(请先想清楚为什么)。")
                return 1
        ensure_clean_tree()
        if gh_available():
            download_url = upload_via_gh(repo, args.version, asset, exe_path, notes)
        else:
            token = get_token()
            if not token:
                print("[ERROR] 没有 gh CLI, 也找不到 token"
                      "(GITHUB_TOKEN / ~/.github_token)")
                return 1
            rid = create_release(repo, token, args.version, notes, "master")
            download_url = upload_asset(repo, token, rid, exe_path, asset)
        print(f"  下载地址: {download_url}")

        print("[4/4] 更新 version.json + tag")
        update_version_json_and_tag(repo, args.version, compute_sha256(exe_path),
                                    download_url, notes)
        if args.verify:
            print("[5/5] 校验用户那条链路(取清单 -> sha256 -> 资产 -> MZ 头)...")
            ok, out = run_post_release_verify(args.version, deep=args.deep)
            print(out)
            if not ok:
                print("\n[ERROR] 发布后校验未通过! version.json 可能已生效而资产不可用"
                      "(用户点更新就是 404)。\n        请立刻检查 Release 资产; 必要时"
                      "先回退 version.json 再重新发布。")
                return 1
        print("\n完成。已装旧版的用户会在下次检查更新时收到这个版本。")
    else:
        print("[4/4] [dry-run] 将写 version.json 并 push master + tag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
