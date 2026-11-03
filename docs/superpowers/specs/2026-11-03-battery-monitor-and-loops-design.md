# 电量监测 + 循环步骤 设计

日期: 2026-11-03
状态: 已确认（用户 2026-11-03 批准）

## 1. 电量监测

### 启动
- 运行条新增「电量监测」勾选框，配套参数：`步长 N%`（默认 5）、`采样间隔 s`（默认 60）。
- 参数用 QSettings 记住上次值。
- 勾选后从**本轮第一条用例开始**采样；整轮结束或手动停止时停止。前置阶段不采样。

### 采样
- 独立线程 `BatteryMonitor(threading.Thread)`，**共用执行线程的 `d`**（不新建 uiautomator2 连接）。
- 每 `interval` 秒调用 `core.session.get_battery_level(d)` 一次；启动时立即采一次。
- 读不到（-1）记为无效点：不进原始/里程碑，但计入采样失败次数。
- 停止：`threading.Event` + `join(timeout=5)`；`stop()` 幂等。
- 样本：`(t 秒, battery, label)`，`label` 取当前 `用例名(第N次)`。

### 报告（新增一页「电量监测」，同一页上下两段）
- **汇总（上）**
  - 全局：总运行时长、起始电量、结束电量、总耗电、平均每 1% 用时、每 N% 段平均用时、有效采样点数、读取失败次数。
  - 按 `用例/轮次` 分行：起始电量、结束电量、耗电、用时、平均每 1%。
- **详细（下）**
  - 列：序号、相对时间、用例/轮次、电量、变化、距上次变化 (mm:ss)、累计 (mm:ss)、类型。
  - 类型：`1% 原始`（每次实际变化）/ `N% 里程碑`。
- 里程碑锚点：**相对监测起点的累计下降量**。起点 87%、N=5 → 82、77、…
  - 一次采样跨过多个阈值时只记一行（实际电量 + 跨过的目标），下一锚点从实际电量重新计算，避免出现多条 dt=0 的噪声行。
- 时间统一 `mm:ss`。
- 没有有效样本时，页内写明原因（页面未展示电量 / 设备断开），不伪造数据。

## 2. for / while 循环步骤

### 动作
- `for`：循环直到条件满足。每轮先判条件，成立→结束（成功）；不成立→执行循环体。
- `while`：条件满足时循环。每轮先判条件，不成立→结束；成立→执行循环体。
- 字段：条件（必填）、最大次数 `max_loops`（默认 100）、每次间隔 `loop_wait` 秒（默认 0）。
- 循环体 YAML 键：`do`（子步骤列表）。
- 条件语法复用 `if`：文本 / 数值（`电量 > 20`、`面积 < 5`）/ 图片名 / resource-id。
- 条件求值**只判一次**（不套 `if` 的 10 次轮询）。
- `电量/电池` 关键字直接走 `session.get_battery_level`（`extract_value` 原本会跳过带 `%` 的节点，读不到）。

### 安全与失败
- 达到 `max_loops` 仍未结束 → 抛错，该步骤 FAIL，后续步骤不执行（与现有步骤失败一致）。
- 循环体任一步失败 → 立即抛出，步骤 FAIL。
- 循环体为空时：视为"等待条件"，按 `loop_wait`（为 0 时用 5 秒）轮询，避免忙等。
- 结果表：每轮子步骤描述带 `[第N次]` 前缀。

### GUI
- 卡片复用 `then/else` 的子步骤机制，分支键 `do`，徽标显示 `▸循环体`。
- 摘要徽标显示 `循环体 N步`。

## 3. 改动面
- 新增 `core/battery_monitor.py`（线程 + 报告数据纯函数）
- `core/excel_report.py`：`add_battery_report`
- `core/actions/basic.py`：`eval_condition` 抽取、`for`/`while`
- `core/actions/data_ops.py`：`extract_value` 电量/电池关键字特判
- `core/runner.py`：容器子结果落账保持现有机制（循环不增加 `_if_depth`，由 `run_steps` 统一落账）
- `gui/schema.py`、`gui/main_window.py`、`gui/step_card.py`
- `tools/validate_cases.py`、`tests/test_yaml_runner.py`
- 测试：`tests/test_battery_monitor.py`、`tests/test_excel_report.py`、`tests/test_actions_basic.py`、`tests/test_schema.py`

## 4. 风险
- 监控线程与执行共用设备：设备端串行处理，采样时可能让点击/截图多等几百毫秒。
- 电量读不到时报告只标注"未读到"，不补值。
