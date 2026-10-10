# -*- coding: utf-8 -*-
"""端到端: 打包好的程序能不能收到"有新版本"的提示?

做法: 起一个本地 HTTP 服务冒充更新源(返回一个比本地新的版本清单), 把程序的
config 指向它, 启动打包好的程序, 然后看它的运行日志里有没有走到"发现新版本"。

不碰线上仓库、不发任何东西; 只在临时目录里跑一份程序副本。
"""
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

ROOT = r"D:\claude_test\Auto_test"
SRC_APP = os.path.join(ROOT, "dist", "AutoTest")
FAKE_VER = "9.9"


def main():
    if not os.path.isdir(SRC_APP):
        print("[FAIL] 没有打包产物:", SRC_APP)
        return 1
    work = os.path.join(tempfile.gettempdir(), "chk_update_e2e")
    shutil.rmtree(work, ignore_errors=True)
    app = os.path.join(work, "app")
    shutil.copytree(SRC_APP, app)
    # 清掉复制过来的用户数据(用自己的干净 config)
    for d in ("config", "Test_cases", "Test_img", "Test_preconditions",
              "reports", "backups"):
        shutil.rmtree(os.path.join(app, d), ignore_errors=True)

    # ① 本地假更新源: /{repo}/{branch}/version.json -> 一个更高版本
    payload = json.dumps({
        "version": FAKE_VER, "sha256": "0" * 64,
        "download_url": "http://127.0.0.1/whatever.zip",   # 只测"检查", 不真下
        "release_date": "2026-10-11", "release_notes": "假清单(端到端检查用)",
        "min_version": "1.0"}, ensure_ascii=False).encode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.rstrip("/").endswith("version.json"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"① 本地假更新源: http://127.0.0.1:{port}/  (对外报 v{FAKE_VER})")

    # ② 给程序配一个只走本地源的 config
    cfg_dir = os.path.join(app, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    shutil.copy2(os.path.join(app, "_internal", "config", "locators.yaml"),
                 os.path.join(cfg_dir, "locators.yaml"))
    with open(os.path.join(cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(
            "app:\n  name: 测试\n  package: com.test\n"
            "device:\n  default: ''\n"
            "update:\n  enabled: true\n  repository: o/r\n  branch: master\n"
            "  version_file: version.json\n  check_interval_hours: 8\n"
            "  mirrors:\n"
            f"    - name: local\n      base_url: http://127.0.0.1:{port}/{{repo}}/{{branch}}\n"
            "      download_prefix: ''\n")
    print("② config 只指向本地源(mirrors 只留 local)")

    # ③ 启动打包好的程序(off-screen), 看日志
    exe = os.path.join(app, "AutoTest.exe")
    log = os.path.join(app, "reports", "test.log")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    t0 = time.time()
    p = subprocess.Popen([exe], cwd=app, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    text = ""
    deadline = time.time() + 45
    while time.time() < deadline:
        if os.path.isfile(log):
            text = open(log, encoding="utf-8", errors="replace").read()
            if "取到版本清单" in text or "取版本清单失败" in text:
                break
        if p.poll() is not None:
            break
        time.sleep(0.3)
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    print(f"③ 程序跑了 {time.time()-t0:.1f} 秒退出, 日志里的更新相关行:")
    for line in text.splitlines():
        if "[更新]" in line:
            print("   ", line.strip())

    ok = False
    local_ver = open(os.path.join(app, "_internal", "VERSION"),
                     encoding="utf-8").read().strip()
    if f"发现新版本 {FAKE_VER}" in text:
        print(f"\n结论: 打包的 v{local_ver} **收到了 v{FAKE_VER} 的更新提示** ✓")
        print("      (弹窗路径: 检查线程 -> _on_update_checked -> 发现新版本弹窗, "
              "日志这行就是它之前的必经点)")
        ok = True
    else:
        print("\n结论: ★没走到「发现新版本」—— 检查链路有问题")
    print("复现目录(未删):", app)
    srv.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
