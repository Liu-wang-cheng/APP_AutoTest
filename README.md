# APP 自动化测试平台 (v1.0)

用 YAML 描述操作步骤 → 驱动真机/模拟器上的扫地机 APP → 产出 Excel 报告。

蓝本为 `D:\AI_Test\vacuum_app_test`(纯 uiautomator2 + SIFT 方案),本版按新架构重写:
**执行引擎拆分 + VLM 扩展点 + 保留 PySide6 用例编排器**。

## 快速开始

```bash
# 1. 安装依赖(Python 3.13)
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. 准备配置(含设备序列号,不入库)
copy config\config.example.yaml config\config.yaml

# 3. 无设备自检
.venv\Scripts\python selfcheck.py

# 4. 启动 GUI 用例编排器
.venv\Scripts\python gui\main.py

# 5. 命令行跑真机用例(必须显式 --mode real)
.venv\Scripts\python -m pytest tests\test_yaml_runner.py --mode real --device auto
.venv\Scripts\python -m pytest tests\test_yaml_runner.py --mode real --case 全局清扫
```

## 目录结构

```
Auto_test/
├── core/                    执行引擎
│   ├── driver.py            路径定位 / 配置读写(保注释回写 / 企业加密检测)
│   ├── logger.py            5MB 环形日志
│   ├── registry.py          @action 装饰器注册表(priority 排序分发)
│   ├── runner.py            ActionRunner: 主循环 / 分发 / 共享定位工具
│   ├── actions/             动作实现,按类别拆分
│   │   ├── basic.py         click/input/back/swipe/if_click/find_click/wait_for/if
│   │   ├── asserts.py       assert/switch_to/assert_switch/compare/diff
│   │   ├── data_ops.py      grab/match + 空间邻近取值算法
│   │   ├── map_ops.py       room_zones/room_click/merge_zones/split_zone
│   │   └── timer_ops.py     set_time(OCR滚轮)/add_timer/latest_record
│   ├── vision.py            SIFT 模板匹配(默认视觉后端)
│   ├── trace.py             失败时序证据(内存 N 步 / 失败落盘)
│   ├── session.py           前置准备(重启APP/充电/地图/电量)
│   ├── app_detect.py        设备发现 / 包名自动检测
│   └── excel_report.py      Excel 报告
├── vlm/backend.py           VisionBackend 协议 + VisionRouter(预留,后端未实现)
├── gui/                     PySide6 用例编排器
├── Test_cases/              YAML 用例
├── Test_img/templates/      模板图(需从真机截图裁剪)
├── config/                  config.example.yaml(入库) + locators.yaml
├── mock_server/             Flask 假后端(mock 模式)
├── tests/                   单元测试(除 test_yaml_runner 外都无需设备)
└── tools/validate_cases.py  用例静态校验
```

## 添加新动作

在 `core/actions/` 任意模块里加装饰器即可,不用改 runner:

```python
from core import registry as reg

@reg.action("my_action", priority=25)
def do_my_action(runner, step):
    runner.d.click(...)
```

`priority` 越小越先命中 —— 同一步里有多个动作键时,只有优先级最高的那个执行。

同时在 `gui/schema.py` 的 `ACTIONS` 里加一条声明(供 GUI 生成表单与校验必填),
然后跑 `python selfcheck.py` 确认两边的动作集合一致。

## YAML 用例格式

```yaml
module: 用例组名          # 报告分组 / pytest 用例 id 前缀
cases:
  - name: 用例名
    priority: P0          # P0=冒烟
    wait: 5               # 可选,覆盖全局 step_interval
    steps:
      - desc: 点击开始清扫           # 人话描述,进报告
        click: 开始清扫.png          # 模板图
        timeout: 10
      - desc: 存在才点
        if_click: 确认,确定
      - desc: 条件分支(条件成立则跳过,不成立执行 else)
        if not: 扫地机器人
        else:
          - desc: 重置地图
            click: ${TuyaT4.reset_map}
```

`click` 的目标支持四种形式:
- `开始清扫.png` —— 模板图(SIFT 定位)
- `确认` —— textContains(找不到时回退 description);`=确认` 精确匹配;`确认#2` 取第 2 个
- `[531, 1900]` —— 坐标直点
- `//*[@text='地图管理']` —— XPath(从 v1.4 Appium 用例迁移来的定位串)

## 26 个动作速查

| 类别 | 动作 |
|------|------|
| 交互 | click · long_click · input · back · swipe · if_click · find_click · wait_for · wait_loading |
| 断言 | assert · compare · diff |
| 开关 | switch_to · assert_switch |
| 地图 | room_zones · room_click · merge_zones · split_zone |
| 数据 | grab · match · latest_record |
| 定时 | set_time · add_timer |
| 条件 | if · if not |
| 输出 | screenshot |

修饰参数(任何步骤可用):`desc` `wait` `timeout` `retry` `screenshot` `threshold`

## 两条运行入口

- **GUI**:`python gui/main.py` —— 卡片式编排、设备选择、运行监控、截图预览。
  运行时强制先把用例存盘,执行线程再读磁盘文件 —— 运行内容始终以磁盘为准。
- **pytest**:`pytest tests/test_yaml_runner.py --mode real` —— 扫 `Test_cases/` 参数化执行。

## ★安全阀

`--mode` 默认 `mock`,真机用例在**收集阶段**就被跳过 —— 裸跑 `pytest tests/`
永远不会误操控扫地机。必须显式 `--mode real`。

其余测试(动作注册、取值算法、配置回写、Excel 报告、GUI 离屏)全部无需设备。

## 前置条件

GUI 工具栏「前置条件」四项(默认全选),对应 `core/session.py` 的 prepare():

| 项 | 行为 |
|----|------|
| 重启APP | app_stop → app_start → 进目标设备页 |
| 等待充电 | 未充电则点回充,轮询 20 分钟 |
| 等待地图加载 | 以「地图编辑」入口出现为准,30s |
| 电量≥50% | 不足则等待,兜底 30 分钟 |

## 已知约束

- **模板图需自行截取**:`Test_img/templates/` 初始为空,从真机截图裁剪目标元素放进去。
- **企业透明加密**:若 `config.yaml` 变成密文(`%TSD-Header` 开头),用白名单进程
  (如 git)取回明文;框架会给出明确提示而不是裸报编码错误。
- **VLM 未实现**:`vlm/backend.py` 只提供协议与路由,`config.yaml` 里 `vlm.enabled` 默认关闭。
  接 GLM-4V 时新增一个 `VisionBackend` 实现类即可,引擎零改动。
