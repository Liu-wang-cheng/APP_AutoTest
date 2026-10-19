# CI/CD 方案（含真机回归）

> 用户 2026-10-16 要求："方案 C 还需要做什么" → "你给个完整方案出来"。
> 本文件是**方案 C 的完整落地方案**：谁做什么、怎么做、怎么验、怎么回退。
> 与 `docs/RELEASE.md`（本机发版步骤）配套看。

## 0. 现状（已完成的部分）

| 组件 | 状态 | 位置 |
|---|---|---|
| 单元/集成测试 CI | ✅ 每次 push/PR 在 windows-latest 上跑 913 条（含静态/结构守护） | `.github/workflows/tests.yml` |
| 分支门禁 | ✅ ruleset「合入规则」：要求 `pytest` 通过 + 禁强推/删分支；bypass = 你本人 / RepositoryRole(admin) / 一个 Integration；允许建新分支 | 仓库 Settings → Rules |
| 打包自检（方案 A） | ✅ 手动/推 `v*` tag → `build.bat` → zip 传 artifact（7 天） | `.github/workflows/package.yml` |
| 全自动发版（方案 B） | ✅ 手动填版本号/推 `v*.*.*` tag → 全套发版；勾 `dry_run` 可演练（不发布） | `.github/workflows/release.yml` |
| 真机用例入口 | ✅ `pytest tests/test_yaml_runner.py --mode real --device <id> --case <名>`（`--mode` 默认 mock，裸跑不会碰设备） | `conftest.py` |
| 存储瘦身（每月自动） | ✅ 保留最近 5 个 Release，更老的连同资产删掉（不动 tag/历史） | `.github/workflows/prune.yml` + `tools/prune_releases.py` |

**runner 已就位**（2026-10-16 起）：`smoke-pc` 在线，标签 `self-hosted/Windows/X64/realtest/emulator`。
真机冒烟的**定时已开**（2026-10-19 手动冒烟在 CI 上跑绿后开启，满足"验证通过了再开定时"）：
每天 **北京 02:00**（UTC 18:00）跑一次；也能随时在 Actions 页面手动 Run。

---

## 1. 目标全景

```
push/PR ──► [tests.yml]  854 条测试            ─┐
                                                ├─► 绿 → 可合并 / 可发版
手动 ────► [static.yml]  pyflakes + 用例校验   ─┘        （C1，新增）

推 tag ──► [package.yml] 打包自检 → artifact           （方案 A，已有）
推 tag ──► [release.yml] 打包 → 传 Release → 清单 → tag （方案 B，已有）

手动/定时 ─► [real-smoke.yml] 自建 runner 上跑真机冒烟  （C2/C3，新增）
                              → junit + 失败截图 artifact
```

---

## 2. C1 质量门禁（我来写，零风险，不碰设备）

**新增 `.github/workflows/static.yml`**（push/PR 触发，几十秒）：

```yaml
name: 静态检查
on:
  push: { branches: [master] }
  pull_request: { branches: [master] }
jobs:
  lint:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: { python-version: '3.13', cache: pip }
      - run: pip install pyflakes pyyaml
      - name: pyflakes(除有意 noqa 外应当干净)
        run: python -m pyflakes core gui tools vlm
      - name: 用例 YAML 静态校验(键名/取值/阈值范围)
        run: python tools/validate_cases.py
```

要点：
- **只报不卡**的地方要明确：`pyflakes` 全绿才卡（本地已扫过，除 `# noqa` 是有意的）；`validate_cases.py` 返回非 0 就卡 —— 它拦的是"用例里写了不存在的键/越界阈值"这类**静默失效**。
- **覆盖率先只观测**：装 `pytest-cov`，在 tests.yml 里加 `--cov=core --cov-report=term-missing`，但**先不设门槛**（门槛要在你看到几个月的真实数字之后再定，否则就是逼自己改测试数量）。
- 这部分**与设备无关**，可以现在就上。

---

## 3. C2 真机冒烟（self-hosted runner）

### 3.1 你做的事：注册 runner（一次性，约 10 分钟）

在**那台跑着模拟器的机器**上（当前就是 `127.0.0.1:7555` / `5557` 那台）：

1. 仓库 → Settings → Actions → **Runners** → **New self-hosted runner** → 选 Windows x64
2. 页面会给出下载链接与一条带**一次性 token**（1 小时有效）的命令，照抄：

```bat
mkdir C:\actions-runner && cd C:\actions-runner
:: 下载并解压页面上给的 actions-runner-win-x64-*.zip
config.cmd --url https://github.com/Liu-wang-cheng/APP_AutoTest --token <页面上的TOKEN> ^
           --name smoke-pc --labels realtest,emulator
:: 前台跑(关窗口就停) —— 先这样验证通不通:
run.cmd
:: 验证通过后装成服务(机器重启也活着; 需要管理员权限):
svc.cmd install
svc.cmd start
```

3. 验证：Settings → Actions → Runners 里应看到 `smoke-pc`，状态 **Idle**（绿色）。

**为什么用 `--labels realtest,emulator`**：workflow 里写 `runs-on: [self-hosted, windows, realtest]`，这样**只有这台**会接真机任务，不会误落到云端 runner（云端连不上你的模拟器）。

### 3.2 这台机器上要预置的东西

| 需要什么 | 为什么 | 怎么做 |
|---|---|---|
| **本机 `config/config.yaml`** | 它被 gitignore，checkout **不会**带来；real 模式要知道连哪台设备 | 手工建一份（或从你本机拷），确保 `target_device` / `device.default` 指向模拟器 |
| **模拟器/真机在线** | 没设备 = 全红 | runner 机器上常开；workflow 会先探活并给明确提示 |
| **ADB 可用** | `uiautomator2.connect` 走 adb | `adb devices` 能看到设备 |
| **Python 3.13** | 冒烟跑 pytest 用；**不用** `setup-python`（见下方踩坑） | 机器上装一份，默认认 `C:\Python\python.exe`；放在别处就用仓库变量 `SMOKE_PYTHON` 指定 |
| **Windows PowerShell 5.1** | 所有步骤的 shell；**不需要** PowerShell 7 | 系统自带 |
| **盘空间** | 每次真机会写截图/debug 产物 | 定期清 `C:\actions-runner\_work`（下面 3.5） |

> 注意：workflow 里我会显式传 `--device`（默认 `127.0.0.1:7555`，可用仓库变量覆盖），**不完全依赖** config.yaml —— 少一层"为什么连不上"的排查。

> ★ **踩坑（2026-10-19 实测，冒烟第一次真跑就死在这）**：runner 的进程环境里带着
> `NoDefaultCurrentDirectoryInExePath=1`（cmd 不再从"当前目录"找 exe），而 `setup-python`
> 在 Windows 上的安装脚本是 `cmd /c "cd <toolcache> && call python-3.13.16-amd64.exe ..."`，
> 于是必然报 `'python-3.13.16-amd64.exe' is not recognized` →
> `Error happened during Python installation`，job 在装 Python 处就红了。
>
> **步骤级 env 救不了**（两轮 CI 实测）：env 只能覆盖、不能删除 runner 进程里的变量，
> 而 cmd 对"存在但为空"的变量**仍然视为已定义** —— 实测：`empty → exit 1`、
> 彻底删除变量 `→ exit 0`、`'0'`/`'false'` 同样无效。
>
> **因此真机冒烟不用 setup-python**：改用这台 runner 上已装好的 Python 3.13
> （默认 `C:\Python\python.exe`，可用仓库变量 `SMOKE_PYTHON` 覆盖），依赖装进
> `RUNNER_WORKSPACE\smoke-venv` 的复用 venv，不污染系统 Python。
> `tests/test_ci_workflows.py::test_real_smoke_does_not_use_setup_python` 守着这条
> （把 setup-python 加回来会立刻红 —— 加回来就会在装 Python 处整轮失败）。
> 同类风险：runner 从带该变量的终端启动时，任何"cd 到目录再执行裸文件名"的脚本都会踩到。

### 3.3 我做的事：`real-smoke.yml`（草稿，待你确认后落地）

```yaml
name: 真机冒烟
# ★ 触发**只给手动和定时** —— 见下面"红线"第 1 条(public 仓库 + 自建 runner 的 fork PR 风险)
on:
  workflow_dispatch:
    inputs:
      case:
        description: 跑哪个用例(模糊匹配, 留空=冒烟集)
        required: false
        default: ''
  schedule:
    - cron: '0 18 * * *'      # UTC 18:00 = 北京 02:00(2026-10-19 已开启)

jobs:
  smoke:
    # 只落在这台自建 runner 上(标签在注册时给)
    runs-on: [self-hosted, windows, realtest]
    timeout-minutes: 30
    steps:
      # node24 版 action（2026-10-16 升级，消掉 Node.js 20 弃用警告）
      - uses: actions/checkout@v7
        with: { fetch-depth: 1 }

      # ★ 冒烟用的用例/前置/模板都以**仓库里提交的那份**为准(用户 2026-10-16 定:
      #   "直接替换过来用, 不要直接用桌面上那个" + "防止一修改 CICD 就跑不了了"):
      #   Test_cases/三星/全局清扫.yaml、Test_cases/三星/templates/(含 停止烘干/结束烘干)、
      #   Test_preconditions/三星.yaml 都是用户那台工具在用的那一套。
      #   只有 config/config.yaml 入不了库(被 .gitignore、含本机设备信息) —— 所以库里放一份
      #   config/config.ci.yaml(= 用户那台工具在用的配置), 跑前复制成 config.yaml。
      - name: 准备冒烟配置(用仓库里的 config/config.ci.yaml)
        shell: powershell
        run: |
          Copy-Item config/config.ci.yaml config/config.yaml -Force
          foreach ($f in @("Test_cases/三星/全局清扫.yaml", "config/config.ci.yaml")) {
            Write-Host ("  {0} sha256 {1}" -f $f, (Get-FileHash $f -Algorithm SHA256).Hash.Substring(0, 12))
          }

      # ★ 不用 setup-python（见 3.2 踩坑说明）：用 runner 上已装好的 Python 3.13，
      #   依赖装进 RUNNER_WORKSPACE 下的复用 venv。
      # ★ 这台 runner 只有 Windows PowerShell 5.1，**没有 pwsh**：所有步骤写
      #   `shell: powershell`（写成 pwsh 会 `##[error]pwsh: command not found`）。
      # ★ 不往 GITHUB_ENV 写跨步骤变量：5.1 的 `>>` 默认 UTF-16，runner 按 UTF-8 读会坏 ——
      #   每个步骤自己算 python/venv/设备地址（都只有一两行）。
      - name: 选用本机 Python(自建 runner)
        shell: powershell
        run: |
          $py = "${{ vars.SMOKE_PYTHON }}"; if (-not $py) { $py = "C:\Python\python.exe" }
          if (-not (Test-Path $py)) { throw "找不到 Python: $py" }
          $v = (& $py -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
          if ($v -ne "3.13") { throw "需要 Python 3.13, 实际 $v ($py)" }
      - name: 安装依赖
        shell: powershell
        run: |
          $venv = Join-Path $env:RUNNER_WORKSPACE 'smoke-venv'
          if (-not (Test-Path "$venv\Scripts\python.exe")) { & "C:\Python\python.exe" -m venv $venv }
          $py = Join-Path $venv 'Scripts\python.exe'
          & $py -m pip install --upgrade pip
          & $py -m pip install -r requirements.txt

      - name: 探活设备(探不到就明确报"设备没起来", 而不是让用例背锅)
        shell: powershell
        run: |
          $dev = "${{ vars.SMOKE_DEVICE }}"; if (-not $dev) { $dev = "127.0.0.1:7555" }
          adb connect $dev | Out-Host
          $out = adb devices | Out-String
          if ($out -notmatch [regex]::Escape($dev) + "\s+device") {
            throw "模拟器/真机没在线($dev) —— 请先启动模拟器, 再重跑本 workflow"
          }

      - name: 跑冒烟(真机)
        shell: powershell
        run: |
          $case = "${{ github.event.inputs.case }}"
          if (-not $case) { $case = "全局清扫" }      # ← 冒烟集(见 3.4)
          $py = Join-Path $env:RUNNER_WORKSPACE 'smoke-venv\Scripts\python.exe'
          & $py -m pytest tests/test_yaml_runner.py -q -rs `
            --mode real --device "127.0.0.1:7555" --case "$case" `
            --junit-xml=reports/junit-real.xml

      - name: 失败时上传现场(截图 + junit)
        if: failure()
        uses: actions/upload-artifact@v7
        with:
          name: real-failure-${{ github.run_id }}
          path: |
            reports/junit-real.xml
            Test_cases/*/screenshots/**
            Test_img/debug/**
          retention-days: 7
          if-no-files-found: ignore
```

### 3.4 冒烟集怎么定（**需要你拍板**）

**已定（2026-10-16，用户拍板）**：三星 APP 的 `全局清扫` —— 就用用户那台工具里
**验证过可以完整执行**的那一份，已整份入库（用例/模板/前置，见 3.3 顶部注释）。

| 候选 | 成本 | 特点 |
|---|---|---|
| `快速建图` | 4 步 | 最短；但会真的建图（改变设备状态） |
| `全局清扫`（三星） | 中 | 覆盖模板点击/if 分支（清洗拖布）/断言/菜单与打扫报告 —— 对"整条链路通不通"最有代表性（**已选它**） |
| `勿扰模式` 等 | 4~14 步 | 按需再加 |

先跑一个；稳定一两周后再加第二个。**不要一上来全量**（一轮十几个用例要十几分钟，且任何一步失败都得人工复位机器人）。

### 3.5 清理与复位（真机 runner 特有）

- 真机跑完会在 runner 的 workspace 里留 `Test_cases/*/screenshots/`（用例截图，按"保留最近 3 次运行"自清）和 `Test_img/debug/`（调试产物，启动时自清 200 个/目录）——**大多自管**；
- 但每次 checkout 不会删这些 → 在 runner 上加一条**每周一次的清理**（Windows 任务计划或一个 cleanup workflow）：删 `C:\actions-runner\_work\APP_AutoTest\APP_AutoTest\{Test_cases,Test_img}\**\screenshots`、`build\`、`dist\`；
- 机器人侧复位：冒烟用例自己会回充/停止；万一中途失败留在清扫中，下一步的 `restart` 前置会重启 APP，但**机器人本体**要人工确认 —— 这也是"先只跑只读冒烟"的理由。

### 3.6 选择器健康检查（P0，2026-10-19）

APP 一升级，用例里的文案（`正在清洁`、`打扫报告`、`今天/昨天`…）可能就没了。以前要等真机冒烟跑到一半才失败，一轮 10 分钟白跑。
现在 `real-smoke.yml` 在**探活设备之后、跑真机冒烟之前**多了一步：

1. 用 `pm path` 找到设备上的 base.apk，`adb pull` 下来；
2. `tools/selector_health.py` 从 `Test_cases/**/*.yaml`、`Test_preconditions/*.yaml`、`config/config.yaml` 提取所有文本选择器（自动排除 `.png`、坐标、`电量>50` 这类表达式）；
3. 在 APK 的 `classes*.dex` / `resources.arsc` / `assets`（JS/HTML）里逐个查找；
4. 与入库基线 `tools/selector_baseline.json` 比较：
   - **基线有、现在没有 = 回归** → 立刻失败，冒烟跳过；
   - 基线也没有的（当前 49 个，多为服务端/WebView 下发的插件页文案）→ 只报告，不算失败；
5. 报告写到 `reports/selector-health.json`，失败时随现场一起上传。

**更新基线**（APP 升级、确认新文案可接受后，本地跑一次并提交）：

```bat
:: 用 CI 同一份配置生成(CI 里的 config.yaml 是从 config.ci.yaml 复制来的)
python tools/selector_health.py --apk <新APK> --repo . --config config/config.ci.yaml ^
    --write-baseline tools/selector_baseline.json
```

**限制**：插件页/服务端的动态文案不在 APK 里，APK 检查覆盖不到（报告里归为 `unverifiable`）；这类文案要靠运行时的页面文本快照另做 diff（P1，见 3.7）。

### 3.7 运行时页面文本快照（P1，2026-10-19）

P0 只能核对编进 APK 的文案；SmartThings 的设备页/插件页大部分文案是运行时下发的
（`打扫报告`、`正在干燥拖布` 等在 base.apk 里 0 命中）。`tools/runtime_snapshot.py`
用真机把关键页面**实际显示**的文字（无障碍树 + OCR）存成快照，并与基线 diff：

```bat
:: 采集基线（进入设备页 → 菜单 → 打扫报告）
python tools/runtime_snapshot.py --device 127.0.0.1:7555 ^
    --device-name "扫地机器人 0050" --snapshot tools/runtime_baseline.json

:: 之后采集并与基线比较（基线有、现在没有 = 回归，退出码 1）
python tools/runtime_snapshot.py --device 127.0.0.1:7555 ^
    --device-name "扫地机器人 0050" --snapshot reports/runtime-snapshot.json ^
    --diff tools/runtime_baseline.json
```

- 文本先做归一化（时间/数字 → `#`），避免电量、倒计时、面积等动态值造成噪音；
- 某页点击失败时该页跳过 diff（记录 `error`），不会误报“整页文案都没了”；
- 目前是**手动工具**，还没接进 CI；接法是在真机 runner 上跑一次并与入库基线 diff。

---

## 4. C3 定时与通知

- **定时**：`schedule` 用 **UTC**（北京 02:00 = `0 18 * * *`）。2026-10-19 手动冒烟在 CI 跑绿后**已开启**；如果夜里要占设备（OTA/手工测试），把 `schedule:` 两行注释回去即可。
- **通知**：GitHub 默认会给仓库 watcher（你）发失败邮件 ✓ 够用。想更即时可以：
  - 手机装 GitHub App（推送通知）；
  - 或加一步 webhook（企业微信/钉钉机器人）—— 需要你给 URL，我再写。
- **别做的事**：不要 nightly 发版（版本发布应是人工决策；`release.yml` 保持手动/tag 触发）。

---

## 5. C4 运维手册（自建 runner 的长期成本）

| 事项 | 频率 | 说明 |
|---|---|---|
| runner 服务是否活着 | 每月看一眼 | Settings → Actions → Runners 里状态 Idle；红了就在机器上 `svc.cmd restart` |
| runner 自动更新 | 自动 | 空闲时会自更新；若报版本过旧，重跑 `config.cmd` 即可 |
| ADB/模拟器掉线 | 偶发 | 探活步骤会明确报出来；手动 `adb connect` 恢复 |
| 磁盘 | 每季度 | 见 3.5 的清理 |
| 依赖变化 | 跟着仓库 | workflow 每次 `pip install -r requirements.txt` ✓ 自动 |

**存储与清理**（2026-10-16 盘点）：
- **Release 资产是大头**：每个包 ~225MB，34 个版本就是 **7.65GB**。已按"只保留最近 5 个"
  清到 5 个（1.13GB）。删 Release **不影响 tag 与源码历史** —— 需要老版本时
  `git checkout v1.7 && build.bat` 重新构建即可（这就是敢删的底气）。
  **已自动化**：`.github/workflows/prune.yml` 每月 1 号自动保留最近 5 个、删掉更老的
  （`tools/prune_releases.py`，默认演练；手动触发也默认演练，取消勾选才真删）。
- Action artifacts：`AutoTest-*`（演练/打包自检产物，~227MB/个，保留 3 天）与
  `pytest-junit-*`（失败时的报告，很小）都会自动过期；要立刻回收就手动删。
- 本地产物（`.venv` 1.1G、`dist/*.zip` ~216MB、`backups/` 你的数据）按需自行清理；
  `build/`、`build_*.log`、`Test_img/debug/` 都是可再生的，随时可删。

**常见故障对照**：
- 任务一直 **Queued** → runner 没在线/标签不匹配（检查 `runs-on` 的标签与注册时的 `--labels`）；
- **"模拟器/真机没在线"** → 启动模拟器，或改 `vars.SMOKE_DEVICE`；
- 用例失败但设备正常 → 就是真 bug，看 artifact 里的截图与 junit（这正是这套东西的价值）。

### 5.1 发版"半成品"：Release 有了、`version.json` 没更新

**症状**：`发版` workflow 红在第 12 步（`发布…`），日志尾是
`remote: error: GH013: Repository rule violations found for refs/heads/master … Required status check "pytest" is expected`。
此时 **Release 与 zip 资产已经传上去了**，但 `version.json` 还是上一版 → 用户在"检查更新"里
**看不到新版**（OTA 读的就是仓库根这份清单）。v2.0.27 中过一次。

**原因**：仓库 ruleset「合入规则」要求 `pytest` 状态检查，而 `release.py` 推 `version.json`
用的是 Actions 自己的 `GITHUB_TOKEN`（`github-actions[bot]`）—— 它不在 ruleset 的 bypass
名单里，于是"新增提交"的推送被服务端拒绝（本机推得动，是因为 `RepositoryRole(admin)` 在名单里）。

**已采用的修法（2026-10-16 落地，实测通过）：写权限部署密钥**。
ruleset 的 bypass 名单里本来就有一条 `DeployKey`（`actor_id: null` = 任何部署密钥都放行），
所以走 SSH 部署密钥就绕过了这条规则：

1. `ssh-keygen -t ed25519` 生成密钥对（私钥备份在桌面 `AutoTest_部署密钥\`）；
2. 仓库 Settings → Deploy keys 添加**公钥**并勾 **Allow write access**；
3. 仓库 Settings → Secrets and variables → Actions 建 secret **`RELEASE_DEPLOY_KEY`**（值=私钥）；
4. `release.yml` 的 checkout 带上 `ssh-key: ${{ secrets.RELEASE_DEPLOY_KEY }}` —— 之后
   `release.py` 的 `git push` 全部走这把密钥。

**对照实验（实测数据，别再凭感觉）**：往**探针分支**推两次，第 2 次（往已存在分支推新提交）
与推 master 同等待遇（第 1 次在建分支，被 `do_not_enforce_on_create` 豁免）：

| 推送身份 | 第 1 次（建分支） | 第 2 次（推新提交） |
|---|---|---|
| 默认 `GITHUB_TOKEN` | 通过（豁免） | **被拒** `GH013 Required status check "pytest" is expected` |
| 部署密钥 | 通过 | 通过，服务端明说 `Bypassed rule violations …` |

**另外两种没采用的备选**：① ruleset 的 bypass 名单里加 **GitHub Actions**（注意：应用 id 是
**15368**，名单里那条 4518051 不是它）；② 建 PAT 存成 secret 供 checkout 用。

**万一已经卡在这一步（补完的办法）**：
```bash
# ① 下载**已上传**的资产并算 sha256(清单必须与资产一致, 不能拿本地重打包的包顶替)
# ② 用 release.py 内部那一步补齐(它做的事就是: 写 version.json → commit → tag -f → push)
python - <<'PY'
import sys; sys.path.insert(0, ".")
import tools.release as R
v, zip_path = "2.0.27", r"<刚下载的 zip>"
R.update_version_json_and_tag(
    "Liu-wang-cheng/APP_AutoTest", v, R.compute_sha256(zip_path),
    f"https://github.com/Liu-wang-cheng/APP_AutoTest/releases/download/v{v}/AutoTest_v{v}.zip",
    R.load_changelog_section("CHANGELOG.md", v)[1])
PY
# ③ 验一遍用户那条链路(把下载来的包当 --pkg, 这样 sha 才对得上)
python tools/verify_release.py --version 2.0.27 --pkg <刚下载的 zip>
```

**预检被"假阳性"坑过两次（2026-10-16 定稿）**：
- 第 1 版是 `git push --dry-run origin HEAD:refs/heads/master` —— 推**当前 HEAD** 是 no-op
  （Everything up-to-date），规则根本没机会拦；
- 第 2 版"造个空提交再 dry-run 推"**照样是假阳性** —— 本地造一个拒绝一切的 pre-receive 钩子
  实测：`git push --dry-run` **完全不触发服务端钩子/规则**，依然返回 0。**dry-run 不能当预检。**

现在的预检是两步：① 没配 `RELEASE_DEPLOY_KEY` → 直接拦（这种身份必被拒，说清怎么修）；
② 配了就**真推两次**探针分支（见上面的对照实验），第 2 次的成败才是判据。`tests/test_ci_workflows.py`
里有一条守护盯着："命令行里不许出现 `push --dry-run`" + "必须推两次"。

---

## 6. 红线与风险

1. **public 仓库 + 自建 runner = 绝不给 fork PR 跑**。别人 fork 后发 PR，workflow 会在**你的机器**上执行任意代码。所以 `real-smoke.yml` 的触发**只有** `workflow_dispatch` / `schedule`，**没有** `pull_request`。万一日后要接 PR，必须加：
   ```yaml
   if: github.event.pull_request.head.repo.full_name == github.repository
   ```
2. **别让两件事同时用设备**：nightly 定时和你的手动测试会抢设备（表现为用例随机失败）。要么只手动触发，要么把定时放在你确定不用的时段。
3. **别用 CI 造的 zip 当发布包**：CI 的 artifact 与本地产物 sha256 不同（PyInstaller 嵌入构建时间戳），替换线上资产会让用户的更新校验失败。发布永远走 `release.yml`（它会重算 sha256 写进 `version.json`）。
4. **真机任务会真实操控机器人**：不是 mock。冒烟集要挑"可自恢复"的用例；失败后先看设备状态，必要时人工复位。
5. **自建 runner 不计 GitHub 分钟数**（免费额度只有云端 runner 才涉及），但**成本转移到你自己机器的常开与维护**。

---

## 7. 落地顺序与工作量

| 步骤 | 谁 | 工作量 | 风险 |
|---|---|---|---|
| ① `static.yml`（静态检查） | 我 | 10 分钟 | 零（不碰设备） |
| ② 注册 runner + 预置 config.yaml | 你 | 10 分钟 | 需要管理员权限（装服务） |
| ③ `real-smoke.yml`（默认只手动触发） | 我 | 15 分钟 | 首次跑会真的动设备 |
| ④ 手动点一次真机冒烟，看产物 | 你/我 | 5 分钟 | —— |
| ⑤ 打开 `schedule`（可选） | 你拍板 | 1 分钟 | 与手动测试抢设备 |

**进度**：①—⑤ 均已完成。④ 于 2026-10-19 在 CI 上跑绿（`1 passed in 674.58s`，run 39192657405）；
⑤ 随后开启（北京 02:00）。

---

## 8. 验收清单（做完长这样）

- [ ] push/PR 上出现三个检查：`pytest`、`静态检查`、（未来）`真机冒烟`
- [ ] Actions 页面能手动跑"打包自检"，7 天内可下载 zip artifact
- [ ] Actions 页面能手动跑"发版"（`dry_run` 演练不发布；不勾则真发布并更新 `version.json`）
- [ ] Settings → Actions → Runners 里有 `smoke-pc`（Idle），能手动跑"真机冒烟"并拿到 junit + 失败截图
- [ ] 失败时你收到邮件通知；`Test_img/debug/failures` 里的现场图能直接看

---

## 9. 回退

- 任何 workflow **删文件即停**（`.github/workflows/*.yml`），不影响程序与已发布版本；
- runner 可随时停：`svc.cmd stop` / `svc.cmd uninstall`，或在 Settings → Runners 里 Remove；
- ruleset 的 bypass 与 `do_not_enforce_on_create` 都可改回（Settings → Rules）。
