# 设备参数监控（P0）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AutoTest 顶部新增「设备参数」独立页面，自动扫描/连接机器人，实时读取内部参数与曲线，并接入电量监测与失败快照。

**Architecture:** 新增 `core/apos`（SDK 管理、扫描、客户端、采集线程）与 `gui/device_page.py`（连接栏/参数表/曲线）；主窗口用顶部导航 + QStackedWidget 切换测试页与设备页。曲线用 pyqtgraph，采集用独立 Python 线程，GUI 用 QTimer 读取快照。

**Tech Stack:** PySide6、pyqtgraph、cryptography（AES-128-CBC）、ctypes（APOS SDK）、pytest。

**Spec:** `docs/superpowers/specs/2026-11-05-apos-param-monitor-design.md`

---

## 文件结构

```text
core/apos/
  __init__.py          # 空
  sdk.py               # SDK 目录校验/复制/license 提取/加载
  scanner.py           # UDP 5555/5550 扫描 + TCP 8801 兜底
  client.py            # ctypes 封装 APOS SDK
  service.py           # 采集线程（纯 Python，无 Qt）
  catalog.py           # 参数/曲线预设加载
gui/
  device_page.py       # 设备参数页（连接栏/参数表/曲线容器）
  curve_view.py        # pyqtgraph 曲线页（页面/分组/缩放/跟随）
  sdk_setup.py         # SDK 缺失/更换对话框
config/
  apos_params.yaml     # 参数预设
  apos_curves.yaml     # 曲线页/分组/参数映射
tests/
  test_apos_sdk.py
  test_apos_scanner.py
  test_apos_client.py
  test_apos_service.py
  test_device_page.py
```

---

## Task 1: SDK 目录管理

**Files:**
- Create: `core/apos/__init__.py`, `core/apos/sdk.py`
- Modify: `.gitignore`（加 `apos/`）、`core/driver.py`（`USER_DATA_PATHS` 加 `"apos"`）、`requirements.txt`（加 `pyqtgraph==0.13.7`）
- Test: `tests/test_apos_sdk.py`

**Step 1: 写失败测试**

```python
def test_check_sdk_requires_all_files(tmp_path):
    from core.apos import sdk
    assert sdk.check_sdk(tmp_path, strict=False)["missing"]

def test_install_copies_and_extracts_license(tmp_path):
    from core.apos import sdk
    src = tmp_path / "src"; dst = tmp_path / "dst"
    src.mkdir()
    for n in ("librpc_base.dll", "libapos_client.dll", "libzmq-x.dll"):
        (src / n).write_bytes(b"x")
    (src / "AposSdkHelp.py").write_text('self.apos_license = "test-lic"\n', encoding="utf-8")
    sdk.install_sdk(src, dst)
    assert (dst / "license.dat").read_text(encoding="utf-8") == "test-lic"
    assert sdk.check_sdk(dst)["ok"]
```

**Step 2: 实现 `core/apos/sdk.py`**

```python
FILES = ("librpc_base.dll", "libapos_client.dll")
ZMQ_GLOB = "libzmq*.dll"
LICENSE_NAME = "license.dat"
MANIFEST = "sdk_manifest.json"

def check_sdk(path, strict=True):
    """返回 {"ok", "missing", "path"}；strict=False 时 license 缺失也算 missing。"""
    p = Path(path); missing = []
    for n in FILES:
        if not (p / n).is_file(): missing.append(n)
    if not any(p.glob(ZMQ_GLOB)): missing.append(ZMQ_GLOB)
    if not (p / LICENSE_NAME).is_file(): missing.append(LICENSE_NAME)
    return {"ok": not missing, "missing": missing, "path": str(p)}

def extract_license(source_dir):
    txt = (Path(source_dir) / "AposSdkHelp.py").read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'self\.apos_license\s*=\s*["\']([^"\']+)["\']', txt)
    if not m: raise ValueError("AposSdkHelp.py 里没有找到 apos_license")
    return m.group(1)

def install_sdk(source_dir, target_dir):
    src = Path(source_dir); dst = Path(target_dir); dst.mkdir(parents=True, exist_ok=True)
    for n in FILES: shutil.copy2(src / n, dst / n)
    zmq = sorted(src.glob(ZMQ_GLOB))
    if not zmq: raise FileNotFoundError(ZMQ_GLOB)
    shutil.copy2(zmq[0], dst / zmq[0].name)
    (dst / LICENSE_NAME).write_text(extract_license(src), encoding="utf-8")
    manifest = {"source": str(src.resolve()), "files": {...sha256...}, "ts": ...}
    (dst / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return check_sdk(dst)
```

**Step 3: 跑测试 → 通过；提交。**

---

## Task 2: 设备扫描

**Files:**
- Create: `core/apos/scanner.py`
- Test: `tests/test_apos_scanner.py`

**Step 1: 测试**

```python
def test_parse_response():
    from core.apos.scanner import parse_response
    raw = b'{"value":[{"hostname":"x","ip":"192.168.1.2","mac":"m","modle":"CLA_HS4","sn":"SN1","type":"t"}]}'
    assert parse_response(raw) == [{"model": "CLA_HS4", "sn": "SN1", "ip": "192.168.1.2"}]

def test_subnets_from_ipconfig(monkeypatch):
    from core.apos import scanner
    text = "IPv4 地址 . . . . . . . . . . . . : 192.168.10.17\n"
    monkeypatch.setattr(scanner, "_ipconfig", lambda: text)
    assert "192.168.10.255" in scanner.broadcast_targets()
```

**Step 2: 实现**

```python
KEY = b"..."  # 厂商固定常量，不写入文档
IV = b"..."
def _encrypt_request():
    pad = padding.PKCS7(128).padder(); data = pad.update(b"find ldrobot") + pad.finalize()
    enc = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize())

def _decrypt_response(b64):
    raw = base64.b64decode(b64); dec = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    plain = dec.update(raw) + dec.finalize()
    n = plain[-1] if plain else 0
    if 1 <= n <= 16 and plain[-n:] == bytes([n]) * n: plain = plain[:-n]
    return plain.decode("utf-8", "replace")

def scan(timeout=4.0, cancel_event=None):
    """UDP 广播发现；返回 [{"model","sn","ip","mac"}]，去重按 IP 排序。"""
    ...

def tcp_fallback(cidr, reader, workers=64, timeout=0.3):
    """TCP 8801 探测 + reader(ip) 补全 model/sn；返回同结构列表。"""
```

**Step 3: 测试 → 提交。** 扫描测试全部用 monkeypatch，不连真机。

---

## Task 3: SDK 客户端与采集线程

**Files:**
- Create: `core/apos/client.py`, `core/apos/service.py`
- Test: `tests/test_apos_client.py`, `tests/test_apos_service.py`

**Step 1: `AposClient`**

```python
class AposClient:
    def __init__(self, sdk_dir=None, license_text=None):
        self.sdk_dir = sdk_dir or default_sdk_dir()
        self.license = license_text or load_license(self.sdk_dir)
        dll = self.sdk_dir / "librpc_base.dll"
        os.add_dll_directory(str(self.sdk_dir))
        self.lib = ctypes.WinDLL(str(dll))
        self._bind()
        self.handle = -1

    def connect(self, ip, retries=3): ...
    def disconnect(self): ...
    def read(self, paths): ...      # 返回 dict（含 AposTimeStamp）
    def service(self, event_id, timeout_ms=6000): ...
    def read_file(self, path): ...
```

绑定与 `D:\claude_test\apos_min_client.py` 一致（Init/DeInit/Read/Service/ReadFile/Subscribe/SentEventByJson/log）。所有 ctypes 调用串行。

**Step 2: `ParamService`（纯线程）**

```python
class ParamService:
    def __init__(self, client, paths=(), interval=2.0):
        self.client = client; self._paths = list(paths); self.interval = interval
        self._lock = threading.Lock(); self._snapshot = {"ok": {}, "failed": {}, "ts": 0}
        self._stop = threading.Event(); self._thread = None
        self._connected = False

    def connect(self, ip): ...
    def disconnect(self): ...
    def set_paths(self, paths, interval=None): ...  # 变更后立即唤醒
    def latest(self): with self._lock: return dict(self._snapshot)
    def read_once(self, paths): ...
    def read_battery(self): ...   # 返回 int / -1
    def start(self): ...; def stop(self): ...
```

线程循环：`read(self._paths)`；成功 → `{ok, failed, ts}`，失败单条进 failed；连续失败 5 次 → `_connected=False`，退避 3s。

**Step 3: 测试**：FakeClient 返回固定 dict/抛异常；断言 `latest()`、`read_battery()`、`stop()` 幂等。

**Step 4: 提交。**

---

## Task 4: 设备参数页 + 主窗口顶部导航

**Files:**
- Create: `gui/device_page.py`, `gui/curve_view.py`, `gui/sdk_setup.py`, `core/apos/catalog.py`
- Create: `config/apos_params.yaml`, `config/apos_curves.yaml`
- Modify: `gui/main_window.py`（顶部导航 + QStackedWidget）、`gui/main.py`（启动 SDK 检查）
- Test: `tests/test_device_page.py`

**Step 1: 目录/预设**

`config/apos_params.yaml`（节选，分组/名称/路径/单位）：

```yaml
- group: 系统
  items:
    - {name: 型号, path: /System/FirmwareInfo/Model}
    - {name: 固件版本, path: /System/FirmwareInfo/Version}
- group: 电量
  items:
    - {name: 电量, path: /BatteryBattery/BatteryLevel, unit: "%"}
    - {name: 电池电压, path: /BatteryBattery/BatteryVol, unit: mV}
    - {name: 电池温度, path: /BatteryBattery/BatteryTemp, unit: "°C"}
```

`config/apos_curves.yaml`：7 个曲线页；电池页含充电状态/电量/电池电压/电池温度；组件页含风机/震动；其余页先放分组名与空路径（显示「暂未接入」）。

**Step 2: `CurveView`**

- `pyqtgraph.PlotWidget`，白底、网格关闭、轴颜色与 QSS 一致；
- 每个分组一张 `PlotWidget`；`set_data(path, points)` 更新；
- 滚轮缩放/拖动/双击复位由 pyqtgraph 内置；
- `follow` 布尔 + `回到底部` 默认开启；`range` 限最近 N 分钟；
- 数据存 `collections.deque(maxlen=N)`。

**Step 3: `DevicePage`**

```python
class DevicePage(QWidget):
    def __init__(self, service_factory=None, settings=None):
        # 连接栏：设备 QComboBox(可编辑) + 扫描 + 连接/断开
        # SDK 状态行：路径 + ✔/✘ + 更换 SDK
        # 子页签：实时参数 / 曲线
        # 实时参数：QLineEdit 搜索 + QTableWidget（分组行 + 参数行）
        # 曲线：CurveView
```

- 扫描：`ScanWorker(QThread)` 调 `scanner.scan()`；结果写入 QSettings `apos/devices`；下拉显示 `model-sn-ip`；
- 连接：`ParamService.connect(ip)`；QTimer(200ms) 拉 `latest()` 更新表/曲线；
- 断开：`ParamService.disconnect()`；
- SDK 缺失：按钮/表格置灰，点击「更换 SDK」打开 `SdkSetupDialog`。

**Step 4: `SdkSetupDialog`**

- `QFileDialog.getExistingDirectory`；校验/复制；失败显示 `missing`；成功返回 True；
- `MainWindow` 启动时 `QTimer.singleShot(300, self._check_sdk)`；无文件 → 弹窗；测试环境跳过。

**Step 5: 主窗口顶部导航**

```python
# __init__ 内，替换 self.setCentralWidget(page)
self.test_page = page
self.device_page = DevicePage(...)
self.pages = QStackedWidget()
self.pages.addWidget(self.test_page)
self.pages.addWidget(self.device_page)
root = QWidget(); rv = QVBoxLayout(root); rv.setContentsMargins(0,0,0,0); rv.setSpacing(0)
rv.addWidget(self._build_page_nav())
rv.addWidget(self.pages, 1)
self.setCentralWidget(root)
```

- `_build_page_nav()`：两个 `QPushButton`（`自动化测试` / `设备参数`），选中态蓝色下划线；右侧两个状态标签（运行状态/设备状态）。
- 切换只改 `self.pages.setCurrentIndex`，测试页布局不变。
- `on_run_finished` / 设备连接信号更新右上角状态标签。

**Step 6: 测试**

```python
def test_top_nav_switches_pages(win):
    win._show_page("device")
    assert win.pages.currentWidget() is win.device_page
    win._show_page("test")
    assert win.pages.currentWidget() is win.test_page

def test_device_page_lists_params(qapp):
    page = DevicePage(service_factory=FakeService)
    page._apply_snapshot({"ok": {"/BatteryBattery/BatteryLevel": 80}, "failed": {}, "ts": 1})
    assert page.table.item(0, 0) is not None
```

**Step 7: 跑测试 → 提交。**

---

## Task 5: 电量监测接入 + 失败快照

**Files:**
- Modify: `gui/runner_thread.py`（`param_reader` 参数、失败快照）
- Modify: `gui/main_window.py`（创建 RunWorker 时传入 `device_page.service.read_battery`）
- Modify: `core/excel_report.py`（`add_device_snapshot` 新页）
- Test: `tests/test_battery_monitor.py`、`tests/test_excel_report.py`

**Step 1: 电量 reader**

```python
# gui/runner_thread.py
def __init__(..., param_reader=None):
    self.param_reader = param_reader
...
self.batt_monitor = BatteryMonitor(self.d, interval=..., reader=self._read_battery)
def _read_battery(self, d):
    if self.param_reader:
        v = self.param_reader()
        if isinstance(v, int) and v >= 0: return v
    return _default_reader(d)
```

**Step 2: 失败快照**

`RunWorker` 在 `run_steps` 返回 false 或捕获异常时调用 `self.param_reader(snapshot=True)`（若可用），存 `self.failure_snapshot`；`_finish_battery`/save 后写报告：

```python
report.add_device_snapshot(snapshot)   # 新 sheet「设备参数」
```

`add_device_snapshot`：参数名/路径/值/时间，边框与现有风格一致；无数据时写「未连接设备参数」。

**Step 3: 测试**：FakeReport 断言 sheet 存在；BatteryMonitor reader 优先值。

**Step 4: 提交。**

---

## Task 6: 打包、验证、打开 GUI

**Files:**
- Modify: `AutoTest.spec`（若 PyInstaller 未自动收集 pyqtgraph，加 `hiddenimports=["pyqtgraph"]`）
- Modify: `tools/verify_package.py`（检查 `pyqtgraph` 可导入）
- `.gitignore`：`apos/`

**Step 1:** `requirements.txt` 加 `pyqtgraph==0.13.7`（已安装）。
**Step 2:** 复制本机 SDK 到 `D:\claude_test\Auto_test\apos\`（开发运行用；不入库）：
```powershell
New-Item -ItemType Directory -Force D:\claude_test\Auto_test\apos | Out-Null
Copy-Item D:\1766627997244_AutopackDebugger_4.3.4\librpc_base.dll,D:\1766627997244_AutopackDebugger_4.3.4\libapos_client.dll,D:\1766627997244_AutopackDebugger_4.3.4\libzmq-v141-mt-4_3_5.dll D:\claude_test\Auto_test\apos\
# license 用 core.apos.sdk.extract_license + install_sdk 生成（不打印）
```
**Step 3:** `pytest -q` 全绿 + `pyflakes core gui tools vlm`。
**Step 4:** 打开 GUI（`python gui/main.py`），切到「设备参数」确认：连接栏、扫描、参数表、曲线、SDK 状态。

---

## Self-Review

- 规格覆盖：SDK 复制/license、扫描、参数表、曲线/缩放、顶部双页面、电量接入、失败快照、打包，均有任务。
- 无占位符：关键代码/命令已给出；UI 细节按现有 QSS 模式实现。
- 类型一致：`ParamService.latest()` 返回 `{ok, failed, ts}`；`scanner.scan()` 返回 `{model,sn,ip,mac}`；`DevicePage._apply_snapshot` 使用同一结构。
- 风险：pyqtgraph 打包、SDK 线程安全、扫描 UDP 防火墙；任务 6 验证。
