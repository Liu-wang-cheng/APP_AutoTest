# -*- coding: utf-8 -*-
"""打包产物验证 —— 打完包后跑一遍, 确认"能启动"且"关键能力没丢"。

    python tools/verify_package.py [dist/AutoTest.exe]

★ 为什么不能只看"exe 起来了"
  打包最常见的失效是**能力静默缺失**: OCR 的 onnx 模型没被收集 → 程序照常启动、
  照常跑用例, 只是识别不到任何文字(而 OCR 正是插件页读不到文本时唯一的兜底);
  Qt 的某个 DLL 被砍多了 → 界面某个控件画不出来。这些都不会让进程崩, 只会让
  真机测试悄悄给出错的结果。

  onefile 下模型/DLL 都封装在 exe 里, 静态检查够不着 —— 所以这里:
  ① 静态: exe 存在、体积合理(突然变小 = 依赖没打全)、MZ 头;
  ② 动态: offscreen 真启动一次, 进程稳定运行到超时(缺 DLL/缺插件会当场崩);
  ③ 启动后会顺带验证"首次铺资源": exe 旁应生成 config/(缺才补的默认文件)。
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: onefile 产物体积的合理下限(MB)。Qt 精简后约 250MB; 掉到几十 MB 说明
#: 依赖没打全(比如某个 hook 失效把大头丢了), 启动也大概率缺 DLL
_MIN_SIZE_MB = 100
#: 启动稳定判定窗口(秒)。onefile 每次启动要先解压载荷, 给足余量
_LAUNCH_WAIT = 40


def _static_checks(exe):
    bad = []
    if not os.path.isfile(exe):
        return [f"产物不存在: {exe}"]
    size_mb = os.path.getsize(exe) / 1048576
    if size_mb < _MIN_SIZE_MB:
        bad.append(f"exe 只有 {size_mb:.0f}MB(低于合理下限 {_MIN_SIZE_MB}MB) —— "
                   f"依赖可能没打全")
    with open(exe, "rb") as f:
        if f.read(2) != b"MZ":
            bad.append("不是 Windows 可执行文件(缺少 MZ 头)")
    print(f"① 静态: {size_mb:.0f}MB "
          + ("OK" if not bad else "有问题"))
    for b in bad:
        print(f"   [FAIL] {b}")
    return bad


def _mei_dirs():
    """%TEMP% 下 PyInstaller 一次性解压目录(_MEI*)的名字集合"""
    import tempfile
    try:
        return {n for n in os.listdir(tempfile.gettempdir())
                if n.startswith("_MEI")}
    except OSError:
        return set()


def _clean_mei_leftovers(before):
    """删掉**本次启动新产生**的 _MEI* 解压目录; 返回 (释放字节, 残留名字)。

    ★ 为什么必须自己收: onefile 每次启动都把约 400MB 载荷解压到 %TEMP%\\_MEIxxxx,
      正常退出时 bootloader 会清掉 —— 但**硬杀**(taskkill /F, 本脚本就是这么收尾的)
      跳过清理。实测: 两天里攒了 6 个 403MB 的目录, 合计 2.36GB。
    ★ 只删"启动前不存在 + 里面有本程序打进包的 VERSION"的: 别的 PyInstaller 程序
      也叫 _MEI*, 一律不碰。杀完句柄释放有延迟, 所以重试几次。
    """
    import shutil
    import tempfile
    import time
    root = tempfile.gettempdir()
    freed, left = 0, []
    for name in sorted(_mei_dirs() - before):
        p = os.path.join(root, name)
        if not os.path.isfile(os.path.join(p, "VERSION")):
            left.append(f"{name}(不是本程序, 未删)")
            continue
        size = sum(os.path.getsize(os.path.join(b, f))
                   for b, _d, fs in os.walk(p) for f in fs)
        for _ in range(5):
            shutil.rmtree(p, ignore_errors=True)
            if not os.path.exists(p):
                freed += size
                break
            time.sleep(0.5)
        else:
            left.append(f"{name}(删不掉, 句柄未释放)")
    return freed, left


def _launch_check(exe, app_dir, wait=_LAUNCH_WAIT):
    """offscreen 真启动一次: 活过 wait 秒 = 初始化没问题(缺 DLL 会当场崩)。

    同时确认"首次铺资源"生效: 启动后 exe 旁应有 config/(缺才补的默认文件)。
    """
    print(f"② 启动验证(offscreen, 最多等 {wait}s, onefile 首次解压会慢)...")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    mei_before = _mei_dirs()          # 收尾时要认准"本次新增"的解压目录
    try:
        proc = subprocess.Popen([exe], cwd=app_dir, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        return [f"启动失败: {e}"], False

    seeded = False
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        # 铺资源发生在日志初始化前后, 轮询观察即可(不依赖时序)
        if not seeded and os.path.isfile(os.path.join(app_dir, "config",
                                                      "config.yaml")):
            seeded = True
        time.sleep(1)

    if proc.poll() is None:
        # ★ 必须杀**整棵进程树**: onefile 是"bootloader 父进程 + 真正的应用子进程"
        #   两段结构, terminate() 只结束父进程, 用 offscreen 启动的应用子进程会活下来
        #   变成"无窗口僵尸"—— 实测就这样留下过一个跑了 15 天的进程(用户以为程序
        #   没启动, 其实僵尸占着内存、还锁着 config 等文件)。
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=30)
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        print("   OK 进程稳定运行, 已结束(含子进程)")
        print(f"   {'OK' if seeded else '[WARN]'} 首次铺资源(config/config.yaml): "
              + ("已生成" if seeded else "未见生成(可能本就存在)"))
        freed, left = _clean_mei_leftovers(mei_before)
        if freed:
            print(f"   OK 已清理本次启动的一次性解压目录(释放 "
                  f"{freed / 1048576:.0f}MB)")
        for x in left:
            print(f"   [WARN] %TEMP% 残留: {x}")
        return [], seeded

    out = b""
    try:
        out = proc.stdout.read() or b""
    except Exception:
        pass
    tail = out.decode("utf-8", "replace").strip().splitlines()[-6:]
    return [f"启动后自行退出(returncode={proc.returncode}); 输出尾部: {tail}"], False


def main(argv=None):
    # ★ argparse: 裸 sys.argv 时 `--help` 会被当成"要验证的产物路径", 报一堆
    #   "产物不存在: D:\...\--help"(实测 2026-10-10)
    ap = argparse.ArgumentParser(
        description="验证打包产物: 静态检查(大小/MZ 头) + offscreen 真启动一次")
    ap.add_argument("exe", nargs="?",
                    default=os.path.join(ROOT, "dist", "AutoTest.exe"),
                    help="产物路径(默认 dist/AutoTest.exe)")
    args = ap.parse_args(argv)
    exe = os.path.abspath(args.exe)
    app_dir = os.path.dirname(exe)
    if not os.path.isdir(app_dir):
        print(f"[FAIL] 目录不存在: {app_dir}\n       先构建: "
              f"pyinstaller AutoTest.spec")
        return 1

    print(f"验证产物: {exe}")
    bad = _static_checks(exe)
    if bad:
        # ★ 静态就不合格的产物没必要再花几十秒去启动它(还会铺出 config/ 等沙箱数据),
        #   而且"启动验证"那几行输出会让人以为问题在后面(实测: 传个不存在的路径,
        #   它照样去打启动验证, 报的却是"产物不存在")
        print("\n存在问题, 见上面 [FAIL](静态检查没过, 不再启动)")
        return 1
    launch_bad, _seeded = _launch_check(exe, app_dir)
    ok = not launch_bad
    print("\n" + ("全部通过" if ok else "存在问题, 见上面 [FAIL]"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
