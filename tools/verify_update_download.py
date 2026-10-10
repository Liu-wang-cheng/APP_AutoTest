# -*- coding: utf-8 -*-
"""端到端: 用**程序自己的 OTA 代码**把线上的包真下一次(用户点「立即更新」走的路)。

    python tools/verify_update_download.py            # 版本以线上清单为准

与 tools/verify_release.py 的分工: 那个只查"清单/资产/大小/MZ 头"; 这个真的把
整包拉下来并跑 `updater.download` + `verify_sha256` + `validate_new_exe` —— 镜像选源、
流式下载、加速前缀这些**只在真下载里才暴露**的环节靠它(教训: 直连拉 208MB 实测下不动,
而清单走直连最快, 两者必须分开选源)。

只落临时目录, 不碰程序与用户数据, **不运行**替换脚本。约需几分钟(208MB)。
"""
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import updater                                          # noqa: E402

REPO = os.environ.get("AUTOTEST_REPO") or "Liu-wang-cheng/APP_AutoTest"


def main(argv=None):
    # ★ 必须有 argparse: 这个脚本一跑就是几分钟、会真下 200MB+。原先不认参数 ——
    #   `--help` 会被当成"无参数"直接开始下载(实测 2026-10-10 被自己的测试踩到)。
    ap = argparse.ArgumentParser(
        description="端到端真下一遍线上安装包(用程序自己的 OTA 代码, 走镜像)")
    ap.add_argument("--repo", default=REPO, help=f"owner/repo(默认 {REPO})")
    args = ap.parse_args(argv)
    repo = args.repo
    print(f"仓库: {repo}")
    t0 = time.time()
    raw = (f"https://raw.githubusercontent.com/{repo}/master/version.json"
           f"?t={int(time.time())}")
    with urllib.request.urlopen(raw, timeout=20) as r:
        info = updater.parse_version_info(json.loads(r.read().decode("utf-8")))
    print(f"清单: v{info.version} sha={info.sha256[:16]}…")
    print(f"原始地址: {info.download_url}")

    conf = updater.default_config()
    conf["repository"] = repo
    mirrors = updater.build_mirrors(conf, repo)
    ranked = updater.race_mirrors(mirrors, conf["version_file"])
    print("镜像测速:", ", ".join(
        f"{m.name}{'%.0fms' % m.latency_ms if m.success else '不可达'}" for m in ranked))
    dm = updater.pick_download_mirror(ranked)
    prefix = dm.download_prefix if dm else ""
    url = updater.apply_download_prefix(info.download_url, prefix)
    print(f"下载选源: {dm.name if dm else '无'} (前缀 {prefix or '直连'})")
    print(f"实际下载: {url}")

    tmp = tempfile.mkdtemp(prefix="ota_e2e_")
    dest = os.path.join(tmp, "_update_download.exe")
    last = [0.0]

    def prog(got, total, speed):
        if time.time() - last[0] >= 10:          # 每 10 秒报一次, 别刷屏
            last[0] = time.time()
            pct = f"{got*100//total}%" if total else f"{got/1048576:.0f}MB"
            print(f"  …{pct} {speed}", flush=True)

    updater.download(url, dest, progress_cb=prog)
    size = os.path.getsize(dest)
    print(f"下载完成: {size} 字节, 用时 {int(time.time()-t0)}s "
          f"({size/1048576/max(time.time()-t0,1e-6):.1f} MB/s 平均)")

    ok = True
    if not updater.verify_sha256(dest, info.sha256):
        print("[FAIL] sha256 与清单不符 —— 用户会拿到一个校验不过的包")
        ok = False
    else:
        print("sha256 与清单一致")
    try:
        updater.validate_new_exe(dest)
        print("MZ 头与大小校验通过")
    except ValueError as e:
        print(f"[FAIL] validate_new_exe: {e}")
        ok = False

    exe_name = "AutoTest.exe"
    open(os.path.join(tmp, exe_name), "wb").write(b"MZ" + b"x" * (2 << 20))
    bat = updater.generate_update_bat(tmp, pid=999999, exe_name=exe_name,
                                      probe_seconds=1)
    bt = open(bat, encoding="utf-8").read()
    marker = os.path.join(tmp, updater.TARGET_MARKER)
    print("生成的替换脚本认目标名:", open(marker, encoding="utf-8").read().strip())
    for must in ('ren "%EXE_NAME%" "%EXE_NAME%.bak"',
                 '/FI "IMAGENAME eq %EXE_NAME%" /FO CSV /NH',
                 "正在回滚"):
        if must not in bt:
            print(f"[FAIL] 替换脚本缺少: {must}")
            ok = False
    print("\n结论:", "OTA 下载链路端到端可用" if ok else "有 [FAIL], 见上")
    print("临时目录(未删, 供人工查看):", tmp)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
