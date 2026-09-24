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


def _launch_check(exe, app_dir, wait=_LAUNCH_WAIT):
    """offscreen 真启动一次: 活过 wait 秒 = 初始化没问题(缺 DLL 会当场崩)。

    同时确认"首次铺资源"生效: 启动后 exe 旁应有 config/(缺才补的默认文件)。
    """
    print(f"② 启动验证(offscreen, 最多等 {wait}s, onefile 首次解压会慢)...")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
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
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        print("   OK 进程稳定运行, 未被终止")
        print(f"   {'OK' if seeded else '[WARN]'} 首次铺资源(config/config.yaml): "
              + ("已生成" if seeded else "未见生成(可能本就存在)"))
        return [], seeded

    out = b""
    try:
        out = proc.stdout.read() or b""
    except Exception:
        pass
    tail = out.decode("utf-8", "replace").strip().splitlines()[-6:]
    return [f"启动后自行退出(returncode={proc.returncode}); 输出尾部: {tail}"], False


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist",
                                                             "AutoTest.exe")
    exe = os.path.abspath(exe)
    app_dir = os.path.dirname(exe)
    if not os.path.isdir(app_dir):
        print(f"[FAIL] 目录不存在: {app_dir}\n       先构建: "
              f"pyinstaller AutoTest.spec")
        return 1

    print(f"验证产物: {exe}")
    bad = _static_checks(exe)
    launch_bad, _seeded = _launch_check(exe, app_dir)
    ok = not bad and not launch_bad
    print("\n" + ("全部通过" if ok else "存在问题, 见上面 [FAIL]"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
