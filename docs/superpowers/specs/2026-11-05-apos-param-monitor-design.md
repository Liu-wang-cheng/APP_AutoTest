# 设备参数监控设计（v1）

- 日期：2026-11-05
- 状态：待用户评审
- 范围：v1 只读（参数监控 / 曲线 / 电量接入 / 失败快照）；写参数、快速控制、故障注入另立阶段
- 关联：`上位机工具解析报告_模板与AutopackDebugger实例.md`、`AutopackDebugger_分析报告.md`

---

## 1. 目标

在 AutoTest 顶部增加第二个工作区「设备参数」，按 IP 连接机器人并实时读取内部参数；同时把参数能力接入现有测试链路。

必须满足：

1. 顶部双页面：`自动化测试` / `设备参数`，测试页现有布局不变；
2. 设备自动扫描（与 AutopackDebugger 同协议），也可手动输入 IP；
3. 参数表实时刷新，曲线可缩放/平移/复位；
4. 首次选择一次 AutopackDebugger 目录，复制 DLL + license 到工具自己的 `apos\` 目录，之后完全自包含；
5. v1 只读，不改机器状态。

非目标（后续阶段）：写参数、快速控制、虚拟电量、`Sim_*` 故障注入、地图/雷达可视化。

---

## 2. 验证结论（真机）

### 2.1 设备扫描协议（已实测）

从 `ModuleDebugTool.dll` 的 `UDPThread2` 还原并真机验证：

| 项 | 值 |
|---|---|
| 发送 | UDP 广播 `x.x.x.255:5555` |
| 接收 | UDP `5550` |
| 请求明文 | `find ldrobot` |
| 加密 | AES-128-CBC；key/IV 为厂商固定常量（不写入本文档） |
| 请求填充 | PKCS7，结果再 Base64 |
| 响应 | Base64 → AES-128-CBC，`PaddingMode.None`，手动去填充 |
| 响应 JSON | `{"value":[{"hostname","ip","mac","modle","sn","type"}]}` |
| 显示名 | `modle-sn-ip`（`modle` 是厂商拼写，即 model） |

实测：一次广播在实验室网段发现 6 台机器人；响应中的 `sn` 即 **CustomSN**，与 `/System/CustomSN` 一致。

### 2.2 SDK 参数读取（192.168.10.63，只读）

| 场景 | 延迟 |
|---|---|
| 读 1 个参数 | 中位 49.8 ms，p95 99.5 ms |
| 读 4 个参数 | 中位 40.6 ms |
| 读 8 个参数 | 中位 41.1 ms |
| 读 12 个参数 | 中位 40.4 ms |
| 持续批量读 4 参数 | **23.8 Hz**（239 次 / 10 秒） |

结论：批量读的上限约 20–24 Hz，足够做 5–10 Hz 的实时曲线；没有必须走 7809 调试流的理由（v1）。

### 2.3 曲线渲染（900×400，离屏）

| 点数 | 自绘 QWidget | pyqtgraph |
|---|---|---|
| 300 | 24 ms | 3 ms |
| 1,200 | 127 ms | 6 ms |
| 6,000 | 683 ms | 16 ms |
| 30,000 | 3,436 ms | 57 ms |

结论：**曲线使用 pyqtgraph**。5 分钟 @ 24 Hz ≈ 7,200 点，pyqtgraph 约 16 ms/帧，且缩放/平移/自适应内置。

### 2.4 SDK 依赖

```
librpc_base.dll (70KB, x64)
  ├─ libapos_client.dll (151KB)
  │    └─ libzmq-v141-mt-4_3_5.dll (548KB)
  └─ MSVCP140 / VCRUNTIME140 / VCRUNTIME140_1（系统 VC++ 运行库）
```

不需要 `BinpackCLR.dll`，也不需要 158MB 的 `AutopackDebugger.exe`；license 从 `AposSdkHelp.py` 提取。

---

## 3. 总体架构

```text
┌────────────────────────── GUI ──────────────────────────┐
│ 顶部导航：自动化测试 | 设备参数                          │
│                                                         │
│ 自动化测试页（现有布局不变）                             │
│  运行条 / 用例列表 / 用例编辑区 / 底部页签               │
│                                                         │
│ 设备参数页                                               │
│  连接栏（扫描 / 设备下拉 / 连接 / 断开）                 │
│  SDK 状态行（路径、文件齐全、更换 SDK）                  │
│  子页签：实时参数 | 曲线                                  │
│   实时参数：分类栏 + 参数表                              │
│   曲线：曲线页 + 分组 + pyqtgraph 图                     │
│  SDK 配置弹窗（缺文件时）                                │
└───────────────┬─────────────────────────────────────────┘
                │ signals / slots
┌───────────────▼─────────────────────────────────────────┐
│ AposService（QThread，独占一个 SDK handle）              │
│  · 设备扫描（UDP/TCP）                                   │
│  · 批量 read，按路径返回快照                             │
│  · 连接状态机 / 重连 / 超时                              │
└───────────────┬─────────────────────────────────────────┘
                │
┌───────────────▼───────────┐   ┌─────────────────────────┐
│ AposClient（ctypes）      │   │ RobotScanner（UDP/TCP） │
│  加载 BASE_DIR\apos\*.dll │   │  不依赖 SDK / license   │
└───────────────────────────┘   └─────────────────────────┘
```

新增模块：

| 模块 | 职责 |
|---|---|
| `core/apos/client.py` | ctypes 封装 SDK：init/deinit/read/service/事件/文件 |
| `core/apos/scanner.py` | UDP 广播发现 + TCP 8801 兜底 |
| `core/apos/service.py` | 采集线程、请求队列、连接状态、快照信号 |
| `core/apos/catalog.py` | 参数预设（来自 `BinpackParam.xml`）与分组 |
| `gui/device_page.py` | 设备参数页（连接栏、参数表、曲线） |
| `gui/sdk_setup.py` | 首次配置 / 更换 SDK 对话框 |
| `core/actions/apos_ops.py` | P1：`read_param` / `assert_param` |

---

## 4. SDK 目录：选择一次，复制到工具

### 4.1 目标目录

```text
BASE_DIR\apos\
  librpc_base.dll
  libapos_client.dll
  libzmq-v141-mt-4_3_5.dll
  license.dat
  sdk_manifest.json      # 来源目录、复制时间、文件 sha256
```

- `BASE_DIR` = `AutoTest.exe` 所在目录（打包版）或仓库根（源码运行）；
- 不放进 `_internal\`：OTA 更新只替换 `AutoTest.exe` + `_internal\`，`apos\` 会保留；
- `apos\` 加入 `.gitignore`，不进入安装包/公开仓库。

### 4.2 启动检查与弹窗

```text
启动 → 主窗口显示后（QTimer.singleShot，不阻塞构造）
  └─ 检查 BASE_DIR\apos\ 四个文件
      ├─ 齐全 → 不提示
      └─ 缺失 → 对话框「需要配置 AutopackDebugger」
           [选择目录…]  [稍后]
```

- `选择目录…`：文件夹选择 → 校验源目录 → 复制 → 提取 license → 写 manifest → 提示「SDK 已就绪」；
- 校验项：3 个 DLL 存在、x64、license 可提取；
- 复制失败（权限/占用/磁盘）→ 显示原因，保留对话框重选；
- `稍后`：本次不提示，参数功能置灰；下次启动继续检查；
- 测试/CI：`PYTEST_CURRENT_TEST` 或 `APOS_SKIP_SDK_PROMPT=1` 时跳过检查与弹窗。

### 4.3 license 提取

只读取 `AposSdkHelp.py` 中的 `self.apos_license = "..."` 字符串，写入 `apos\license.dat`；**不执行**该 Python 文件，不复制厂商代码。提取失败按「目录不完整」处理。

### 4.4 更换 SDK

设备参数页 SDK 状态行提供「更换 SDK」：重新选择目录，覆盖复制并更新 manifest。

---

## 5. 设备自动扫描

### 5.1 触发

- 首次进入「设备参数」页自动扫描一次；
- 连接栏「扫描」按钮手动重扫；
- 扫描在后台线程执行，UI 显示进度，可取消。

### 5.2 流程

```text
枚举本机 IPv4 网卡（排除 127.*、169.254.*）
  └─ 对每个 /24 生成 x.x.x.255 广播地址（最多 4 个网段）
       └─ UDP 5555 发送加密的 "find ldrobot"
            └─ UDP 5550 接收 3–5 秒
                 └─ 解密 → JSON value[] → {model, sn, ip}
                      └─ 去重、按 IP 排序，显示 model-sn-ip
```

- 响应里的 `sn` 即 CustomSN；
- 信号槽把结果交给 UI，不阻塞；
- 同一 IP 只保留最新一条。

### 5.3 兜底：TCP 扫描

UDP 无响应的设备（防火墙/跨网段）用 TCP 探测 `8801`：

- 对 /24 并发连接（64 线程，300 ms 超时）；
- 端口开放的主机用 SDK `read(["/System/FirmwareInfo", "/System/CustomSN"])` 补全 model/sn；
- 仅 v1 手动触发或 UDP 结果为空时执行，避免每次扫描过慢。

### 5.4 设备列表

- 扫描结果 + 手动输入的 IP 保存在 `QSettings("vacuum_test","case_studio")` 的 `apos/devices`（JSON 列表：`{model, sn, ip, last_seen}`）；
- 下拉显示 `model-sn-ip`；选择后填入 IP；
- 扫描不到的旧设备保留在列表，标注「离线」；
- 支持手动输入 IP（直接连接，成功后补全名称并保存）。

---

## 6. 参数采集服务

### 6.1 线程模型

- `AposService` 是 QThread，独占一个 SDK handle；
- 所有 `read` / `service` / 文件读取串行执行（SDK 非线程安全）；
- UI 只接收快照信号，不直接调用 SDK。

### 6.2 采样频率

| 场景 | 默认 | 可调 |
|---|---|---|
| 实时参数表 | 2 秒 | 1–10 秒 |
| 曲线页 | 5 Hz（200 ms） | 2–10 Hz |
| 空闲/后台 | 10 秒 | — |

- 曲线页只采集当前页选中的参数，减少请求量；
- 参数表与曲线共用同一批 `read`，按需合并路径；
- 根据真机基准（24 Hz 上限），5–10 Hz 留有充足余量。

### 6.3 快照与错误

```python
{"ok": {"/BatteryBattery/BatteryLevel": 80, ...},
 "failed": {"/MotorFan/Speed": "timeout"},   # 单条失败不整表清空
 "ts": 176...}
```

- 读取失败标红并保留上一次值，显示「读取失败」；
- 连续失败 N 次 → 标记「连接异常」，自动降频重连；
- 断线重连成功后恢复频率。

### 6.4 电池监测接入

`BatteryMonitor` 的 reader 改为优先调用 `AposService.read_battery()`：

1. SDK 已连接 → 读 `/BatteryBattery/BatteryLevel`；
2. 不可用/失败 → 回退现有 OCR 逻辑；
3. 报告注明数据来源（参数 / OCR）。

---

## 7. UI 设计

### 7.1 顶部双页面

```text
[AutoTest vX]                        ● 运行中 · 用例 2/5   ● 设备 已连接 · CLA_HS4
 自动化测试   设备参数
```

- 左侧两个页面切换（蓝色下划线）；
- 右侧常驻测试运行状态与设备连接状态；
- 自动化测试页保持现有：运行条、用例列表、用例编辑区、底部页签（执行结果 / YAML 源码 / 运行日志 / 测试报告）；
- 底部**不增加**「设备参数」页签。

### 7.2 设备参数页

```text
设备参数
机器人 [CLA_HS4-B43...-192.168.10.63 ▾] [扫描] [连接] [断开] ● 已连接 · 2s  [导出快照]
SDK 目录 ...\apos\  ✔ 文件齐全                                    [更换 SDK]
[实时参数] [曲线] [快速控制·后续] [故障模拟·后续]        [搜索…] [只看变化]
┌ 分类 ──┬ 参数表（参数名 / 路径 / 当前值 / 单位 / 更新时间） ┐
│ 全部 77 │ 系统、电量、状态、电机、传感器、耗材               │
└────────┴──────────────────────────────────────────────────┘
```

- 不设右侧概览卡片（与参数表内容重复）；
- 参数表按分组展示，变化值带「刚更新」，异常标红；
- 扫描结果下拉显示 `model-CustomSN-IP`，离线设备灰显。

### 7.3 曲线页

与 AutopackDebugger 的 `CurveConfig.xaml` 一致：

```text
曲线页：组件 / 电池 / IMU / 地检 / 碰撞 / 沿墙 / 超声波
   └─ 分组（电池页）：充电状态 / 电量 / 电池电压 / 电池温度
        └─ 曲线：电池-实际电池电量、电池-电池电压(mV) …
```

- 左侧曲线页列表（分组数）；
- 右侧分组卡片，每组显示 `ParamGroupName / ParamName` 与曲线；
- 顶部「全部曲线 / 添加曲线」；
- 图表用 pyqtgraph：滚轮缩放、左键拖动、双击复位、「自适应」按钮；
- 实时跟随：默认跟随最新；手动缩放/拖动后暂停跟随，显示「回到实时」；
- 滚动窗口：默认最近 10 分钟，最多保留 30 分钟；超长窗口抽稀到 ≤2 点/像素；
- 曲线更新频率 5 Hz（可调 2–10 Hz）。

---

## 8. 与现有功能融合

### P0（本版）

| 功能 | 改动 |
|---|---|
| 电量监测 | 优先读 `/BatteryBattery/BatteryLevel`，失败回退 OCR；报告标注来源 |
| 失败快照 | 用例失败时抓一次参数快照（当前页参数 + 电池/状态/CPU/内存），写入报告新增「设备参数」页 |
| 报告设备信息 | 型号/固件/SN 从参数写入汇总页 |
| 设备参数页 | 实时参数 + 曲线 + 扫描 + SDK 配置 |

### P1（后续确认）

| 功能 | 改动 |
|---|---|
| `read_param` | 读取参数到变量，供后续步骤使用 |
| `assert_param` | 复用 if 条件语法，断言电量/状态/温度/固件版本 |
| 前置条件 | 电量门槛、工作状态改用参数判定 |

### P2（再后续）

快速控制、虚拟电量、`Sim_*` 故障注入、耗材模拟。写操作必须二次确认、记录前后值、结束时恢复。

---

## 9. 配置

QSettings（`vacuum_test/case_studio`）：

```text
apos/devices        JSON 列表 {model, sn, ip, last_seen}
apos/table_interval 秒，默认 2
apos/curve_hz       Hz，默认 5
apos/window_minutes 默认 10
```

随版本入库：

```text
config/apos_params.yaml     参数预设（分组/名称/路径/单位）
config/apos_curves.yaml     曲线页/分组/参数映射（由 CurveConfig.xaml 整理）
```

不写入 config.yaml，避免改动用户现有配置。

---

## 10. 安全与合规

- `apos\` 与 license 只在工具本机，不入 git、不进入安装包；
- 启动/连接只从固定的 `BASE_DIR\apos\` 加载 DLL，不接受任意路径；
- 扫描只针对本机网卡所在 /24，且由用户操作触发；
- v1 只读，不调用任何写服务；
- 厂商 UDP 扫描的 key/IV 属于协议常量，不写入本文档或日志；
- 日志不记录 license 内容。

---

## 11. 错误处理

| 场景 | 处理 |
|---|---|
| SDK 目录缺失/不完整 | 启动弹窗；选择/复制失败显示原因 |
| DLL 加载失败 | 连接时提示缺依赖/位数不对/VC++ 运行库 |
| 设备离线 | 连接失败提示；扫描列表灰显 |
| 参数读取失败 | 单条标红保留旧值，不整表清空 |
| 连续失败 | 降频、标记异常、自动重连 |
| 扫描超时 | 显示「未发现设备」，提供手动 IP |
| 曲线数据断点 | 断点处断开折线，不连成直线 |

---

## 12. 测试策略

- 单元测试：`AposClient` / `AposService` / `RobotScanner` 用 fake/mock；不依赖真机；
- 扫描协议：单测 AES 加解密 + JSON 解析（用固定样例）；
- UI：离屏 QTest，验证连接栏、参数表、曲线页切换、SDK 弹窗；
- 打包：`verify_package.py` 检查 `apos\` 逻辑、pyqtgraph/numpy 资源；
- 真机冒烟：手动执行一次「扫描 → 连接 → 参数表 → 曲线 → 电量监测」；
- CI 不要求机器人；`APOS_SKIP_SDK_PROMPT=1` 跳过弹窗。

---

## 13. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0-1 | `AposClient` + SDK 目录复制/校验/弹窗 | 首次选目录后可连接；重启不再提示 |
| P0-2 | `RobotScanner`（UDP + TCP 兜底） | 点击扫描发现实验室 6 台设备，显示 model-CustomSN-IP |
| P0-3 | `AposService` + 设备参数页（实时参数） | 2 秒刷新，失败标红 |
| P0-4 | 曲线页（pyqtgraph、分组、缩放/跟随） | 5 Hz 刷新，缩放/平移/复位正常 |
| P0-5 | 电量监测接入 + 失败快照 + 报告设备信息 | 报告中有参数页与来源标注 |
| P1 | `read_param`/`assert_param` + 前置条件 | 用例可断言参数 |
| P2 | 快速控制 / 虚拟电量 / `Sim_*` | 实验室模式，默认关闭 |

---

## 14. 已确认的决策

1. 顶部双页面：`自动化测试` / `设备参数`，测试页布局不变；
2. 设备参数页不设右侧概览卡片；
3. 曲线按 `CurveConfig.xaml` 的「曲线页 → 分组 → 曲线」组织；
4. 曲线使用 pyqtgraph，支持缩放/平移/复位/实时跟随；
5. 采样：参数表 2 秒，曲线 5 Hz（可调）；
6. 设备名格式 `model-CustomSN-IP`，SN 取 `/System/CustomSN`；
7. 设备自动扫描按厂商 UDP 协议（5555/5550），TCP 8801 兜底；
8. SDK 首次手动选目录并复制到 `BASE_DIR\apos\`，license 从 `AposSdkHelp.py` 提取；
9. 不显示 SDK 版本号；
10. v1 只读。
