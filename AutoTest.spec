# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置(**onefile 单文件**)。

★ 用户定的形态(2026-09-24): 只交付一个 exe, 双击时 PyInstaller 自动把内部
  文件解压到 %TEMP% 再运行 —— 分发极简, 更新 = 替换一个文件。
  代价是每次启动都有解压开销(411MB 载荷, SSD 上数秒); 因此 upx 保持关闭
  (再压缩只会拖慢启动, 且杀软误报率高)。

★ 为什么能把 640M 的 PySide6 砍到约 200M
  项目只 import 了 QtCore / QtGui / QtWidgets(已全仓 grep 确认), 其余全是白带的:
    Qt6WebEngineCore.dll      195M  内嵌浏览器, 完全没用
    translations/              60M  项目自己写 QSS, 没用 Qt 自带翻译
    qml/ + Qt6Quick.dll        36M  用的是 Widgets, 不是 QML
    avcodec-61.dll             14M  无多媒体需求
  下面的 excludes 排模块, 再按路径过滤 binaries/datas 兜一道(有些 DLL 是被
  hook 硬塞进来的, 光靠 excludes 排不掉)。

★ 用户数据不进包
  config/config.yaml、Test_cases/ 等是运行时的**外部**数据(见
  core/driver.USER_DATA_PATHS)—— 打包只带随版本走的程序资源; 打包进来反而会
  在每次更新时覆盖用户的用例与配置。

    pyinstaller AutoTest.spec --noconfirm
"""
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.getcwd())

# ── 明确不用的 Qt 模块(见模块 docstring) ──
QT_EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtQuickTest",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtTextToSpeech", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtNetwork",          # 更新检查走标准库 urllib, 不需要 Qt 网络
]
# ── 与本项目无关的重型第三方(有的会被 opencv/uiautomator2 间接拖进来) ──
OTHER_EXCLUDES = [
    "tkinter", "matplotlib", "scipy", "pandas", "IPython", "jupyter",
    "notebook", "pytest", "sphinx", "setuptools", "pip",
]

# 随版本走的程序资源(用户数据一律不带)
datas = [
    ("gui/assets", "gui/assets"),
    ("config/locators.yaml", "config"),        # 定位器配置(可被更新覆盖)
    ("config/config.example.yaml", "config"),  # 配置模板: 首次运行据此生成 config.yaml
    ("VERSION", "."),                          # 落在解压目录, 便于事后取证版本
    ("CHANGELOG.md", "."),
    ("README.md", "."),
]
# 模型文件: PyInstaller 没有这两个包的现成 hook, 不显式收集的话
# **打包后 OCR 会静默失效**(能启动、能跑, 只是识别不到文字) —— 是最容易漏的一处
for pkg in ("rapidocr_onnxruntime", "ddddocr"):
    try:
        datas += collect_data_files(pkg, include_py_files=False)
    except Exception as e:
        print(f"[spec] 收集 {pkg} 数据文件失败(打包后 OCR 可能不可用): {e}")

# 默认用例与模板: 随包带一套, 启动时"缺才补"(本地已有绝不覆盖,
# 见 core/bootstrap._SEED_DIRS)。screenshots/ 与 __pycache__/ 是运行产物,
# 绝不进包; 只收用例 YAML 与模板目录里的全部文件(模板是 png)。
_cases_root = os.path.join(ROOT, "Test_cases")
for _base, _dirs, _files in os.walk(_cases_root):
    _dirs[:] = [d for d in _dirs if d not in ("screenshots", "__pycache__")]
    for _fn in _files:
        _full = os.path.join(_base, _fn)
        _rel_dir = os.path.relpath(os.path.dirname(_full), ROOT)
        _in_templates = os.sep + "templates" + os.sep in _full
        if _in_templates or _fn.lower().endswith((".yaml", ".yml")):
            datas.append((_full, _rel_dir))
for _base, _dirs, _files in os.walk(os.path.join(ROOT, "Test_img", "templates")):
    for _fn in _files:
        _full = os.path.join(_base, _fn)
        datas.append((_full, os.path.relpath(os.path.dirname(_full), ROOT)))

a = Analysis(
    ["gui/main.py"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "core.actions",              # 导入即注册全部动作(注册表模式, 静态分析看不到)
        "core.actions.basic", "core.actions.asserts", "core.actions.data_ops",
        "core.actions.map_ops", "core.actions.timer_ops",
        "uiautomator2", "adbutils",          # 运行期动态导入
        "rapidocr_onnxruntime", "ddddocr",   # OCR 兜底
        "yaml", "openpyxl", "cv2", "PIL",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=QT_EXCLUDES + OTHER_EXCLUDES,
    noarchive=False,
)

# ★ 再兜一道: 有些 Qt 的 DLL/翻译是被 hook 直接塞进 binaries/datas 的, 模块级
#   excludes 对它们无效 —— 这里按路径把翻译与 QML 资源剔掉(合计约 90M)
def _keep(entry):
    p = entry[0].replace("\\", "/").lower()
    if "/translations/" in p or p.startswith("translations/"):
        return False
    if "/qml/" in p or p.startswith("qml/"):
        return False
    if "qt6webengine" in p or "qtwebengine" in p:
        return False
    if "avcodec" in p or "avformat" in p or "avutil" in p:
        return False
    return True

a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)

# onefile: 所有内容打进单个 exe(无 COLLECT 段)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AutoTest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 压缩会拖慢启动, 且杀软误报率高, 不开
    console=False,          # 无控制台窗口(与 start_gui.vbs 的效果一致)
    icon=os.path.join(ROOT, "gui", "assets", "app_icon.ico"),
)
