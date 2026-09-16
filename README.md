# APP_AutoTest

扫地机 App 的 UI 自动化测试框架:用例用 YAML 写,Python 驱动真机/模拟器执行,附带一个可视化编排的 GUI。

被测应用默认为涂鸦智能(`com.tuya.smartiot`),可在配置或 GUI 里改。

---

## 环境要求

- Windows(真机/模拟器通过 adb 连接)
- Python 3.11+(当前开发环境为 3.13)
- 依赖见 [`requirements.txt`](requirements.txt),其中关键几项:

  | 包 | 用途 |
  |---|---|
  | `uiautomator2` | 驱动 Android 设备 / 模拟器 |
  | `PySide6` | GUI 界面 |
  | `opencv-python` + `numpy` | 模板匹配(SIFT)定位图标 |
  | `pillow` | 读取模板图 |
  | `pyyaml` | 用例解析 |
  | `flask` | mock 服务(mock 模式下自启) |
  | `openpyxl` | 生成 Excel 报告 |

## 安装

```bash
pip install -r requirements.txt
```

配置**必须**先从模板复制一份出来——`config/config.yaml` 含机器相关信息,不在版本库里:

```bash
copy config\config.example.yaml config\config.yaml      # Windows
# 或
cp config/config.example.yaml config/config.yaml        # Git Bash / Linux
```

然后按实际环境改 `config/config.yaml`(接哪台设备、进入哪个设备页面等)。GUI 里改 APP/设备配置会自动同步回这个文件。

---

## 启动 GUI

```bash
python gui/main.py
```

等价写法:

```bash
python -m gui.main
```

### 不想看到控制台窗口

`python.exe` 在 Windows 上**自带一个控制台窗口**,GUI 程序用 `pythonw.exe` 启动才没有:

```bash
pythonw gui/main.py
```

如果习惯双击运行,可以自建一个 `启动GUI.bat`,内容:

```bat
@echo off
cd /d "%~dp0"
start "" pythonw "gui\main.py"
```

放在项目根目录,双击即可,不弹控制台。

## 运行测试

```bash
pytest                      # 全部用例(mock 模式)
pytest --mode real          # 真机 / 模拟器
pytest --case 快速建图       # 按用例名模糊过滤
pytest -m smoke             # 只跑 P0 冒烟用例
```

| 参数 | 说明 |
|---|---|
| `--mode mock` | 默认。在本地起一个 mock 服务,不碰真实设备 |
| `--mode real` | 真机/模拟器执行。**必须显式指定** |
| `--device <id>` | 指定设备,默认 `auto`(读 `config.yaml` 的 `device.default`) |
| `--case <关键字>` | 按文件名/用例名模糊过滤 |

> `--mode real` 是一道安全阀:不加这个参数时,真机用例在**收集阶段就会被 skip**,`device` fixture 完全不会启动,免得误操控扫地机。

报告输出在 `reports/` 下(Excel + 日志 + 失败截图)。

---

## 用例怎么写

`Test_cases/` 下每个 YAML 是一个模块:

```yaml
module: 全局清扫          # 模块名,也是报告里的用例组名
cases:
  - name: 全局清扫完整流程
    priority: P0          # P0 会映射成 smoke 标记,可 -m smoke 单独跑
    wait: 5               # 用例级等待(秒),覆盖全局配置
    steps:
      - desc: 点击开始清扫按钮
        click: 开始清扫.png
      - desc: 确认设备进入清扫状态
        assert: 清扫中
        screenshot: screenshots/全局清扫_01_清扫状态.png
```

步骤支持 `click` / `assert` / `long_click` / `swipe` / `input` / `add_timer` / `if-else` 等动作,具体字段见 `gui/schema.py`。

模板图放在 `Test_img/templates/`,步骤里按文件名引用(`click: 开始清扫.png`)。

## 目录结构

```
├── Test_cases/            用例 YAML(每个文件一个模块)
├── Test_img/
│   ├── templates/         模板图(图标定位用)
│   └── screenshots/       运行截图(产物,不入库)
├── common/                核心逻辑
│   ├── driver.py          配置/用例加载
│   ├── action_runner.py   步骤执行引擎
│   ├── vision.py          模板匹配(SIFT + 特征缓存)
│   ├── app_detect.py      设备与 APP 检测
│   ├── session.py         用例前置流程(重启 APP / 进设备页 / 充电检查)
│   └── excel_report.py    报告生成
├── gui/                   可视化编排界面
│   ├── main.py            GUI 入口
│   ├── main_window.py     主窗口
│   ├── schema.py          步骤字段定义与校验
│   └── runner_thread.py   后台执行线程
├── config/                配置(复制模板为 config.yaml 使用)
├── mock_server/           mock 服务
├── tests/                 框架自身的单元测试
├── conftest.py            pytest fixture 与命令行参数
└── pytest.ini
```

---

## 框架自身的测试

`tests/` 下是对框架本身的单元测试(不碰设备),直接跑:

```bash
pytest
```

## 常见问题

**YAML 读不出来,报编码错误 / 报 `%TSD-Header-###%`**

本机若装了企业文档透明加密(DLP),被加密的文件对部分进程是密文。`common/driver.py` 会把这类失败翻译成带文件名和处置建议的一句话,不会只丢一段 codec traceback。按提示用能读到明文的进程取回文件即可。

**GUI 启动了但连不上设备**

先确认 `adb devices` 能看到目标设备。模拟器常需先 `adb connect <host:port>`;`common/app_detect.py` 在用例前置里会自动尝试一次。
