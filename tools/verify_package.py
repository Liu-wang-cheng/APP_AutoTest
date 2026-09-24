# -*- coding: utf-8 -*-
"""打包产物验证 —— 打完包后跑一遍, 确认"能启动"且"关键能力没丢"。

    python tools/verify_package.py [dist/AutoTest]

★ 为什么不能只看"exe 起来了"
  打包最常见的失效是**能力静默缺失**: OCR 的 onnx 模型没被收集 → 程序照常启动、
  照常跑用例, 只是识别不到任何文字(而 OCR 正是插件页读不到文本时唯一的兜底);
  Qt 的某个 DLL 被砍多了 → 界面某个控件画不出来。这些都不会让进程崩, 只会让
  真机测试悄悄给出错的结果。所以这里逐项验"能力", 而不是只验"活着"。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 打包后必须存在的关键文件(相对产物根) —— 少了任何一项都意味着一类能力缺失
#: 路径里的 _internal 是 PyInstaller 6.x onedir 的默认布局
_REQUIRED = [
    ("AutoTest.exe", "主程序"),
    ("_internal/PySide6/Qt6Core.dll", "Qt 核心(缺失=界面起不来)"),
    ("_internal/cv2", "OpenCV(缺失=模板匹配/分区识别全废)"),
    ("_internal/onnxruntime", "onnxruntime(缺失=OCR 不可用)"),
]
#: 模型文件: 名字里带这些的必须能找到至少一个(两个包各有一套)
_MODEL_HINTS = ("onnx",)


def _find_models(root):
    """扫产物里的模型文件(onnx)"""
    found = []
    for base, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".onnx"):
                found.append(os.path.relpath(os.path.join(base, f), root))
    return found


def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def check_layout(dist):
    """① 关键文件与模型的静态检查"""
    bad = []
    for rel, why in _REQUIRED:
        if not os.path.exists(os.path.join(dist, rel)):
            bad.append(f"缺少 {rel} —— {why}")
    models = _find_models(dist)
    if not models:
        bad.append("产物里一个 .onnx 模型都没有 —— 打包后 OCR 会**静默失效**"
                   "(程序能启动、能跑, 只是认不出任何文字)")
    return bad, models


def check_launch(dist, wait=25):
    """② 真启动一次: 用 offscreen 跑起来, 等一会儿看它有没有自己退出。

    offscreen 下窗口不显示, 但 Qt 会完整走一遍初始化 —— 缺 DLL / 缺插件都会在这里
    崩掉, 所以"能活过 wait 秒"是有意义的信号。
    """
    exe = os.path.join(dist, "AutoTest.exe")
    if not os.path.isfile(exe):
        return ["AutoTest.exe 不存在, 无法验证启动"]
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        proc = subprocess.Popen([exe], cwd=dist, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        return [f"启动失败: {e}"]
    time.sleep(wait)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        return []                      # 活过了 wait 秒 -> 初始化没问题
    out = b""
    try:
        out = proc.stdout.read() or b""
    except Exception:
        pass
    tail = out.decode("utf-8", "replace").strip().splitlines()[-6:]
    return [f"启动后自行退出(returncode={proc.returncode}); 输出尾部: {tail}"]


def main():
    dist = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "AutoTest")
    dist = os.path.abspath(dist)
    if not os.path.isdir(dist):
        print(f"[FAIL] 产物目录不存在: {dist}\n       先跑: pyinstaller AutoTest.spec")
        return 1

    print(f"验证产物: {dist}")
    print(f"总体积: {_dir_size(dist) / 1048576:.0f} MB\n")

    bad, models = check_layout(dist)
    print(f"① 关键文件: {'OK' if not bad else '有问题'}")
    for b in bad:
        print(f"   [FAIL] {b}")
    print(f"   模型文件 {len(models)} 个: {models[:4]}{' …' if len(models) > 4 else ''}")

    print(f"\n② 启动验证(offscreen, 最多等 25s)...")
    launch_bad = check_launch(dist)
    for b in launch_bad:
        print(f"   [FAIL] {b}")
    if not launch_bad:
        print("   OK 进程稳定运行, 未被终止")

    ok = not bad and not launch_bad
    print("\n" + ("全部通过" if ok else "存在问题, 见上面 [FAIL]"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
