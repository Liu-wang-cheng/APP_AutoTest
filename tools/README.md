# tools/ 一览

两类脚本:**自动流程里的**(改了要连带跑测试)和**手动排查用的**(平时不动)。
结构整理 2026-10-15: 之前有 6 个脚本全仓 0 引用, 谁也说不清哪个还在用 —— 这里一次写清。

## 一、自动流程调用(改这些要跑 `pytest tests/test_tools.py tests/test_release.py`)

| 脚本 | 谁在用 | 作用 |
|---|---|---|
| `verify_package.py` | `build.bat` | 打包后校验产物完整性(exe / _internal / 关键数据文件在不在) |
| `prune_releases.py` | `.github/workflows/prune.yml`(每月自动) | 存储瘦身: 删掉「最近 N 个」之外的 GitHub Release(只删 Release 与资产, **不动 tag/历史**); 默认演练, `--apply` 才真删 |
| `release.py` | `build.bat`、`docs/RELEASE.md` | 发版: 版本一致性闸门 → 走 CI → 传 GitHub Release → 更新 version.json → 打 tag → 校验下载链路 |
| `verify_release.py` | 上面 release 的收尾 | 站在**用户视角**验证: 取清单 → sha256 → 资产 → zip 头 |
| `check_compat.py` | 测试参数化调用 | 兼容性检查(依赖/接口/配置项) |
| `check_render.py` | 测试参数化调用 | 离屏渲染 GUI, 确认界面能起来 |
| `verify_update_download.py` | 测试参数化调用 | 校验更新包下载链路 |
| `selector_health.py` | `.github/workflows/real-smoke.yml`（跑真机冒烟前） | P0 选择器健康检查：从用例/前置/配置提取文本选择器，在设备 APK 的 DEX / resources.arsc / assets 里核对，并与 `selector_baseline.json` 比较；**回归即失败**（服务端/WebView 下发的动态文案只报告，不算失败） |

## 二、手动排查用(不参与自动流程, 但要留着 —— 真机调模板/调用例时会用)

| 脚本 | 什么场景用 |
|---|---|
| `dump_screen_texts.py` | 真机: 把当前屏幕的无障碍树文本 + OCR 文本全打出来(写用例/定断言前先看页面上到底有什么) |
| `recapture_template.py` | 真机: 从当前屏幕裁模板图(`Test_cases/<组>/templates/`) |
| `check_templates.py` | 离线: 扫用例引用的模板图是否都存在(改名/挪目录后快速自查) |
| `ocr_crop_preview.py` | 真机: 把"OCR 要不要裁掉顶部区域"的效果画在真实截图上(全图 vs 裁剪后的识别结果逐行对比 + 标注图) |
| `validate_cases.py` | 离线: 用例 YAML 静态校验(键名/取值/阈值范围), 也可以 `python tools/validate_cases.py` 单独跑 |
| `check_env.py` | 离线: 运行环境自检(Python/依赖/ADB/设备) |
| `check_update_e2e.py` | 手动: 起本地假更新源, 端到端走一遍"检查更新 → 下载 → 校验" |
| `rehearse_autoupdate.py` | 手动: 用真实安装目录彩排一次自动更新(替换/回滚), **不碰线上** |
| `runtime_snapshot.py` | 真机(P1): 进入设备页/菜单/打扫报告, 采集无障碍树+OCR 文本存快照; 与基线 diff —— 覆盖 APK 里没有的服务端/WebView 动态文案(基线有、现在没有 = 回归) |

## 三、约定

- **`selector_health.py` / `runtime_snapshot.py` 仅用于 CI/测试**, 不被 `core/gui` 引用、也不参与打包;
  它们与具体 APP 的文案/页面绑定(基线、导航), 换 APP 时要重新生成基线或调整页面路径;
- 新脚本请在上面对应小节登记一行, 不要让它变成"0 引用的神秘文件";
- 只在"自动流程"小节里的脚本才允许被 CI / build.bat 依赖;
- 工具脚本**只调用 core 的公开 API**(不要 `from core.xxx import _private`), 唯一例外见
  `check_compat.py` / `check_render.py` / `validate_cases.py` 对 `gui.schema` 的引用 ——
  它们本来就是"校验 GUI"的工具, 方向合理。
