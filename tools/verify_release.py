# -*- coding: utf-8 -*-
"""发布后校验: 确认**用户那条链路**真的能拿到这一版。

    python tools/verify_release.py --version 1.4

发布工具(tools/release.py)只保证"东西传上去了"; 这个脚本回答的是另一个问题:
客户端按 version.json 去下, 能不能拿到一个**内容正确**的包。四项:
  ① 直连 raw.githubusercontent.com 取 version.json(OTA 取清单就走这条, 带 ?t= 破 CDN 缓存)
  ② version == 期望版本; sha256 == 本地产物(否则用户下到的包与清单对不上, 更新白装)
  ③ download_url HEAD 得 200, 且 content-length 与本地产物一致
  ④ 真拉前 1KB, 确认是 `MZ` 开头的可执行文件(不是镜像报错页/错误页)

只读, 不改任何东西。返回码非 0 = 有项不通过。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "AutoTest-Verify"}


def local_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest(), os.path.getsize(path)


def repo_from_git_remote():
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return ""
    url = (r.stdout or "").strip()
    for pre in ("https://github.com/", "git@github.com:"):
        if url.startswith(pre):
            return url[len(pre):].removesuffix(".git").strip("/")
    return ""


def _get(url, method="GET", extra=None, timeout=30, limit=None):
    """请求并读取响应体; limit=None 表示读全部(别写成 `if limit else b""` —— 那样
    不传 limit 会读回空字节, 清单直接解析失败)。"""
    req = urllib.request.Request(url, method=method, headers={**UA, **(extra or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r, (r.read() if limit is None else r.read(limit))


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布后校验(用户链路)")
    ap.add_argument("--version", required=True, help="期望的版本号, 如 1.4")
    ap.add_argument("--repo", default="", help="owner/repo(默认从 git remote 解析)")
    ap.add_argument("--branch", default="master", help="version.json 所在分支")
    ap.add_argument("--exe", default="", help="本地产物(默认 dist/AutoTest.exe)")
    args = ap.parse_args(argv)

    repo = args.repo or repo_from_git_remote()
    if not repo:
        print("[ERROR] 解析不出 owner/repo(--repo 或 git remote origin)")
        return 1
    exe = args.exe or os.path.join(ROOT, "dist", "AutoTest.exe")
    if not os.path.isfile(exe):
        print(f"[ERROR] 找不到本地产物 {exe}(先构建)")
        return 1
    want_sha, want_size = local_sha256(exe)
    print(f"本地产物: {want_size} 字节, sha256 {want_sha[:16]}…")
    ok = True

    # ① 清单(直连 + 破缓存)
    url = (f"https://raw.githubusercontent.com/{repo}/{args.branch}/version.json"
           f"?t={int(time.time())}")
    try:
        _r, body = _get(url, timeout=20)
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        print(f"[FAIL] 取不到 version.json: {type(e).__name__}: {e}")
        return 1
    print(f"① 清单: version={data.get('version')} date={data.get('release_date')}")
    if data.get("version") != args.version:
        print(f"[FAIL] 远程版本是 {data.get('version')!r}, 期望 {args.version!r}")
        ok = False

    # ② sha256
    if str(data.get("sha256", "")).lower() != want_sha:
        print(f"[FAIL] sha256 不一致: 远程 {str(data.get('sha256'))[:16]}… "
              f"本地 {want_sha[:16]}…")
        ok = False
    else:
        print("② sha256 与本地产物一致")

    # ③ 资产可达 + 大小一致
    dl = str(data.get("download_url") or "")
    print(f"③ 下载地址: {dl}")
    if not dl:
        print("[FAIL] 清单里没有 download_url")
        return 1
    try:
        r, _ = _get(dl, method="HEAD")
        size = int(r.headers.get("content-length") or 0)
        print(f"   HTTP {r.status}, content-length={size}")
        if r.status != 200 or size != want_size:
            print(f"[FAIL] 资产状态或大小不对(期望 {want_size})")
            ok = False
    except urllib.error.HTTPError as e:
        print(f"[FAIL] 资产取不到: HTTP {e.code} —— 客户端会在这一步失败")
        ok = False
    except Exception as e:
        print(f"[FAIL] 资产请求异常: {type(e).__name__}: {e}")
        ok = False

    # ④ 真读一小段, 确认不是错误页
    try:
        _r, head = _get(dl, extra={"Range": "bytes=0-1023"}, limit=1024)
        if head[:2] != b"MZ":
            print(f"[FAIL] 前两字节是 {head[:2]!r}, 不是 Windows 可执行文件")
            ok = False
        else:
            print("④ 前 1024 字节是 MZ 可执行文件")
    except Exception as e:
        print(f"[FAIL] 试读失败: {type(e).__name__}: {e}")
        ok = False

    print("\n结论:", "全部通过 —— 旧版本能检查到 v%s 并下载到正确的包" % args.version
          if ok else "有不通过项, 见上面的 [FAIL]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
