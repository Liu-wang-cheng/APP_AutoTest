# -*- coding: utf-8 -*-
r"""自动更新全链路彩排: 让**旧版本自己**走完 检查->下载->校验->解压->替换->重启。

    python tools/rehearse_autoupdate.py --old-zip dist\AutoTest_v1.7.zip \
                                       --new-zip dist\AutoTest_v1.8.zip

做法(不碰线上、不发任何东西):
  ① 把 old-zip 解压成"用户正在用的旧版本" -> 工作目录里的 old_app\
  ② 起一个本地 HTTP 服务冒充更新源(清单 + 分发包都从它下)
  ③ 旧版本的 config 只留这一个本地镜像
  ④ 用一个"扮演旧版本程序"的子进程(本脚本 --as-app)执行**真实代码路径**:
     check_for_update -> download -> verify_sha256 -> validate_update_zip ->
     extract_update -> generate_update_bat -> 起 bat -> 立刻退出(与正式代码
     _apply_update_and_restart 的 os._exit(0) 等价)
  ⑤ 替换脚本等它退出, 做两次瞬时改名 + 换 exe + 启动新版本
  ⑥ 验证: 版本变成新的、旧载荷留在 _internal_old(退路)、残留被清理、新程序能启动

★ 为什么值得留着这个脚本: 更新链是"发出去就收不回来"的部分, 而它没法在 CI 里跑
  (要两个真实产物 + 真跑 cmd)。改了打包/更新相关代码后, 用它跑一遍比读代码可靠。
"""
import argparse
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import updater                                          # noqa: E402


def _app_side(app_dir, port, repo, branch, exe_name):
    """扮演"正在运行的旧版本": 走真实代码路径把更新准备好并交给替换脚本。

    ★ 直接调 updater 的各函数(与 gui/main_window.UpdateDownloadThread.run 同一批),
      只是路径显式传参 —— 脚本不在打包环境里, 不能用 core.driver.DATA_DIR。
    """
    cfg = {"update": {"enabled": True, "repository": repo, "branch": branch,
                      "version_file": "version.json",
                      "mirrors": [{"name": "local",
                                   "base_url": f"http://127.0.0.1:{port}/{{repo}}/{{branch}}",
                                   "download_prefix": ""}]}}
    res = updater.check_for_update(cfg, current=open(
        os.path.join(app_dir, "_internal", "VERSION"), encoding="utf-8").read().strip())
    print(f"[app] 检查更新: {res.status} —— {res.message}", flush=True)
    if res.status != "has_update":
        return 1
    ranked = res.ranked
    dm = updater.pick_download_mirror(ranked)
    prefix = getattr(dm, "download_prefix", "") or ""
    url = updater.apply_download_prefix(res.info.download_url, prefix)
    pkg = os.path.join(app_dir, "_update_download.zip")
    updater.download(url, pkg, progress_cb=lambda d, t, s: None)
    ok = updater.verify_sha256(pkg, res.info.sha256)
    print(f"[app] 下载完成 {os.path.getsize(pkg)/1048576:.0f}MB, sha256 校验: {ok}", flush=True)
    if not ok:
        return 1
    updater.validate_update_zip(pkg, res.info.version)
    print(f"[app] 包校验通过(内含 VERSION={updater.read_zip_version(pkg)})", flush=True)
    updater.extract_update(pkg, os.path.join(app_dir, "_update_extracted"))
    print("[app] 已解压到 _update_extracted", flush=True)
    bat = updater.generate_update_bat(app_dir, os.getpid(), exe_name)
    subprocess.Popen(["cmd", "/c", bat], cwd=app_dir,
                     creationflags=subprocess.CREATE_NEW_CONSOLE
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
    print("[app] 替换脚本已启动, 本进程立刻退出(与 os._exit(0) 等价)", flush=True)
    os._exit(0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="自动更新全链路彩排")
    ap.add_argument("--old-zip", required=True, help="旧版本分发包(如 v1.7 的 zip)")
    ap.add_argument("--new-zip", required=True, help="新版本分发包(如 v1.8 的 zip)")
    ap.add_argument("--exe-name", default="AutoTest.exe")
    ap.add_argument("--repo", default="o/r")
    ap.add_argument("--branch", default="master")
    ap.add_argument("--work", default=os.path.join(tempfile.gettempdir(),
                                                   "autoupdate_rehearsal"))
    ap.add_argument("--as-app", default="", help="内部用: 扮演旧版本程序")
    args = ap.parse_args(argv)

    if args.as_app:
        return _app_side(args.as_app, int(os.environ["REH_PORT"]), args.repo,
                         args.branch, args.exe_name)

    old_zip, new_zip = os.path.abspath(args.old_zip), os.path.abspath(args.new_zip)
    for p in (old_zip, new_zip):
        if not os.path.isfile(p):
            print(f"[FAIL] 找不到 {p}")
            return 1
    shutil.rmtree(args.work, ignore_errors=True)
    app = os.path.join(args.work, "old_app")
    os.makedirs(args.work)
    with zipfile.ZipFile(old_zip) as z:
        z.extractall(app)
    old_ver = open(os.path.join(app, "_internal", "VERSION"), encoding="utf-8").read().strip()
    new_ver = updater.read_zip_version(new_zip)
    print(f"① 旧版本 v{old_ver} 解压到 {app}")
    print(f"   新版本包 v{new_ver} ({os.path.getsize(new_zip)/1048576:.0f}MB)")

    # ② 本地假更新源: 清单 + 分发包
    payload = json.dumps({
        "version": new_ver, "sha256": updater.hashlib.sha256(
            open(new_zip, "rb").read()).hexdigest(),
        "download_url": "http://127.0.0.1/PLACEHOLDER", "release_date": "2026-10-11",
        "release_notes": "彩排用清单", "min_version": "1.0"}, ensure_ascii=False).encode()

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            port = self.server.server_address[1]
            if self.path.rstrip("/").endswith("version.json"):
                body = json.dumps(json.loads(payload), ensure_ascii=False).replace(
                    "http://127.0.0.1/PLACEHOLDER",
                    f"http://127.0.0.1:{port}/dl/{os.path.basename(new_zip)}").encode()
                self._send(body, "application/json")
            elif self.path.rstrip("/").endswith(os.path.basename(new_zip)):
                self._send(open(new_zip, "rb").read(), "application/octet-stream")
            else:
                self.send_response(404)
                self.end_headers()

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"② 本地假更新源: http://127.0.0.1:{port}/(对外报 v{new_ver})")

    # ③ 旧版本的 config 只留本地镜像
    cfg_dir = os.path.join(app, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    src = os.path.join(app, "_internal", "config", "locators.yaml")
    if os.path.isfile(src):
        shutil.copy2(src, os.path.join(cfg_dir, "locators.yaml"))
    with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("app:\n  name: 彩排\n  package: com.test\n"
                "update:\n  enabled: true\n  repository: %s\n  branch: %s\n"
                "  version_file: version.json\n  check_interval_hours: 8\n"
                "  mirrors:\n    - name: local\n"
                "      base_url: http://127.0.0.1:%d/{repo}/{branch}\n"
                "      download_prefix: ''\n" % (args.repo, args.branch, port))

    # ④ 让"旧版本程序"走完整流程(真实代码路径)
    env = dict(os.environ, REH_PORT=str(port))
    print("④ 旧版本开始走更新流程…")
    rc = subprocess.call([sys.executable, os.path.abspath(__file__),
                          "--old-zip", old_zip, "--new-zip", new_zip,
                          "--exe-name", args.exe_name, "--repo", args.repo,
                          "--branch", args.branch, "--work", args.work,
                          "--as-app", app], env=env, cwd=app)
    print(f"   [app] 退出码 {rc}(0=已把活交给替换脚本)")

    # ⑤ 等替换脚本做完(它要等进程退出 + 两次改名 + 启动 + 存活确认)
    print("⑤ 等替换脚本完成…")
    deadline = time.time() + 120
    done = False
    while time.time() < deadline:
        cur = os.path.join(app, "_internal", "VERSION")
        v = open(cur, encoding="utf-8").read().strip() if os.path.isfile(cur) else ""
        if v == new_ver and not os.path.exists(os.path.join(app, "_update.bat")):
            done = True
            break
        time.sleep(1)

    ok = True
    def check(cond, msg):
        nonlocal ok
        print(("   OK  " if cond else "   ★FAIL ") + msg)
        ok = ok and cond

    cur = open(os.path.join(app, "_internal", "VERSION"), encoding="utf-8").read().strip()
    check(done, "替换脚本已收工")
    check(cur == new_ver, f"载荷已升到 v{cur}(期望 v{new_ver})")
    olddir = os.path.join(app, "_internal_old")
    check(os.path.isdir(olddir), "_internal_old 保留(回滚退路)")
    if os.path.isdir(olddir):
        ov = open(os.path.join(olddir, "VERSION"), encoding="utf-8").read().strip()
        check(ov == old_ver, f"_internal_old 里是 v{ov}(期望 v{old_ver})")
    check(os.path.isfile(os.path.join(app, args.exe_name + ".bak")), "exe 备份保留")
    check(not os.path.isdir(os.path.join(app, "_update_extracted")), "_update_extracted 已清理")
    check(not os.path.isfile(os.path.join(app, "_update_download.zip")), "下载包已清理")
    # ⑥ 新版本能启动吗
    exe = os.path.join(app, args.exe_name)
    log = os.path.join(app, "reports", "test.log")
    before = os.path.getsize(log) if os.path.isfile(log) else 0
    t0 = time.perf_counter()
    p = subprocess.Popen([exe], cwd=app, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline:
        if os.path.isfile(log) and os.path.getsize(log) > before:
            break
        if p.poll() is not None:
            break
        time.sleep(0.05)
    dt = time.perf_counter() - t0
    alive = p.poll() is None
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    # 替换脚本启动的那个实例是独立进程, 按镜像名收掉
    subprocess.run(["taskkill", "/F", "/IM", args.exe_name], capture_output=True)
    check(alive, f"升级后的 v{new_ver} 能启动并稳定运行(启动 {dt:.2f} 秒)")

    print("\n结论:", "自动更新全链路通过" if ok else "有 ★FAIL, 见上")
    print("彩排目录(未删, 供查看):", app)
    srv.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
